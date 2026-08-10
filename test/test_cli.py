# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import errno
import json
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest
import yaml

import codenib.web.local as local_module
from codenib import cli
from codenib.source_fingerprint import RepositorySourceBinding
from codenib.web import launcher
from codenib.web.local import prepare_local_wiki


def test_parser_exposes_release_commands() -> None:
    parser = cli.build_parser()

    for command in ("index", "wiki", "export", "publish", "mcp", "doctor"):
        args = parser.parse_args([command])
        assert args.command == command

    toolchain = parser.parse_args(
        ["toolchain", "install", ".", "--scope", "all", "--dry-run"]
    )
    assert toolchain.command == "toolchain"
    assert toolchain.toolchain_command == "install"
    assert toolchain.scope == "all"
    assert toolchain.dry_run is True

    publish = parser.parse_args(["publish", "."])
    assert publish.preset == "auto"


def test_mcp_artifact_repo_is_explicit() -> None:
    parser = cli.build_parser()

    query_only = parser.parse_args(["mcp", "--artifact", "/tmp/context"])
    bound = parser.parse_args(
        ["mcp", "--artifact", "/tmp/context", "--repo", "/tmp/repo"]
    )

    assert query_only.repo is None
    assert bound.repo == "/tmp/repo"


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--port", "0"),
        ("--port", "-1"),
        ("--port", "65536"),
        ("--api-port", "0"),
        ("--api-port", "70000"),
    ],
)
def test_wiki_parser_rejects_invalid_tcp_ports(flag: str, value: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.build_parser().parse_args(["wiki", ".", flag, value])

    assert exc_info.value.code == 2


def test_wiki_rejects_colliding_ports_before_repository_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = cli.build_parser().parse_args(
        ["wiki", ".", "--port", "8123", "--api-port", "8123"]
    )
    monkeypatch.setattr(
        cli,
        "resolve_repo_path",
        lambda _value: pytest.fail("port preflight must run before repository work"),
    )

    with pytest.raises(cli.CLIError, match="must be different"):
        cli._run_wiki(args)


def test_export_parser_accepts_pages_mount_options() -> None:
    args = cli.build_parser().parse_args(
        [
            "export",
            ".",
            "--output",
            "/tmp/wiki",
            "--base-path",
            "/project",
            "--frontend-dir",
            "/tmp/frontend",
        ]
    )

    assert args.output == "/tmp/wiki"
    assert args.base_path == "/project"
    assert args.frontend_dir == "/tmp/frontend"


def test_publish_and_artifact_parsers_expose_distribution_options() -> None:
    publish = cli.build_parser().parse_args(
        [
            "publish",
            ".",
            "--preset",
            "semantic",
            "--site-output",
            "/tmp/site",
            "--context-output",
            "/tmp/context",
            "--repository",
            "example/project",
            "--base-path",
            "/project",
            "--embedding-provider",
            "openai",
        ]
    )
    artifact = cli.build_parser().parse_args(
        [
            "artifact",
            "pack",
            ".",
            "--output",
            "/tmp/context",
            "--view",
            "bm25,vector",
        ]
    )

    assert publish.preset == "semantic"
    assert publish.site_output == "/tmp/site"
    assert publish.context_output == "/tmp/context"
    assert publish.repository == "example/project"
    assert publish.embedding_provider == "openai"
    assert artifact.artifact_command == "pack"
    assert artifact.view == ["bm25,vector"]


def test_wiki_parser_accepts_headless_quality_audit() -> None:
    args = cli.build_parser().parse_args(["wiki", ".", "--audit", "--audit-json"])

    assert args.audit is True
    assert args.audit_json is True


def test_doctor_parser_accepts_model_backend_options() -> None:
    args = cli.build_parser().parse_args(
        [
            "doctor",
            "--require",
            "agent",
            "--model",
            "openai/local-model",
            "--model-provider",
            "openai",
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
    assert args.model_provider == "openai"
    assert args.api_base == "http://localhost:4000/v1"
    assert args.api_key_env == "LOCAL_LLM_KEY"
    assert args.model_option == [
        "api_version=2025-01-01",
        "extra_body.reasoning.enabled=false",
    ]
    assert args.probe_model is True


def test_index_parser_accepts_remote_embedding_route() -> None:
    args = cli.build_parser().parse_args(
        [
            "index",
            ".",
            "--preset",
            "semantic",
            "--embedding-provider",
            "openai",
            "--embedding-model",
            "text-embedding-3-small",
            "--embedding-dimension",
            "1536",
            "--embedding-endpoint",
            "https://inference.example.test/v1",
            "--embedding-api-key-env",
            "MODELS_TOKEN",
        ]
    )

    assert args.embedding_provider == "openai"
    assert args.embedding_model == "text-embedding-3-small"
    assert args.embedding_dimension == 1536
    assert args.embedding_endpoint == "https://inference.example.test/v1"
    assert args.embedding_api_key_env == "MODELS_TOKEN"


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


def test_doctor_model_probe_checks_product_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.llm.litellm_chat as chat_module

    class FakeStructured:
        def __init__(self, schema):
            self.schema = schema

        def invoke(self, _messages):
            return self.schema(status="ok")

    class FakeLLM:
        def __init__(self, **_kwargs):
            pass

        def complete(self, _messages):
            return "OK"

        def _call_raw(self, _messages, **_kwargs):
            call = SimpleNamespace(
                function=SimpleNamespace(name="report_backend_ready")
            )
            message = SimpleNamespace(tool_calls=[call])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        def with_structured_output(self, schema):
            return FakeStructured(schema)

    monkeypatch.setattr(chat_module, "LiteLLMChat", FakeLLM)
    args = cli.build_parser().parse_args(
        [
            "doctor",
            "--model",
            "ollama/qwen3:8b",
            "--api-base",
            "http://localhost:11434",
            "--probe-model",
        ]
    )

    checks = cli._probe_doctor_model(args)

    assert checks == [
        ("Model text probe", True, "response received"),
        ("Model tool probe", True, "function call received"),
        ("Model structured probe", True, "schema response received"),
    ]


def test_doctor_model_probe_stops_after_text_failure_and_redacts_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.llm.litellm_chat as chat_module

    class FailingLLM:
        def __init__(self, **_kwargs):
            pass

        def complete(self, _messages):
            raise RuntimeError("authentication failed for secret-value")

    monkeypatch.setattr(chat_module, "LiteLLMChat", FailingLLM)
    monkeypatch.setenv("MODEL_KEY", "secret-value")
    args = cli.build_parser().parse_args(
        [
            "doctor",
            "--model",
            "openai/model",
            "--api-key-env",
            "MODEL_KEY",
            "--probe-model",
        ]
    )

    checks = cli._probe_doctor_model(args)

    assert checks[0] == (
        "Model text probe",
        False,
        "authentication failed for ***",
    )
    assert checks[1][2] == "skipped: text completion failed"
    assert checks[2][2] == "skipped: text completion failed"


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


def test_openai_chat_route_maps_to_litellm_without_storing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODELS_TOKEN", "runtime-secret")
    args = cli.build_parser().parse_args(
        [
            "doctor",
            "--model-provider",
            "openai",
            "--model",
            "gpt-4.1",
            "--api-base",
            "https://inference.example.test/v1",
            "--api-key-env",
            "MODELS_TOKEN",
        ]
    )

    backend = cli._model_backend_for_args(args)

    assert backend is not None
    assert backend.model == "openai/gpt-4.1"
    assert backend.api_base == "https://inference.example.test/v1"
    assert backend.api_key == "runtime-secret"
    assert backend.auth_source == "MODELS_TOKEN"
    assert "runtime-secret" not in repr(backend)


def test_remote_embedding_defaults_and_requires_dimension_for_custom_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "runtime-secret")
    default_args = cli.build_parser().parse_args(
        ["index", "--embedding-provider", "openai"]
    )

    route = cli._embedding_route_for_args(default_args)

    assert route.model == "text-embedding-3-small"
    assert route.dimension == 1536
    custom_args = cli.build_parser().parse_args(
        [
            "index",
            "--embedding-provider",
            "openai",
            "--embedding-model",
            "vendor/custom-model",
        ]
    )
    with pytest.raises(cli.CLIError, match="embedding-dimension"):
        cli._embedding_route_for_args(custom_args)


def test_embedding_dimension_rejects_boolean_values() -> None:
    with pytest.raises(cli.CLIError, match="positive integer"):
        cli._optional_int(True, source="embedding dimension")


def test_openai_embedding_reports_missing_credential_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    args = cli.build_parser().parse_args(["index", "--embedding-provider", "openai"])

    route = cli._embedding_route_for_args(args)

    with pytest.raises(ValueError) as raised:
        route.credential()
    assert str(raised.value) == (
        "credential environment variable is unset or empty: OPENAI_API_KEY"
    )


def test_fast_index_does_not_resolve_unused_embedding_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "sample.py").write_text("VALUE = 1\n")
    monkeypatch.setenv("CODENIB_EMBEDDING_PROVIDER", "not a provider")
    captured = {}

    def fake_index(repo_path, **kwargs):
        captured.update(repo_path=repo_path, **kwargs)
        return (
            SimpleNamespace(
                repo_path=str(repo_path),
                languages=kwargs["languages"],
                indexes={"bm25": SimpleNamespace(status="fresh", metadata={})},
            ),
            [],
        )

    monkeypatch.setattr(cli, "index_repository", fake_index)

    assert cli.run(["index", str(tmp_path), "--preset", "fast"]) == 0
    assert captured["views"] == ["bm25"]
    assert "embedding_provider" not in captured


def test_remote_semantic_dependency_check_does_not_require_sentence_transformers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = []

    def check(module):
        checked.append(module)
        return module in {"faiss", "openai"}

    monkeypatch.setattr(cli, "_check_module", check)

    cli._check_view_dependencies(
        ["vector"],
        embedding_provider="openai",
    )

    assert "faiss" in checked
    assert "openai" in checked
    assert "sentence_transformers" not in checked


def test_doctor_reports_remote_embedding_route_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "doctor-secret")
    monkeypatch.setattr(
        cli, "_check_module", lambda module: module in {"faiss", "openai"}
    )
    args = cli.build_parser().parse_args(["doctor", "--embedding-provider", "openai"])

    semantic = cli._doctor_rows(args)["semantic"]
    checks = {label: (ok, detail) for label, ok, detail in semantic}

    assert checks["Embedding route"][0] is True
    assert "openai:text-embedding-3-small" in checks["Embedding route"][1]
    assert "auth=OPENAI_API_KEY" in checks["Embedding route"][1]
    assert "doctor-secret" not in str(semantic)
    assert checks["OpenAI SDK"] == (True, "installed")
    assert "sentence-transformers" not in checks


