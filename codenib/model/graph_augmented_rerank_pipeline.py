# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from .dense_graph_expand_rerank_pipeline import DenseGraphExpandRerankPipeline


class GraphAugmentedRerankPipeline(DenseGraphExpandRerankPipeline):
    """Compatibility alias for :class:`DenseGraphExpandRerankPipeline`.

    The old name was ambiguous: the pipeline is specifically dense-first,
    graph-expansion-augmented, then reranked. New code should import
    ``DenseGraphExpandRerankPipeline``.
    """


__all__ = ["GraphAugmentedRerankPipeline"]
