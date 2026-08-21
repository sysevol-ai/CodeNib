# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end integration tests for the MCP server.

Builds a tiny synthetic repo (a few Python files), constructs a real BM25
index and a real CodeGraph + RegexNodeIndex on it, writes a real manifest,
loads everything through ``ServerContext``, and exercises the search tool
implementations against the loaded indexes.

These tests do not depend on any external indexer binary (no scip-python,
no Go / clangd) and are safe to run in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codenib.compiler.artifact_fingerprints import regular_file_fingerprint
from codenib.compiler.index_builders import BM25IndexBuilder
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.graph.code_graph import CodeGraph
from codenib.mcp.context import ServerContext
from codenib.mcp.tools.search import search_bm25_impl, search_regex_impl
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import fingerprint_repository

# Synthetic repo: three small Python files with predictable symbols so that
# both keyword (BM25) and regex search have something to match.
REPO_FILES: dict[str, str] = {
    "billing/tax.py": (
        "def calculate_tax(amount):\n"
        '    """Compute sales tax for a given amount."""\n'
        "    return amount * 0.08\n"
    ),
    "billing/exceptions.py": (
        "class TaxError(Exception):\n"
        '    """Raised when tax computation fails."""\n'
        "    pass\n"
    ),
    "auth/session.py": (
        "def authenticate_user(username, password):\n"
        '    """Return True if credentials are valid."""\n'
        "    return False\n"
    ),
}


def _build_synthetic_repo(repo_dir: Path) -> CodeGraph:
    """Materialize REPO_FILES under repo_dir and build a matching CodeGraph.

    The graph is constructed by hand (no SCIP / LSP) so the test is
    self-contained.  Each function/class becomes one symbol vertex and
    each source file becomes one file vertex.
    """
    for rel_path, content in REPO_FILES.items():
        path = repo_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    graph = CodeGraph(project_root=str(repo_dir))

    # File nodes
    for rel_path in REPO_FILES:
        graph._add_vertex(rel_path, {"type": "file"})

    # Symbol nodes use CodeGraph's 0-based inclusive source ranges.
    symbols = [
        ("billing/tax.py:calculate_tax", "function", "billing/tax.py", 0, 2),
        ("billing/exceptions.py:TaxError", "class", "billing/exceptions.py", 0, 2),
        ("auth/session.py:authenticate_user", "function", "auth/session.py", 0, 2),
    ]
    for name, node_type, file, start_line, end_line in symbols:
        graph._add_vertex(
            name,
            {
                "type": node_type,
                "file": file,
                "start_line": start_line,
                "end_line": end_line,
            },
        )

    return graph


@pytest.fixture()
def manifest_path(tmp_path: Path) -> Path:
    """Build a real BM25 + symbol_graph manifest from a synthetic repo."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    graph = _build_synthetic_repo(repo_dir)

    # Persist the symbol graph
    graph_dir = tmp_path / "symbol_graph"
    graph_dir.mkdir()
    graph_pkl = graph_dir / "graph.pkl"
    graph.save_graph(str(graph_pkl))

    selection = RepositorySourceSelection()

    # Build and persist the BM25 index through its current compiler contract.
    bm25_dir = tmp_path / "bm25"
    bm25_status = BM25IndexBuilder(
        languages=["python"],
        source_selection=selection,
    ).build(
        scope="current_repo",
        repo_path=str(repo_dir),
        output_dir=str(bm25_dir),
        source_selection=selection,
    )
    source = fingerprint_repository(repo_dir, selection=selection)
    commit = "integration-test"
    selection_digest = selection.digest

    manifest = RepoManifest(
        repo_path=str(repo_dir),
        commit=commit,
        last_indexed_commit=commit,
        source_fingerprint=source.value,
        last_indexed_source_fingerprint=source.value,
        source_selection=selection,
        last_indexed_source_selection_digest=selection_digest,
        languages=["python"],
        file_count=source.file_count,
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(bm25_dir),
                built_at="2026-01-01T00:00:00",
                built_at_epoch=1735689600.0,
                status="fresh",
                config=dict(bm25_status.metadata),
                metadata=dict(bm25_status.metadata),
                commit=commit,
                source_fingerprint=source.value,
                source_selection_digest=selection_digest,
            ),
            "symbol_graph": IndexEntry(
                index_type="symbol_graph",
                path=str(graph_dir),
                built_at="2026-01-01T00:00:00",
                built_at_epoch=1735689600.0,
                status="fresh",
                config={
                    "graph_artifact": {
                        "relative_path": "graph.pkl",
                        **regular_file_fingerprint(graph_pkl),
                    }
                },
                commit=commit,
                source_fingerprint=source.value,
                source_selection_digest=selection_digest,
            ),
        },
    )
    manifest.derive_capabilities()
    out_path = tmp_path / "repo_manifest.json"
    manifest.save(out_path)
    return out_path


def test_server_context_loads_real_indexes(manifest_path: Path) -> None:
    """ServerContext.load hydrates BM25, symbol_graph, and the regex index."""
    ctx = ServerContext.load(manifest_path)

    assert ctx.bm25 is not None, f"BM25 failed to load: {ctx.errors}"
    assert ctx.symbol_graph is not None, f"symbol_graph failed: {ctx.errors}"
    assert ctx.regex_index is not None, f"regex index failed: {ctx.errors}"
    assert ctx.errors == {}


def test_search_bm25_finds_known_symbol(manifest_path: Path) -> None:
    """A keyword query for a known symbol returns it in the top results."""
    ctx = ServerContext.load(manifest_path)
    results = search_bm25_impl(ctx, query="calculate tax", top_k=10)

    assert results, "BM25 should return at least one hit"
    names = [r["node_name"] for r in results]
    assert any(
        "calculate_tax" in n for n in names
    ), f"calculate_tax not found in BM25 results: {names}"


def test_search_regex_finds_function_definitions(manifest_path: Path) -> None:
    """Regex search returns function-typed nodes only when filtered."""
    ctx = ServerContext.load(manifest_path)
    results = search_regex_impl(
        ctx,
        pattern=r"def\s+\w+",
        top_k=10,
        node_type="function",
    )

    assert results, "Regex should match at least one function"
    names = [r["node_name"] for r in results]
    assert any("calculate_tax" in n for n in names)
    assert any("authenticate_user" in n for n in names)
    # No class nodes should leak through the node_type filter
    types = {r["type"] for r in results}
    assert types == {"function"}, f"Unexpected types: {types}"


def test_search_regex_file_glob_filter(manifest_path: Path) -> None:
    """file_glob restricts results to a subdirectory."""
    ctx = ServerContext.load(manifest_path)
    results = search_regex_impl(
        ctx,
        pattern=r".",
        top_k=20,
        file_glob="billing/*",
    )

    assert results
    files = {r["file"] for r in results}
    assert all(f.startswith("billing/") for f in files)


def test_search_regex_returns_file_nodes_with_glob_filter(
    manifest_path: Path,
) -> None:
    ctx = ServerContext.load(manifest_path)
    results = search_regex_impl(
        ctx,
        pattern="calculate_tax",
        top_k=20,
        file_glob="billing/*.py",
        node_type="file",
    )

    assert [(result["type"], result["file"]) for result in results] == [
        ("file", "billing/tax.py")
    ]


def test_search_regex_invalid_pattern_raises_friendly_error(
    manifest_path: Path,
) -> None:
    """Invalid regex patterns surface as a descriptive RuntimeError."""
    ctx = ServerContext.load(manifest_path)
    with pytest.raises(RuntimeError, match="Invalid regex pattern"):
        search_regex_impl(ctx, pattern="[unclosed")
