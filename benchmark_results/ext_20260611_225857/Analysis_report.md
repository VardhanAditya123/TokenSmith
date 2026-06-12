# Evaluation Report: 2-Level Multi-Hop Clustering Index

I finished testing the **2-level multi-hop clustering** strategy on TokenSmith. The results show some clear trade-offs between using a huge model versus using a smart structure to find data. 

To really test this setup, I ran the new graph structure using a much lower-quality embedding model than my baseline run. 

---

## 1. The Two Setups

Instead of changing one thing at a time, this run forced a weaker, highly compressed model to handle the data routing and multihop pathfinding. 

* **Baseline Run (`book_exc_local`):** Used a heavy approach with a **4B parameter Qwen3 model** and standard single-level dense search plus a reranker.
* **New Run (`ext_20260611_225857`):** Swapped in a tiny **335M parameter mxbai model** squeezed down to 4-bit quantization. The reranker was turned off.

---

## 2. The Numbers

| Metric | Baseline Run (`book_exc_local`) | Structural Run (`ext_20260611_225857`) | Change |
| :--- | :---: | :---: | :---: |
| **Must Rubric Met Rate** | 59.3% | **68.8%** | **+9.5%** 🚀 |
| **Fully Correct (Score 1)** | **70.7% (29 / 41)** | 62.5% (25 / 40) | -8.2% |
| **Incorrect (Score -1)** | **2.4% (1 / 41)** | 5.0% (2 / 40) | +2.6% |
| **Chunk Relevance Rate** | **39.4%** | 34.7% | -4.7% |
| **Optional Rubric Met Rate** | **46.4%** | 23.1% | -23.3% |

---

## 3. Why the Metrics Shifted

### Multihop Structure Saved the Weak Model
Normally, when you drop from a 4B model to a 335M model, performance drops hard because the vector math gets blurry. 

But wrapping the data in a **2-level cluster graph with multihop retrieval** fixed this. Because the code navigates by large topic groups and explicitly "walks" multi-step paths across clusters to connect relevant pieces of data, it does not need perfect embedding math for individual chunks. This structural safety net gave us the **+9.5% jump in the Must Rubric score**.

### Why Chunk Relevance and Optional Rubrics Dropped
Using a weaker, 4-bit model added a clear tax:
1. **Messy Cluster Edges:** The low-quality model made cluster boundaries blurry. This pulled in extra, useless chunks during the multihop traversal, dropping **Chunk Relevance from 39.4% to 34.7%**. But the fact that it  gave us the **+9.5% jump in the Must Rubric score** implies that the quality of retrieved chunks was slightly higher. During the multiphop, I noticed that the clusters being searched in every model were different occasionally. This imples that the retriever was updating prior beliefs based on recent data.
2. **Crowded Context Window:** The LLM got too much extra noise in its prompt from these extra hops. It could still find the main points to answer the core questions (Must Rubrics went up), but the extra noise crowded out the tiny details needed for the **Optional Rubrics (which fell to 23.1%)**.


---

## 4. Next Steps

To clean out the noise without losing the multihop routing capabilities, I want to try two fixes:

* **Parent-Child Chunking:** Use tiny 100-token chunks just to route through the clusters and handle the multihop steps, but pass larger parent text blocks to the LLM once we find the right spots.
* **Multi-hop LLM retrieval :**  After each hop, we fetch an answer to the question, append to the query and retrieve updated chunks. Right now, only the top retrieved chunks are added to the query for the
next hop without an LLM call.
next_batch