def test_doctor_embedding_probe_uses_resolved_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.index.embedding.vector_store as vector_module

    captured = {}

    class FakeStore:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.dimension = kwargs["dimension"]

        def close(self):
            captured["closed"] = True

    monkeypatch.setenv("OPENAI_API_KEY", "probe-secret")
    monkeypatch.setattr(cli, "_check_module", lambda _module: True)
    monkeypatch.setattr(vector_module, "CodeVectorStore", FakeStore)
    args = cli.build_parser().parse_args(
        ["doctor", "--embedding-provider", "openai", "--probe-embedding"]
    )

    check = cli._probe_doctor_embedding(args)

    assert check == ("Embedding probe", True, "vector received; dimension=1536")
    assert captured["embedding_provider"] == "openai"
    assert "base_url" not in captured
    assert captured["api_key"] == "probe-secret"
    assert captured["closed"] is True


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


def test_index_command_auto_preset_falls_back_without_dense_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "main.py").write_text("def main():\n    return 0\n")
    captured = {}
    monkeypatch.setattr(cli, "_check_module", lambda _module: False)

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


def test_auto_preset_selects_hybrid_views_when_semantic_extra_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = cli.build_parser().parse_args(["index", "."])
    monkeypatch.setattr(cli, "_check_module", lambda _module: True)

    assert args.preset == "auto"
    assert cli._selected_views_for_args(args) == ["bm25", "vector"]


