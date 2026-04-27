"""Unit tests for the MCP server tool registration and async wrappers."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from codeminer.compiler.manifest import RepoManifest
from codeminer.mcp import server as server_mod
from codeminer.types import NodeInfo


@pytest.fixture(autouse=True)
def _reset_ctx():
    """Ensure _ctx is cleaned up after each test."""
    original = server_mod._ctx
    yield
    server_mod._ctx = original


def _make_server_ctx(*, bm25_results=None, regex_results=None):
    ctx = MagicMock()
    ctx.manifest = RepoManifest(repo_path="/repo", commit="abc123", languages=["python"])
    ctx.bm25 = MagicMock() if bm25_results is not None else None
    ctx.regex_index = MagicMock() if regex_results is not None else None
    ctx.errors = {}
    if bm25_results is not None:
        ctx.bm25.search.return_value = bm25_results
    if regex_results is not None:
        ctx.regex_index.search.return_value = regex_results
    return ctx


def test_search_bm25_tool() -> None:
    nodes = [NodeInfo(node_name="foo", type="function", file="a.py", content="def foo(): pass")]
    server_mod._ctx = _make_server_ctx(bm25_results=nodes)

    result = asyncio.run(server_mod.search_bm25(query="foo", top_k=5, filter_test=False))

    assert len(result) == 1
    assert result[0]["node_name"] == "foo"


def test_search_regex_tool() -> None:
    nodes = [NodeInfo(node_name="bar", type="class", file="b.py", content="class bar: ...")]
    server_mod._ctx = _make_server_ctx(regex_results=nodes)

    result = asyncio.run(server_mod.search_regex(pattern="class", top_k=10))

    assert len(result) == 1
    assert result[0]["node_name"] == "bar"


def test_get_manifest_tool() -> None:
    server_mod._ctx = _make_server_ctx(bm25_results=[])

    result = asyncio.run(server_mod.get_manifest())

    assert result["repo"]["path"] == "/repo"
    assert result["repo"]["commit"] == "abc123"


def test_search_bm25_raises_without_ctx() -> None:
    server_mod._ctx = None
    with pytest.raises(RuntimeError, match="Server not initialized"):
        asyncio.run(server_mod.search_bm25(query="x"))


def test_parse_args_basic() -> None:
    args = server_mod._parse_args(["/tmp/manifest.json"])
    assert args.manifest == "/tmp/manifest.json"
    assert args.manifest_flag is None
    assert args.log_level == "INFO"


def test_parse_args_with_manifest_flag() -> None:
    args = server_mod._parse_args(["--manifest", "/tmp/manifest.json"])
    assert args.manifest is None
    assert args.manifest_flag == "/tmp/manifest.json"


def test_parse_args_with_log_level() -> None:
    args = server_mod._parse_args(["/tmp/m.json", "--log-level", "DEBUG"])
    assert args.log_level == "DEBUG"
