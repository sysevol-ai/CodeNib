# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end smoke test for the bm25_search skill.

This test builds a real BM25 index over the httpie/cli repository,
then exercises the full skill loading and execution path.

Marked with @pytest.mark.slow so it is excluded from the default test run.
To run explicitly:

    pytest test/agent/test_bm25_search_e2e.py -v -m slow

The index is written to /tmp/bm25_e2e_index/ and is reused across runs.
Delete that directory to force a rebuild.
"""

from __future__ import annotations

from pathlib import Path

import pytest

BM25_INDEX_PATH = "/tmp/bm25_e2e_index"


@pytest.fixture(scope="session")
def bm25_indexer(httpie_cli_repo):
    """Build or load a BM25CodeIndexer for the httpie/cli repo."""
    from codenib.code_chunker import CodeChunker, RepoChunkingConfig
    from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer

    repo_path = str(httpie_cli_repo)
    documents_file = Path(BM25_INDEX_PATH) / "documents.json"

    if documents_file.exists():
        print(f"\n[e2e] Loading cached BM25 index from {BM25_INDEX_PATH}")
        indexer = BM25CodeIndexer(max_k=128)
        indexer.load_index(BM25_INDEX_PATH)
    else:
        print(f"\n[e2e] Building BM25 index for {repo_path}")
        chunker = CodeChunker(
            language="python",
            repo_config=RepoChunkingConfig(languages=["python"]),
            max_lines_per_chunk=300,
        )
        chunks = chunker.chunk_repository(repo_path=repo_path)
        assert chunks, "No code chunks generated from the repository"
        print(f"  BM25: indexed {len(chunks)} chunks")

        indexer = BM25CodeIndexer(chunks=chunks, max_k=128)
        indexer.project_root = repo_path

        Path(BM25_INDEX_PATH).mkdir(parents=True, exist_ok=True)
        indexer.save_index(BM25_INDEX_PATH)

    return indexer


@pytest.fixture(scope="session")
def bm25_retrieve_context(bm25_indexer):
    """Construct a real RetrieveContext backed by the session-scoped BM25 index."""
    from codenib.ops.retrieve import RetrieveContext

    return RetrieveContext(bm25=bm25_indexer)


@pytest.fixture(scope="session")
def bm25_executor_fn(bm25_retrieve_context):
    """Load the bm25_search skill and return the bound executor callable."""
    import codenib.agent.skills as pkg
    from codenib.agent.skills.loader import SkillLoader
    from codenib.agent.skills.registry import SkillRegistry

    skill_dir = str(Path(pkg.__file__).parent / "bm25_search")

    SkillRegistry.reset()
    loader = SkillLoader()
    meta = loader.load_skill(skill_dir, contexts={"retrieve": bm25_retrieve_context})
    assert meta is not None, f"SkillLoader returned None for {skill_dir}"
    assert meta.executor_fn is not None, "executor_fn is None after loading"
    return meta.executor_fn


@pytest.mark.slow
class TestBM25SearchE2E:
    """Full-stack tests using a real BM25 index over httpie/cli."""

    def test_basic_query_returns_well_formed_results(self, bm25_executor_fn):
        """A broad query returns non-empty results with expected fields."""
        results = bm25_executor_fn("HTTP request handling", top_k=5)
        assert isinstance(results, list)
        assert len(results) > 0

        for node in results:
            assert node.node_name
            assert node.type

    def test_return_content_flag(self, bm25_executor_fn):
        """return_content=False omits content."""
        results = bm25_executor_fn("session", top_k=5, return_content=False)
        assert len(results) > 0
        for node in results:
            content = getattr(node, "content", None)
            assert not content

    def test_filter_test_reduces_results(self, bm25_executor_fn):
        """filter_test=True should return no more results than unfiltered."""
        all_results = bm25_executor_fn("test", top_k=20, filter_test=False)
        filtered = bm25_executor_fn("test", top_k=20, filter_test=True)
        assert len(filtered) <= len(all_results)

    def test_keyword_query_finds_relevant_node(self, bm25_executor_fn):
        """A keyword query should surface nodes related to the keyword."""
        results = bm25_executor_fn("HTTPie", top_k=10)
        assert len(results) > 0
        names = [node.node_name for node in results]
        relevant = [n for n in names if "http" in n.lower() or "client" in n.lower()]
        assert relevant, f"No HTTP-related node in top-10 results. Got: {names}"
