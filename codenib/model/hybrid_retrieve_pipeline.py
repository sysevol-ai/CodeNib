# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Cascade-style hybrid retrieval: BM25 → embedding rerank (no agent loop).

This pipeline is the pure-retrieval baseline for RFC #133's agent evaluation.
It uses the same index artifacts as the agent path (via ``build_skill_contexts``)
but applies a deterministic heuristic instead of LLM-driven tool selection.

Architecture
------------
    Stage 1: BM25 sparse search over code chunks (recall-oriented filter)
    Stage 2: Embedding similarity rerank within BM25 results (precision-oriented)

Fusion Strategy
---------------
This pipeline implements a **cascade** (also called "rerank" or "filter-then-rank")
strategy, distinct from common hybrid retrieval variants in IR literature:

1. **Cascade (this pipeline)**:
   - BM25 retrieves top-K₁ candidates (K₁ >> K₂, e.g., 128)
   - Embedding reranker scores ONLY those K₁ candidates, returns top-K₂ (e.g., 50)
   - Final ranking: purely embedding similarity (BM25 scores discarded after stage 1)
   - Advantage: embedding model only scores promising candidates (faster)
   - Used by: ColBERT late interaction [Khattab+ SIGIR'20], SPLADE+rerank

2. **Reciprocal Rank Fusion (RRF)**:
   - BM25 and embedding run in parallel, each returns ranked list
   - Merge via: score(doc) = 1/(k + rank_BM25) + 1/(k + rank_embedding)
   - Final ranking: RRF combined scores
   - Advantage: simple, no weight tuning, robust across domains
   - Used by: Elasticsearch hybrid search, BEIR benchmark default

3. **Linear Combination**:
   - BM25 and embedding run in parallel
   - Merge via: score(doc) = α·norm(BM25) + β·norm(embedding), α+β=1
   - Final ranking: weighted sum (requires score normalization)
   - Advantage: interpretable weights, can tune α/β per domain
   - Used by: dense-sparse fusion in DPR+BM25, Pyserini hybrid

Our cascade choice is motivated by RFC #133's requirement to **match the agent's
tool-calling behavior**: the agent typically calls ``bm25_search`` first (cheap,
high recall), then decides whether to call ``embedding_search`` on a subset. The
cascade pipeline mirrors this sequential dependency.

Score Normalization
-------------------
- **Stage 1 (BM25)**: raw BM25 scores from ``BM25Retriever`` (Okapi BM25 formula).
  Scores are **not normalized** — only used for stage-1 ranking, then discarded.
- **Stage 2 (Embedding)**: cosine similarity ∈ [-1, 1] via inner-product metric.
  No further normalization needed (already bounded).
- **Final output**: embedding similarity scores (stage-2 output), sorted descending.

The lack of cross-stage score fusion simplifies the pipeline and avoids the
normalization pitfalls documented in BEIR [Thakur+ NeurIPS'21]: min-max / z-score
normalization can distort score distributions when candidate sets have different
characteristics (e.g., BM25's long tail vs embedding's tight cluster).

Contrast with Other Pipelines
------------------------------
- ``SparseSeededGraphRetrievePipeline``: BM25 → **graph expansion** →
  embedding rerank (adds a graph-walk middle stage; graph-first baseline)
- Agent A2 (``bm25_search`` + ``embedding_search``): **LLM decides** when/how to
  call each tool (adaptive, but token-expensive and non-deterministic)
- ``RetrieveRerankPipeline``: parallel **hybrid fusion** with configurable weights
  (RRF or linear combination; more flexible but higher latency)

References
----------
- Khattab & Zaharia (2020): "ColBERT: Efficient and Effective Passage Search via
  Contextualized Late Interaction over BERT." SIGIR.
- Thakur et al. (2021): "BEIR: A Heterogeneous Benchmark for Zero-shot Evaluation
  of Information Retrieval Models." NeurIPS Datasets & Benchmarks.
- Karpukhin et al. (2020): "Dense Passage Retrieval for Open-Domain Question
  Answering." EMNLP. (DPR+BM25 hybrid baseline)
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from ..compiler import build_skill_contexts
from ..log_utils import get_logger
from ..types import QueriedNode

logger = get_logger(__name__)


class HybridRetrievePipeline:
    """Two-stage cascade retrieval: BM25 → embedding rerank.

    Args:
        repo_path: Repository root to index.
        index_cache_dir: Directory for cached indexes (default: <repo>/.codenib_cache).
        stage1_topk: Number of BM25 candidates to retrieve (default: 128).
        stage2_topk: Final number of results after embedding rerank (default: 50).
        embedding_model: Embedding model name (default: nomic-ai/CodeRankEmbed).
        embedding_dimension: Embedding vector dimension (default: 768).
        languages: Languages to index (default: ["python"]).
        embedding_batch_size: Batch size for embedding inference (default: 8 to avoid CUDA OOM).

    Example:
        >>> pipeline = HybridRetrievePipeline(
        ...     repo_path="/path/to/repo",
        ...     index_cache_dir="~/.codenib/cache",
        ... )
        >>> results = pipeline.query("how to parse CSV files", top_k=10)
        >>> for node in results[:5]:
        ...     print(f"{node.file}:{node.node_name} (score={node.score:.3f})")
    """

    def __init__(
        self,
        repo_path: str,
        *,
        index_cache_dir: Optional[str] = None,
        stage1_topk: int = 128,
        stage2_topk: int = 50,
        embedding_model: str = "nomic-ai/CodeRankEmbed",
        embedding_dimension: int = 768,
        languages: Sequence[str] = ("python",),
        embedding_batch_size: int = 8,
    ):
        self.repo_path = repo_path
        self.stage1_topk = stage1_topk
        self.stage2_topk = stage2_topk

        # Load skill registry (required before build_skill_contexts)
        import os as _os

        from ..agent.skills.loader import SkillLoader
        from ..agent.skills.registry import SkillRegistry

        skills_dir = _os.path.join(
            _os.path.dirname(_os.path.dirname(__file__)), "agent", "skills"
        )
        registry = SkillRegistry()
        loader = SkillLoader()
        loaded_skills = loader.load_all(skills_dir, contexts={}, registry=registry)
        logger.debug("Loaded %d skills into registry", len(loaded_skills))

        # Build indexes via compiler (ADR-0001: always-chunks path)
        logger.info(
            "Building indexes for BM25 + embedding (cascade mode) at %s",
            index_cache_dir or "<repo>/.codenib_cache",
        )
        contexts = build_skill_contexts(
            repo_path=repo_path,
            skill_ids=["bm25_search", "embedding_search"],
            languages=list(languages),
            cache_dir=index_cache_dir,
            skill_registry=registry,  # Pass loaded registry
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
            default_top_k=stage1_topk,  # BM25 max_k
            default_level="l2",  # embedding level
            trust_remote_code=True,  # Required for nomic-ai/CodeRankEmbed
            embedding_batch_size=embedding_batch_size,  # Avoid CUDA OOM
        )

        retrieve_ctx = contexts.get("retrieve")
        if retrieve_ctx is None:
            raise RuntimeError(
                "build_skill_contexts did not return a 'retrieve' context. "
                "Check skill registry and index_requirements."
            )

        self.bm25 = retrieve_ctx.bm25
        self.vector_store = retrieve_ctx.vector_store

        if self.bm25 is None:
            raise RuntimeError("BM25 index not built (bm25_search skill failed).")
        if self.vector_store is None:
            raise RuntimeError(
                "Embedding index not built (embedding_search skill failed)."
            )

        logger.info(
            "HybridRetrievePipeline ready: BM25 (top-%d) → embedding rerank (top-%d)",
            stage1_topk,
            stage2_topk,
        )

    def query(self, query: str, top_k: Optional[int] = None) -> List[QueriedNode]:
        """Run cascade retrieval: BM25 → embedding rerank.

        Args:
            query: Search query (e.g., problem statement).
            top_k: Number of final results (overrides stage2_topk if provided).

        Returns:
            List of QueriedNode sorted by embedding similarity (descending).
        """
        final_k = top_k if top_k is not None else self.stage2_topk

        # Stage 1: BM25 sparse search (recall-oriented filter)
        logger.debug("Stage 1: BM25 search (top_k=%d)", self.stage1_topk)
        bm25_results = self.bm25.search(
            query=query,
            top_k=self.stage1_topk,
            return_code_content=False,  # embedding rerank will fetch content
            wrap_with_ln=False,
            filter_test=False,
        )

        if not bm25_results:
            logger.warning("BM25 returned no results for query: %r", query)
            return []

        # Extract node_ids for embedding rerank mask
        candidate_ids = {r.node_id for r in bm25_results if r.node_id}
        logger.debug(
            "Stage 1 complete: %d candidates (unique node_ids: %d)",
            len(bm25_results),
            len(candidate_ids),
        )

        # Stage 2: Embedding rerank within BM25 candidates
        logger.debug(
            "Stage 2: embedding rerank within %d candidates", len(candidate_ids)
        )
        reranked = self.vector_store.search_within_ids(
            query=query,
            mask_node_ids=candidate_ids,
            top_k=final_k,
        )

        logger.info(
            "Pipeline complete: %d results (BM25=%d → embedding=%d)",
            len(reranked),
            len(bm25_results),
            len(reranked),
        )

        # Convert to QueriedNode format (search_within_ids returns NodeInfo)
        return [
            QueriedNode(
                node_name=n.node_name,
                type=n.type,
                file=n.file,
                node_id=n.node_id or n.node_name,
                start_line=n.start_line,
                end_line=n.end_line,
                score=n.score,
                content=n.content,
            )
            for n in reranked
        ]

    def close(self) -> None:
        """Release pipeline resources."""
        if self.vector_store is not None:
            self.vector_store.close()
            self.vector_store = None
        self.bm25 = None