def test_auto_preset_preserves_an_explicit_embedding_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = cli.build_parser().parse_args(
        ["index", ".", "--embedding-provider", "openai"]
    )
    monkeypatch.setattr(cli, "_check_module", lambda _module: False)

    assert cli._selected_views_for_args(args) == ["bm25", "vector"]


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


def test_semantic_index_passes_resolved_openai_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    monkeypatch.setenv("OPENAI_API_KEY", "runtime-secret")
    monkeypatch.setattr(cli, "_check_module", lambda _module: True)
    captured = {}

    def fake_index(repo_path, **kwargs):
        captured.update(repo_path=repo_path, **kwargs)
        entries = {
            view: SimpleNamespace(status="fresh", metadata={})
            for view in kwargs["views"]
        }
        return (
            SimpleNamespace(
                repo_path=str(repo_path),
                languages=kwargs["languages"],
                indexes=entries,
            ),
            [],
        )

    monkeypatch.setattr(cli, "index_repository", fake_index)

    result = cli.run(
        [
            "index",
            str(tmp_path),
            "--preset",
            "semantic",
            "--embedding-provider",
            "openai",
        ]
    )

    assert result == 0
    assert captured["embedding_provider"] == "openai"
    assert captured["embedding_model"] == "text-embedding-3-small"
    assert captured["embedding_dimension"] == 1536
    assert captured["embedding_endpoint"] is None
    assert captured["embedding_credential_env"] == "OPENAI_API_KEY"
    assert "runtime-secret" not in str(captured)


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


