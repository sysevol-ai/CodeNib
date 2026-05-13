# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""GraphPatcher: thin router for incremental CodeGraph updates.

Dispatches to language-specific patchers (patcher_*.py).
Maintains backward-compatible API.
"""

from __future__ import annotations

from typing import Optional

from ...log_utils import get_logger
from ..code_graph import CodeGraph
from . import change_mgr
from .patcher_base import PatcherBase

logger = get_logger(__name__)

# Language → file extensions
LANGUAGE_EXTENSIONS = {
    "rust": {".rs"},
    "python": {".py"},
    "typescript": {".ts", ".tsx", ".js", ".jsx"},
    "ts": {".ts", ".tsx", ".js", ".jsx"},
    "go": {".go"},
    "cpp": {".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx"},
}


def _create_patcher(
    language: str,
    project_root: str,
    code_graph: Optional[CodeGraph],
    mode: str = "symbol",
) -> PatcherBase:
    """Factory: create language-specific patcher."""
    from .patcher_cpp import PatcherCpp
    from .patcher_go import PatcherGo
    from .patcher_python import PatcherPython
    from .patcher_rust import PatcherRust
    from .patcher_ts import PatcherTS

    PATCHER_MAP = {
        "rust": PatcherRust,
        "python": PatcherPython,
        "typescript": PatcherTS,
        "ts": PatcherTS,
        "go": PatcherGo,
        "cpp": PatcherCpp,
        "c": PatcherCpp,
    }

    cls = PATCHER_MAP.get(language)
    if cls is None:
        raise ValueError(f"Unsupported language: {language}")
    return cls(project_root, code_graph or CodeGraph(project_root), mode=mode)


class GraphPatcher:
    """Orchestrates incremental graph updates using LSP.

    Thin router that delegates to language-specific PatcherBase subclasses.

    Usage::

        graph = CodeGraph.load_graph("graph.pkl")
        patcher = GraphPatcher("/path/to/project", graph, "rust")
        changed = GraphPatcher.detect_changed_files(
            "/path/to/project", "abc123", "HEAD"
        )
        stats = patcher.patch_files(changed)
        graph.save_graph("graph.pkl")
    """

    def __init__(
        self,
        project_root: str,
        code_graph: Optional[CodeGraph],
        language: str,
        mode: str = "symbol",
    ):
        self.project_root = project_root
        self.language = language
        self.mode = mode
        self._impl: PatcherBase = _create_patcher(
            language, project_root, code_graph, mode=mode
        )

    @property
    def code_graph(self) -> CodeGraph:
        return self._impl.code_graph

    @code_graph.setter
    def code_graph(self, value: CodeGraph):
        self._impl.code_graph = value

    @property
    def profiler(self):
        return self._impl.profiler

    # ── Delegated methods ─────────────────────────────────

    def start_lsp(self, skip_probe: bool = False):
        """Start the LSP server for warm-up."""
        self._impl.start_lsp(skip_probe=skip_probe)

    def stop_lsp(self):
        """Stop the LSP server."""
        self._impl.stop_lsp()

    def patch_files(self, changed_files: dict, **kwargs) -> dict:
        """Apply incremental patch for all changed files."""
        return self._impl.patch_files(changed_files, **kwargs)

    # ── Static utilities (backward-compatible) ────────────

    @staticmethod
    def detect_changed_files(
        project_root: str,
        base_commit: str,
        target_commit: str = "HEAD",
        extensions: Optional[set[str]] = None,
    ) -> dict:
        """Detect changed files between two commits."""
        return change_mgr.detect_changed_files(
            project_root, base_commit, target_commit, extensions
        )

    @staticmethod
    def get_changed_line_ranges(
        project_root: str,
        file_path: str,
        base_commit: str,
        target_commit: str = "HEAD",
    ) -> list[tuple[int, int, int, int]]:
        """Get line ranges changed between two commits.

        Each hunk is reported as (old_start, old_end, new_start, new_end)
        in 0-indexed inclusive form so callers can compare line counts.
        """
        return change_mgr.get_changed_line_ranges(
            project_root, file_path, base_commit, target_commit
        )
