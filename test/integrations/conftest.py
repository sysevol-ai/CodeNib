# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.graph.code_graph import CodeGraph
from codenib.index.sparse_idx import BM25CodeIndexer
from codenib.types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_IMPORT,
    EDGE_TYPE_REFERENCE,
    EDGE_TYPE_TYPE_USE,
    NODE_TYPE_CLASS,
    NODE_TYPE_DIRECTORY,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    ROOT_NODE,
)

REPOSITORY_FILES = {
    "src/service.py": (
        "class BillingService:\n"
        "    def calculate_tax(self, amount):\n"
        '        """Compute sales tax for an invoice."""\n'
        "        return helper(amount)\n"
        "\n"
        "def helper(amount):\n"
        "    return amount * 0.08\n"
    ),
    "src/other.py": (
        "def helper(amount):\n" '    """Fallback tax helper."""\n' "    return amount\n"
    ),
    "src/model.py": ("class TaxRule:\n" "    rate = 0.08\n"),
    "README.md": "# Billing fixture\n",
}


def _add_vertex(
    graph: CodeGraph,
    name: str,
    *,
    kind: str,
    file_path: str = "",
    unified_name: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
) -> None:
    attrs: dict[str, Any] = {"type": kind}
    if file_path:
        attrs["file"] = file_path
    if unified_name:
        attrs["unified_name"] = unified_name
    if start_line is not None:
        attrs["start_line"] = start_line
    if end_line is not None:
        attrs["end_line"] = end_line
    graph._add_vertex(name, attrs)


def build_integration_graph(repo_root: Path) -> CodeGraph:
    for relative, content in REPOSITORY_FILES.items():
        path = repo_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    graph = CodeGraph(project_root=str(repo_root))
    _add_vertex(graph, ROOT_NODE, kind=NODE_TYPE_DIRECTORY)
    _add_vertex(graph, "src", kind=NODE_TYPE_DIRECTORY)
    for relative in REPOSITORY_FILES:
        _add_vertex(graph, relative, kind=NODE_TYPE_FILE)

    _add_vertex(
        graph,
        "scip:service:BillingService",
        kind=NODE_TYPE_CLASS,
        file_path="src/service.py",
        unified_name="src/service.py:BillingService",
        start_line=0,
        end_line=3,
    )
    _add_vertex(
        graph,
        "scip:service:BillingService.calculate_tax",
        kind=NODE_TYPE_METHOD,
        file_path="src/service.py",
        unified_name="src/service.py:BillingService.calculate_tax()",
        start_line=1,
        end_line=3,
    )
    _add_vertex(
        graph,
        "scip:service:helper",
        kind=NODE_TYPE_FUNCTION,
        file_path="src/service.py",
        unified_name="src/service.py:helper()",
        start_line=5,
        end_line=6,
    )
    _add_vertex(
        graph,
        "scip:other:helper",
        kind=NODE_TYPE_FUNCTION,
        file_path="src/other.py",
        unified_name="src/other.py:helper()",
        start_line=0,
        end_line=2,
    )
    _add_vertex(
        graph,
        "scip:model:TaxRule",
        kind=NODE_TYPE_CLASS,
        file_path="src/model.py",
        unified_name="src/model.py:TaxRule",
        start_line=0,
        end_line=1,
    )

    graph._add_edge(ROOT_NODE, "src", EDGE_TYPE_CONTAIN)
    graph._add_edge(ROOT_NODE, "README.md", EDGE_TYPE_CONTAIN)
    for relative in ("src/service.py", "src/other.py", "src/model.py"):
        graph._add_edge("src", relative, EDGE_TYPE_CONTAIN)
    graph._add_edge("src/service.py", "scip:service:BillingService", EDGE_TYPE_CONTAIN)
    graph._add_edge(
        "scip:service:BillingService",
        "scip:service:BillingService.calculate_tax",
        EDGE_TYPE_CONTAIN,
    )
    graph._add_edge("src/service.py", "scip:service:helper", EDGE_TYPE_CONTAIN)
    graph._add_edge("src/other.py", "scip:other:helper", EDGE_TYPE_CONTAIN)
    graph._add_edge("src/model.py", "scip:model:TaxRule", EDGE_TYPE_CONTAIN)
    graph._add_edge(
        "scip:service:BillingService.calculate_tax",
        "scip:service:helper",
        EDGE_TYPE_REFERENCE,
        anchor_file="src/service.py",
        anchor_line=3,
    )
    graph._add_edge("src/service.py", "src/model.py", EDGE_TYPE_IMPORT)
    graph._add_edge(
        "scip:service:BillingService",
        "scip:model:TaxRule",
        EDGE_TYPE_TYPE_USE,
    )
    graph.build_range_indexes()
    return graph


@pytest.fixture()
def integration_manifest(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    graph = build_integration_graph(repo_root)

    graph_dir = tmp_path / "symbol_graph"
    graph_dir.mkdir()
    graph_path = graph_dir / "graph.pkl"
    graph.save_graph(str(graph_path))

    bm25_dir = tmp_path / "bm25"
    bm25_dir.mkdir()
    bm25 = BM25CodeIndexer()
    bm25.build_index_from_graph(graph)
    bm25.save_index(str(bm25_dir))

    manifest = RepoManifest(
        repo_path=str(repo_root),
        commit="integration-fixture",
        languages=["python"],
        indexes={
            "symbol_graph": IndexEntry(
                index_type="symbol_graph",
                path=str(graph_path),
                built_at="2026-07-30T00:00:00Z",
                built_at_epoch=1_785_369_600.0,
                status="fresh",
            ),
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(bm25_dir),
                built_at="2026-07-30T00:00:00Z",
                built_at_epoch=1_785_369_600.0,
                status="fresh",
            ),
        },
    )
    manifest.derive_capabilities()
    manifest_path = tmp_path / "repo_manifest.json"
    manifest.save(manifest_path)
    return manifest_path
