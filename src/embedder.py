import sqlite3
import hashlib
import multiprocessing
import multiprocessing.pool
import numpy as np
import os
import platform
from pathlib import Path
from typing import List, Union, Optional
from llama_cpp import Llama
from tqdm import tqdm

# Global variables for worker processes
_worker_model: Optional[Llama] = None
_worker_embedding_dim: int = 0


def _get_gpu_layers(use_gpu: bool = True) -> int:
    """
    Centralized GPU layer detection for both main and worker processes.
    
    Detects Apple Silicon (M1/M2/M3) and returns the number of GPU layers
    to use. This eliminates code duplication between _init_worker() and
    SentenceTransformer.__init__().
    
    Args:
        use_gpu: Whether to attempt GPU acceleration (default: True)
        
    Returns:
        Number of GPU layers (0 = CPU-only, >0 = GPU accelerated)
        
    Environment Variables:
        TOKENSMITH_GPU_LAYERS: Override default GPU layers (default: 35 on Apple Silicon)
    """
    system = platform.system()
    machine = platform.machine()
    is_apple_silicon = system == "Darwin" and machine == "arm64"
    
    # Return early if GPU not requested or not available
    if not use_gpu or not is_apple_silicon:
        return 0
    
    # For Apple Silicon, offload ALL layers to GPU by default (use -1 for all layers)
    # Users can override with TOKENSMITH_GPU_LAYERS environment variable
    default_layers = -1  # -1 means offload ALL layers to GPU
    n_layers = int(os.environ.get("TOKENSMITH_GPU_LAYERS", str(default_layers)))
    print(f"[GPU Mode] Using {n_layers} GPU layers on {machine}")
    n_layers = 1
    return n_layers


def _init_worker(model_path: str, n_ctx: int, n_threads: int, use_gpu: bool = True):
    """Initializes the model inside a worker process."""
    global _worker_model, _worker_embedding_dim

    # Use centralized GPU detection function
    n_gpu_layers = _get_gpu_layers(use_gpu)

    _worker_model = Llama(
        model_path=model_path,
        n_ctx=n_ctx,
        n_threads=n_threads,
        embedding=True,
        verbose=False,
        use_mmap=True,
        n_gpu_layers=n_gpu_layers,
    )

    test_emb = _worker_model.create_embedding("test")['data'][0]['embedding']
    _worker_embedding_dim = len(test_emb)


def _encode_batch_worker(texts: List[str]) -> List[List[float]]:
    """Encodes a batch of text using the worker's local model instance."""
    global _worker_model, _worker_embedding_dim
    if _worker_model is None:
        return []

    embeddings = []
    for text in texts:
        try:
            emb = _worker_model.create_embedding(text)['data'][0]['embedding']
            embeddings.append(emb)
        except Exception:
            embeddings.append([0.0] * _worker_embedding_dim)

    return embeddings


