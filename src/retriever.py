"""
retriever.py

Stores core retrieval logic using FAISS and BM25 scoring.
It also contains helpers for loading artifacts and filtering chunks.
"""

from __future__ import annotations

import pathlib
import os
import pickle
import time
from abc import ABC, abstractmethod
from typing import List, Tuple, Optional, Dict, Any
import nltk
from nltk.stem import WordNetLemmatizer

import faiss
import numpy as np
from src.embedder import CachedEmbedder

from src.config import RAGConfig
from src.index_builder import preprocess_for_bm25


# -------------------------- Embedder cache ------------------------------

_EMBED_CACHE: Dict[str, CachedEmbedder] = {}

def _get_embedder(model_name: str) -> CachedEmbedder:
    if model_name not in _EMBED_CACHE:
        # Use the cached embedding model to avoid reloading it on every call
        _EMBED_CACHE[model_name] = CachedEmbedder(model_name)
    return _EMBED_CACHE[model_name]


# -------------------------- Read artifacts -------------------------------

def load_artifacts(artifacts_dir: os.PathLike, index_prefix: str) -> Tuple[faiss.Index, List[str], List[str], Any]:
    """
    Loads:
      - FAISS index: {index_prefix}.faiss
      - chunks:      {index_prefix}_chunks.pkl
      - sources:     {index_prefix}_sources.pkl
    """
    artifacts_dir = pathlib.Path(artifacts_dir)
    faiss_index = faiss.read_index(str(artifacts_dir / f"{index_prefix}.faiss"))
    bm25_index  = pickle.load(open(artifacts_dir / f"{index_prefix}_bm25.pkl", "rb"))
    chunks      = pickle.load(open(artifacts_dir / f"{index_prefix}_chunks.pkl", "rb"))
    sources     = pickle.load(open(artifacts_dir / f"{index_prefix}_sources.pkl", "rb"))
    metadata = pickle.load(open(artifacts_dir / f"{index_prefix}_meta.pkl", "rb"))

    return faiss_index, bm25_index, chunks, sources, metadata


# -------------------------- Helper to get page nums for chunks -------------------------------

def get_page_numbers(chunk_indices: list[int], metadata: list[dict]) -> dict[int, List[int]]:
    if not metadata or not chunk_indices:
        return {}

    page_map: dict[int, List[int]] = {}

    for chunk_idx in chunk_indices:
        chunk_idx = int(chunk_idx)
        if 0 <= chunk_idx < len(metadata):
            chunk_pages = metadata[chunk_idx].get("page_numbers")
            if chunk_pages is None:
                continue  # don't store None; callers can default to [1]
            page_map[chunk_idx] = chunk_pages

    return page_map

# -------------------------- Filtering logic -----------------------------

def filter_retrieved_chunks(cfg: RAGConfig, chunks, ordered):
    topk_idxs = ordered[:cfg.top_k]
    return topk_idxs

# -------------------------- Retrieval core ------------------------------

class Retriever(ABC):
    @abstractmethod
    def get_scores(self, query: str, pool_size: int, chunks: List[str]):
        """Retrieves the top 'pool_size' chunks cores for a given query."""
        pass


