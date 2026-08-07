# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the MCP server tool registration and async wrappers."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from codenib.compiler.manifest import RepoManifest
from codenib.mcp import server as server_mod
from codenib.types import NodeInfo


@pytest.fixture(autouse=True)
def _reset_ctx():
    """Ensure _ctx is cleaned up after each test."""
    original = server_mod._ctx
    yield
    server_mod._ctx = original


def _make_server_ctx(*, bm25_results=None, regex_results=None, zoekt_results=None):
    ctx = MagicMock()
    ctx.manifest = RepoManifest(
        repo_path="/repo", commit="abc123", languages=["python"]
    )
    ctx.bm25 = MagicMock() if bm25_results is not None else None
    ctx.vector = None
    ctx.symbol_graph = None
    ctx.regex_index = MagicMock() if regex_results is not None else None
    ctx.zoekt = MagicMock() if zoekt_results is not None else None
    ctx.errors = {}
    ctx.artifact = None
    if bm25_results is not None:
        ctx.bm25.search.return_value = bm25_results
    if regex_results is not None:
        ctx.regex_index.search.return_value = regex_results
    if zoekt_results is not None:
        ctx.zoekt.search.return_value = zoekt_results
    return ctx


def test_search_bm25_tool() -> None:
    nodes = [
        NodeInfo(
            node_name="foo", type="function", file="a.py", content="def foo(): pass"
        )
    ]
    server_mod._ctx = _make_server_ctx(bm25_results=nodes)

    result = asyncio.run(
        server_mod.search_bm25(query="foo", top_k=5, filter_test=False)
    )

    assert len(result) == 1
    assert result[0]["node_name"] == "foo"


def test_search_context_tool() -> None:
    nodes = [
        NodeInfo(
            node_name="foo", type="function", file="a.py", content="def foo(): pass"
        )
    ]
    server_mod._ctx = _make_server_ctx(bm25_results=nodes)

    result = asyncio.run(server_mod.search_context(query="foo", top_k=5))

    assert result["plan"]["name"] == "fast_lexical"
    assert result["results"][0]["node_name"] == "foo"


def test_search_regex_tool() -> None:
    nodes = [
        NodeInfo(node_name="bar", type="class", file="b.py", content="class bar: ...")
    ]
    server_mod._ctx = _make_server_ctx(regex_results=nodes)

    result = asyncio.run(server_mod.search_regex(pattern="class", top_k=10))

    assert len(result) == 1
    assert result[0]["node_name"] == "bar"


def test_search_zoekt_tool() -> None:
    nodes = [
        NodeInfo(
            node_name="src/main.py",
            type="file",
            file="src/main.py",
            start_line=1,
            end_line=3,
            content="print('hi')",
        )
    ]
    server_mod._ctx = _make_server_ctx(zoekt_results=nodes)

    result = asyncio.run(
        server_mod.search_zoekt(query="hi", top_k=5, file_filter="*.py")
    )

    assert len(result) == 1
    assert result[0]["type"] == "file"
    assert result[0]["file"] == "src/main.py"
    _, kwargs = server_mod._ctx.zoekt.search.call_args
    assert kwargs["file_filter"] == "*.py"


def test_search_zoekt_empty_filter_normalized_to_none() -> None:
    server_mod._ctx = _make_server_ctx(zoekt_results=[])

    asyncio.run(server_mod.search_zoekt(query="x", file_filter=""))

    _, kwargs = server_mod._ctx.zoekt.search.call_args
    assert kwargs["file_filter"] is None


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


def test_main_applies_log_level_before_initialization(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(
        server_mod,
        "set_console_log_level",
        lambda level: events.append(("log_level", level)),
    )
    monkeypatch.setattr(
        server_mod,
        "init_server",
        lambda manifest_path: events.append(("init", manifest_path)),
    )
    monkeypatch.setattr(
        server_mod.mcp,
        "run",
        lambda *, transport: events.append(("run", transport)),
    )

    server_mod.main(["/tmp/m.json", "--log-level", "ERROR"])

    assert events == [
        ("log_level", "ERROR"),
        ("init", "/tmp/m.json"),
        ("run", "stdio"),
    ]


def test_main_keeps_startup_traceback_at_debug_level(monkeypatch) -> None:
    failure = ValueError("artifact identity mismatch")

    def fail_init(manifest_path) -> None:
        raise failure

    monkeypatch.setattr(server_mod, "set_console_log_level", lambda level: None)
    monkeypatch.setattr(server_mod, "init_server", fail_init)
    error = MagicMock()
    debug = MagicMock()
    monkeypatch.setattr(server_mod.logger, "error", error)
    monkeypatch.setattr(server_mod.logger, "debug", debug)

    with pytest.raises(SystemExit) as exit_info:
        server_mod.main(["/tmp/m.json"])

    assert exit_info.value.code == 1
    error.assert_called_once_with("Failed to start server: %s", failure)
    debug.assert_called_once_with("MCP server startup failure", exc_info=True)
