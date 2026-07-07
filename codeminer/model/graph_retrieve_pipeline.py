# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..graph.roi_subgraph import ROISubgraph
from ..index.embedding import CodeVectorStore, build_hierarchical_vector_store
from ..index.sparse_idx.bm25_index import BM25CodeIndexer
from ..log_utils import get_logger
from ..ls_router import build_graph_for_languages
from ..profiler import Profiler
from ..types import QueriedNode

logger = get_logger(__name__)


def _section(
    profiler: Optional[Profiler], label: str, metadata: Optional[Dict[str, Any]] = None
):
    """Open a profiler section if profiler is provided, otherwise a no-op."""
    if profiler is None:
        return nullcontext()
    return profiler.section(label, metadata)


class SparseSeededGraphRetrievePipeline:
    """Sparse-seeded graph-first retrieval pipeline.

    Stage 1: BM25 sparse search selects a small set of seed nodes.
    Stage 2: Graph expansion via k-hop BFS or Personalized PageRank.
    Stage 3 (optional): Embedding rerank within the expanded set.

    This is a graph-first baseline. It should not be used as the direct
    comparison point for dense embedding retrieval unless that sparse seeding
    design is the intended ablation.

    Args:
        repo_path: Repository root to index.
        index_path: Directory used for graph and vector index caches.
        stage1_topk: Number of BM25 seed nodes (default: 5).
        stage2_topk: Max nodes after graph expansion (default: 50).
        k_hop: BFS expansion depth (default: 2). Ignored when use_ppr=True.
        use_ppr: Use Personalized PageRank instead of BFS for Stage 2.
        ppr_damping: PPR damping factor (default: 0.85).
        use_embedding_rerank: Enable embedding rerank in Stage 3.
        embedding_model: Embedding model for Stage 3.
        embedding_provider: Embedding provider for Stage 3.
        embedding_dimension: Embedding vector dimension for Stage 3.
        languages: Languages to index. Multiple entries build per-language
            graphs and merge them before BM25/graph expansion. The full list is
            also used for Stage 3 chunking.
        graph_route: ``"active"`` for public graph routes, ``"lsp"`` for
            backend comparison, or ``"scip-candidate"`` to explicitly evaluate
            candidate SCIP routes.
        max_lines_per_chunk: Maximum lines per chunk.
        project_name: Project name for SCIP indexing; defaults to index_path basename.
    """

    def __init__(
        self,
        repo_path: str,
        index_path: str,
        *,
        stage1_topk: int = 5,
        stage2_topk: int = 50,
        k_hop: int = 2,
        use_ppr: bool = False,
        ppr_damping: float = 0.85,
        use_embedding_rerank: bool = False,
        embedding_model: str = "nomic-ai/CodeRankEmbed",
        embedding_provider: str = "huggingface",
        embedding_dimension: int = 768,
        languages: Optional[List[str]] = None,
        graph_route: str = "active",
        max_lines_per_chunk: int = 300,
        project_name: Optional[str] = None,
        profiler: Optional[Profiler] = None,
    ) -> None:
        self.stage1_topk = stage1_topk
        self.stage2_topk = stage2_topk
        self.k_hop = k_hop
        self.use_ppr = use_ppr
        self.ppr_damping = ppr_damping
        self.use_embedding_rerank = use_embedding_rerank
        # Populated by query(): BM25 stage-1 results from the most recent
        # query. Surfaced so callers can compute correctness signals such as
        # bm25_seed_recall@k without re-running BM25.
        self.last_bm25_seeds: List[Any] = []

        pname = project_name or Path(index_path).name
        index_languages = languages or ["python"]

        # Build CodeGraph via the registry-backed graph indexer.
        with _section(profiler, "index.graph_build", {"repo": pname}):
            self.code_graph = build_graph_for_languages(
                repo_path,
                index_path,
                languages=index_languages,
                project_name=pname,
                skip_level="graph",
                profiler=profiler,
                graph_route=graph_route,
            )
            if self.code_graph is None:
                raise RuntimeError(f"Failed to build code graph for {repo_path!r}")

        # Build BM25 index from graph
        with _section(profiler, "index.bm25_build", {"max_k": stage1_topk}):
            self.bm25_index = BM25CodeIndexer(max_k=stage1_topk, language="english")
            self.bm25_index.build_index_from_graph(self.code_graph)

        # Build vector store for Stage 3 if requested
        self.vector_store: Optional[CodeVectorStore] = None
        if use_embedding_rerank:
            with _section(
                profiler,
                "index.vector_store_build",
                {"model": embedding_model, "dim": embedding_dimension},
            ):
                self.vector_store = build_hierarchical_vector_store(
                    repo_path=repo_path,
                    index_path=index_path,
                    plan_name=None,
                    languages=index_languages,
                    max_lines_per_chunk=max_lines_per_chunk,
                    build_levels=["l2"],
                    embedding_model=embedding_model,
                    embedding_provider=embedding_provider,
                    embedding_dimension=embedding_dimension,
                    embedding_kwargs={
                        "model_kwargs": {"trust_remote_code": True},
                        "encode_kwargs": {"batch_size": 4},
                    },
                    index_metric="ip",
                    profiler=profiler,
                )

    def query(
        self,
        query: str,
        profiler: Optional[Profiler] = None,
    ) -> List[QueriedNode]:
        """Run the three-stage graph retrieval pipeline.

        Args:
            query: The search query (e.g., problem statement).
            profiler: Optional profiler. When provided, stages are recorded as
                ``query.bm25_seed``, ``query.graph_expand``,
                ``query.embedding_rerank``.

        Returns:
            List of QueriedNode results.
        """
        # Stage 1: BM25 seed selection
        with _section(profiler, "query.bm25_seed", {"top_k": self.stage1_topk}):
            bm25_results = self.bm25_index.search(query, top_k=self.stage1_topk)
            seed_names = [r.node_name for r in bm25_results]
        self.last_bm25_seeds = list(bm25_results)
        logger.info("Stage 1: %d seed nodes from BM25", len(seed_names))

        # Stage 2: Graph expansion
        with _section(
            profiler,
            "query.graph_expand",
            {"mode": "ppr" if self.use_ppr else "bfs", "top_k": self.stage2_topk},
        ):
            roi = ROISubgraph(self.code_graph)
            if self.use_ppr:
                expanded_nodes = roi.expand_ppr(
                    seed_names,
                    top_k=self.stage2_topk,
                    damping=self.ppr_damping,
                    filter_tests=True,
                )
                logger.info(
                    "Stage 2: %d nodes after PPR expansion (damping=%.2f)",
                    len(expanded_nodes),
                    self.ppr_damping,
                )
            else:
                subgraph = roi.extract_subgraph(
                    seed_names, k_hop=self.k_hop, direction="both"
                )
                expanded_nodes = roi.get_filtered_subgraph_nodes(
                    subgraph, exclude_nodes=None, filter_tests=True
                )
                expanded_nodes = expanded_nodes[: self.stage2_topk]
                logger.info(
                    "Stage 2: %d nodes after %d-hop BFS expansion",
                    len(expanded_nodes),
                    self.k_hop,
                )

        # Stage 3 (optional): Embedding rerank within expanded set only
        if self.use_embedding_rerank and expanded_nodes and self.vector_store:
            with _section(
                profiler,
                "query.embedding_rerank",
                {"candidates": len(expanded_nodes)},
            ):
                mask_ids: set = set()
                for node in expanded_nodes:
                    if node.node_name:
                        mask_ids.add(node.node_name)
                    if node.node_id:
                        mask_ids.add(node.node_id)
                # Search ONLY within the graph-expanded node set — do NOT search
                # the full FAISS index globally.  This is the key design: graph
                # expansion reduces the corpus, then embedding ranks within it.
                reranked = self.vector_store.search_within_ids(
                    query, mask_node_ids=mask_ids, top_k=self.stage2_topk
                )
                results = [
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
            logger.info("Stage 3: %d nodes after embedding rerank", len(results))
        else:
            # ROISubgraph returns node_id=None; use node_name as node_id since
            # it has the "file:symbol()" format that extract_predictions expects.
            results = [
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
                for n in expanded_nodes
            ]

        return results

    def close(self) -> None:
        """Release pipeline resources."""
        if self.vector_store is not None:
            self.vector_store.close()
            self.vector_store = None


class GraphRetrievePipeline(SparseSeededGraphRetrievePipeline):
    """Compatibility alias for :class:`SparseSeededGraphRetrievePipeline`."""