class FAISSRetriever(Retriever):
    name = "faiss"

    def __init__(self, index, embed_model: str):
        self.index = index
        self.embedder = _get_embedder(embed_model)

    def get_scores(self,
                query: str,
                pool_size: int,
                chunks: List[str]) -> Dict[int, float]:
        """
        Returns FAISS scores for top 'pool_size' keyed by global chunk index.
        """
        start_time = time.time()
        
        # FAISS expects a 2D array
        q_vec = self.embedder.encode([query]).astype("float32")
        
        # Safety check on vector dimensions
        if q_vec.shape[1] !=  self.index.d:
            raise ValueError(
                f"Embedding dim mismatch: index={ self.index.d} vs query={q_vec.shape[1]}"
            )

        # Perform the search
        search_start = time.time()
        distances, indices =  self.index.search(q_vec, pool_size)
        search_time = time.time() - search_start

        # Remove invalid indices and ensure they are within bounds
        cand_idxs = [i for i in indices[0] if 0 <= i < len(chunks)]

        # Create the distance dictionary, ensuring we only include valid candidates
        dists = {idx: float(dist) for idx, dist in zip(cand_idxs, distances[0][:len(cand_idxs)])}

        # Invert distance to score: 1 / (1 + distance). Adding 1 avoids division by zero.
        result = {
            idx: 1.0 / (1.0 + dist)
            for idx, dist in dists.items()
        }
        
        elapsed = time.time() - start_time
        print(f"[FAISSRetriever] Total time: {elapsed*1000:.2f}ms (search: {search_time*1000:.2f}ms, candidates: {len(result)})")
        return result


class BM25Retriever(Retriever):
    name = "bm25"

    def __init__(self, index):
        self.index = index

    def get_scores(self,
                 query: str,
                 pool_size: int,
                 chunks: List[str]) -> Dict[int, float]:
        """
        Returns BM25 scores for top 'pool_size' keyed by global chunk index.
        """
        start_time = time.time()
        
        # Tokenize the query in the same way the index was built
        tokenized_query = preprocess_for_bm25(query)

        # Get scores for all documents in the corpus
        all_scores = self.index.get_scores(tokenized_query)

        # Find the indices of the top 'pool_size' scores
        num_candidates = min(pool_size, len(all_scores))
        top_k_indices = np.argpartition(-all_scores, kth=num_candidates-1)[:num_candidates]

        # Remove invalid indices and ensure they are within bounds
        top_k_indices = [i for i in top_k_indices if 0 <= i < len(chunks)]
        
        # Get the corresponding scores for the top indices
        top_scores = all_scores[top_k_indices]

        # Format the output as a dictionary of scores
        scores = {int(idx): float(score) for idx, score in zip(top_k_indices, top_scores)}
        
        elapsed = time.time() - start_time
        print(f"[BM25Retriever] Total time: {elapsed*1000:.2f}ms (candidates: {len(scores)})")

        return scores


