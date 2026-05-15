# TokenSmith vs PageIndex

A practical comparison of two document retrieval approaches.

## Architecture

**TokenSmith** chunks documents into 1000-character pieces, embeds them (384 dimensions), and clusters them using k-means (14 clusters for 149 chunks). Queries are embedded and searched against the top 3 nearest clusters—simple and deterministic.

```
Markdown → Extract Sections → Chunk (1000 chars)
    ↓
Embed  → Build FAISS Index
    ↓
Compute K-means Clustering (any number of user defined clusters)
    ↓
Pre-compute Cluster Metadata & Save
```

**PageIndex** uses LLM reasoning to build a hierarchical tree of the document. Each node has a semantic summary. Retrieval means traversing the tree with LLM-guided decisions at each level.

```
PDF → Parse Document → Extract Natural Sections
    ↓
Generate Hierarchical Tree (via LLM)
    ↓
Create Semantic Summaries for Each Node
    ↓
Save Tree Structure (JSON)
```

---

## Speed

TokenSmith: ~5.6 seconds per query. Embed → cluster lookup → FAISS search → rank → generate. Deterministic and fast.

PageIndex: Involves multiple LLM reasoning steps during tree traversal, making it slower than vector-based approaches. Exact latency not publicly documented, but expect significantly more than TokenSmith per query.

**Winner: TokenSmith** (much faster)

## Accuracy

TokenSmith achieves 81% keyword match and 90% pass rate on SQL questions from a textbook.

PageIndex reports 98.7% accuracy on FinanceBench (financial documents, SEC filings). The reasoning-based approach handles complex documents better.

**Winner: PageIndex** (higher accuracy for complex docs)

---

## Scalability & Resource Efficiency

**TokenSmith**: Efficient scaling. 5.6x speedup on textbook (149 chunks). FAISS memory: ~4 bytes per embedding × chunk count. Runs on laptops, M1 Macs, or CPU-only. Cluster count capped at 100 so speedup plateaus at large scales.

**PageIndex**: Build time scales with document complexity. Single document takes minutes (LLM calls), amortized over many queries. Moderate JSON storage size. Requires API access, server infrastructure preferred.

---

## Use Cases

**TokenSmith ideal for:**
- Real-time Q&A (textbooks, educational platforms)
- High-volume queries (>100/day) 
- Local-first or offline deployment
- Privacy-sensitive applications

**PageIndex ideal for:**
- Financial/legal document analysis
- Complex, variable document structures
- Where accuracy >95% required
- Compliance/audit trails needed

## Explainability & Hardware

**TokenSmith**: Cluster IDs + relevance scores visible (medium explainability). Runs on laptops, M1 Macs, phones. 16GB RAM sufficient.

**PageIndex**: Full LLM reasoning trace visible (high explainability). Requires API access (OpenAI, etc.), server infrastructure preferred.

---

## Qualitative Comparison

### Strengths & Weaknesses

**TokenSmith Strengths**:
- ✓ Completely deterministic—same query always finds same clusters
- ✓ Works offline, no internet dependency
- ✓ Transparent retrieval path (cluster → chunks → ranking)
- ✓ Scales linearly with data added
- ✓ Fine control over clustering parameters

**TokenSmith Weaknesses**:
- ✗ Misses relevant content if clusters don't align with semantic similarity
- ✗ No semantic understanding—purely distance-based
- ✗ Poor on cross-cluster queries (e.g., "compare X from section A to Y from section B")
- ✗ Requires good chunking strategy upfront

**PageIndex Strengths**:
- ✓ Understands document semantics and context
- ✓ Handles multi-step reasoning queries naturally
- ✓ Preserves document structure (no artificial chunks)
- ✓ Can reference page numbers and sections naturally
- ✓ Adapts reasoning based on conversation history

**PageIndex Weaknesses**:
- ✗ Non-deterministic (LLM reasoning varies)
- ✗ Expensive at scale (costs accumulate)
- ✗ Slower per-query (multiple LLM calls)
- ✗ Requires ongoing API access and management
- ✗ Opaque during development (harder to debug why LLM chose path X)

### Document Type Suitability

**TokenSmith works well with**:
- Textbooks (well-structured chapters)
- Technical manuals (consistent formatting)
- FAQs (clear sections)
- Lecture notes (naturally chunked)

**TokenSmith struggles with**:
- Legal contracts (cross-references everywhere)
- Financial reports (complex inter-dependencies)
- Narrative documents (reasoning required)

**PageIndex works well with**:
- SEC filings (complex but consistent structure)
- Legal documents (benefits from reasoning)
- Mixed-format PDFs (adapts to structure)
- Financial reports (multi-layer dependencies)

**PageIndex struggles with**:
- Very large corpora (build time)
- Rapidly changing documents (rebuild costs)
- Simple Q&A (overkill)

---

## The Trade-off

**TokenSmith** = Speed + Cost ($0/query)  
**PageIndex** = Accuracy (98.7%) + Reasoning  

For educational Q&A or high-volume use: **TokenSmith wins**.  
For financial/legal analysis where accuracy matters most: **PageIndex worth the cost**.

## Hybrid Approach

Combine both:
1. TokenSmith clustering → quickly narrow sections (fast)
2. PageIndex reasoning → verify results (accurate)
3. Result: Fast + accurate, costs increase

## Bottom Line

Choose based on constraints:
- **Speed + low cost?** → TokenSmith
- **High accuracy + reasoning?** → PageIndex
- **Both?** → Combine them

---

*Analysis: May 18, 2026*


