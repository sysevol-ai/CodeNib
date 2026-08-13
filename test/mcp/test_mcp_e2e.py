# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end MCP server tests with real embedding indexes.

Tests MCP server layer (init_server, semantic_search) using pre-built indexes
from the configured CodeNib prebuilt root.
"""

import asyncio
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import codenib.mcp.server as server_module
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.mcp.context import ServerContext
from codenib.paths import prebuilt_data_dir

# Test data from codenib-base-dataset
CODENIB_DATA = prebuilt_data_dir()
TEST_REPO = "astropy__astropy-12907"
TEST_REPO_PATH = CODENIB_DATA / TEST_REPO


@pytest.mark.slow
@pytest.mark.skipif(
    not TEST_REPO_PATH.exists(),
    reason=f"codenib-base-dataset not available at {CODENIB_DATA}",
)
class TestMCPServerE2E:
    """End-to-end tests for MCP server with real indexes."""

    @pytest.fixture(autouse=True)
    def reset_server_context(self):
        """Reset global server context before each test."""
        previous_context = server_module._ctx
        server_module._ctx = None
        if previous_context is not None:
            previous_context.close()
        yield
        current_context = server_module._ctx
        server_module._ctx = None
        if current_context is not None:
            current_context.close()

    @pytest.fixture
    def test_manifest_path(self, tmp_path):
        """Create a temporary manifest for testing."""
        manifest = RepoManifest(
            repo_path=str(TEST_REPO_PATH),
            commit="test_commit",
            languages=["python"],
            file_count=1000,
            indexes={
                "vector": IndexEntry(
                    index_type="vector",
                    path=str(TEST_REPO_PATH),
                    built_at="2024-04-12T20:57:00Z",
                    built_at_epoch=1712957820.0,
                    status="fresh",
                    config={
                        "embedding_model": "jinaai/jina-code-embeddings-1.5b",
                        "embedding_provider": "huggingface",
                        "dimension": 1536,
                        "index_metric": "ip",
                    },
                )
            },
        )

        manifest_path = tmp_path / "repo_manifest.json"
        manifest.save(str(manifest_path))
        return str(manifest_path)

    def test_mcp_server_initialization(self, test_manifest_path):
        """Test MCP server can initialize with real index."""
        # Mock MCPServer to avoid dependency
        mock_mcp = MagicMock()
        with patch.object(server_module, "MCPServer", return_value=mock_mcp):
            with patch.object(server_module, "mcp", mock_mcp):
                server_module.init_server(test_manifest_path)

                ctx = server_module.get_context()
                assert isinstance(ctx, ServerContext)
                assert ctx.vector is not None
                assert ctx.manifest.repo_path == str(TEST_REPO_PATH)

                # Check vector store stats
                stats = ctx.vector.get_stats()
                assert stats["total_documents"] > 0
                print(f"\nLoaded {stats['total_documents']} documents")
                print(f"  L0: {stats.get('l0_documents', 0)}")
                print(f"  L2: {stats.get('l2_documents', 0)}")

    def test_mcp_semantic_search(self, test_manifest_path):
        """Test MCP semantic_search tool with real query."""
        # Initialize server
        mock_mcp = MagicMock()
        with patch.object(server_module, "MCPServer", return_value=mock_mcp):
            with patch.object(server_module, "mcp", mock_mcp):
                server_module.init_server(test_manifest_path)

        # Known query for astropy separability issue
        query = "function that calculates model separability matrix"

        results = asyncio.run(
            server_module.semantic_search(
                query=query,
                top_k=10,
                level="l2",
            )
        )

        # Validate results
        assert isinstance(results, list)
        assert len(results) > 0
        assert len(results) <= 10

        # Check structure
        first = results[0]
        required_fields = ["node_id", "file", "content", "score"]
        for field in required_fields:
            assert field in first, f"Missing field: {field}"

        # Score should be float, not tensor
        assert isinstance(first["score"], (int, float))

        # Quality check for this specific query
        assert first["score"] > 0.5, "Top result should have good similarity"

        # Verify semantic relevance
        top_3_node_ids = [r["node_id"] for r in results[:3]]
        has_separability = any("separability" in nid.lower() for nid in top_3_node_ids)
        assert has_separability, "Should find separability-related functions"

        print(f"\n=== MCP Search Results ===")
        print(f"Query: {query}")
        print(f"Found {len(results)} results\n")
        for i, r in enumerate(results[:5], 1):
            print(f"{i}. {r['node_id']}")
            print(f"   Score: {r['score']:.4f}")
            print(f"   File: {Path(r['file']).name}")
            print()

    def test_mcp_vs_direct_call_equivalence(self, test_manifest_path):
        """
        Verify MCP layer produces identical results to direct API call.

        Ensures MCP protocol layer adds no transformation overhead.
        """
        from codenib.index.embedding.artifact_integrity import (
            capture_authenticated_vector_view,
        )
        from codenib.index.embedding.vector_store import CodeVectorStore
        from codenib.native_index_authorization import (
            _mint_trusted_local_admin_authorization,
        )

        # Direct vector store call
        artifact_contract = {
            "embedding_model": "jinaai/jina-code-embeddings-1.5b",
            "embedding_provider": "huggingface",
            "dimension": 1536,
            "index_metric": "ip",
        }
        with capture_authenticated_vector_view(TEST_REPO_PATH) as vector_view:
            native_authorization = _mint_trusted_local_admin_authorization(
                vector_view.ownership,
                view_type="vector",
                semantic_contract=artifact_contract,
                evidence=(
                    "mcp-e2e-direct-vector-view",
                    "captured-vector-tree-subject",
                ),
            )

        with ExitStack() as resources:
            vector_store = CodeVectorStore(
                embedding_model="jinaai/jina-code-embeddings-1.5b",
                embedding_provider="huggingface",
                dimension=1536,
                index_metric="ip",
                store_path=str(TEST_REPO_PATH),
                artifact_metadata=artifact_contract,
            )
            resources.callback(vector_store.close)
            vector_store.load(
                native_index_authorization=native_authorization,
            )

            query = "coordinate transformation"
            top_k = 5

            direct_results = vector_store.search_with_content(
                query=query, top_k=top_k, level="l2"
            )

            # MCP server call
            mock_mcp = MagicMock()
            with patch.object(server_module, "MCPServer", return_value=mock_mcp):
                with patch.object(server_module, "mcp", mock_mcp):
                    server_module.init_server(test_manifest_path)

            mcp_results = asyncio.run(
                server_module.semantic_search(query=query, top_k=top_k, level="l2")
            )

            # Should have same number of results
            assert len(mcp_results) == len(direct_results)

            # Scores should match (within floating point tolerance)
            for i in range(len(direct_results)):
                direct_score = float(direct_results[i].score)
                mcp_score = float(mcp_results[i]["score"])

                assert abs(direct_score - mcp_score) < 1e-6, (
                    f"Score mismatch at position {i}: "
                    f"direct={direct_score:.6f}, mcp={mcp_score:.6f}"
                )

            print("\n=== MCP Layer Equivalence ===")
            print(f"Results: {len(mcp_results)}")
            print("Scores match: ✓")
            print("MCP adds no transformation overhead: ✓")

    def test_mcp_server_status(self, test_manifest_path):
        """Test server_status resource returns correct info."""
        mock_mcp = MagicMock()
        with patch.object(server_module, "MCPServer", return_value=mock_mcp):
            with patch.object(server_module, "mcp", mock_mcp):
                server_module.init_server(test_manifest_path)

        status = server_module.server_status()

        assert isinstance(status, str)
        assert "vector:" in status.lower()
        assert "jina-code-embeddings" in status
        assert "docs)" in status

        print(f"\n=== Server Status ===")
        print(status)
