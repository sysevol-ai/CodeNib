# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""LSP-based incremental patching without eager graph-runtime imports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..._lazy import exported_dir, load_export

if TYPE_CHECKING:  # pragma: no cover - imported only by static analyzers
    from .graph_patcher import GraphPatcher
    from .lsp_client import LSPClient
    from .patcher_base import IncrementalPatchRebuildRequired, PatcherBase

_EXPORTS = {
    "GraphPatcher": ("codenib.graph.incremental.graph_patcher", "GraphPatcher"),
    "LSPClient": ("codenib.graph.incremental.lsp_client", "LSPClient"),
    "IncrementalPatchRebuildRequired": (
        "codenib.graph.incremental.patcher_base",
        "IncrementalPatchRebuildRequired",
    ),
    "PatcherBase": ("codenib.graph.incremental.patcher_base", "PatcherBase"),
}

__all__ = [
    "GraphPatcher",
    "LSPClient",
    "IncrementalPatchRebuildRequired",
    "PatcherBase",
]


def __getattr__(name: str) -> Any:
    return load_export(globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return exported_dir(globals(), _EXPORTS)
