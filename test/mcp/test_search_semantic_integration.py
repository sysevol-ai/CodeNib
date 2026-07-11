# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for search_semantic with real embedding indexes.

Uses pre-built indexes from /mnt/data/codeminer (Turquoise-T's dataset).
"""

import asyncio
from pathlib import Path

import pytest

from codeminer.compiler.manifest import IndexEntry, RepoManifest
from codeminer.mcp.context import ServerContext
from codeminer.mcp.tools.search import search_semantic

# Test data directory
CODEMINER_DATA = Path("/mnt/data/codeminer")
TEST_REPO = "astropy__astropy-12907"
TEST_REPO_PATH = CODEMINER_DATA / TEST_REPO


@pytest.mark.slow
@pytest.mark.skipif(
    not TEST_REPO_PATH.exists(),
    reason="Test data not available at /mnt/data/codeminer",
)
class TestSearchSemanticIntegration:
    """Integration tests with real FAISS indexes."""

    @pytest.fixture
    def real_context(self):
        """Create ServerContext with real embedding index."""
        # Create manifest for test repo
        # Note: No actual manifest file exists, we construct it programmatically
        manifest = RepoManifest(
            repo_path=str(TEST_REPO_PATH),
            commit="test",
            indexes={
                "vector": IndexEntry(
                    index_type="vector",
                    path=str(TEST_REPO_PATH),  # Parent directory containing l0/ and l2/
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

        # Manually load vector store (since we're bypassing ServerContext.load)
        ctx = ServerContext(manifest=manifest)

        from codeminer.index.embedding.vector_store import CodeVectorStore

        ctx.vector = CodeVectorStore(
            embedding_model="jinaai/jina-code-embeddings-1.5b",
            embedding_provider="huggingface",
            dimension=1536,
            index_metric="ip",
            store_path=str(TEST_REPO_PATH),  # Parent directory
        )
        ctx.vector.load()

        return ctx

    def test_search_semantic_real_query(self, real_context):
        """Test semantic search with a real query on astropy code."""
        results = asyncio.run(
            search_semantic(
                real_context,
                query="find coordinate transformation functions",
                top_k=5,
                level="l2",
            )
        )

        # Basic validation
        assert isinstance(results, list)
        assert len(results) > 0
        assert len(results) <= 5

        # Check result structure
        first_result = results[0]
        assert "node_name" in first_result
        assert "type" in first_result
        assert "file" in first_result
        assert "score" in first_result
        assert "content" in first_result

        # Score should be reasonable (IP metric, higher is better)
        assert first_result["score"] > 0.0

        # Print results for manual inspection
        print("\n=== Search Results ===")
        for i, r in enumerate(results):
            print(f"{i+1}. {r['node_name']} (score: {r['score']:.3f})")
            print(f"   File: {r['file']}")
            print(f"   Type: {r['type']}")
            print()

    def test_search_semantic_score_threshold(self, real_context):
        """Test score threshold filtering with real index."""
        # Search without threshold
        all_results = asyncio.run(
            search_semantic(real_context, query="unit test", top_k=20)
        )

        # Search with threshold
        filtered_results = asyncio.run(
            search_semantic(
                real_context, query="unit test", top_k=20, score_threshold=0.5
            )
        )

        # Filtered should have fewer or equal results
        assert len(filtered_results) <= len(all_results)

        # All filtered results should meet threshold
        for r in filtered_results:
            assert r["score"] >= 0.5

    def test_search_semantic_performance(self, real_context):
        """Test that search completes in reasonable time."""
        import time

        start = time.time()
        results = asyncio.run(
            search_semantic(real_context, query="coordinate transformation", top_k=10)
        )
        elapsed = time.time() - start

        # Should complete within 2 seconds (generous threshold)
        assert elapsed < 2.0
        assert len(results) > 0

        print(f"\n=== Performance ===")
        print(f"Search completed in {elapsed:.3f}s")
