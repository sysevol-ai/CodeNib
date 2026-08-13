# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fast tests for TypeScript repository filtering before SCIP generation."""

import json

import pytest

from codenib.repository_filters import default_exclude_patterns
from codenib.scip_interface.scip_indexer_base import SCIPIndexerBase
from codenib.scip_interface.scip_indexer_ts import SCIPTypeScriptIndexer


def test_default_excludes_cover_root_and_nested_generated_trees():
    patterns = default_exclude_patterns()

    assert "dist/**" in patterns
    assert "**/dist/**" in patterns
    assert "third_party/**" in patterns
    assert "**/third_party/**" in patterns


def test_typescript_temporary_config_includes_repository_excludes(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    original = {
        "compilerOptions": {"allowJs": True},
        "exclude": ["custom/**"],
    }
    config_path = project / "tsconfig.json"
    config_path.write_text(json.dumps(original), encoding="utf-8")
    indexer = SCIPTypeScriptIndexer(
        project,
        output_dir=tmp_path / "index",
        exclude_patterns=default_exclude_patterns(),
    )

    patched_path = indexer._ensure_allow_js()

    assert patched_path is not None
    patched = json.loads(patched_path.read_text(encoding="utf-8"))
    assert patched["exclude"][0] == "custom/**"
    assert "dist/**" in patched["exclude"]
    assert "**/dist/**" in patched["exclude"]
    assert json.loads(config_path.read_text(encoding="utf-8")) == original

    indexer._cleanup_patched_tsconfig()
    assert not patched_path.exists()


def test_read_only_pipeline_skips_project_dependency_installation(
    tmp_path, monkeypatch
):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "package.json").write_text("{}\n", encoding="utf-8")
    indexer = SCIPTypeScriptIndexer(project, output_dir=tmp_path / "index")

    monkeypatch.setattr(
        indexer,
        "_install_dependencies",
        lambda: pytest.fail("read-only onboarding must not install dependencies"),
    )
    monkeypatch.setattr(indexer, "_ensure_allow_js", lambda: None)
    monkeypatch.setattr(
        SCIPIndexerBase,
        "run_pipeline",
        lambda _self, **_kwargs: "graph",
    )

    assert (
        indexer.run_pipeline(allow_project_preparation=False, report_profile=False)
        == "graph"
    )
