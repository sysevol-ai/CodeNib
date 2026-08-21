# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for MCP server entry point.

Tests server initialization, tool registration, and resource endpoints.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import MagicMock, patch

import pytest
from mcp import Client
from mcp.types import LATEST_PROTOCOL_VERSION

# Import server module components
import codenib.mcp.server as server_module
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.index.embedding.vector_store import CodeVectorStore
from codenib.mcp.context import ServerContext
from codenib.mcp.tools.lsp import (
    lsp_definition_impl,
    lsp_references_impl,
    lsp_route_impl,
)
from codenib.repository_source_selection import DEFAULT_REPOSITORY_SOURCE_SELECTION

SOURCE_FINGERPRINT = "sha256-v2:" + ("a" * 64)
SOURCE_SELECTION_DIGEST = DEFAULT_REPOSITORY_SOURCE_SELECTION.digest


def test_server_import_keeps_optional_index_runtimes_lazy() -> None:
    script = """
import sys
import codenib.mcp.server

optional = ("faiss", "igraph", "litellm", "requests", "sentence_transformers")
loaded = [name for name in optional if name in sys.modules]
if loaded:
    raise SystemExit(f"optional runtimes loaded eagerly: {loaded}")
"""

    subprocess.run([sys.executable, "-c", script], check=True)


def test_server_negotiates_modern_and_legacy_protocols() -> None:
    async def negotiate(mode: Literal["auto", "legacy"]) -> tuple[str, set[str]]:
        async with Client(server_module.mcp, mode=mode, cache=None) as client:
            tools = await client.list_tools()
            return client.protocol_version, {tool.name for tool in tools.tools}

    modern_version, modern_tools = asyncio.run(negotiate("auto"))
    legacy_version, legacy_tools = asyncio.run(negotiate("legacy"))

    assert modern_version == LATEST_PROTOCOL_VERSION
    assert legacy_version != modern_version
    assert modern_tools == legacy_tools
    assert {
        "get_manifest",
        "read_source",
        "search_context",
        "search_bm25",
        "search_semantic",
    } <= modern_tools


def test_search_tool_schemas_publish_bounded_inputs() -> None:
    tools = {tool.name: tool for tool in asyncio.run(server_module.mcp.list_tools())}

    for name in (
        "search_context",
        "search_semantic",
        "search_bm25",
        "search_regex",
        "search_zoekt",
    ):
        schema = tools[name].input_schema
        text_field = "pattern" if name == "search_regex" else "query"
        assert schema["properties"][text_field]["minLength"] == 1
        expected_text_limit = 4_096 if name == "search_regex" else 16_000
        assert schema["properties"][text_field]["maxLength"] == expected_text_limit
        assert schema["properties"]["top_k"] == {
            "default": 10 if name in {"search_context", "search_semantic"} else 20,
            "maximum": 100,
            "minimum": 1,
            "title": "Top K",
            "type": "integer",
        }

    semantic = tools["search_semantic"].input_schema["properties"]
    assert semantic["level"]["enum"] == ["l0", "l2"]

    regex = tools["search_regex"].input_schema["properties"]
    assert regex["file_glob"]["maxLength"] == 4_096
    assert regex["node_type"]["maxLength"] == 4_096

    dependency = tools["dependency_subgraph"].input_schema["properties"]
    assert dependency["direction"]["enum"] == ["impact", "dependencies", "both"]
    assert dependency["depth"]["minimum"] == 1
    assert dependency["depth"]["maximum"] == 8
    assert dependency["max_nodes"]["maximum"] == 100
    assert dependency["max_edges"]["maximum"] == 2000

    route_symbols = tools["lsp_route"].input_schema["properties"]["symbols"]
    assert "minItems" not in route_symbols
    assert route_symbols["maxItems"] == 32
    assert route_symbols["items"]["maxLength"] == 1024
    assert tools["lsp_route"].input_schema["properties"]["query"]["maxLength"] == 16000
    for name in ("lsp_definition", "lsp_references"):
        properties = tools[name].input_schema["properties"]
        assert properties["file_path"]["maxLength"] == 4096
        assert properties["symbol"]["maxLength"] == 1024
    assert (
        tools["lsp_definition"].input_schema["properties"]["line"]["anyOf"][0][
            "minimum"
        ]
        == 1
    )

    source = tools["read_source"].input_schema["properties"]
    assert source["file_path"]["minLength"] == 1
    assert source["file_path"]["maxLength"] == 4096
    assert source["start_line"]["minimum"] == 1


