"""
Unit tests for MCP server entry point.

Tests server initialization, tool registration, and resource endpoints.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codeminer.index.embedding.vector_store import CodeVectorStore
from codeminer.mcp.context import ServerContext

# Import server module components
import codeminer.mcp.server as server_module


@pytest.fixture
def mock_manifest(tmp_path: Path) -> Path:
    """Create a mock manifest file."""
    manifest_data = {
        "repo_path": "/fake/repo",
        "commit": "abc123",
        "languages": ["python"],
        "file_count": 100,
        "capabilities": ["vector_search"],
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
    # Mock FastMCP availability
    with patch.object(server_module, "FastMCP", MagicMock()):
        with pytest.raises(FileNotFoundError, match="Manifest not found"):
            server_module.init_server("/nonexistent/manifest.json")


def test_init_server_success(mock_manifest: Path):
    """Test successful server initialization."""
    mock_mcp = MagicMock()
    with patch.object(server_module, "FastMCP", return_value=mock_mcp):
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


@pytest.mark.asyncio
async def test_semantic_search_tool_no_vector_index(mock_manifest: Path):
    """Test semantic_search tool returns error when vector index not loaded."""
    # Initialize server with mock that fails vector loading
    mock_mcp = MagicMock()
    with patch.object(server_module, "FastMCP", return_value=mock_mcp):
        with patch.object(server_module, "mcp", mock_mcp):
            with patch.object(CodeVectorStore, "load", side_effect=Exception("Load failed")):
                server_module.init_server(str(mock_manifest))

    result = await server_module.semantic_search(query="test query")

    # Should return error message instead of raising
    assert isinstance(result, dict)
    assert "error" in result or "Vector index not loaded" in str(result)


@pytest.mark.asyncio
async def test_semantic_search_tool_with_vector_index(mock_manifest: Path):
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
    with patch.object(server_module, "FastMCP", return_value=mock_mcp):
        with patch.object(server_module, "mcp", mock_mcp):
            with patch.object(CodeVectorStore, "load"):
                with patch.object(CodeVectorStore, "__init__", return_value=None):
                    server_module.init_server(str(mock_manifest))
                    ctx = get_context()
                    ctx.vector = mock_vector

    result = await server_module.semantic_search(query="test query", top_k=5)

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["node_id"] == "test_node"
    assert result[0]["score"] == 0.95


def test_server_status_resource(mock_manifest: Path):
    """Test server_status resource returns correct info."""
    mock_vector = MagicMock(spec=CodeVectorStore)
    mock_vector.embedding_model = "text-embedding-3-small"
    mock_vector.get_stats.return_value = {"total_documents": 150}

    mock_mcp = MagicMock()
    with patch.object(server_module, "FastMCP", return_value=mock_mcp):
        with patch.object(server_module, "mcp", mock_mcp):
            with patch.object(CodeVectorStore, "load"):
                with patch.object(CodeVectorStore, "__init__", return_value=None):
                    server_module.init_server(str(mock_manifest))
                    ctx = get_context()
                    ctx.vector = mock_vector

    status = server_module.server_status()

    assert isinstance(status, str)
    # Just check that status is returned and contains vector info
    assert "text-embedding-3-small" in status
    assert "150 docs" in status
