# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the skill package loader."""

from __future__ import annotations

import json
import os
import textwrap

import pytest
import yaml

from codeminer.agent.skills.core import Cost, SkillType
from codeminer.agent.skills.loader import SkillLoader
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.compiler.resources import IndexRequirement


@pytest.fixture(autouse=True)
def _clean_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


def _write_skill_package(
    base_dir,
    skill_id: str,
    *,
    config: dict,
    skill_md: str = "",
    executor_code: str = "",
):
    """Helper to create a skill package directory."""
    skill_dir = os.path.join(base_dir, skill_id)
    os.makedirs(skill_dir, exist_ok=True)

    # __init__.py
    with open(os.path.join(skill_dir, "__init__.py"), "w") as f:
        f.write("")

    # config.yaml
    with open(os.path.join(skill_dir, "config.yaml"), "w") as f:
        yaml.dump(config, f)

    # skill.md
    if skill_md:
        with open(os.path.join(skill_dir, "skill.md"), "w") as f:
            f.write(skill_md)

    # executor.py
    if executor_code:
        with open(os.path.join(skill_dir, "executor.py"), "w") as f:
            f.write(executor_code)

    return skill_dir


class TestSkillLoader:
    def test_load_minimal_config(self, tmp_path):
        config = {
            "skill_id": "test_skill",
            "skill_type": "retrieval",
            "operator": "mock.test",
            "cost": "low",
            "inputs": [{"name": "query", "type": "str", "required": True}],
            "outputs": {"type": "List[QueriedNode]"},
        }
        _write_skill_package(str(tmp_path), "test_skill", config=config)

        loader = SkillLoader()
        skills = loader.load_all(str(tmp_path))

        assert len(skills) == 1
        meta = skills[0]
        assert meta.skill_id == "test_skill"
        assert meta.skill_type == SkillType.RETRIEVAL
        assert meta.cost == Cost.LOW
        assert meta.operator == "mock.test"
        assert len(meta.inputs) == 1
        assert meta.inputs[0].name == "query"

    def test_load_with_defaults(self, tmp_path):
        config = {
            "skill_id": "bm25",
            "skill_type": "retrieval",
            "defaults": {"top_k": 20, "filter_test": False},
        }
        _write_skill_package(str(tmp_path), "bm25", config=config)

        loader = SkillLoader()
        skills = loader.load_all(str(tmp_path))

        assert skills[0].defaults == {"top_k": 20, "filter_test": False}

    def test_load_skill_md(self, tmp_path):
        config = {"skill_id": "with_doc", "skill_type": "retrieval"}
        skill_md = "# Test Skill\n\nUse this for testing."
        _write_skill_package(
            str(tmp_path),
            "with_doc",
            config=config,
            skill_md=skill_md,
        )

        loader = SkillLoader()
        skills = loader.load_all(str(tmp_path))

        assert "Test Skill" in skills[0].skill_doc
        assert "Use this for testing" in skills[0].skill_doc

    def test_load_with_executor(self, tmp_path):
        config = {"skill_id": "exec_test", "skill_type": "custom"}
        executor_code = textwrap.dedent(
            """\
            def create_executor():
                def execute(query="default"):
                    return f"result:{query}"
                return execute
        """
        )
        _write_skill_package(
            str(tmp_path),
            "exec_test",
            config=config,
            executor_code=executor_code,
        )

        loader = SkillLoader()
        skills = loader.load_all(str(tmp_path))

        meta = skills[0]
        assert meta.executor_fn is not None
        assert meta.executor_fn("hello") == "result:hello"

    def test_load_with_context_executor(self, tmp_path):
        config = {"skill_id": "ctx_test", "skill_type": "retrieval"}
        executor_code = textwrap.dedent(
            """\
            def create_executor(context):
                def execute(query="default"):
                    return f"ctx:{context.name}:{query}"
                return execute
        """
        )
        _write_skill_package(
            str(tmp_path),
            "ctx_test",
            config=config,
            executor_code=executor_code,
        )

        # Provide a mock context
        class MockCtx:
            name = "mock"

        loader = SkillLoader()
        skills = loader.load_all(str(tmp_path), contexts={"retrieve": MockCtx()})

        meta = skills[0]
        assert meta.executor_fn is not None
        assert meta.executor_fn("hello") == "ctx:mock:hello"

    def test_load_index_requirements(self, tmp_path):
        config = {
            "skill_id": "with_idx",
            "skill_type": "retrieval",
            "index_requirements": [
                {"type": "bm25", "max_age_seconds": 3600, "scope": "current_repo"},
                {"type": "vector", "max_age_seconds": 7200, "required": False},
            ],
        }
        _write_skill_package(str(tmp_path), "with_idx", config=config)

        loader = SkillLoader()
        skills = loader.load_all(str(tmp_path))

        reqs = skills[0].index_requirements
        assert len(reqs) == 2
        assert isinstance(reqs[0], IndexRequirement)
        assert reqs[0].index_type == "bm25"
        assert reqs[0].max_age_seconds == 3600
        assert reqs[1].index_type == "vector"
        assert reqs[1].required is False

    def test_load_all_multiple_packages(self, tmp_path):
        for sid in ("alpha", "beta", "gamma"):
            _write_skill_package(
                str(tmp_path),
                sid,
                config={"skill_id": sid, "skill_type": "custom"},
            )

        loader = SkillLoader()
        skills = loader.load_all(str(tmp_path))

        assert len(skills) == 3
        ids = {s.skill_id for s in skills}
        assert ids == {"alpha", "beta", "gamma"}

    def test_load_registers_in_registry(self, tmp_path):
        config = {"skill_id": "reg_test", "skill_type": "retrieval"}
        _write_skill_package(str(tmp_path), "reg_test", config=config)

        loader = SkillLoader()
        loader.load_all(str(tmp_path))

        reg = SkillRegistry()
        assert reg.has("reg_test")

    def test_skip_non_package_dirs(self, tmp_path):
        # Create a regular file (not a package)
        (tmp_path / "not_a_package.txt").write_text("hello")
        # Create a dir without config.yaml
        (tmp_path / "no_config").mkdir()
        (tmp_path / "no_config" / "__init__.py").write_text("")

        loader = SkillLoader()
        skills = loader.load_all(str(tmp_path))

        assert len(skills) == 0

    def test_empty_directory(self, tmp_path):
        loader = SkillLoader()
        skills = loader.load_all(str(tmp_path))
        assert skills == []

    def test_nonexistent_directory(self):
        loader = SkillLoader()
        skills = loader.load_all("/nonexistent/path")
        assert skills == []

    def test_load_with_dependencies(self, tmp_path):
        config = {
            "skill_id": "hybrid",
            "skill_type": "aggregate",
            "dependencies": ["bm25_search", "embedding_search"],
        }
        _write_skill_package(str(tmp_path), "hybrid", config=config)

        loader = SkillLoader()
        skills = loader.load_all(str(tmp_path))

        assert skills[0].dependencies == ["bm25_search", "embedding_search"]

    def test_load_with_resources(self, tmp_path):
        config = {
            "skill_id": "gpu_skill",
            "skill_type": "retrieval",
            "resources": ["GPU"],
        }
        _write_skill_package(str(tmp_path), "gpu_skill", config=config)

        loader = SkillLoader()
        skills = loader.load_all(str(tmp_path))

        assert skills[0].resources == ["GPU"]

    def test_metadata_to_dict_serialisable(self, tmp_path):
        config = {
            "skill_id": "serial_test",
            "skill_type": "retrieval",
            "operator": "mock.op",
            "defaults": {"top_k": 20},
            "index_requirements": [{"type": "bm25", "max_age_seconds": 3600}],
        }
        _write_skill_package(str(tmp_path), "serial_test", config=config)

        loader = SkillLoader()
        skills = loader.load_all(str(tmp_path))

        d = skills[0].to_dict()
        json_str = json.dumps(d)
        loaded = json.loads(json_str)
        assert loaded["skill_id"] == "serial_test"
        assert loaded["defaults"] == {"top_k": 20}
