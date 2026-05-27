# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for per-repo skill-registry isolation, config, and the QA registry."""

from codeminer.agent.skills.registry import SkillRegistry
from codeminer.web.config import (
    QAConfig,
    RepoEntry,
    load_config,
    load_registry,
    save_registry,
)
from codeminer.web.repo_registry import _fresh_registry


def test_fresh_registry_is_isolated_from_singleton():
    singleton = SkillRegistry()
    reg_a = _fresh_registry()
    reg_b = _fresh_registry()

    assert reg_a is not singleton
    assert reg_b is not singleton
    assert reg_a is not reg_b
    assert reg_a._skills is not reg_b._skills
    assert reg_a._skills is not singleton._skills


def test_config_index_types_for_mode():
    assert QAConfig(mode="sparse").index_types() == ["bm25"]
    assert QAConfig(mode="hybrid").index_types() == ["bm25", "vector"]


def test_config_paths(tmp_path):
    cfg = QAConfig(data_dir=str(tmp_path / "data"))
    assert cfg.registry_path.endswith("/data/qa_registry.json")
    assert cfg.repo_dir("django__django-123").endswith("/data/repos/django__django-123")


def test_load_config_from_yaml(tmp_path):
    cfg_file = tmp_path / "qa.yaml"
    cfg_file.write_text(
        "model: my-model\n"
        "mode: hybrid\n"
        "dataset: foo/bar\n"
        "per_language: 2\n"
        "languages: [python, go]\n"
        "instances: [a__a-1, b__b-2]\n"
    )
    cfg = load_config(str(cfg_file))
    assert cfg.model == "my-model"
    assert cfg.mode == "hybrid"
    assert cfg.dataset == "foo/bar"
    assert cfg.per_language == 2
    assert cfg.languages == ["python", "go"]
    assert cfg.instances == ["a__a-1", "b__b-2"]


def test_registry_round_trip(tmp_path):
    path = str(tmp_path / "qa_registry.json")
    entries = [
        RepoEntry(
            instance_id="django__django-11099",
            repo="django/django",
            base_commit="abcdef1234567890",
            language="python",
            repo_dir="/data/repos/django__django-11099/django_django",
            manifest_path="/data/.../repo_manifest.json",
            problem_statement="Something is broken.",
        )
    ]
    save_registry(path, entries)
    loaded = load_registry(path)
    assert len(loaded) == 1
    assert loaded[0].instance_id == "django__django-11099"
    assert loaded[0].repo == "django/django"
    assert loaded[0].commit_short == "abcdef12"


def test_load_registry_missing_returns_empty(tmp_path):
    assert load_registry(str(tmp_path / "nope.json")) == []
