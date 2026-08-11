# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Agent integration tests: AgentRunner must invoke embedding_search.

Uses real FAISS over httpie/cli (shared cache with e2e tests via CODENIB_INDEX_PATH).

Run: pytest test/agent/test_agent_embedding_search.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codenib.agent.runner import AgentRunner
from codenib.agent.skills.loader import SkillLoader
from codenib.agent.skills.registry import SkillRegistry
from codenib.paths import temp_state_dir

pytestmark = pytest.mark.slow

EMBEDDING_INDEX_PATH = os.environ.get(
    "CODENIB_INDEX_PATH",
    str(temp_state_dir() / "embedding-e2e-index"),
)


class TestAgentEmbeddingSearch:
    """
    Vertex (or CODENIB_VERTEX_ROUTING_MODEL) + real FAISS; assert
    tool_calls use embedding_search.

    Index default: ``${CODENIB_TEMP_DIR}/e2e-index``; override with
    ``CODENIB_INDEX_PATH``.
    """

    @pytest.fixture(scope="class", autouse=True)
    def reset_for_class(self):
        SkillRegistry.reset()
        yield
        SkillRegistry.reset()

    @pytest.fixture(scope="class")
    def real_embedding_search(self, httpie_cli_repo):
        """Load embedding_search skill with real FAISS index over httpie/cli."""
        import codenib.agent.skills as pkg
        from codenib.index.embedding import build_hierarchical_vector_store
        from codenib.ops.retrieve import RetrieveContext

        repo_path = str(httpie_cli_repo)
        build_kwargs = {
            "repo_path": repo_path,
            "index_path": EMBEDDING_INDEX_PATH,
            "languages": ["python"],
            "embedding_model": "nomic-ai/CodeRankEmbed",
            "embedding_provider": "huggingface",
            "embedding_dimension": 768,
            "embedding_kwargs": {
                "model_kwargs": {"trust_remote_code": True},
                "encode_kwargs": {
                    "batch_size": 8,
                    "normalize_embeddings": True,
                },
            },
        }
        try:
            vs = build_hierarchical_vector_store(**build_kwargs)
        except ValueError as exc:
            if "Vector config embedding revision mismatch" not in str(exc):
                raise
            print(
                "Cached embedding index has an incompatible model revision; "
                f"rebuilding {EMBEDDING_INDEX_PATH}"
            )
            vs = build_hierarchical_vector_store(
                **build_kwargs,
                force_rebuild=True,
            )

        ctx = RetrieveContext(vector_store=vs, default_level="l2")
        loader = SkillLoader()

        skill_dir = str(Path(pkg.__file__).parent / "embedding_search")
        meta = loader.load_skill(skill_dir, contexts={"retrieve": ctx})
        assert meta is not None
        SkillRegistry().register(meta)

        return meta

    def test_vertex_agent_selects_embedding_search(
        self, real_embedding_search, require_vertex_credentials, vertex_model
    ):
        """Vertex AI agent must select embedding_search for a semantic query."""
        runner = AgentRunner(
            model=vertex_model,
            registry=SkillRegistry(),
            max_turns=3,
        )
        result = runner.run(
            "function that sends an HTTP request with authentication",
        )

        assert result.total_turns >= 1
        assert len(result.tool_calls) >= 1

        skill_ids = [tc.skill_id for tc in result.tool_calls]
        assert (
            "embedding_search" in skill_ids
        ), f"Expected embedding_search for {vertex_model}, got: {skill_ids}"
