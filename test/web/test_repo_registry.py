# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for per-repo skill-registry isolation and config loading."""

from codeminer.agent.skills.registry import SkillRegistry
from codeminer.web.config import DemoConfig, RepoConfig, load_config
from codeminer.web.repo_registry import _fresh_registry


def test_fresh_registry_is_isolated_from_singleton():
    singleton = SkillRegistry()
    reg_a = _fresh_registry()
    reg_b = _fresh_registry()

    assert reg_a is not singleton
    assert reg_b is not singleton
    assert reg_a is not reg_b
    # Independent backing stores: writing to one must not leak into others.
    assert reg_a._skills is not reg_b._skills
    assert reg_a._skills is not singleton._skills


def test_config_index_types_for_mode():
    assert DemoConfig(mode="sparse").index_types() == ["bm25"]
    assert DemoConfig(mode="hybrid").index_types() == ["bm25", "vector"]


def test_config_paths(tmp_path):
    cfg = DemoConfig(data_dir=str(tmp_path / "data"))
    remote = RepoConfig(id="r", name="R", url="https://example.com/r.git")
    local = RepoConfig(id="l", name="L", path=str(tmp_path / "local"))

    assert cfg.repo_dir(remote).endswith("/data/r")
    assert cfg.repo_dir(local) == str(tmp_path / "local")
    assert cfg.manifest_path(remote).endswith(
        "/data/r/.codeminer_cache/repo_manifest.json"
    )


def test_load_config_from_yaml(tmp_path):
    cfg_file = tmp_path / "demo.yaml"
    cfg_file.write_text(
        "model: my-model\n"
        "mode: hybrid\n"
        "repos:\n"
        "  - id: x\n"
        "    name: X\n"
        "    url: https://example.com/x.git\n"
        "    languages: [python, go]\n"
    )
    cfg = load_config(str(cfg_file))
    assert cfg.model == "my-model"
    assert cfg.mode == "hybrid"
    assert len(cfg.repos) == 1
    assert cfg.repos[0].id == "x"
    assert cfg.repos[0].languages == ["python", "go"]


def test_get_repo_lookup():
    cfg = DemoConfig(repos=[RepoConfig(id="a", name="A"), RepoConfig(id="b", name="B")])
    assert cfg.get_repo("b").name == "B"
    assert cfg.get_repo("missing") is None
