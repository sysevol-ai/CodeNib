# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Model pipelines and planners exposed without eager optional imports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._lazy import exported_dir, load_export

if TYPE_CHECKING:  # pragma: no cover - imported only by static analyzers
    from .agentless_pipeline import AgentlessPipeline
    from .bm25_retrieve_pipeline import BM25RetrievePipeline
    from .dense_graph_expand_rerank_pipeline import DenseGraphExpandRerankPipeline
    from .embedding_retrieve_pipeline import EmbeddingRetrievePipeline
    from .graph_augmented_rerank_pipeline import GraphAugmentedRerankPipeline
    from .graph_retrieve_pipeline import (
        GraphRetrievePipeline,
        SparseSeededGraphRetrievePipeline,
    )
    from .hybrid_retrieve_pipeline import HybridRetrievePipeline
    from .retrieval_planner import (
        GraphExpansionPlan,
        RetrievalBudget,
        RetrievalCapabilities,
        RetrievalPathPlan,
        RetrievalPlanner,
        RetrievalStagePlan,
    )
    from .retrieve_rerank_pipeline import (
        RetrieveRerankPipeline,
        RetrieveStageConfig,
        build_retrieve_plan,
    )

_EXPORTS = {
    "AgentlessPipeline": ("codenib.model.agentless_pipeline", "AgentlessPipeline"),
    "BM25RetrievePipeline": (
        "codenib.model.bm25_retrieve_pipeline",
        "BM25RetrievePipeline",
    ),
    "DenseGraphExpandRerankPipeline": (
        "codenib.model.dense_graph_expand_rerank_pipeline",
        "DenseGraphExpandRerankPipeline",
    ),
    "EmbeddingRetrievePipeline": (
        "codenib.model.embedding_retrieve_pipeline",
        "EmbeddingRetrievePipeline",
    ),
    "GraphAugmentedRerankPipeline": (
        "codenib.model.graph_augmented_rerank_pipeline",
        "GraphAugmentedRerankPipeline",
    ),
    "GraphRetrievePipeline": (
        "codenib.model.graph_retrieve_pipeline",
        "GraphRetrievePipeline",
    ),
    "SparseSeededGraphRetrievePipeline": (
        "codenib.model.graph_retrieve_pipeline",
        "SparseSeededGraphRetrievePipeline",
    ),
    "HybridRetrievePipeline": (
        "codenib.model.hybrid_retrieve_pipeline",
        "HybridRetrievePipeline",
    ),
    "GraphExpansionPlan": (
        "codenib.model.retrieval_planner",
        "GraphExpansionPlan",
    ),
    "RetrievalBudget": ("codenib.model.retrieval_planner", "RetrievalBudget"),
    "RetrievalCapabilities": (
        "codenib.model.retrieval_planner",
        "RetrievalCapabilities",
    ),
    "RetrievalPathPlan": (
        "codenib.model.retrieval_planner",
        "RetrievalPathPlan",
    ),
    "RetrievalPlanner": ("codenib.model.retrieval_planner", "RetrievalPlanner"),
    "RetrievalStagePlan": (
        "codenib.model.retrieval_planner",
        "RetrievalStagePlan",
    ),
    "RetrieveRerankPipeline": (
        "codenib.model.retrieve_rerank_pipeline",
        "RetrieveRerankPipeline",
    ),
    "RetrieveStageConfig": (
        "codenib.model.retrieve_rerank_pipeline",
        "RetrieveStageConfig",
    ),
    "build_retrieve_plan": (
        "codenib.model.retrieve_rerank_pipeline",
        "build_retrieve_plan",
    ),
}

__all__ = [
    "AgentlessPipeline",
    "BM25RetrievePipeline",
    "DenseGraphExpandRerankPipeline",
    "EmbeddingRetrievePipeline",
    "GraphAugmentedRerankPipeline",
    "GraphExpansionPlan",
    "GraphRetrievePipeline",
    "HybridRetrievePipeline",
    "RetrievalBudget",
    "RetrievalCapabilities",
    "RetrievalPathPlan",
    "RetrievalPlanner",
    "RetrievalStagePlan",
    "RetrieveRerankPipeline",
    "RetrieveStageConfig",
    "SparseSeededGraphRetrievePipeline",
    "build_retrieve_plan",
]


def __getattr__(name: str) -> Any:
    return load_export(globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return exported_dir(globals(), _EXPORTS)