def test_mcp_parser_limits_tool_surface_and_defaults_to_full() -> None:
    parser = cli.build_parser()

    assert parser.parse_args(["mcp"]).tool_surface == "full"
    assert parser.parse_args(["mcp", "--tool-surface", "explore"]).tool_surface == (
        "explore"
    )
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["mcp", "--tool-surface", "hidden"])

    assert exc_info.value.code == 2


def test_mcp_manifest_forwards_explore_tool_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.mcp import server

    manifest_path = tmp_path / "repo_manifest.json"
    manifest_path.write_text("{}")
    calls = []
    monkeypatch.setattr(cli, "_require_modules", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "resolve_manifest_path", lambda _value: manifest_path)
    monkeypatch.setattr(server, "main", calls.append)
    args = cli.build_parser().parse_args(
        [
            "mcp",
            str(tmp_path),
            "--log-level",
            "WARNING",
            "--tool-surface",
            "explore",
        ]
    )

    assert cli._run_mcp(args) == 0
    assert calls == [
        [
            str(manifest_path),
            "--log-level",
            "WARNING",
            "--tool-surface",
            "explore",
        ]
    ]


def test_mcp_portable_artifact_forwards_full_tool_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.mcp import server

    artifact_path = tmp_path / "portable-context"
    repo_path = tmp_path / "checkout"
    calls = []
    monkeypatch.setattr(cli, "_require_modules", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "resolve_repo_path", lambda _value: repo_path)
    monkeypatch.setattr(server, "main", calls.append)
    args = cli.build_parser().parse_args(
        [
            "mcp",
            "--artifact",
            str(artifact_path),
            "--repo",
            "ignored-checkout",
            "--repository",
            "owner/repository",
            "--log-level",
            "ERROR",
        ]
    )

    assert cli._run_mcp(args) == 0
    assert calls == [
        [
            "--artifact",
            str(artifact_path.resolve()),
            "--repo",
            str(repo_path),
            "--log-level",
            "ERROR",
            "--tool-surface",
            "full",
            "--repository",
            "owner/repository",
        ]
    ]


