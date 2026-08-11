# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the MCP server tool registration and async wrappers."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest

from codenib.compiler.manifest import RepoManifest
from codenib.mcp import server as server_mod
from codenib.mcp.context import ServerContext
from codenib.types import NodeInfo


@pytest.fixture(autouse=True)
def _reset_ctx():
    """Ensure _ctx is cleaned up after each test."""
    original = server_mod._ctx
    original_surface = server_mod.mcp.tool_surface
    yield
    server_mod._ctx = original
    server_mod.configure_tool_surface(original_surface)


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
    ctx.loaded_views = frozenset()
    ctx.source_verified = False
    ctx.source_error = "unverified"
    ctx.explore_runtime = None
    ctx.lsp_provider_selection = {
        "provider": "codenib_static_index",
        "backend": "persisted-symbol-graph-v1",
        "status": "ok",
    }
    if bm25_results is not None:
        ctx.bm25.search.return_value = bm25_results
    if regex_results is not None:
        ctx.regex_index.search.return_value = regex_results
    if zoekt_results is not None:
        ctx.zoekt.search.return_value = zoekt_results
    return ctx


def _empty_explore_response() -> dict:
    return {
        "query": "where",
        "plan": {"delivery": {}},
        "summary": {},
        "source": {"verified": False},
        "files": [],
        "relationships": [],
        "diagnostics": [],
    }


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
    assert result["runtime"]["lsp_provider"]["backend"] == ("persisted-symbol-graph-v1")
    assert result["runtime"]["tool_surface"] == "full"
    assert result["runtime"]["explore_session"] == {
        "calls": 0,
        "ranges": 0,
        "max_ranges": 160,
        "evictions": 0,
    }


def test_default_server_registers_complete_compatible_tool_set() -> None:
    server_mod.configure_tool_surface("full")

    tools = asyncio.run(server_mod.mcp.list_tools())

    assert {tool.name for tool in tools} == {
        "explore_context",
        "search_context",
        "search_semantic",
        "search_bm25",
        "search_regex",
        "search_zoekt",
        "dependency_subgraph",
        "lsp_definition",
        "lsp_references",
        "lsp_route",
        "read_source",
        "get_manifest",
    }
    assert len(tools) == 12


def test_explore_tool_schema_is_bounded() -> None:
    tools = {tool.name: tool for tool in asyncio.run(server_mod.mcp.list_tools())}
    properties = tools["explore_context"].input_schema["properties"]

    assert properties["query"]["maxLength"] == 16_000
    assert properties["symbols"]["anyOf"][0]["maxItems"] == 32
    assert properties["symbols"]["anyOf"][0]["items"]["maxLength"] == 1_024
    assert properties["top_k"]["minimum"] == 1
    assert properties["top_k"]["maximum"] == 20
    assert properties["budget"]["enum"] == ["fast", "balanced", "thorough"]


def test_explore_surface_instructions_and_prompt_name_only_visible_tool() -> None:
    server_mod.configure_tool_surface("explore")

    guide = asyncio.run(server_mod.codenib_guide())
    instructions = server_mod.mcp._lowlevel_server.instructions
    hidden_tools = {
        "search_context",
        "search_semantic",
        "search_bm25",
        "search_regex",
        "search_zoekt",
        "dependency_subgraph",
        "lsp_definition",
        "lsp_references",
        "lsp_route",
        "read_source",
        "get_manifest",
    }

    assert "explore_context" in guide
    assert "explore_context" in instructions
    assert all(name not in guide for name in hidden_tools)
    assert all(name not in instructions for name in hidden_tools)


def test_mcp_lifespan_creates_and_discards_connection_runtime() -> None:
    ctx = ServerContext(manifest=RepoManifest(repo_path="/repo"))
    server_mod._ctx = ctx

    async def run_lifespan() -> None:
        assert ctx.explore_runtime is None
        async with server_mod._mcp_lifespan(server_mod.mcp) as runtime:
            assert runtime is not None
            assert ctx.explore_runtime is runtime
            runtime.ledger.project(_empty_explore_response())
            assert runtime.ledger.stats()["calls"] == 1
        assert ctx.explore_runtime is None
        assert runtime.ledger.stats()["calls"] == 0

    asyncio.run(run_lifespan())


