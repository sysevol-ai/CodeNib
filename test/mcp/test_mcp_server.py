# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for MCP server entry point.

Tests server initialization, tool registration, and resource endpoints.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Literal
from unittest.mock import MagicMock, patch

import pytest
from mcp import Client
from mcp.types import LATEST_PROTOCOL_VERSION

# Import server module components
import codenib.mcp.server as server_module
from codenib.index.embedding.vector_store import CodeVectorStore
from codenib.mcp.context import ServerContext
from codenib.mcp.tools.lsp import lsp_definition_impl, lsp_references_impl


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
        "search_context",
        "search_bm25",
        "search_semantic",
    } <= modern_tools


@pytest.fixture
def mock_manifest(tmp_path: Path) -> Path:
    """Create a mock manifest file."""
    manifest_data = {
        "repo_path": "/fake/repo",
        "commit": "abc123",
        "languages": ["python"],
        "file_count": 100,
        "capabilities": {"vector_search": True},
        "compiled_at": "2026-04-20T12:00:00Z",
        "compiled_at_epoch": 1745323200.0,
        "indexes": {
            "vector": {
                "index_type": "vector",
                "path": str(tmp_path / "vector"),
                "built_at": "2026-04-20T12:00:00Z",
                "built_at_epoch": 1745323200.0,
                "status": "fresh",
                "config": {
                    "embedding_model": "text-embedding-3-small",
                    "embedding_provider": "openai",
                    "dimension": 1536,
                    "index_metric": "ip",
                },
                "metadata": {},
            }
        },
    }

    manifest_path = tmp_path / "repo_manifest.json"
    manifest_path.write_text(json.dumps(manifest_data, indent=2))
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


def test_lsp_tools_reuse_agent_line_boundary():
    """MCP LSP tools share the agent 1-based input boundary."""
    ctx = MagicMock(symbol_graph=MagicMock())
    with patch(
        "codenib.agent.lsp_provider.StaticLSPProvider.definition"
    ) as mock_definition:
        mock_definition.return_value = []

        lsp_definition_impl(ctx, file_path="caller.py", line=0)

    assert mock_definition.call_args.kwargs["line"] == 0


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