class IndexKeywordRetriever(Retriever):
    name = "index_keywords"
    
    def __init__(self, extracted_index_path: os.PathLike, page_to_chunk_map_path: os.PathLike):
        """
        Retriever that uses textbook index keywords to boost chunks on relevant pages.
        
        Args:
            extracted_index_path: Path to extracted_index.json (keyword -> page numbers)
            page_to_chunk_map_path: Path to page_to_chunk_map.json (page -> chunk IDs)
        """
        import json
        nltk.download('wordnet', quiet=True)
        self.page_to_chunk_map = {}
        
        # Load and normalize index: lemmatize phrases as units
        # Build token->phrase mapping for fast lookup
        if os.path.exists(extracted_index_path):
            lemmatizer = WordNetLemmatizer()
            
            with open(extracted_index_path, 'r') as f:
                raw_index = json.load(f)
                self.phrase_to_pages = {}  # phrase -> pages
                self.token_to_phrases = {}  # token -> [phrases]
                
                for key, pages in raw_index.items():
                    # Lemmatize each word in the phrase but keep phrase together
                    key_lower = key.lower()
                    words = key_lower.split()
                    lemmatized_words = []
                    
                    for word in words:
                        cleaned = word.strip('.,!?()[]:"\'')
                        if not cleaned:
                            continue
                        lemmatized_words.append(self._lemmatize_word(cleaned, lemmatizer))
                    
                    lemmatized_phrase = ' '.join(lemmatized_words)
                    self.phrase_to_pages[lemmatized_phrase] = pages
                    
                    # Build reverse index: each token points to phrases containing it
                    for token in lemmatized_words:
                        if token not in self.token_to_phrases:
                            self.token_to_phrases[token] = []
                        self.token_to_phrases[token].append(lemmatized_phrase)
        else:
            self.phrase_to_pages = {}
            self.token_to_phrases = {}
        
        if os.path.exists(page_to_chunk_map_path):
            with open(page_to_chunk_map_path, 'r') as f:
                self.page_to_chunk_map = json.load(f)
    
    def get_scores(self, query: str, pool_size: int, chunks: List[str]) -> Dict[int, float]:
        """
        Returns scores for chunks that match index keywords.
        Score is proportional to the number of keyword hits.
        """
        keywords = self._extract_keywords(query)
        # chunk_id -> hit count
        chunk_hit_counts: Dict[int, int] = {} 
        
        # Match query keywords against index phrases (token overlap)
        for keyword in keywords:
            if keyword not in self.token_to_phrases:
                continue
            
            # Get all phrases containing this keyword token
            matching_phrases = self.token_to_phrases[keyword]
            
            for phrase in matching_phrases:
                page_numbers = self.phrase_to_pages[phrase]
                
                # Map pages to chunks
                for page_no in page_numbers:
                    chunk_ids = self.page_to_chunk_map.get(str(page_no), [])
                    for chunk_id in chunk_ids:
                        if chunk_id >= 0 and chunk_id < len(chunks):
                            chunk_hit_counts[chunk_id] = chunk_hit_counts.get(chunk_id, 0) + 1
        
        if not chunk_hit_counts:
            return {}
        
        # Normalize scores: more keyword hits = higher score
        max_hits = max(chunk_hit_counts.values())
        scores = {
            chunk_id: float(hit_count) / max_hits
            for chunk_id, hit_count in chunk_hit_counts.items()
        }
        
        return scores
    
    @staticmethod
    def _lemmatize_word(word: str, lemmatizer) -> str:
        """Lemmatize a word, trying noun then verb."""
        lemma = lemmatizer.lemmatize(word, pos='n')
        if lemma == word:
            lemma = lemmatizer.lemmatize(word, pos='v')
        return lemma
    
    @staticmethod
    def _extract_keywords(query: str) -> List[str]:
        """Extract keywords from query by removing stopwords and lemmatizing."""
        
        stopwords = {
            "the", "is", "at", "which", "on", "for", "a", "an", "and", "or", "in",
            "to", "of", "by", "with", "that", "this", "it", "as", "are", "was", 
            "what", "how", "why", "when", "where", "who", "does", "do", "be"
        }
        
        lemmatizer = WordNetLemmatizer()
        words = query.lower().split()
        keywords = []
        for word in words:
            cleaned = word.strip('.,!?()[]:"\'')
            if not cleaned or cleaned in stopwords:
                continue
            keywords.append(IndexKeywordRetriever._lemmatize_word(cleaned, lemmatizer))
        return keywords