@pytest.fixture
def mock_manifest(tmp_path: Path) -> Path:
    """Create a mock manifest file."""
    commit = "a" * 40
    manifest = RepoManifest(
        repo_path="/fake/repo",
        commit=commit,
        last_indexed_commit=commit,
        source_fingerprint=SOURCE_FINGERPRINT,
        last_indexed_source_fingerprint=SOURCE_FINGERPRINT,
        last_indexed_source_selection_digest=SOURCE_SELECTION_DIGEST,
        languages=["python"],
        file_count=100,
        capabilities={"vector_search": True},
        compiled_at="2026-04-20T12:00:00Z",
        compiled_at_epoch=1745323200.0,
        indexes={
            "vector": IndexEntry(
                index_type="vector",
                path=str(tmp_path / "vector"),
                built_at="2026-04-20T12:00:00Z",
                built_at_epoch=1745323200.0,
                status="fresh",
                config={
                    "embedding_model": "text-embedding-3-small",
                    "embedding_provider": "openai",
                    "dimension": 1536,
                    "index_metric": "ip",
                },
                commit=commit,
                source_fingerprint=SOURCE_FINGERPRINT,
                source_selection_digest=SOURCE_SELECTION_DIGEST,
            )
        },
    )

    manifest_path = tmp_path / "repo_manifest.json"
    manifest.save(manifest_path)
    return manifest_path


def test_init_server_missing_manifest():
    """Test that init_server raises FileNotFoundError for missing manifest."""
    # Mock MCPServer availability
    with patch.object(server_module, "MCPServer", MagicMock()):
        with pytest.raises(FileNotFoundError, match="Manifest not found"):
            server_module.init_server("/nonexistent/manifest.json")


def test_init_server_success(mock_manifest: Path):
    """Test successful server initialization."""
    mock_mcp = MagicMock()
    with patch.object(server_module, "MCPServer", return_value=mock_mcp):
        with patch.object(server_module, "mcp", mock_mcp):
            with patch.object(CodeVectorStore, "load"):
                with patch.object(CodeVectorStore, "__init__", return_value=None):
                    server_module.init_server(str(mock_manifest))
                    ctx = server_module.get_context()

                    assert isinstance(ctx, ServerContext)
                    # Check basic manifest loading worked
                    assert ctx.manifest is not None


def test_get_context_uninitialized():
    """Test that get_context raises RuntimeError when not initialized."""
    # Reset global context
    server_module._ctx = None

    with pytest.raises(RuntimeError, match="ServerContext not initialized"):
        server_module.get_context()


def test_semantic_search_tool_no_vector_index(mock_manifest: Path):
    """Test semantic_search tool returns error when vector index not loaded."""
    # Initialize server with mock that fails vector loading
    mock_mcp = MagicMock()
    with patch.object(server_module, "MCPServer", return_value=mock_mcp):
        with patch.object(server_module, "mcp", mock_mcp):
            with patch.object(
                CodeVectorStore, "load", side_effect=Exception("Load failed")
            ):
                server_module.init_server(str(mock_manifest))

    result = asyncio.run(server_module.semantic_search(query="test query"))

    # Should return error message instead of raising
    assert isinstance(result, dict)
    assert "error" in result or "Vector index not loaded" in str(result)


def test_semantic_search_tool_with_vector_index(mock_manifest: Path):
    """Test semantic_search tool with mocked vector store."""
    mock_vector = MagicMock(spec=CodeVectorStore)
    mock_vector.embedding_model = "text-embedding-3-small"
    mock_vector.get_stats.return_value = {"total_documents": 100}

    # Mock search results
    mock_node = MagicMock()
    mock_node.model_dump.return_value = {
        "node_id": "test_node",
        "file_path": "test.py",
        "content": "def test(): pass",
        "score": 0.95,
    }
    mock_vector.search_with_content.return_value = [mock_node]

    mock_mcp = MagicMock()
    with patch.object(server_module, "MCPServer", return_value=mock_mcp):
        with patch.object(server_module, "mcp", mock_mcp):
            with patch.object(CodeVectorStore, "load"):
                with patch.object(CodeVectorStore, "__init__", return_value=None):
                    server_module.init_server(str(mock_manifest))
                    ctx = server_module.get_context()
                    ctx.vector = mock_vector

    result = asyncio.run(server_module.semantic_search(query="test query", top_k=5))

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["node_id"] == "test_node"
    assert result[0]["score"] == 0.95


