"""Unit tests for codeminer.mcp.context.ServerContext."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codeminer.compiler.manifest import IndexEntry, RepoManifest
from codeminer.mcp.context import ServerContext


@pytest.fixture()
def manifest_dir(tmp_path: Path) -> Path:
    """Create a minimal manifest on disk with a BM25 index."""
    bm25_dir = tmp_path / "bm25"
    bm25_dir.mkdir()
    (bm25_dir / "documents.json").write_text(
        json.dumps([{"page_content": "def foo(): pass", "metadata": {"node_name": "foo", "type": "function"}}])
    )
    (bm25_dir / "bm25_metadata.json").write_text(
        json.dumps({"project_root": str(tmp_path), "max_k": 10, "language": "english"})
    )

    manifest = RepoManifest(
        repo_path=str(tmp_path),
        commit="abc123",
        languages=["python"],
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(bm25_dir),
                built_at="2026-01-01T00:00:00",
                built_at_epoch=1735689600.0,
                status="fresh",
            ),
        },
    )
    manifest.derive_capabilities()
    manifest_path = tmp_path / "repo_manifest.json"
    manifest.save(manifest_path)
    return tmp_path


def test_load_with_bm25_only(manifest_dir: Path) -> None:
    """ServerContext loads BM25 when symbol_graph is absent."""
    ctx = ServerContext.load(manifest_dir / "repo_manifest.json")

    assert ctx.bm25 is not None
    assert ctx.symbol_graph is None
    assert ctx.regex_index is None
    assert "symbol_graph" not in ctx.errors


def test_load_missing_bm25_path(tmp_path: Path) -> None:
    """BM25 load failure is recorded in errors, not raised."""
    manifest = RepoManifest(
        repo_path=str(tmp_path),
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(tmp_path / "nonexistent"),
                built_at="2026-01-01T00:00:00",
                built_at_epoch=0.0,
                status="fresh",
            ),
        },
    )
    manifest.save(tmp_path / "repo_manifest.json")

    ctx = ServerContext.load(tmp_path / "repo_manifest.json")
    assert ctx.bm25 is None
    assert "bm25" in ctx.errors


def test_skip_non_fresh_index(tmp_path: Path) -> None:
    """Indexes with status != 'fresh' are skipped."""
    manifest = RepoManifest(
        repo_path=str(tmp_path),
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(tmp_path / "bm25"),
                built_at="2026-01-01T00:00:00",
                built_at_epoch=0.0,
                status="failed",
            ),
        },
    )
    manifest.save(tmp_path / "repo_manifest.json")

    ctx = ServerContext.load(tmp_path / "repo_manifest.json")
    assert ctx.bm25 is None
    assert "bm25" not in ctx.errors


def test_regex_index_built_when_graph_available(manifest_dir: Path) -> None:
    """RegexNodeIndex is built when symbol_graph loads successfully."""
    graph_dir = manifest_dir / "symbol_graph"
    graph_dir.mkdir()

    mock_graph = MagicMock()
    mock_vs = [MagicMock()]
    mock_vs[0].index = 0
    mock_vs[0].__getitem__ = lambda self, key: "my_func" if key == "name" else None
    mock_vs[0].attributes.return_value = {
        "type": "function",
        "file": "main.py",
        "start_line": 1,
        "end_line": 5,
    }
    mock_graph.get_graph.return_value.vs = mock_vs
    mock_graph.get_node_content.return_value = "def my_func(): pass"

    manifest = RepoManifest.load(manifest_dir / "repo_manifest.json")
    manifest.indexes["symbol_graph"] = IndexEntry(
        index_type="symbol_graph",
        path=str(graph_dir / "graph.pkl"),
        built_at="2026-01-01T00:00:00",
        built_at_epoch=0.0,
        status="fresh",
    )
    manifest.save(manifest_dir / "repo_manifest.json")

    with patch("codeminer.mcp.context.CodeGraph.load_graph", return_value=mock_graph):
        ctx = ServerContext.load(manifest_dir / "repo_manifest.json")

    assert ctx.symbol_graph is mock_graph
    assert ctx.regex_index is not None
    assert len(ctx.regex_index.nodes) == 1
