# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Agent integration tests for bm25_search skill.

TestBM25SearchAgentE2E: real BM25 index over httpie/cli + Vertex AI agent routing.
Marked @pytest.mark.slow, run with: pytest -v -m slow
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codenib.agent.runner import AgentRunner
from codenib.agent.skills.loader import SkillLoader
from codenib.agent.skills.registry import SkillRegistry

BM25_INDEX_PATH = "/tmp/bm25_e2e_index"


@pytest.mark.slow
class TestBM25SearchAgentE2E:
    """
    Full round-trip: real BM25 index + Vertex AI agent routing.

    BM25 index is cached at /tmp/bm25_e2e_index (reused across runs).
    """

    @pytest.fixture(scope="class", autouse=True)
    def reset_for_class(self):
        SkillRegistry.reset()
        yield
        SkillRegistry.reset()

    @pytest.fixture(scope="class")
    def real_bm25_search(self, httpie_cli_repo):
        """Load bm25_search skill with real BM25 index over httpie/cli."""
        import codenib.agent.skills as pkg
        from codenib.code_chunker import CodeChunker, RepoChunkingConfig
        from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer
        from codenib.ops.retrieve import RetrieveContext

        repo_path = str(httpie_cli_repo)
        documents_file = Path(BM25_INDEX_PATH) / "documents.json"

        if documents_file.exists():
            print(f"Loading existing BM25 index from {BM25_INDEX_PATH}")
            indexer = BM25CodeIndexer(max_k=128)
            indexer.load_index(BM25_INDEX_PATH)
        else:
            print(f"Building new BM25 index at {BM25_INDEX_PATH}")
            chunker = CodeChunker(
                language="python",
                repo_config=RepoChunkingConfig(languages=["python"]),
                max_lines_per_chunk=300,
            )
            chunks = chunker.chunk_repository(repo_path=repo_path)
            assert chunks, "No code chunks generated"
            indexer = BM25CodeIndexer(chunks=chunks, max_k=128)
            indexer.project_root = repo_path
            Path(BM25_INDEX_PATH).mkdir(parents=True, exist_ok=True)
            indexer.save_index(BM25_INDEX_PATH)

        ctx = RetrieveContext(bm25=indexer)
        loader = SkillLoader()

        skill_dir = str(Path(pkg.__file__).parent / "bm25_search")
        meta = loader.load_skill(skill_dir, contexts={"retrieve": ctx})
        assert meta is not None
        SkillRegistry().register(meta)

        return meta

    def test_bm25_search_returns_results(self, real_bm25_search):
        """bm25_search must return results for a keyword query."""
        results = real_bm25_search.executor_fn(
            query="HTTPie",
            top_k=5,
        )
        assert isinstance(results, list)
        assert len(results) > 0

    @pytest.mark.parametrize(
        "vertex_model",
        [
            "vertex_ai/gemini-2.5-flash",
            "vertex_ai/gemini-2.0-flash",
            "vertex_ai/claude-sonnet-4@20250514",
        ],
    )
    def test_vertex_agent_selects_bm25_search(self, real_bm25_search, vertex_model):
        """Vertex AI agent must select bm25_search for a keyword query."""
        try:
            runner = AgentRunner(
                model=vertex_model,
                registry=SkillRegistry(),
                max_turns=3,
            )
            result = runner.run("HTTPie class definition")
        except Exception as exc:
            pytest.skip(f"Vertex agent unavailable for {vertex_model}: {exc}")

        assert result.total_turns >= 1
        assert len(result.tool_calls) >= 1

        skill_ids = [tc.skill_id for tc in result.tool_calls]
        assert (
            "bm25_search" in skill_ids
        ), f"Expected bm25_search for {vertex_model}, got: {skill_ids}"