def test_lsp_definition_tool_no_symbol_graph():
    """Test lsp_definition returns an error when symbol_graph is unavailable."""
    result = lsp_definition_impl(MagicMock(symbol_graph=None), symbol="load_config")

    assert result == {"error": "symbol_graph index not available"}


def test_lsp_definition_uses_injected_provider_and_serializes_metadata():
    from codenib.agent.lsp_provider import LSPProviderMetadata, LSPProviderNodes
    from codenib.types import QueriedNode

    class Provider:
        def definition(self, **_kwargs):
            return LSPProviderNodes(
                [
                    QueriedNode(
                        node_name="demo",
                        file="demo.cpp",
                        start_line=4,
                        end_line=4,
                        content="definition of demo",
                    )
                ],
                metadata=LSPProviderMetadata(
                    provider="codenib_static_index",
                    capability="definition",
                    status="ok",
                    lsp_method="textDocument/definition",
                    backend="native-clangd-fact-query-v1",
                    index_snapshot="clangd_fact_query:sha256:test",
                ),
            )

    result = lsp_definition_impl(
        SimpleNamespace(lsp_provider=Provider(), symbol_graph=None),
        symbol="demo",
    )

    assert result[0]["start_line"] == 5
    assert result[0]["lsp_provider"]["backend"] == ("native-clangd-fact-query-v1")
    assert result[0]["lsp_provider"]["index_snapshot"] == (
        "clangd_fact_query:sha256:test"
    )


def test_lsp_definition_tool_serializes_one_based_lines():
    """Test MCP serialization converts internal graph lines to 1-based lines."""
    mock_graph = MagicMock()
    from codenib.types import QueriedNode

    mock_graph.query_range.side_effect = ValueError("should not be called")
    ctx = MagicMock(symbol_graph=mock_graph)
    with patch(
        "codenib.agent.lsp_provider.StaticLSPProvider.definition"
    ) as mock_definition:
        mock_definition.return_value = [
            QueriedNode(
                node_name="load_config",
                file="config.py",
                start_line=4,
                end_line=8,
                content="definition of load_config",
            )
        ]
        result = lsp_definition_impl(ctx, symbol="load_config")

    assert result[0]["start_line"] == 5
    assert result[0]["end_line"] == 9


def test_lsp_references_tool_delegates_to_core():
    """Test lsp_references wrapper calls the core graph helper."""
    from codenib.types import QueriedNode

    ctx = MagicMock(symbol_graph=MagicMock())
    with patch(
        "codenib.agent.lsp_provider.StaticLSPProvider.references"
    ) as mock_references:
        mock_references.return_value = [
            QueriedNode(
                node_name="caller",
                file="caller.py",
                start_line=1,
                end_line=1,
                content="reference to load_config",
            )
        ]
        result = lsp_references_impl(
            ctx, file_path="caller.py", line=2, symbol="", include_declaration=False
        )

    assert result[0]["start_line"] == 2
    kwargs = mock_references.call_args.kwargs
    assert kwargs["file_path"] == "caller.py"
    assert kwargs["line"] == 1
    assert kwargs["include_declaration"] is False


def test_lsp_tools_reject_zero_based_input_lines():
    """MCP rejects malformed 0-based lines instead of silently clamping them."""
    ctx = MagicMock(symbol_graph=MagicMock())
    with patch(
        "codenib.agent.lsp_provider.StaticLSPProvider.definition"
    ) as mock_definition:
        mock_definition.return_value = []

        with pytest.raises(ValueError, match="line must be between 1"):
            lsp_definition_impl(ctx, file_path="caller.py", line=0)

    mock_definition.assert_not_called()


def test_lsp_tools_validate_result_and_seed_bounds():
    ctx = MagicMock(symbol_graph=MagicMock())

    with pytest.raises(ValueError, match="top_k must be between"):
        lsp_references_impl(ctx, symbol="load_config", top_k=101)
    with pytest.raises(ValueError, match="at most 32 entries"):
        lsp_route_impl(ctx, symbols=[f"symbol_{index}" for index in range(33)])


