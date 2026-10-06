# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fast tests for TypeScript repository filtering before SCIP generation."""

import json
from pathlib import Path

import pytest

from codenib.repository_filters import default_exclude_patterns
from codenib.scip_interface.scip_indexer_base import SCIPIndexerBase
from codenib.scip_interface.scip_indexer_ts import SCIPTypeScriptIndexer
from codenib.scip_interface.typescript_semantics import (
    typescript_static_module_fingerprint,
)


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


def test_read_only_pipeline_skips_all_project_preparation(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "package.json").write_text("{}\n", encoding="utf-8")
    existing_patch = project / ".tsconfig.scip.json"
    existing_patch.write_text('{"owned": "by-user"}\n', encoding="utf-8")
    indexer = SCIPTypeScriptIndexer(project, output_dir=tmp_path / "index")

    monkeypatch.setattr(
        indexer,
        "_install_dependencies",
        lambda: pytest.fail("read-only onboarding must not install dependencies"),
    )
    monkeypatch.setattr(
        indexer,
        "_ensure_allow_js",
        lambda: pytest.fail("read-only onboarding must not write a tsconfig"),
    )
    observed: dict[str, object] = {}

    def run_pipeline(_self, **kwargs):
        observed.update(kwargs)
        return "graph"

    monkeypatch.setattr(
        SCIPIndexerBase,
        "run_pipeline",
        run_pipeline,
    )

    assert (
        indexer.run_pipeline(allow_project_preparation=False, report_profile=False)
        == "graph"
    )
    assert observed["infer_tsconfig"] is False
    patched = observed["patched_tsconfig"]
    assert isinstance(patched, str)
    assert patched.startswith(str(tmp_path / "index"))
    assert not (project / "tsconfig.json").exists()
    assert not (tmp_path / "index" / ".tsconfig.scip.readonly.json").exists()
    assert existing_patch.read_text(encoding="utf-8") == '{"owned": "by-user"}\n'


def test_read_only_pipeline_uses_external_config_for_jsconfig(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "jsconfig.json").write_text(
        json.dumps({"exclude": ["custom/**"]}) + "\n",
        encoding="utf-8",
    )
    indexer = SCIPTypeScriptIndexer(
        project,
        output_dir=tmp_path / "index",
        exclude_patterns=["dist/**"],
    )
    observed: dict[str, object] = {}

    def run_pipeline(_self, **kwargs):
        observed.update(kwargs)
        observed["config"] = json.loads(
            Path(kwargs["patched_tsconfig"]).read_text(encoding="utf-8")
        )
        return "graph"

    monkeypatch.setattr(SCIPIndexerBase, "run_pipeline", run_pipeline)

    assert (
        indexer.run_pipeline(allow_project_preparation=False, report_profile=False)
        == "graph"
    )
    assert observed["infer_tsconfig"] is False
    patched_path = observed["patched_tsconfig"]
    assert isinstance(patched_path, str)
    config = observed["config"]
    assert isinstance(config, dict)
    assert config["extends"] == str((project / "jsconfig.json").resolve())
    assert f"{project.resolve()}/**/*.js" in config["include"]
    root = project.resolve().as_posix()
    assert config["exclude"] == [f"{root}/custom/**", f"{root}/dist/**"]
    assert not (project / "tsconfig.json").exists()
    assert not (tmp_path / "index" / ".tsconfig.scip.readonly.json").exists()


def test_read_only_pipeline_anchors_repository_excludes_to_the_checkout(
    tmp_path, monkeypatch
):
    project = tmp_path / "repo"
    project.mkdir()
    indexer = SCIPTypeScriptIndexer(
        project,
        output_dir=tmp_path / "index",
        exclude_patterns=["dist/**", "**/vendor/**"],
    )
    observed: dict[str, object] = {}

    def run_pipeline(_self, **kwargs):
        observed["config"] = json.loads(
            Path(kwargs["patched_tsconfig"]).read_text(encoding="utf-8")
        )
        return "graph"

    monkeypatch.setattr(SCIPIndexerBase, "run_pipeline", run_pipeline)

    assert (
        indexer.run_pipeline(allow_project_preparation=False, report_profile=False)
        == "graph"
    )
    config = observed["config"]
    assert isinstance(config, dict)
    root = project.resolve().as_posix()
    assert config["exclude"] == [f"{root}/dist/**", f"{root}/**/vendor/**"]
    assert not (tmp_path / "index" / ".tsconfig.scip.readonly.json").exists()


def _checkout_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _capture_read_only_run(tmp_path, monkeypatch, project, **indexer_kwargs):
    indexer = SCIPTypeScriptIndexer(
        project, output_dir=tmp_path / "index", **indexer_kwargs
    )
    observed: dict[str, object] = {}

    def run_pipeline(_self, **kwargs):
        observed.update(kwargs)
        patched = kwargs.get("patched_tsconfig")
        if patched is not None:
            observed["config"] = json.loads(Path(patched).read_text(encoding="utf-8"))
        return "graph"

    monkeypatch.setattr(SCIPIndexerBase, "run_pipeline", run_pipeline)
    assert (
        indexer.run_pipeline(allow_project_preparation=False, report_profile=False)
        == "graph"
    )
    return observed


