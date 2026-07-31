# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Graph-related modules for CodeNib."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._lazy import exported_dir, load_export

if TYPE_CHECKING:  # pragma: no cover - imported only by static analyzers
    from codenib.graph.code_graph import CodeGraph
    from codenib.graph.incremental import GraphPatcher, LSPClient, PatcherBase
    from codenib.graph.layers import MultiGraphIndex, build_graph_layers
    from codenib.graph.roi_subgraph import ROISubgraph
    from codenib.graph.traverse_graph import traverse_tree_structure

_EXPORTS = {
    "CodeGraph": ("codenib.graph.code_graph", "CodeGraph"),
    "GraphPatcher": ("codenib.graph.incremental", "GraphPatcher"),
    "LSPClient": ("codenib.graph.incremental", "LSPClient"),
    "PatcherBase": ("codenib.graph.incremental", "PatcherBase"),
    "MultiGraphIndex": ("codenib.graph.layers", "MultiGraphIndex"),
    "build_graph_layers": ("codenib.graph.layers", "build_graph_layers"),
    "ROISubgraph": ("codenib.graph.roi_subgraph", "ROISubgraph"),
    "traverse_tree_structure": (
        "codenib.graph.traverse_graph",
        "traverse_tree_structure",
    ),
}

__all__ = [
    "CodeGraph",
    "GraphPatcher",
    "LSPClient",
    "MultiGraphIndex",
    "PatcherBase",
    "ROISubgraph",
    "build_graph_layers",
    "traverse_tree_structure",
]


def __getattr__(name: str) -> Any:
    return load_export(globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return exported_dir(globals(), _EXPORTS)
