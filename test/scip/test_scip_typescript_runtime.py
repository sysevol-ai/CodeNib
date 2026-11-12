# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fast tests for TypeScript project preparation before SCIP generation."""

import hashlib
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from codenib.paths import CODENIB_HOME_ENV, PROJECT_INSTALL_SENTINEL, repo_state_dir
from codenib.repository_filters import default_exclude_patterns
from codenib.scip_interface import scip_indexer_ts as ts_indexer
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
    root = project.resolve().as_posix()
    assert f"{root}/**/*.js" in config["include"]
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
    (project / "package.json").write_text('{"name": "axios-fixture"}\n')
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
    (project / "package.json").write_text('{"name": "fixture"}\n')
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
    (project / "package.json").write_text('{"name": "fixture"}\n')
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


@pytest.mark.parametrize("entry_field", ["main", "exports", "tsconfig"])
def test_read_only_wrapper_defers_package_config_resolution(
    tmp_path, monkeypatch, entry_field
):
    project = tmp_path / "repo"
    package = project / "node_modules" / "@scope" / "config"
    package.mkdir(parents=True)
    (project / "package.json").write_text('{"name": "fixture"}\n')
    (project / "tsconfig.json").write_text('{"extends": "@scope/config"}\n')
    (package / "package.json").write_text(json.dumps({entry_field: "./selected.json"}))
    (package / "selected.json").write_text('{"include": ["selected/**/*.ts"]}\n')
    (package / "tsconfig.json").write_text("{}\n")
    before = _checkout_snapshot(project)

    observed = _capture_read_only_run(tmp_path, monkeypatch, project)

    assert "patched_tsconfig" not in observed
    assert _checkout_snapshot(project) == before


