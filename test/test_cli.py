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
            "--model-option",
            "api_version=2025-01-01",
            "--model-option",
            "extra_body.reasoning.enabled=false",
            "--probe-model",
        ]
    )

    assert args.model == "openai/local-model"
    assert args.api_base == "http://localhost:4000/v1"
    assert args.api_key_env == "LOCAL_LLM_KEY"
    assert args.model_option == [
        "api_version=2025-01-01",
        "extra_body.reasoning.enabled=false",
    ]
    assert args.probe_model is True


def test_doctor_parser_accepts_repository_graph_context() -> None:
    args = cli.build_parser().parse_args(
        [
            "doctor",
            ".",
            "--require",
            "graph",
            "--language",
            "python,typescript",
        ]
    )

    assert args.repo == "."
    assert args.require == ["graph"]
    assert args.language == ["python,typescript"]


@pytest.mark.parametrize(
    ("tools", "expected_code", "expected_status"),
    [
        ({"scip-go": "/tools/scip-go"}, 1, "[MISSING] Python (python)"),
        ({"scip-python": "/tools/scip-python"}, 0, "[OK     ] Python (python)"),
    ],
)
def test_doctor_requires_the_repository_language_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tools: dict[str, str],
    expected_code: int,
    expected_status: str,
) -> None:
    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    monkeypatch.setattr(cli, "_check_module", lambda _name: True)
    monkeypatch.setattr(cli.shutil, "which", lambda command: tools.get(command))

    code = cli.run(
        [
            "doctor",
            str(tmp_path),
            "--require",
            "graph",
            "--language",
            "python",
        ]
    )

    output = capsys.readouterr().out
    assert code == expected_code
    assert expected_status in output
    assert "scip; command=scip-python index" in output


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


def test_cli_model_options_layer_environment_and_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CODENIB_DEMO_MODEL_OPTIONS",
        '{"timeout":20,"extra_body":{"reasoning":{"enabled":true}}}',
    )
    monkeypatch.setenv(
        "CODENIB_DEMO_WIKI_MODEL_OPTIONS",
        '{"timeout":90,"extra_body":{"wiki_only":true}}',
    )
    args = SimpleNamespace(
        model="openai/local-model",
        model_option=[
            "timeout=45",
            "extra_body.reasoning.enabled=false",
        ],
    )

    assert cli._model_options_for_args(args) == {
        "timeout": 45,
        "extra_body": {"reasoning": {"enabled": False}},
    }
    assert cli._model_options_for_args(
        args,
        include_wiki_environment=True,
    ) == {
        "timeout": 45,
        "extra_body": {
            "reasoning": {"enabled": False},
            "wiki_only": True,
        },
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.paths import repo_index_dir

    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    manifest = repo_index_dir(tmp_path) / "repo_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")

    assert cli.resolve_manifest_path(str(tmp_path)) == manifest


def test_resolve_manifest_path_falls_back_to_legacy_repository_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
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


def test_fast_index_builds_fresh_bm25_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.paths import repo_index_dir

    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
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
    assert (repo_index_dir(tmp_path) / "repo_manifest.json").is_file()
    assert not (tmp_path / ".codenib_cache").exists()


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


def test_graph_preset_selects_bm25_and_symbol_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    captured = {}

    def fake_index(repo_path, *, languages, views, rebuild):
        captured["views"] = views
        entries = {view: SimpleNamespace(status="fresh", metadata={}) for view in views}
        return (
            SimpleNamespace(
                repo_path=str(repo_path),
                languages=list(languages),
                indexes=entries,
            ),
            [],
        )

    monkeypatch.setattr(cli, "_check_view_dependencies", lambda _views: None)
    monkeypatch.setattr(cli, "index_repository", fake_index)

    assert cli.run(["index", str(tmp_path), "--preset", "graph"]) == 0
    assert captured["views"] == ["bm25", "symbol_graph"]


def test_index_summary_reports_partial_graph_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    entry = SimpleNamespace(
        status="fresh",
        metadata={
            "build_duration_seconds": 1.25,
            "partial": True,
            "available_languages": ["python", "typescript"],
            "failed_languages": {"cpp": "compilation database missing"},
        },
    )
    manifest = SimpleNamespace(
        repo_path=str(tmp_path),
        languages=["python", "typescript", "cpp"],
        indexes={"symbol_graph": entry},
    )

    cli._print_index_summary(manifest, ["symbol_graph"])

    output = capsys.readouterr().out
    assert "symbol_graph" in output
    assert "partial: python, typescript" in output
    assert "unavailable: cpp" in output


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


def test_prepare_local_wiki_rejects_mismatched_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import RepoManifest

    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    RepoManifest(
        repo_path=str(tmp_path),
        commit="a" * 40,
        languages=["python"],
    ).save(str(manifest_path))
    monkeypatch.setattr(
        "codenib.web.local._checkout_commit",
        lambda _repo_path: "b" * 40,
    )

    with pytest.raises(
        ValueError,
        match="repository checkout does not match the indexed snapshot",
    ):
        prepare_local_wiki(
            tmp_path,
            manifest_path,
            frontend_port=3000,
        )


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
        model_options={
            "api_version": "2025-01-01",
            "extra_body": {"reasoning": {"enabled": False}},
        },
    )

    config = yaml.safe_load(local.config_path.read_text())
    assert config["wiki_agent"] is True
    assert config["model"] == "openai/local-model"
    assert config["model_api_base"] == "http://localhost:4000/v1"
    assert config["model_options"] == {
        "api_version": "2025-01-01",
        "extra_body": {"reasoning": {"enabled": False}},
    }
    assert "key" not in local.config_path.read_text().lower()
    assert local.runtime_env == {
        "CODENIB_DEMO_MODEL": "openai/local-model",
        "CODENIB_DEMO_API_BASE": "http://localhost:4000/v1",
        "CODENIB_DEMO_MODEL_OPTIONS": (
            '{"api_version":"2025-01-01","extra_body":'
            '{"reasoning":{"enabled":false}}}'
        ),
        "CODENIB_DEMO_API_KEY": "super-secret",
    }


def test_installed_package_frontend_is_prebuilt(
    tmp_path: Path,
) -> None:
    packaged = tmp_path / "site-packages" / "codenib" / "web" / "frontend"
    packaged.mkdir(parents=True)
    (packaged / "index.html").write_text("<title>CodeNib Wiki</title>\n")

    assert launcher.is_prebuilt_frontend(packaged) is True


def test_vite_source_checkout_is_not_treated_as_prebuilt(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text('<div id="root"></div>\n')
    (tmp_path / "package.json").write_text('{"scripts":{"dev":"vite"}}\n')

    assert launcher.is_prebuilt_frontend(tmp_path) is False


def test_doctor_does_not_require_node_for_prebuilt_frontend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<title>CodeNib Wiki</title>\n")
    monkeypatch.setattr(launcher, "find_frontend_dir", lambda: frontend)
    monkeypatch.setattr(launcher, "node_runtime_status", lambda: (False, "missing"))
    monkeypatch.setattr("codenib.cli.shutil.which", lambda _command: None)

    rows = cli._doctor_rows()
    wiki = {name: (ok, detail) for name, ok, detail in rows["wiki"]}

    assert wiki["Node.js"] == (True, "not required (prebuilt frontend)")
    assert wiki["npm"] == (True, "not required (prebuilt frontend)")


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
