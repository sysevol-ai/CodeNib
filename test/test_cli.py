# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest
import yaml

from codenib import cli
from codenib.web import launcher
from codenib.web.local import prepare_local_wiki


def test_parser_exposes_release_commands() -> None:
    parser = cli.build_parser()

    for command in ("index", "wiki", "mcp", "doctor"):
        args = parser.parse_args([command])
        assert args.command == command


def test_doctor_parser_accepts_model_backend_options() -> None:
    args = cli.build_parser().parse_args(
        [
            "doctor",
            "--require",
            "agent",
            "--model",
            "openai/local-model",
            "--api-base",
            "http://localhost:4000/v1",
            "--api-key-env",
            "LOCAL_LLM_KEY",
            "--probe-model",
        ]
    )

    assert args.model == "openai/local-model"
    assert args.api_base == "http://localhost:4000/v1"
    assert args.api_key_env == "LOCAL_LLM_KEY"
    assert args.probe_model is True


def test_doctor_model_config_reports_missing_named_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_LLM_KEY", raising=False)
    args = SimpleNamespace(
        model="openai/local-model",
        api_base="http://localhost:4000/v1",
        api_key_env="MISSING_LLM_KEY",
    )

    label, ok, detail = cli._doctor_model_config(args)

    assert label == "Model configuration"
    assert ok is False
    assert "MISSING_LLM_KEY is unset" in detail


def test_doctor_model_config_uses_litellm_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    captured = {}

    def validate_environment(**kwargs):
        captured.update(kwargs)
        return {"keys_in_environment": True, "missing_keys": []}

    monkeypatch.setenv("LOCAL_LLM_KEY", "secret")
    monkeypatch.setattr(cli, "_check_module", lambda _name: True)
    monkeypatch.setattr(litellm, "validate_environment", validate_environment)
    args = SimpleNamespace(
        model="openai/local-model",
        api_base="http://localhost:4000/v1",
        api_key_env="LOCAL_LLM_KEY",
    )

    _, ok, detail = cli._doctor_model_config(args)

    assert ok is True
    assert "endpoint=http://localhost:4000/v1" in detail
    assert captured == {
        "model": "openai/local-model",
        "api_base": "http://localhost:4000/v1",
        "api_key": "secret",
    }


def test_detect_languages_orders_by_file_count_and_skips_generated_dirs(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "one.py").write_text("def one():\n    pass\n")
    (tmp_path / "src" / "two.py").write_text("def two():\n    pass\n")
    (tmp_path / "main.go").write_text("package main\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.ts").write_text("export {};\n")
    (tmp_path / ".next").mkdir()
    (tmp_path / ".next" / "ignored.js").write_text("export {};\n")
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "ignored.cpp").write_text("int generated = 1;\n")

    assert cli.detect_languages(tmp_path) == ["python", "go"]


def test_normalize_languages_accepts_aliases_and_commas() -> None:
    assert cli.normalize_languages(["py,typescript", "c++"]) == [
        "python",
        "typescript",
        "cpp",
    ]


def test_resolve_manifest_path_accepts_repository_directory(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest.parent.mkdir()
    manifest.write_text("{}")

    assert cli.resolve_manifest_path(str(tmp_path)) == manifest


def test_index_command_uses_fast_preset_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "main.py").write_text("def main():\n    return 0\n")
    captured = {}

    def fake_index(repo_path, *, languages, views, rebuild):
        captured.update(
            repo_path=repo_path,
            languages=languages,
            views=views,
            rebuild=rebuild,
        )
        entry = SimpleNamespace(
            status="fresh",
            metadata={"build_duration_seconds": 0.1},
        )
        manifest = SimpleNamespace(
            repo_path=str(repo_path),
            languages=list(languages),
            indexes={"bm25": entry},
        )
        return manifest, []

    monkeypatch.setattr(cli, "index_repository", fake_index)

    assert cli.run(["index", str(tmp_path)]) == 0
    assert captured == {
        "repo_path": tmp_path,
        "languages": ["python"],
        "views": ["bm25"],
        "rebuild": False,
    }


def test_fast_index_builds_fresh_bm25_manifest(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        "def greet(name: str) -> str:\n"
        '    """Return a greeting."""\n'
        '    return f"hello {name}"\n'
    )

    manifest, failed = cli.index_repository(
        tmp_path,
        languages=["python"],
        views=["bm25"],
    )

    assert failed == []
    assert manifest.languages == ["python"]
    assert manifest.indexes["bm25"].status == "fresh"
    assert (tmp_path / ".codenib_cache" / "repo_manifest.json").is_file()