def test_explore_context_serializes_calls_and_commits_in_order(monkeypatch) -> None:
    ctx = ServerContext(manifest=RepoManifest(repo_path="/repo"))
    server_mod._ctx = ctx
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def implementation(*_args, **_kwargs):
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.03)
        with state_lock:
            active -= 1
        return _empty_explore_response()

    monkeypatch.setattr(server_mod, "explore_context_impl", implementation)

    async def run_calls():
        return await asyncio.gather(
            server_mod.explore_context("first"),
            server_mod.explore_context("second"),
        )

    results = asyncio.run(run_calls())

    assert maximum_active == 1
    assert [result["summary"]["session_call"] for result in results] == [1, 2]
    assert ctx.explore_runtime is not None
    assert ctx.explore_runtime.ledger.stats()["calls"] == 2


def test_explore_context_failure_does_not_commit_ledger(monkeypatch) -> None:
    ctx = ServerContext(manifest=RepoManifest(repo_path="/repo"))
    server_mod._ctx = ctx

    def fail(*_args, **_kwargs):
        raise RuntimeError("backend failed")

    monkeypatch.setattr(server_mod, "explore_context_impl", fail)

    with pytest.raises(RuntimeError, match="backend failed"):
        asyncio.run(server_mod.explore_context("where"))

    assert ctx.explore_runtime is not None
    assert ctx.explore_runtime.ledger.stats()["calls"] == 0


def test_explore_context_cancellation_does_not_commit_ledger(monkeypatch) -> None:
    ctx = ServerContext(manifest=RepoManifest(repo_path="/repo"))
    server_mod._ctx = ctx
    started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    call_count = 0

    def implementation(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            started.set()
            release.wait(timeout=2)
        else:
            second_started.set()
        return _empty_explore_response()

    monkeypatch.setattr(server_mod, "explore_context_impl", implementation)

    async def cancel_call() -> None:
        task = asyncio.create_task(server_mod.explore_context("where"))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        next_task = asyncio.create_task(server_mod.explore_context("next"))
        await asyncio.sleep(0.02)
        assert not second_started.is_set()
        release.set()
        assert await asyncio.to_thread(second_started.wait, 0.5)
        result = await next_task
        assert result["summary"]["session_call"] == 1

    try:
        asyncio.run(cancel_call())
    finally:
        release.set()

    assert ctx.explore_runtime is not None
    assert ctx.explore_runtime.ledger.stats()["calls"] == 1


def test_search_bm25_raises_without_ctx() -> None:
    server_mod._ctx = None
    with pytest.raises(RuntimeError, match="Server not initialized"):
        asyncio.run(server_mod.search_bm25(query="x"))


def test_parse_args_basic() -> None:
    args = server_mod._parse_args(["/tmp/manifest.json"])
    assert args.manifest == "/tmp/manifest.json"
    assert args.manifest_flag is None
    assert args.log_level == "INFO"
    assert args.tool_surface == "full"


def test_parse_args_with_explore_surface() -> None:
    args = server_mod._parse_args(["/tmp/manifest.json", "--tool-surface", "explore"])

    assert args.tool_surface == "explore"


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


def test_main_applies_requested_tool_surface(monkeypatch) -> None:
    monkeypatch.setattr(server_mod, "set_console_log_level", lambda _level: None)
    monkeypatch.setattr(server_mod, "init_server", lambda _path: None)
    monkeypatch.setattr(server_mod.mcp, "run", lambda *, transport: None)

    server_mod.main(["/tmp/m.json", "--tool-surface", "explore"])

    assert server_mod.mcp.tool_surface == "explore"
    assert server_mod.mcp._lowlevel_server.instructions == (
        server_mod.CODENIB_EXPLORE_INSTRUCTIONS
    )


def test_import_does_not_configure_root_logging() -> None:
    script = """\
import logging

root_logger = logging.getLogger()
root_logger.handlers.clear()
import codenib.mcp.server  # noqa: F401,E402
assert not root_logger.handlers
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


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
