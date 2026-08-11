# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the opt-in explore-only MCP experiment surface."""

from __future__ import annotations

import asyncio
from typing import Literal

import pytest
from mcp import Client

from codenib.compiler.manifest import RepoManifest
from codenib.mcp import server as server_mod
from codenib.mcp.context import ServerContext
from codenib.mcp.explore_bounds import (
    MAX_EXPLORE_CALL_RESULT_BYTES,
    call_tool_result_bytes,
)
from codenib.mcp.tool_surface import ToolSurfaceMCPServer, normalize_tool_surface


def test_explore_surface_lists_and_accepts_only_composed_tool() -> None:
    server = ToolSurfaceMCPServer("surface-test")

    @server.tool(name="explore_context")
    async def explore_context(query: str) -> str:
        return query

    @server.tool(name="search_context")
    async def search_context(query: str) -> str:
        return query

    server.set_tool_surface("explore")

    tools = asyncio.run(server.list_tools())
    assert [tool.name for tool in tools] == ["explore_context"]
    response = asyncio.run(server.call_tool("explore_context", {"query": "x"}))
    assert response.is_error is False
    with pytest.raises(ValueError, match="unavailable under the 'explore'"):
        asyncio.run(server.call_tool("search_context", {"query": "x"}))


def test_full_surface_preserves_all_registered_tools() -> None:
    server = ToolSurfaceMCPServer("surface-test")

    @server.tool(name="explore_context")
    async def explore_context(query: str) -> str:
        return query

    @server.tool(name="search_context")
    async def search_context(query: str) -> str:
        return query

    assert {tool.name for tool in asyncio.run(server.list_tools())} == {
        "explore_context",
        "search_context",
    }


def test_explore_surface_fails_closed_if_a_tool_bypasses_response_bound() -> None:
    server = ToolSurfaceMCPServer("surface-test", tool_surface="explore")

    @server.tool(name="explore_context")
    async def explore_context(query: str) -> dict:
        return {"query": query, "oversized": "x" * MAX_EXPLORE_CALL_RESULT_BYTES}

    response = asyncio.run(server.call_tool("explore_context", {"query": "x"}))

    assert response.is_error is True
    assert response.structured_content is None
    assert "safety limit" in response.content[0].text
    assert call_tool_result_bytes(response) < MAX_EXPLORE_CALL_RESULT_BYTES


def test_tool_surface_normalization_rejects_unknown_value() -> None:
    assert normalize_tool_surface(None) == "full"
    assert normalize_tool_surface(" EXPLORE ") == "explore"
    with pytest.raises(ValueError, match="unknown MCP tool surface"):
        normalize_tool_surface("minimal")


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        ("explore", {"explore_context"}),
        (
            "full",
            {
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
            },
        ),
    ],
)
def test_actual_server_surface_works_with_modern_and_legacy_clients(
    mode: Literal["auto", "legacy"],
    surface: str,
    expected: set[str],
) -> None:
    original_surface = server_mod.mcp.tool_surface
    original_ctx = server_mod._ctx
    server_mod._ctx = None
    server_mod.configure_tool_surface(surface)

    async def list_names() -> tuple[set[str], bool | None]:
        async with Client(server_mod.mcp, mode=mode, cache=None) as client:
            tools = await client.list_tools()
            hidden_error = None
            if surface == "explore":
                hidden = await client.call_tool("search_context", {"query": "x"})
                hidden_error = hidden.is_error
            return {tool.name for tool in tools.tools}, hidden_error

    try:
        names, hidden_error = asyncio.run(list_names())
    finally:
        server_mod._ctx = original_ctx
        server_mod.configure_tool_surface(original_surface)

    assert names == expected
    if surface == "explore":
        assert hidden_error is True


def test_sequential_clients_get_independent_explore_ledgers(monkeypatch) -> None:
    original_surface = server_mod.mcp.tool_surface
    original_ctx = server_mod._ctx
    ctx = ServerContext(manifest=RepoManifest(repo_path="/repo"))
    server_mod._ctx = ctx
    server_mod.configure_tool_surface("explore")
    monkeypatch.setattr(
        server_mod,
        "explore_context_impl",
        lambda *_args, **_kwargs: {
            "query": "where",
            "plan": {"delivery": {}},
            "summary": {},
            "source": {"verified": False},
            "files": [],
            "relationships": [],
            "diagnostics": [],
        },
    )

    async def call_sequence() -> tuple[list[int], list[int]]:
        async with Client(server_mod.mcp, mode="auto", cache=None) as first:
            first_calls = [
                (
                    await first.call_tool(
                        "explore_context",
                        {"query": query},
                    )
                ).structured_content["summary"]["session_call"]
                for query in ("first", "second")
            ]
        async with Client(server_mod.mcp, mode="auto", cache=None) as second:
            second_calls = [
                (
                    await second.call_tool(
                        "explore_context",
                        {"query": "third"},
                    )
                ).structured_content["summary"]["session_call"]
            ]
        return first_calls, second_calls

    try:
        first_calls, second_calls = asyncio.run(call_sequence())
    finally:
        ctx.close()
        server_mod._ctx = original_ctx
        server_mod.configure_tool_surface(original_surface)

    assert first_calls == [1, 2]
    assert second_calls == [1]