def test_semantic_preset_reports_required_extra(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    monkeypatch.setattr(
        cli,
        "_check_module",
        lambda module: module != "faiss",
    )

    assert cli.run(["index", str(tmp_path), "--preset", "semantic"]) == 2
    assert "codenib[semantic]" in capsys.readouterr().err


def test_mcp_command_reports_required_extra(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_check_module", lambda _module: False)

    assert cli.run(["mcp"]) == 2
    assert "codenib[mcp]" in capsys.readouterr().err


def test_prepare_local_wiki_writes_single_repo_registry(
    tmp_path: Path,
) -> None:
    from codenib.compiler.manifest import IndexEntry, RepoManifest

    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    manifest = RepoManifest(
        repo_path=str(tmp_path),
        commit="abc123",
        languages=["python"],
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(manifest_path.parent / "bm25"),
                built_at="2026-07-25T00:00:00+00:00",
                built_at_epoch=0.0,
                status="fresh",
            )
        },
    )
    manifest.save(str(manifest_path))

    local = prepare_local_wiki(
        tmp_path,
        manifest_path,
        frontend_port=3000,
    )

    assert local.config_path.is_file()
    assert (local.data_dir / "qa_registry.json").is_file()
    assert local.repo_id == tmp_path.name.lower()


def test_prepare_local_wiki_uses_github_origin_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import RepoManifest

    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    RepoManifest(
        repo_path=str(tmp_path),
        commit="abc123",
        languages=["python"],
    ).save(str(manifest_path))
    monkeypatch.setattr(
        "codenib.web.local._origin_url",
        lambda _repo_path: "git@github.com:Example/Project.git",
    )

    local = prepare_local_wiki(
        tmp_path,
        manifest_path,
        frontend_port=3000,
    )

    registry = json.loads((local.data_dir / "qa_registry.json").read_text())
    assert local.repo_id == "project"
    assert registry[0]["repo"] == "example/project"


def test_prepare_generated_wiki_keeps_secret_out_of_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import IndexEntry, RepoManifest

    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    RepoManifest(
        repo_path=str(tmp_path),
        commit="abc123",
        languages=["python"],
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(manifest_path.parent / "bm25"),
                built_at="2026-07-25T00:00:00+00:00",
                built_at_epoch=0.0,
                status="fresh",
            )
        },
    ).save(str(manifest_path))
    monkeypatch.setenv("LOCAL_LLM_KEY", "super-secret")

    local = prepare_local_wiki(
        tmp_path,
        manifest_path,
        frontend_port=3000,
        agent_wiki=True,
        model="openai/local-model",
        api_base="http://localhost:4000/v1",
        api_key_env="LOCAL_LLM_KEY",
    )

    config = yaml.safe_load(local.config_path.read_text())
    assert config["wiki_agent"] is True
    assert config["model"] == "openai/local-model"
    assert config["model_api_base"] == "http://localhost:4000/v1"
    assert "key" not in local.config_path.read_text().lower()
    assert local.runtime_env == {"CODENIB_DEMO_API_KEY": "super-secret"}


def test_packaged_frontend_is_materialized_in_user_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packaged = tmp_path / "site-packages" / "codenib" / "web" / "frontend"
    packaged.mkdir(parents=True)
    (packaged / "package.json").write_text('{"name":"codenib-wiki"}\n')
    (packaged / "app").mkdir()
    (packaged / "app" / "page.tsx").write_text("export default function Page() {}\n")
    state = tmp_path / "state"

    monkeypatch.setattr(launcher, "_packaged_frontend_dir", lambda: packaged)
    monkeypatch.setattr(launcher, "user_state_dir", lambda: state)

    runtime = launcher.materialize_frontend(packaged)

    assert runtime != packaged
    assert (runtime / "package.json").is_file()
    assert (runtime / "app" / "page.tsx").is_file()
    assert (runtime / ".codenib-frontend-digest").is_file()


def test_node_runtime_status_rejects_unsupported_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, "v18.17.1\n", ""),
    )

    ok, detail = launcher.node_runtime_status()

    assert ok is False
    assert detail == "v18.17.1 (requires >= 18.18.0)"