class SentenceTransformer:
    def __init__(self, model_path: str, n_ctx: int = 4096, n_threads: int = None, use_gpu: bool = True):
        """
        Initialize with a local GGUF model file path.

        Args:
            model_path: Path to your local .gguf file
            n_ctx:      Context window size. Defaults to 4096.
            n_threads:  Number of threads (None = auto-detect)
            use_gpu:    Whether to use GPU acceleration on Mac M1 (default True)
        """
        self.model_path = model_path
        self.n_ctx = n_ctx

        # Use centralized GPU detection function
        n_gpu_layers = _get_gpu_layers(use_gpu)
        
        # Log CPU/GPU mode
        if n_gpu_layers > 0:
            print(f"[GPU Mode] Running embedder on Mac M1/M2/M3 GPU (Metal Performance Shaders)")
            print(f"[GPU Mode] GPU layers: {n_gpu_layers} (set TOKENSMITH_GPU_LAYERS env var to adjust)")
        else:
            system = platform.system()
            machine = platform.machine()
            print(f"[CPU Mode] Running embedder on CPU only (system: {system} {machine})")

        self.model = Llama(
            model_path=model_path,
            n_ctx=n_ctx,
            n_threads=n_threads,
            embedding=True,
            verbose=False,
            use_mmap=True,
            n_gpu_layers=n_gpu_layers,
        )
        self._embedding_dimension = None

        # Warm up — also caches embedding dimension
        _ = self.embedding_dimension

    @property
    def embedding_dimension(self) -> int:
        """Get embedding dimension (cached after first call)."""
        if self._embedding_dimension is None:
            test_embedding = self.model.create_embedding("test")['data'][0]['embedding']
            self._embedding_dimension = len(test_embedding)
        return self._embedding_dimension

    def encode(
        self,
        texts: Union[str, List[str]],
        batch_size: int = 1,
        normalize: bool = False,
        show_progress_bar: bool = False,
        **kwargs,
    ) -> np.ndarray:
        """
        Encode texts to embeddings sequentially.

        Args:
            texts:             Single text or list of texts to encode.
            batch_size:        Unused — kept for API compatibility only.
                               All encoding is sequential (one chunk per
                               forward pass) due to llama-cpp-python
                               n_seq_max limitations in 0.3.x.
            normalize:         Whether to L2-normalize embeddings.
            show_progress_bar: Whether to show a tqdm progress bar.
        Returns:
            numpy.ndarray: Float32 embeddings of shape (len(texts), dim).
        """
        if isinstance(texts, str):
            texts = [texts]

        if not texts:
            return np.array([], dtype=np.float32).reshape(0, self.embedding_dimension)

        embeddings = []
        failed_indices = []

        for i, text in enumerate(tqdm(texts, desc="Encoding", disable=not show_progress_bar)):
            try:
                emb = self.model.create_embedding(text)['data'][0]['embedding']
                embeddings.append(emb)
            except Exception as e:
                print(f"  [ERROR] Failed to embed chunk {i}: {e}")
                print(f"  Preview: '{text[:80]}...'")
                failed_indices.append(i)
                embeddings.append([0.0] * self.embedding_dimension)

        if failed_indices:
            print(f"\n[WARNING] {len(failed_indices)} chunk(s) failed embedding: indices {failed_indices}")
            print("These chunks will have zero vectors in the FAISS index and will not be retrievable.\n")

        vecs = np.array(embeddings, dtype=np.float32)

        if normalize:
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            vecs = vecs / np.where(norms == 0, 1e-12, norms)

        return vecs

    def get_sentence_embedding_dimension(self) -> int:
        """Compatibility method."""
        return self.embedding_dimension

    def start_multi_process_pool(self, num_workers: int = None) -> multiprocessing.pool.Pool:
        """Starts a pool of worker processes."""
        # Disable multiprocessing on Apple Silicon due to GPU memory constraints
        is_apple_silicon = platform.system() == "Darwin"
        if is_apple_silicon:
            workers = 1
            print("[Apple Silicon] Using sequential encoding (multiprocessing disabled to prevent GPU memory exhaustion)")
        else:
            workers = num_workers if num_workers else max(1, multiprocessing.cpu_count() - 2)
            print(f"Creating {workers} worker processes...")

        pool = multiprocessing.Pool(
            processes=workers,
            initializer=_init_worker,
            initargs=(self.model_path, self.n_ctx, 1),
        )
        return pool

    def encode_multi_process(
        self,
        texts: List[str],
        pool: multiprocessing.pool.Pool,
        batch_size: int = 32,
    ) -> np.ndarray:
        """Distributes encoding work across the worker pool."""
        indices = np.argsort([len(t) for t in texts])[::-1]
        sorted_texts = [texts[i] for i in indices]

        chunks = [sorted_texts[i:i + batch_size] for i in range(0, len(sorted_texts), batch_size)]

        results = []
        print(f"Dispatching {len(chunks)} batches to pool...")
        for batch_result in tqdm(
            pool.imap(_encode_batch_worker, chunks),
            total=len(chunks),
            desc="Parallel Encoding",
        ):
            results.append(batch_result)

        flat_embeddings = [emb for batch in results for emb in batch]

        inverse_indices = np.empty_like(indices)
        inverse_indices[indices] = np.arange(len(indices))
        ordered_embeddings = [flat_embeddings[i] for i in inverse_indices]

        return np.array(ordered_embeddings, dtype=np.float32)

    @staticmethod
    def stop_multi_process_pool(pool: multiprocessing.pool.Pool):
        pool.close()
        pool.join()


class EmbeddingCache:
    """Persistent SQLite cache for embeddings."""

    def __init__(self, cache_dir: str = "index/cache"):
        self.db_path = Path(cache_dir) / "embeddings.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    model_name TEXT,
                    model_hash TEXT,
                    query_text TEXT,
                    embedding BLOB,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (model_hash, query_text)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_model_name ON embeddings(model_name)")

    def get(self, model_path: str, query: str) -> Optional[np.ndarray]:
        model_hash = hashlib.md5(model_path.encode()).hexdigest()[:16]
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT embedding FROM embeddings WHERE model_hash=? AND query_text=?",
                (model_hash, query),
            ).fetchone()
            if row:
                return np.frombuffer(row[0], dtype=np.float32)
        return None

    def set(self, model_path: str, query: str, embedding: np.ndarray):
        model_name = Path(model_path).stem
        model_hash = hashlib.md5(model_path.encode()).hexdigest()[:16]
        blob = embedding.astype(np.float32).tobytes()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings "
                "(model_name, model_hash, query_text, embedding) VALUES (?,?,?,?)",
                (model_name, model_hash, query, blob),
            )


class CachedEmbedder:
    """
    Wrapper around SentenceTransformer that caches query embeddings.
    Drop-in replacement for SentenceTransformer.
    """

    def __init__(self, model_path: str, **kwargs):
        self.embedder = SentenceTransformer(model_path, **kwargs)
        self.cache = EmbeddingCache()
        self.model_path = model_path

    def encode(self, texts, **kwargs):
        if isinstance(texts, str):
            texts = [texts]

        results = []
        to_compute = []
        to_compute_indices = []

        for i, text in enumerate(texts):
            cached = self.cache.get(self.model_path, text)
            if cached is not None:
                results.append((i, cached))
            else:
                to_compute.append(text)
                to_compute_indices.append(i)

        if to_compute:
            computed = self.embedder.encode(to_compute, **kwargs)
            for idx, text, emb in zip(to_compute_indices, to_compute, computed):
                self.cache.set(self.model_path, text, emb)
                results.append((idx, emb))

        results.sort(key=lambda x: x[0])
        return np.array([emb for _, emb in results])

    def __getattr__(self, name):
        return getattr(self.embedder, name)