def test_lsp_route_uses_query_fallback_without_symbol_seeds():
    from codenib.graph.code_graph import CodeGraph

    graph = CodeGraph()
    graph._add_vertex(
        "src/cache.py:CachePathResolver.resolve()",
        {
            "type": "method",
            "file": "src/cache.py",
            "start_line": 4,
            "end_line": 12,
            "unified_name": "CachePathResolver.resolve()",
        },
    )

    result = lsp_route_impl(
        MagicMock(symbol_graph=graph),
        symbols=[],
        query="  resolve cached path  ",
        top_k=5,
    )

    assert isinstance(result, list)
    assert [node["node_name"] for node in result] == ["CachePathResolver.resolve()"]
    assert result[0]["file"] == "src/cache.py"
    assert result[0]["start_line"] == 5


def test_lsp_route_skips_provider_without_symbols_or_query():
    ctx = MagicMock(symbol_graph=None)
    with patch("codenib.agent.lsp_provider.StaticLSPProvider") as provider:
        result = lsp_route_impl(ctx, symbols=[], query="   ")

    assert result == []
    provider.assert_not_called()


@pytest.mark.parametrize(
    "symbols",
    [
        [""] * 33,
        ",".join([""] * 33),
    ],
)
def test_lsp_route_counts_blank_entries_against_request_budget(symbols):
    ctx = MagicMock(symbol_graph=MagicMock())
    with patch("codenib.agent.lsp_provider.StaticLSPProvider") as provider:
        with pytest.raises(ValueError, match="at most 32 entries"):
            lsp_route_impl(ctx, symbols=symbols)

    provider.assert_not_called()


@pytest.mark.parametrize(
    ("impl", "kwargs", "error"),
    [
        (
            lsp_definition_impl,
            {"symbol": "s" * 1_025},
            "symbol must not exceed 1024 characters",
        ),
        (
            lsp_references_impl,
            {"file_path": "p" * 4_097, "line": 1},
            "file_path must not exceed 4096 characters",
        ),
        (
            lsp_route_impl,
            {"symbols": ["s" * 1_025]},
            "each symbol must not exceed 1024 characters",
        ),
        (
            lsp_route_impl,
            {"symbols": ["symbol"], "query": "q" * 16_001},
            "query must not exceed 16000 characters",
        ),
        (
            lsp_route_impl,
            {"symbols": ["s" * 1_000] * 17},
            "symbols must not exceed 16000 total characters",
        ),
    ],
)
def test_lsp_tools_reject_oversized_text_before_provider(impl, kwargs, error):
    ctx = MagicMock(symbol_graph=MagicMock())
    with patch("codenib.agent.lsp_provider.StaticLSPProvider") as provider:
        with pytest.raises(ValueError, match=error):
            impl(ctx, **kwargs)

    provider.assert_not_called()


def test_lsp_route_accepts_exact_text_and_entry_budgets():
    ctx = MagicMock(symbol_graph=MagicMock())
    symbols = ["s" * 500] * 32
    with patch(
        "codenib.agent.lsp_provider.StaticLSPProvider.route",
        return_value=[],
    ) as route:
        assert lsp_route_impl(ctx, symbols=symbols, query="q" * 16_000) == []

    assert route.call_args.kwargs["symbols"] == symbols
    assert route.call_args.kwargs["query"] == "q" * 16_000


def test_server_status_resource(mock_manifest: Path):
    """Test server_status resource returns correct info."""
    mock_vector = MagicMock(spec=CodeVectorStore)
    mock_vector.embedding_model = "text-embedding-3-small"
    mock_vector.get_stats.return_value = {"total_documents": 150}

    mock_mcp = MagicMock()
    with patch.object(server_module, "MCPServer", return_value=mock_mcp):
        with patch.object(server_module, "mcp", mock_mcp):
            with patch.object(CodeVectorStore, "load"):
                with patch.object(CodeVectorStore, "__init__", return_value=None):
                    server_module.init_server(str(mock_manifest))
                    ctx = server_module.get_context()
                    ctx.vector = mock_vector

    status = server_module.server_status()

    assert isinstance(status, str)
    # Just check that status is returned and contains vector info
    assert "text-embedding-3-small" in status
    assert "150 docs" in status