def test_read_only_pipeline_wraps_tsconfig_that_leaves_javascript_out(
    tmp_path, monkeypatch
):
    # axios shape: a root tsconfig.json for the type declarations, with no
    # allowJs and no include, over a plain-JavaScript source tree.
    project = tmp_path / "repo"
    (project / "lib" / "core").mkdir(parents=True)
    (project / "tsconfig.json").write_text(
        "{\n  // declarations only\n"
        '  "compilerOptions": {"module": "node16", "noEmit": true,},\n}\n',
        encoding="utf-8",
    )
    (project / "index.d.ts").write_text("export {};\n", encoding="utf-8")
    (project / "lib" / "core" / "Axios.js").write_text(
        "export default class Axios {}\n", encoding="utf-8"
    )
    before = _checkout_snapshot(project)

    observed = _capture_read_only_run(
        tmp_path, monkeypatch, project, exclude_patterns=["dist/**"]
    )

    assert observed["infer_tsconfig"] is False
    patched = observed["patched_tsconfig"]
    assert isinstance(patched, str)
    assert patched.startswith(str(tmp_path / "index"))
    config = observed["config"]
    assert isinstance(config, dict)
    root = project.resolve().as_posix()
    assert config["extends"] == str((project / "tsconfig.json").resolve())
    assert config["compilerOptions"] == {"allowJs": True}
    for extension in ("js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts"):
        assert f"{root}/**/*.{extension}" in config["include"]
    assert config["exclude"] == [f"{root}/dist/**"]
    assert _checkout_snapshot(project) == before
    assert not (tmp_path / "index" / ".tsconfig.scip.readonly.json").exists()


def test_read_only_pipeline_wraps_tsconfig_through_local_extends(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "tsconfig.base.json").write_text(
        json.dumps({"compilerOptions": {"strict": True}}) + "\n",
        encoding="utf-8",
    )
    (project / "tsconfig.json").write_text(
        json.dumps({"extends": "./tsconfig.base"}) + "\n",
        encoding="utf-8",
    )
    before = _checkout_snapshot(project)

    observed = _capture_read_only_run(tmp_path, monkeypatch, project)

    config = observed["config"]
    assert isinstance(config, dict)
    assert config["extends"] == str((project / "tsconfig.json").resolve())
    assert _checkout_snapshot(project) == before


@pytest.mark.parametrize(
    ("files", "reason"),
    [
        ({"tsconfig.json": {"compilerOptions": {"allowJs": True}}}, "allowJs"),
        (
            {
                "tsconfig.json": {"extends": "./tsconfig.base.json"},
                "tsconfig.base.json": {"compilerOptions": {"allowJs": True}},
            },
            "inherited allowJs",
        ),
        ({"tsconfig.json": {"include": ["src/**/*.ts"]}}, "explicit include"),
        ({"tsconfig.json": {"files": ["index.d.ts"]}}, "explicit files"),
        (
            {
                "tsconfig.json": {"extends": "./tsconfig.base.json"},
                "tsconfig.base.json": {"include": ["src"]},
            },
            "inherited include",
        ),
        (
            {"tsconfig.json": {"references": [{"path": "./packages/core"}]}},
            "project references",
        ),
        (
            {"tsconfig.json": {"extends": "@scope/missing/tsconfig.json"}},
            "unresolvable extends",
        ),
    ],
)
def test_read_only_pipeline_keeps_tsconfig_that_defines_its_own_surface(
    tmp_path, monkeypatch, files, reason
):
    project = tmp_path / "repo"
    project.mkdir()
    for name, payload in files.items():
        (project / name).write_text(json.dumps(payload) + "\n", encoding="utf-8")
    before = _checkout_snapshot(project)

    observed = _capture_read_only_run(tmp_path, monkeypatch, project)

    assert observed["infer_tsconfig"] is False
    assert "patched_tsconfig" not in observed, reason
    assert _checkout_snapshot(project) == before


def test_root_tsconfig_takes_precedence_over_auto_workspace_mode(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "tsconfig.json").write_text(
        json.dumps({"include": ["packages/*/src"]}) + "\n",
        encoding="utf-8",
    )
    (project / "pnpm-workspace.yaml").write_text(
        "packages:\n  - packages/*\n",
        encoding="utf-8",
    )
    indexer = SCIPTypeScriptIndexer(project, output_dir=tmp_path / "index")
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        SCIPIndexerBase,
        "run_pipeline",
        lambda _self, **kwargs: observed.update(kwargs) or "graph",
    )
    monkeypatch.setattr(
        "codenib.scip_interface.scip_indexer_ts.shutil.which",
        lambda _command: "/managed/bin/tool",
    )

    assert (
        indexer.run_pipeline(allow_project_preparation=False, report_profile=False)
        == "graph"
    )
    assert observed["infer_tsconfig"] is False
    assert not any(
        observed.get(flag)
        for flag in ("yarn_workspaces", "pnpm_workspaces", "npm_workspaces")
    )


def test_static_module_parser_safely_reuses_mixed_language_grammars():
    typescript = b'import type { Runner } from "./types";\n'
    javascript = b'import value from "./value.js";\nexport { value };\n'

    for _ in range(32):
        ts_fingerprint = typescript_static_module_fingerprint(typescript, ".ts")
        js_fingerprint = typescript_static_module_fingerprint(javascript, ".js")

    assert len(ts_fingerprint) == 1
    assert len(js_fingerprint) == 1
    assert ts_fingerprint[0][0] == "import_statement"
    assert js_fingerprint[0][0] == "import_statement"