def test_read_only_wrapper_preserves_inherited_excludes(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    configs = project / "configs"
    configs.mkdir(parents=True)
    (project / "package.json").write_text('{"name": "fixture"}\n')
    (project / "tsconfig.json").write_text('{"extends": "./configs/base.json"}\n')
    (configs / "base.json").write_text(
        '{"exclude": ["../generated/**", "private/**"]}\n'
    )
    before = _checkout_snapshot(project)

    observed = _capture_read_only_run(
        tmp_path, monkeypatch, project, exclude_patterns=["dist/**"]
    )

    root = project.resolve().as_posix()
    configs_root = configs.resolve().as_posix()
    assert observed["config"]["exclude"] == [
        f"{root}/generated/**",
        f"{configs_root}/private/**",
        f"{root}/dist/**",
    ]
    assert _checkout_snapshot(project) == before


def test_existing_config_without_manifest_keeps_scip_package_boundary(
    tmp_path, monkeypatch
):
    (tmp_path / "package.json").write_text('{"name": "unrelated-parent"}\n')
    project = tmp_path / "repo"
    project.mkdir()
    (project / "tsconfig.json").write_text("{}\n")
    (project / "index.ts").write_text("export const value = 1;\n")
    before = _checkout_snapshot(project)

    observed = _capture_read_only_run(tmp_path, monkeypatch, project)

    assert "patched_tsconfig" not in observed
    assert observed["infer_tsconfig"] is False
    assert _checkout_snapshot(project) == before


NPM_CI_THEN_FALLBACK = [["npm", "ci"], ["npm", "install", "--no-package-lock"]]


class _FakeNpm:
    """Stand-in for ``subprocess.run`` that records install commands."""

    def __init__(self, *, fail: bool, npm_version: str = "10.9.7"):
        self.fail = fail
        self.npm_version = npm_version
        self.installs: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        # argv[0] is resolved through shutil.which, so compare the tool name.
        tool = cmd[0].replace("\\", "/").rsplit("/", 1)[-1].split(".")[0]
        cmd = [tool, *cmd[1:]]
        if cmd[1:] == ["--version"]:
            version = self.npm_version if tool == "npm" else "v22.22.2"
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{version}\n")
        self.installs.append(list(cmd))
        if self.fail:
            raise subprocess.CalledProcessError(1, cmd, stderr="gyp ERR! not ok")
        return subprocess.CompletedProcess(cmd, 0, stdout="")


def _npm_project(tmp_path, monkeypatch):
    monkeypatch.setenv(CODENIB_HOME_ENV, str(tmp_path / "home"))
    monkeypatch.delenv(ts_indexer.RETRY_FAILED_INSTALL_ENV, raising=False)
    project = tmp_path / "repo"
    project.mkdir()
    (project / "package.json").write_text('{"name": "demo"}\n')
    (project / "package-lock.json").write_text('{"lockfileVersion": 1}\n')
    return project, SCIPTypeScriptIndexer(project, output_dir=tmp_path / "index")


def _install(indexer, monkeypatch, fake):
    monkeypatch.setattr(ts_indexer.subprocess, "run", fake)
    fake.installs.clear()
    indexer._install_dependencies()
    return fake.installs


def test_failed_install_is_recorded_outside_checkout_and_skipped(
    tmp_path, monkeypatch, caplog
):
    project, indexer = _npm_project(tmp_path, monkeypatch)
    fake = _FakeNpm(fail=True)

    assert _install(indexer, monkeypatch, fake) == NPM_CI_THEN_FALLBACK
    # Per-repository CodeNib state: not the checkout, not an index view.
    record = repo_state_dir(project) / indexer._INSTALL_FAILURE_RECORD_NAME
    assert record.parent.is_relative_to(tmp_path / "home")
    assert not any((tmp_path / "index").iterdir())
    assert json.loads(record.read_text())["inputs"]["tool_versions"] == {
        "node": "v22.22.2",
        "npm": "10.9.7",
    }
    assert sorted(p.name for p in project.iterdir()) == [
        "package-lock.json",
        "package.json",
    ]

    caplog.set_level(logging.WARNING, logger=ts_indexer.logger.name)
    ts_indexer.logger.addHandler(caplog.handler)
    try:
        assert _install(indexer, monkeypatch, fake) == []
    finally:
        ts_indexer.logger.removeHandler(caplog.handler)
    assert "Skipping dependency install: npm ci failed" in caplog.text
    assert ts_indexer.RETRY_FAILED_INSTALL_ENV in caplog.text


@pytest.mark.parametrize("changed", ["lockfile", "package_json", "npm_version"])
def test_failed_install_retries_when_its_inputs_change(tmp_path, monkeypatch, changed):
    project, indexer = _npm_project(tmp_path, monkeypatch)
    fake = _FakeNpm(fail=True)
    _install(indexer, monkeypatch, fake)

    if changed == "lockfile":
        (project / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    elif changed == "package_json":
        (project / "package.json").write_text('{"name": "demo", "private": true}\n')
    else:
        fake.npm_version = "11.0.0"

    assert _install(indexer, monkeypatch, fake) == NPM_CI_THEN_FALLBACK
    # The new failure replaces the old record, so the next build skips again.
    assert _install(indexer, monkeypatch, fake) == []


@pytest.mark.parametrize("trigger", ["expired", "forced"])
def test_failed_install_retries_after_expiry_or_on_request(
    tmp_path, monkeypatch, trigger
):
    _project, indexer = _npm_project(tmp_path, monkeypatch)
    fake = _FakeNpm(fail=True)
    _install(indexer, monkeypatch, fake)

    if trigger == "expired":
        record_path = indexer._install_failure_record
        record = json.loads(record_path.read_text())
        record["failed_at"] -= indexer._INSTALL_FAILURE_TTL_SEC + 1
        record_path.write_text(json.dumps(record))
    else:
        monkeypatch.setenv(ts_indexer.RETRY_FAILED_INSTALL_ENV, "1")

    assert _install(indexer, monkeypatch, fake) == NPM_CI_THEN_FALLBACK


def test_successful_install_keeps_sentinel_cache_and_clears_failure(
    tmp_path, monkeypatch
):
    project, indexer = _npm_project(tmp_path, monkeypatch)
    fake = _FakeNpm(fail=True)
    _install(indexer, monkeypatch, fake)
    monkeypatch.setenv(ts_indexer.RETRY_FAILED_INSTALL_ENV, "1")
    fake.fail = False

    assert _install(indexer, monkeypatch, fake) == [["npm", "ci"]]
    assert not indexer._install_failure_record.exists()
    lockfile = project / "package-lock.json"
    sentinel = repo_state_dir(project) / PROJECT_INSTALL_SENTINEL
    assert sentinel.read_text() == hashlib.sha1(lockfile.read_bytes()).hexdigest()
    assert sorted(p.name for p in project.iterdir()) == [
        "package-lock.json",
        "package.json",
    ]

    # With node_modules and a matching sentinel, nothing runs at all.
    (project / "node_modules").mkdir()
    monkeypatch.setattr(
        ts_indexer.subprocess,
        "run",
        lambda *a, **k: pytest.fail("cached install must not run a command"),
    )
    indexer._install_dependencies()


@pytest.mark.parametrize(
    "files, which, expected",
    [
        ({}, {"npm"}, [["npm", "install", "--no-package-lock"]]),
        ({"package-lock.json": "{}"}, {"npm"}, NPM_CI_THEN_FALLBACK),
        (
            {"yarn.lock": ""},
            {"yarn"},
            [
                ["yarn", "install", "--frozen-lockfile"],
                ["yarn", "install", "--pure-lockfile"],
            ],
        ),
        (
            {"pnpm-lock.yaml": ""},
            {"pnpm"},
            [
                ["pnpm", "install", "--frozen-lockfile"],
                ["pnpm", "install", "--no-frozen-lockfile", "--config.lockfile=false"],
            ],
        ),
        (
            {"pnpm-workspace.yaml": ""},
            {"pnpm"},
            [["pnpm", "install", "--no-frozen-lockfile", "--config.lockfile=false"]],
        ),
        (
            {"bun.lock": ""},
            {"bun"},
            [["bun", "install", "--frozen-lockfile"], ["bun", "install", "--no-save"]],
        ),
    ],
)
def test_installs_never_write_a_lockfile(tmp_path, monkeypatch, files, which, expected):
    """Every install, and every fallback, leaves the checkout's lockfiles alone."""

    monkeypatch.setenv(CODENIB_HOME_ENV, str(tmp_path / "home"))
    monkeypatch.delenv(ts_indexer.RETRY_FAILED_INSTALL_ENV, raising=False)
    project = tmp_path / "repo"
    project.mkdir()
    (project / "package.json").write_text('{"name": "demo"}\n')
    for name, text in files.items():
        (project / name).write_text(text)
    monkeypatch.setattr(
        ts_indexer.shutil,
        "which",
        lambda tool: f"/bin/{tool}" if tool in which else None,
    )
    indexer = SCIPTypeScriptIndexer(project, output_dir=tmp_path / "index")

    assert _install(indexer, monkeypatch, _FakeNpm(fail=True)) == expected


def _capture_pipeline(indexer, monkeypatch) -> dict:
    observed: dict[str, object] = {}

    def run_pipeline(_self, **kwargs):
        observed.update(kwargs)
        patched = kwargs.get("patched_tsconfig")
        observed["config"] = (
            json.loads(Path(patched).read_text(encoding="utf-8")) if patched else None
        )
        return "graph"

    monkeypatch.setattr(SCIPIndexerBase, "run_pipeline", run_pipeline)
    monkeypatch.setattr(indexer, "_install_dependencies", lambda: None)
    assert indexer.run_pipeline(report_profile=False) == "graph"
    return observed


def test_index_without_root_config_uses_the_external_config(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    (project / "src").mkdir(parents=True)
    (project / "package.json").write_text('{"name": "demo"}\n', encoding="utf-8")
    (project / "src" / "main.js").write_text("export const x = 1;\n")
    indexer = SCIPTypeScriptIndexer(project, output_dir=tmp_path / "index")

    observed = _capture_pipeline(indexer, monkeypatch)

    # scip-typescript's --infer-tsconfig would write tsconfig.json into the
    # checkout; the external config lives in the index directory instead.
    assert observed["infer_tsconfig"] is False
    assert str(observed["patched_tsconfig"]).startswith(str(tmp_path / "index"))
    assert observed["config"]["compilerOptions"] == {"allowJs": True}
    assert f"{project.resolve().as_posix()}/**/*.js" in observed["config"]["include"]
    assert sorted(p.name for p in project.iterdir()) == ["package.json", "src"]
    assert not (tmp_path / "index" / ".tsconfig.scip.readonly.json").exists()
    command = indexer._build_index_command(
        infer_tsconfig=False, patched_tsconfig=observed["patched_tsconfig"]
    )
    assert "--infer-tsconfig" not in command


def test_index_with_workspaces_and_no_root_config_still_infers(tmp_path, monkeypatch):
    """Workspace members are indexed one by one; that path is unchanged."""

    project = tmp_path / "repo"
    project.mkdir()
    (project / "package.json").write_text(
        '{"name": "demo", "workspaces": ["packages/*"]}\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        ts_indexer.shutil,
        "which",
        lambda tool: f"/bin/{tool}" if tool == "npm" else None,
    )
    indexer = SCIPTypeScriptIndexer(project, output_dir=tmp_path / "index")

    observed = _capture_pipeline(indexer, monkeypatch)

    assert observed["npm_workspaces"] is True
    assert observed["infer_tsconfig"] is True
    assert observed.get("patched_tsconfig") is None


def test_generate_index_does_not_infer_beside_an_external_config(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    project.mkdir()
    indexer = SCIPTypeScriptIndexer(project, output_dir=tmp_path / "index")
    seen: dict[str, object] = {}

    def generate_index(_self, **kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(SCIPIndexerBase, "generate_index", generate_index)

    assert indexer.generate_index(patched_tsconfig=str(tmp_path / "x.json"))
    assert seen["infer_tsconfig"] is False
    assert indexer.generate_index()
    assert seen["infer_tsconfig"] is True


def _recording_run(calls):
    def fake(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="1.0\n", stderr="")

    return fake


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("resolved", [True, False])
def test_node_commands_resolve_with_which_and_never_use_a_shell(
    tmp_path, monkeypatch, platform, resolved
):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "package.json").write_text('{"name": "demo"}\n')
    (project / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
    indexer = SCIPTypeScriptIndexer(project, output_dir=tmp_path / "index")
    full = {
        "npm": "C:/node/npm.cmd",
        "pnpm": "C:/node/pnpm.cmd",
        "scip-typescript": "C:/node/scip-typescript.cmd",
        "node": "C:/node/node.exe",
    }
    # The first pnpm lookup must miss so the global npm install runs; every
    # other lookup returns a path (or None) depending on the case.
    pnpm_lookups = []

    def which(name):
        if name == "pnpm":
            pnpm_lookups.append(name)
            if len(pnpm_lookups) == 1:
                return None
        return full[name] if resolved else None

    monkeypatch.setattr(ts_indexer.shutil, "which", which)
    monkeypatch.setattr(sys, "platform", platform)
    calls = []
    monkeypatch.setattr(ts_indexer.subprocess, "run", _recording_run(calls))

    indexer._check_indexer_available()
    indexer._install_dependencies()
    indexer._run_install(["pnpm", "install"], ["pnpm", "install", "--x"], 5)
    indexer._install_inputs(["pnpm", "install"], None)

    assert calls
    for argv, kwargs in calls:
        assert kwargs.get("shell") is not True
        name = Path(argv[0]).name
        bare = name.split(".")[0]
        assert bare in full
        if resolved:
            assert argv[0] == full[bare]
        else:
            assert argv[0] == bare
    assert any(argv[1:3] == ["install", "-g"] for argv, _ in calls)


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("resolved", [True, False])
def test_generate_index_resolves_the_indexer_with_which(
    tmp_path, monkeypatch, platform, resolved
):
    project = tmp_path / "repo"
    project.mkdir()
    indexer = SCIPTypeScriptIndexer(project, output_dir=tmp_path / "index")
    monkeypatch.setattr(indexer, "_check_indexer_available", lambda: True)
    monkeypatch.setattr(
        indexer, "_build_index_command", lambda **_kw: ["scip-typescript", "index"]
    )
    monkeypatch.setattr(
        "codenib.scip_interface.scip_indexer_base.shutil.which",
        lambda name: "C:/node/scip-typescript.cmd" if resolved else None,
    )
    monkeypatch.setattr(sys, "platform", platform)
    calls = []
    monkeypatch.setattr(
        "codenib.scip_interface.scip_indexer_base.subprocess.run",
        _recording_run(calls),
    )

    assert indexer.generate_index() is True

    argv, kwargs = calls[0]
    assert kwargs.get("shell") is not True
    assert argv == [
        "C:/node/scip-typescript.cmd" if resolved else "scip-typescript",
        "index",
    ]