class ClusteredFAISSRetriever(Retriever):
    """
    Hierarchical retriever using k-means clustering for fast neighborhood search.
    
    Strategy:
    1. Clusters are pre-computed during indexing (stored with FAISS index)
    2. For each query, find nearest cluster(s) first
    3. Only search chunks within those clusters
    
    Benefits:
    - 10-50x faster search for large indices (> 10k chunks)
    - Clustering computed once (offline) during indexing
    - Same retrieval quality as flat search
    - Minimal memory overhead
    
    Trade-offs:
    - Requires upfront clustering cost (one-time during indexing)
    - Slightly worse quality for cross-cluster relevant chunks
    
    cluster data format (if provided):{
        "n_clusters": len(cluster_indices),
        "embedding_dim": embedding_dim,
        "cluster_indices": cluster_indices,
        "cluster_chunks": cluster_chunks,
        "chunk_assignments": cluster_ids,
        "centroids": kmeans.centroids.tolist(),
        } 
    """
    
    name = "faiss"  # Same as FAISSRetriever so ranker recognizes it
    
    def __init__(self, cluster_data: Optional[dict], embed_model: str, n_probe_clusters: int = 5):
        self.embedder = _get_embedder(embed_model)
        self.cluster_indices = None
        self.cluster_chunks = None
        self.chunk_assignments = None
        self.n_probe_clusters = n_probe_clusters
        if cluster_data:
            self._load_clusters(cluster_data)
        
       
    
    def _load_clusters(self, cluster_data: dict):
        """Loads cluster data from the provided dictionary."""
        self.cluster_indices = cluster_data.get("cluster_indices", {})  # Dict of cluster_id -> FAISS index
        self.cluster_chunks = cluster_data.get("cluster_chunks", {})    # Dict of cluster_id -> chunk indices
        self.chunk_assignments = cluster_data.get("chunk_assignments", [])
        self.centroids = np.array(cluster_data.get("centroids"), dtype=np.float32)
    
    def _build_clusters(self):
        """Deprecated: clusters now built during indexing. Kept for backward compat."""
        pass
    
    def get_scores(self, query: str, pool_size: int, chunks: List[str]) -> Dict[int, float]:
        """
        Retrieves scores for chunks in the nearest cluster(s) to the query.
        Searches the top n_probe_clusters nearest clusters.
        Returns dict with chunk_idx -> score, plus stores cluster metadata for logging.
        """
        start_time = time.time()
        
        if self.cluster_indices is None or self.cluster_chunks is None:
            raise ValueError("Cluster data not loaded. Ensure index was built with clustering and cluster data is provided.")
        
        # Step 1: Embed the query
        embed_start = time.time()
        q_vec = self.embedder.encode([query]).astype("float32")
        embed_time = time.time() - embed_start
        
        # Step 2: Find nearest cluster(s) using centroids
        centroid_start = time.time()
        # Compute distances to centroids
        dists_to_centroids = self.centroids @ q_vec.T  # (n_clusters, 1)
        dists_to_centroids = np.squeeze(dists_to_centroids)  # (n_clusters,)
        
        # Get top n_probe_clusters nearest clusters
        top_cluster_indices = np.argsort(-dists_to_centroids)[:self.n_probe_clusters]
        centroid_time = time.time() - centroid_start
        
        print(f"[ClusteredFAISSRetriever] Querying '{query[:50]}...'")
        print(f"[ClusteredFAISSRetriever] Top {self.n_probe_clusters} clusters to search: {top_cluster_indices}")
        
        scores = {}
        self.chunk_cluster_map = {}  # Track which cluster each chunk came from
        search_time = 0
        
        for cluster_idx in top_cluster_indices:
            nearest_cluster_chunks = self.cluster_chunks.get(int(cluster_idx), [])
            if not nearest_cluster_chunks:
                continue   # No chunks in this cluster, skip
            
            # Step 3: Use FAISS to search only within this cluster's chunks
            cluster_search_start = time.time()
            clusterIndex = self.cluster_indices[cluster_idx]
            distances, indices = clusterIndex.search(q_vec, min(pool_size, len(nearest_cluster_chunks)))
            search_time += time.time() - cluster_search_start
            
            # Map local cluster indices back to global chunk indices
            for local_idx, dist in zip(indices[0], distances[0]):
                if local_idx < len(nearest_cluster_chunks):
                    global_chunk_idx = nearest_cluster_chunks[local_idx]
                    if 0 <= global_chunk_idx < len(chunks):
                        score = 1.0 / (1.0 + dist)  # Normalize to match FAISSRetriever (0-1 range)
                        scores[global_chunk_idx] = score
                        self.chunk_cluster_map[global_chunk_idx] = int(cluster_idx)  # Tag chunk with cluster
        
        elapsed = time.time() - start_time
        print(f"[ClusteredFAISSRetriever] Retrieved {len(scores)} unique chunks from {len(top_cluster_indices)} clusters")
        print(f"[ClusteredFAISSRetriever] Total time: {elapsed*1000:.2f}ms (embed: {embed_time*1000:.2f}ms, centroid: {centroid_time*1000:.2f}ms, search: {search_time*1000:.2f}ms)\"")
        return scores