def _test_source_fingerprint(repo: Path, cache: Path) -> str:
    from codenib.source_fingerprint import fingerprint_repository

    return fingerprint_repository(repo, exclude_roots=(cache,)).value


def test_prepare_local_wiki_writes_single_repo_registry(
    tmp_path: Path,
) -> None:
    from codenib.compiler.manifest import IndexEntry, RepoManifest

    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    source = _test_source_fingerprint(tmp_path, manifest_path.parent)
    manifest = RepoManifest(
        repo_path=str(tmp_path),
        commit="abc123",
        source_fingerprint=source,
        languages=["python"],
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(manifest_path.parent / "bm25"),
                built_at="2026-07-25T00:00:00+00:00",
                built_at_epoch=0.0,
                status="fresh",
                commit="abc123",
                source_fingerprint=source,
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


def test_prepare_local_wiki_capture_return_cancellation_closes_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import RepoManifest

    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    source = _test_source_fingerprint(tmp_path, manifest_path.parent)
    RepoManifest(
        repo_path=str(tmp_path),
        source_fingerprint=source,
        languages=["python"],
    ).save(str(manifest_path))
    captured: list[RepositorySourceBinding] = []
    real_capture = local_module.capture_repository_source
    interruption = SystemExit("injected after local wiki capture returned")

    def capture(*args, **kwargs):
        binding = real_capture(*args, **kwargs)
        captured.append(binding)
        raise interruption

    monkeypatch.setattr(local_module, "capture_repository_source", capture)
    with pytest.raises(SystemExit) as caught:
        prepare_local_wiki(tmp_path, manifest_path, frontend_port=3000)

    assert caught.value is interruption
    assert len(captured) == 1
    assert captured[0].closed


def test_prepare_local_wiki_persistent_close_preserves_primary_and_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import RepoManifest

    source_path = tmp_path / "module.py"
    source_path.write_text("VALUE = 1\n")
    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    source = _test_source_fingerprint(tmp_path, manifest_path.parent)
    RepoManifest(
        repo_path=str(tmp_path),
        source_fingerprint=source,
        languages=["python"],
    ).save(str(manifest_path))
    source_path.write_text("VALUE = 2\n")
    captured: list[RepositorySourceBinding] = []
    real_capture = local_module.capture_repository_source
    real_close = RepositorySourceBinding.close

    def capture(*args, **kwargs):
        binding = real_capture(*args, **kwargs)
        captured.append(binding)
        return binding

    def persistent_eio(binding: RepositorySourceBinding) -> None:
        if binding in captured:
            raise OSError(errno.EIO, "injected local wiki source close EIO")
        real_close(binding)

    monkeypatch.setattr(local_module, "capture_repository_source", capture)
    monkeypatch.setattr(RepositorySourceBinding, "close", persistent_eio)
    with pytest.raises(ValueError, match="does not match the manifest") as caught:
        prepare_local_wiki(tmp_path, manifest_path, frontend_port=3000)

    assert len(captured) == 1
    owner = caught.value.source_cleanup_owner
    assert owner.pending_sources == (captured[0],)
    assert not captured[0].closed

    monkeypatch.setattr(RepositorySourceBinding, "close", real_close)
    owner.close()
    assert owner.closed
    assert captured[0].closed


def test_prepare_local_wiki_treats_manifest_commit_as_indexed_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import RepoManifest

    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    source = _test_source_fingerprint(tmp_path, manifest_path.parent)
    RepoManifest(
        repo_path=str(tmp_path),
        commit="a" * 40,
        source_fingerprint=source,
        languages=["python"],
    ).save(str(manifest_path))
    monkeypatch.setattr(
        "codenib.compiler.checkout_identity.checkout_commit",
        lambda _repo_path: pytest.fail(
            "local content authentication must not claim a Git HEAD attestation"
        ),
    )

    local = prepare_local_wiki(
        tmp_path,
        manifest_path,
        frontend_port=3000,
    )

    registry = json.loads((local.data_dir / "qa_registry.json").read_text())
    assert registry[0]["base_commit"] == "a" * 40


def test_prepare_local_wiki_rejects_changed_source_at_same_commit(
    tmp_path: Path,
) -> None:
    from codenib.compiler.manifest import RepoManifest
    from codenib.source_fingerprint import fingerprint_repository

    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n")
    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    fingerprint = fingerprint_repository(
        tmp_path,
        exclude_roots=(manifest_path.parent,),
    ).value
    RepoManifest(
        repo_path=str(tmp_path),
        source_fingerprint=fingerprint,
        languages=["python"],
    ).save(str(manifest_path))
    source.write_text("VALUE = 2\n")

    with pytest.raises(
        ValueError,
        match="repository source content does not match the manifest",
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
    source = _test_source_fingerprint(tmp_path, manifest_path.parent)
    RepoManifest(
        repo_path=str(tmp_path),
        commit="abc123",
        source_fingerprint=source,
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
    source = _test_source_fingerprint(tmp_path, manifest_path.parent)
    RepoManifest(
        repo_path=str(tmp_path),
        commit="abc123",
        source_fingerprint=source,
        languages=["python"],
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(manifest_path.parent / "bm25"),
                built_at="2026-07-25T00:00:00+00:00",
                built_at_epoch=0.0,
                status="fresh",
                commit="abc123",
                source_fingerprint=source,
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


def test_prepare_local_wiki_keeps_embedding_secret_process_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import RepoManifest

    manifest_path = tmp_path / ".codenib_cache" / "repo_manifest.json"
    manifest_path.parent.mkdir()
    source = _test_source_fingerprint(tmp_path, manifest_path.parent)
    RepoManifest(
        repo_path=str(tmp_path),
        source_fingerprint=source,
        languages=["python"],
    ).save(str(manifest_path))
    monkeypatch.setenv("EMBEDDING_KEY", "embedding-secret")

    local = prepare_local_wiki(
        tmp_path,
        manifest_path,
        frontend_port=3000,
        embedding_api_key_env="EMBEDDING_KEY",
    )

    assert "embedding-secret" not in local.config_path.read_text()
    assert local.runtime_env["CODENIB_EMBEDDING_API_KEY"] == "embedding-secret"


def test_wiki_audit_exits_without_starting_frontend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    manifest = tmp_path / "repo_manifest.json"
    manifest.write_text("{}")
    prepared = SimpleNamespace(repo_id="sample")
    monkeypatch.setattr(cli, "_check_module", lambda _module: True)
    monkeypatch.setattr(cli, "resolve_manifest_path", lambda _value: manifest)
    monkeypatch.setattr(
        "codenib.web.local.prepare_local_wiki",
        lambda *_args, **_kwargs: prepared,
    )
    monkeypatch.setattr(
        cli,
        "_audit_local_wiki",
        lambda _local: {
            "repository": "owner/sample",
            "passed": True,
            "expected_pages": 2,
            "generated_pages": 2,
            "ready_pages": 2,
            "grounding_valid": 2,
            "structural_valid": 2,
            "narrative_valid": 2,
            "fallbacks": 0,
            "details": [],
        },
    )
    monkeypatch.setattr(
        "codenib.web.launcher.launch_local_wiki",
        lambda *_args, **_kwargs: pytest.fail("frontend should not start"),
    )

    result = cli.run(["wiki", str(tmp_path), "--no-index", "--audit"])

    output = capsys.readouterr().out
    assert result == 0
    assert "Ready:      2/2" in output
    assert "Result: PASS" in output


def test_wiki_generate_resolves_openai_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import IndexEntry, RepoManifest

    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    manifest_path = tmp_path / "repo_manifest.json"
    RepoManifest(
        repo_path=str(tmp_path),
        languages=["python"],
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(tmp_path / "bm25"),
                built_at="2026-08-04T00:00:00+00:00",
                built_at_epoch=0.0,
                status="fresh",
            )
        },
    ).save(manifest_path)
    captured = {}
    local = SimpleNamespace(runtime_env={})
    monkeypatch.setenv("MODELS_TOKEN", "runtime-secret")
    monkeypatch.setattr(cli, "_check_module", lambda _module: True)
    monkeypatch.setattr(cli, "resolve_manifest_path", lambda _value: manifest_path)

    def prepare(*_args, **kwargs):
        captured.update(kwargs)
        return local

    monkeypatch.setattr("codenib.web.local.prepare_local_wiki", prepare)
    monkeypatch.setattr(
        "codenib.web.launcher.launch_local_wiki",
        lambda *_args, **_kwargs: 0,
    )

    result = cli.run(
        [
            "wiki",
            str(tmp_path),
            "--no-index",
            "--generate",
            "--model-provider",
            "openai",
            "--model",
            "gpt-4.1",
            "--api-base",
            "https://inference.example.test/v1",
            "--api-key-env",
            "MODELS_TOKEN",
            "--no-open",
        ]
    )

    assert result == 0
    assert captured["model"] == "openai/gpt-4.1"
    assert captured["api_base"] == "https://inference.example.test/v1"
    assert captured["api_key_env"] == "MODELS_TOKEN"
    assert "runtime-secret" not in str(captured)


def test_wiki_rejects_embedding_route_that_disagrees_with_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from codenib.compiler.index_builders import VectorIndexBuilder
    from codenib.compiler.manifest import IndexEntry, RepoManifest

    (tmp_path / "sample.py").write_text("VALUE = 1\n")
    manifest_path = tmp_path / "repo_manifest.json"
    RepoManifest(
        repo_path=str(tmp_path),
        languages=["python"],
        indexes={
            "vector": IndexEntry(
                index_type="vector",
                path=str(tmp_path / "vector"),
                built_at="2026-08-04T00:00:00+00:00",
                built_at_epoch=0.0,
                status="fresh",
                config=VectorIndexBuilder().artifact_identity(),
            )
        },
    ).save(manifest_path)
    monkeypatch.setenv("OPENAI_API_KEY", "runtime-secret")
    monkeypatch.setattr(cli, "_check_module", lambda _module: True)
    monkeypatch.setattr(cli, "resolve_manifest_path", lambda _value: manifest_path)

    result = cli.run(
        [
            "wiki",
            str(tmp_path),
            "--no-index",
            "--embedding-provider",
            "openai",
        ]
    )

    assert result == 2
    error = capsys.readouterr().err
    assert "does not match the current vector artifact" in error
    assert "runtime-secret" not in error


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


@pytest.mark.parametrize(
    ("version", "expected_ok"),
    [
        ("v18.17.1", False),
        ("v20.18.1", False),
        ("v20.19.0", True),
        ("v21.7.3", False),
        ("v22.11.0", False),
        ("v22.12.0", True),
        ("v24.0.0", True),
    ],
)
def test_node_runtime_status_matches_vite_requirement(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    expected_ok: bool,
) -> None:
    monkeypatch.setattr(launcher.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, f"{version}\n", ""),
    )

    ok, detail = launcher.node_runtime_status()

    assert ok is expected_ok
    if expected_ok:
        assert detail == version
    else:
        assert detail == f"{version} (requires ^20.19.0 or >=22.12.0)"
