# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for search_semantic MCP tool.

Tests the tool wrapper logic with mocked CodeVectorStore.
"""

import asyncio
from unittest.mock import Mock

import pytest

from codenib.compiler.manifest import RepoManifest
from codenib.mcp.context import ServerContext
from codenib.mcp.tools.search import search_semantic
from codenib.types import NodeInfo


@pytest.fixture
def mock_context():
    """Create a ServerContext with mocked vector store."""
    manifest = RepoManifest(
        repo_path="/fake/repo",
        commit="abc123",
    )
    ctx = ServerContext(manifest=manifest)
    ctx.vector = Mock()
    return ctx


def test_search_semantic_normal_path(mock_context):
    """Test search_semantic with normal successful search."""
    # Setup mock to return NodeInfo objects
    mock_results = [
        NodeInfo(
            node_name="test.py::test_function",
            type="function",
            file="test.py",
            start_line=10,
            end_line=20,
            score=0.95,
            content="def test_function():\n    pass",
        ),
        NodeInfo(
            node_name="main.py::main",
            type="function",
            file="main.py",
            start_line=1,
            end_line=5,
            score=0.82,
            content="def main():\n    print('hello')",
        ),
    ]
    mock_context.vector.search_with_content = Mock(return_value=mock_results)

    # Call search_semantic
    results = asyncio.run(
        search_semantic(mock_context, query="test function", top_k=2, level="l2")
    )

    # Verify
    assert len(results) == 2
    assert all(isinstance(r, dict) for r in results)
    assert results[0]["node_name"] == "test.py::test_function"
    assert results[0]["score"] == 0.95
    assert results[0]["content"] == "def test_function():\n    pass"
    assert results[0]["start_line"] == 11
    assert results[0]["end_line"] == 21
    assert results[1]["start_line"] == 2
    assert results[1]["end_line"] == 6
    assert "type" in results[0]
    assert "file" in results[0]

    # Verify parameters were forwarded correctly
    mock_context.vector.search_with_content.assert_called_once_with(
        query="test function", top_k=2, level="l2", score_threshold=None
    )


def test_search_semantic_missing_index(mock_context):
    """Test search_semantic when vector index is not loaded."""
    mock_context.vector = None

    results = asyncio.run(search_semantic(mock_context, query="test"))

    # Should return error dict, not raise exception
    assert isinstance(results, dict)
    assert "error" in results
    assert "not loaded" in results["error"].lower()


def test_search_semantic_score_threshold_forwarding(mock_context):
    """Test that score_threshold is correctly forwarded."""
    mock_context.vector.search_with_content = Mock(return_value=[])

    asyncio.run(
        search_semantic(mock_context, query="test", top_k=5, score_threshold=0.7)
    )

    # Verify score_threshold was passed
    call_args = mock_context.vector.search_with_content.call_args
    assert call_args.kwargs["score_threshold"] == 0.7


def test_search_semantic_default_parameters(mock_context):
    """Test search_semantic with default parameters."""
    mock_context.vector.search_with_content = Mock(return_value=[])

    asyncio.run(search_semantic(mock_context, query="test"))

    # Verify defaults
    call_args = mock_context.vector.search_with_content.call_args
    assert call_args.kwargs["top_k"] == 10
    assert call_args.kwargs["level"] == "l2"  # Default to "l2" if not specified
    assert call_args.kwargs["score_threshold"] is None


def test_search_semantic_empty_results(mock_context):
    """Test search_semantic with no matching results."""
    mock_context.vector.search_with_content = Mock(return_value=[])

    results = asyncio.run(search_semantic(mock_context, query="nonexistent"))

    assert results == []
    assert isinstance(results, list)
