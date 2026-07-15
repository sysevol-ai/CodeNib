# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for reusable agent-runner context loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeminer.agent.skills.loader import SkillLoader
from codeminer.eval.agent_runner.contexts import (
    AgentSkillContextSpec,
    default_agent_skills_dir,
    load_agent_skill_contexts,
)


def test_context_spec_normalizes_ordered_unique_fields():
    spec = AgentSkillContextSpec(
        skill_ids=[" embedding_search ", "bm25_search", "embedding_search"],
        embedding_model="org/embed-small",
        embedding_dimension=128,
        top_k=7,
        languages=["python", "go", "python"],
    )

    assert spec.skill_ids == ("embedding_search", "bm25_search")
    assert spec.languages == ("python", "go")


def test_context_spec_rejects_invalid_values():
    with pytest.raises(ValueError, match="embedding_dimension"):
        AgentSkillContextSpec(
            skill_ids=["bm25_search"],
            embedding_model="org/embed-small",
            embedding_dimension=0,
            top_k=7,
        )
    with pytest.raises(ValueError, match="languages"):
        AgentSkillContextSpec(
            skill_ids=["bm25_search"],
            embedding_model="org/embed-small",
            embedding_dimension=128,
            top_k=7,
            languages=[],
        )
    with pytest.raises(ValueError, match="skill_ids"):
        AgentSkillContextSpec(
            skill_ids=[""],
            embedding_model="org/embed-small",
            embedding_dimension=128,
            top_k=7,
        )


def test_default_agent_skills_dir_points_to_packaged_skills():
    assert default_agent_skills_dir().name == "skills"
    assert default_agent_skills_dir().parent.name == "agent"


def test_load_agent_skill_contexts_loads_metadata_builds_then_reloads(
    monkeypatch,
    tmp_path,
):
    load_calls = []
    build_call = {}
    built_contexts = {"retrieve": object()}
    skills_dir = tmp_path / "skills"

    def fake_load_all(self, loaded_skills_dir, contexts=None):
        load_calls.append((loaded_skills_dir, contexts))
        return []

    def fake_build_skill_contexts(**kwargs):
        build_call.update(kwargs)
        return built_contexts

    monkeypatch.setattr(SkillLoader, "load_all", fake_load_all)
    monkeypatch.setattr(
        "codeminer.compiler.build_skill_contexts",
        fake_build_skill_contexts,
    )

    spec = AgentSkillContextSpec(
        skill_ids=["embedding_search", "bm25_search"],
        embedding_model="org/embed-small",
        embedding_revision="model-commit",
        embedding_dimension=256,
        top_k=11,
        languages=["python", "rust"],
        default_level="l1",
        rebuild=True,
        skills_dir=Path(skills_dir),
        trust_remote_code=True,
        embedding_batch_size=4,
    )

    contexts = load_agent_skill_contexts(
        repo_path=str(tmp_path / "repo"),
        cache_dir=str(tmp_path / "cache"),
        spec=spec,
    )

    assert contexts is built_contexts
    assert load_calls == [
        (str(skills_dir), None),
        (str(skills_dir), built_contexts),
    ]
    assert build_call == {
        "repo_path": str(tmp_path / "repo"),
        "skill_ids": ("embedding_search", "bm25_search"),
        "languages": ("python", "rust"),
        "cache_dir": str(tmp_path / "cache"),
        "skills_dir": str(skills_dir),
        "embedding_model": "org/embed-small",
        "embedding_revision": "model-commit",
        "embedding_dimension": 256,
        "default_top_k": 11,
        "default_level": "l1",
        "rebuild": True,
        "trust_remote_code": True,
        "embedding_batch_size": 4,
    }
