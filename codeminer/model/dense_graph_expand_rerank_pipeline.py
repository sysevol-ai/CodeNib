# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Dict, List, Optional, Sequence

from ..graph.code_graph import CodeGraph
from ..ops.expand import ExpandContext, expand_graph_neighbors
from ..ops.filter import CandidatePredicate, filter_queried_nodes
from ..ops.rerank import RerankContext, rerank_by_embedding, rerank_by_indexed_embedding
from ..ops.retrieve import dedup_queried_nodes, merge_file_rankings, queried_node_key
from ..profiler import Profiler
from ..types import EDGE_TYPE_REFERENCE, NodeInfo, QueriedNode
from .embedding_retrieve_pipeline import EmbeddingRetrievePipeline


def _section(
    profiler: Optional[Profiler],
    label: str,
    metadata: Optional[Dict[str, Any]] = None,
):
    if profiler is None:
        return nullcontext()
    return profiler.section(label, metadata)


class DenseGraphExpandRerankPipeline:
    """Dense-first graph expansion retrieval pipeline.

    Stage 1: retrieve dense top-K candidates from the full vector index.
    Stage 2: append one-hop graph neighbors for those dense candidates.
    Stage 3: optionally filter and deduplicate the combined candidate set.
    Stage 4: optionally rerank only the assembled candidates.

    This pipeline is intended for the "embedding baseline plus graph
    augmentation" experiment family. It is intentionally different from
    :class:`SparseSeededGraphRetrievePipeline`, which uses sparse/BM25 seeds and
    ranks inside the graph-expanded set.
    """

    def __init__(
        self,
        retriever: EmbeddingRetrievePipeline,
        code_graph: Optional[CodeGraph] = None,
        *,
        rerank_context: Optional[RerankContext] = None,
        repo_path: Optional[str] = None,
        dense_top_k: int = 20,
        graph_seed_top_k: Optional[int] = None,
        graph_neighbors_per_seed: int = 5,
        graph_direction: str = "both",
        graph_edge_types: Optional[Sequence[str]] = (EDGE_TYPE_REFERENCE,),
        graph_fusion_weight: float = 0.0,
        graph_fusion_rrf_k: int = 60,
        final_top_k: int = 10,
        candidate_top_k: Optional[int] = None,
        candidate_filters: Optional[Sequence[CandidatePredicate]] = None,
        include_graph_content: bool = True,
        graph_symbol_only: bool = True,
        graph_require_span: bool = True,
        close_retriever: bool = False,
    ) -> None:
        if dense_top_k <= 0:
            raise ValueError("dense_top_k must be positive.")
        if final_top_k <= 0:
            raise ValueError("final_top_k must be positive.")
        if graph_neighbors_per_seed < 0:
            raise ValueError("graph_neighbors_per_seed must be non-negative.")
        if graph_seed_top_k is not None and graph_seed_top_k <= 0:
            raise ValueError("graph_seed_top_k must be positive when provided.")
        if graph_fusion_weight < 0:
            raise ValueError("graph_fusion_weight must be non-negative.")
        if graph_fusion_rrf_k <= 0:
            raise ValueError("graph_fusion_rrf_k must be positive.")
        if graph_fusion_weight and rerank_context is None:
            raise ValueError("graph fusion requires a rerank_context.")

        self.retriever = retriever
        self.expand_context = ExpandContext(code_graph=code_graph)
        self.rerank_context = rerank_context
        self.repo_path = repo_path
        self.dense_top_k = dense_top_k
        self.graph_seed_top_k = graph_seed_top_k
        self.graph_neighbors_per_seed = graph_neighbors_per_seed
        self.graph_direction = graph_direction
        self.graph_edge_types = (
            None if graph_edge_types is None else tuple(graph_edge_types)
        )
        self.graph_fusion_weight = graph_fusion_weight
        self.graph_fusion_rrf_k = graph_fusion_rrf_k
        self.final_top_k = final_top_k
        self.candidate_top_k = candidate_top_k
        self.candidate_filters = list(candidate_filters or [])
        self.include_graph_content = include_graph_content
        self.graph_symbol_only = graph_symbol_only
        self.graph_require_span = graph_require_span
        self.close_retriever = close_retriever

        self.last_dense_results: List[QueriedNode] = []
        self.last_graph_results: List[QueriedNode] = []
        self.last_filtered_candidates: List[QueriedNode] = []
        self.last_candidates: List[QueriedNode] = []
        self.last_semantic_results: List[QueriedNode] = []
        self.last_reranked_results: List[QueriedNode] = []

    @property
    def code_graph(self) -> Optional[CodeGraph]:
        return self.expand_context.code_graph

    def load_index(self, index_path: str) -> None:
        """Hot-swap the backing dense retrieval index."""
        self.retriever.load_index(index_path)

    def load_graph(
        self, code_graph: Optional[CodeGraph], *, repo_path: Optional[str] = None
    ) -> None:
        """Hot-swap the graph used for neighbor expansion."""
        self.expand_context.code_graph = code_graph
        if repo_path is not None:
            self.repo_path = repo_path

    def query(
        self,
        query: str,
        *,
        top_k: Optional[int] = None,
        dense_top_k: Optional[int] = None,
        profiler: Optional[Profiler] = None,
    ) -> List[QueriedNode]:
        """Retrieve dense candidates, append graph neighbors, dedup, and rerank."""
        with self._query_embedding_scope():
            return self._query_impl(
                query,
                top_k=top_k,
                dense_top_k=dense_top_k,
                profiler=profiler,
            )

    def _query_embedding_scope(self):
        context = self.rerank_context
        if context is None or context.embedding_source != "index":
            return nullcontext()
        store = context.embedding_store
        if store is None or getattr(self.retriever, "vector_store", None) is not store:
            return nullcontext()
        return store.reuse_query_embedding()

    def _query_impl(
        self,
        query: str,
        *,
        top_k: Optional[int],
        dense_top_k: Optional[int],
        profiler: Optional[Profiler],
    ) -> List[QueriedNode]:
        final_top_k = top_k if top_k is not None else self.final_top_k
        dense_k = dense_top_k if dense_top_k is not None else self.dense_top_k
        if final_top_k <= 0:
            return []
        if dense_k <= 0:
            raise ValueError("dense_top_k must be positive.")

        with _section(profiler, "query.dense_retrieve", {"top_k": dense_k}):
            dense_results = self.retriever.query(query, top_k=dense_k)

        graph_seed_k = self.graph_seed_top_k or len(dense_results)
        graph_seeds = dense_results[:graph_seed_k]

        with _section(
            profiler,
            "query.graph_neighbor_expand",
            {
                "seeds": len(graph_seeds),
                "per_seed": self.graph_neighbors_per_seed,
                "direction": self.graph_direction,
                "edge_types": self.graph_edge_types,
            },
        ):
            graph_results = expand_graph_neighbors(
                self.expand_context,
                graph_seeds,
                per_seed=self.graph_neighbors_per_seed,
                direction=self.graph_direction,
                repo_path=self.repo_path,
                include_content=self.include_graph_content,
                symbol_only=self.graph_symbol_only,
                require_span=self.graph_require_span,
                edge_types=self.graph_edge_types,
            )

        combined = [*dense_results, *graph_results]
        with _section(
            profiler,
            "query.candidate_filter",
            {"candidates": len(combined), "filters": len(self.candidate_filters)},
        ):
            filtered = filter_queried_nodes(combined, self.candidate_filters)

        with _section(
            profiler,
            "query.candidate_dedup",
            {"dense": len(dense_results), "graph": len(graph_results)},
        ):
            candidates = dedup_queried_nodes(filtered)

        self.last_dense_results = list(dense_results)
        self.last_graph_results = list(graph_results)
        self.last_filtered_candidates = list(filtered)
        self.last_candidates = list(candidates)

        if self.rerank_context is None:
            self.last_semantic_results = []
            self.last_reranked_results = []
            return candidates[:final_top_k]

        candidate_cap = self.candidate_top_k or self.rerank_context.candidate_top_k
        rerank_candidates = (
            candidates[:candidate_cap] if candidate_cap is not None else candidates
        )
        with _section(
            profiler,
            "query.rerank",
            {"candidates": len(rerank_candidates), "top_k": final_top_k},
        ):
            semantic_top_k = (
                len(rerank_candidates) if self.graph_fusion_weight else final_top_k
            )
            reranked = self._rerank(query, rerank_candidates, semantic_top_k)
            self.last_semantic_results = list(reranked)

            if self.graph_fusion_weight:
                candidate_keys = {queried_node_key(node) for node in rerank_candidates}
                graph_branch = dedup_queried_nodes(
                    [
                        node
                        for node in graph_results
                        if queried_node_key(node) in candidate_keys
                    ]
                )
                reranked = merge_file_rankings(
                    [reranked, graph_branch],
                    weights=[1.0, self.graph_fusion_weight],
                    top_k=final_top_k,
                    rrf_k=self.graph_fusion_rrf_k,
                )

        self.last_reranked_results = list(reranked)
        return reranked[:final_top_k]

    def close(self) -> None:
        """Release owned resources."""
        if self.close_retriever:
            self.retriever.close()

    def _rerank(
        self,
        query: str,
        candidates: Sequence[QueriedNode],
        top_k: int,
    ) -> List[QueriedNode]:
        context = self.rerank_context
        if context is None:
            return list(candidates[:top_k])

        if context.embedding_store is not None:
            if context.embedding_source == "index":
                return rerank_by_indexed_embedding(
                    query,
                    candidates,
                    context.embedding_store,
                    top_k=top_k,
                )
            return rerank_by_embedding(
                query,
                candidates,
                context.embedding_store,
                top_k=top_k,
            )

        agent = context.ensure_agent()
        nodes = [NodeInfo(**_dump_node(candidate)) for candidate in candidates]
        return agent.rerank_nodes(
            query=query,
            nodes=nodes,
            top_k=top_k,
            window_size=context.window_size,
            window_step=context.window_step,
            include_content=True,
        )


def _dump_node(node: QueriedNode) -> Dict[str, object]:
    if hasattr(node, "model_dump"):
        return dict(node.model_dump())
    if hasattr(node, "dict"):
        return dict(node.dict())
    raise TypeError(f"Object {node!r} does not support model dumping.")
