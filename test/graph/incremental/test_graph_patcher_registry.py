# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for registry-driven GraphPatcher routing."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from codeminer.graph.code_graph import CodeGraph
from codeminer.graph.incremental.graph_patcher import GraphPatcher
from codeminer.graph.incremental.patcher_cpp import PatcherCpp
from codeminer.graph.incremental.patcher_go import PatcherGo
from codeminer.graph.incremental.patcher_python import PatcherPython
from codeminer.graph.incremental.patcher_rust import PatcherRust
from codeminer.graph.incremental.patcher_ts import PatcherTS


@pytest.mark.parametrize(
    ("language", "module_suffix", "class_name"),
    [
        ("python", "patcher_python", "PatcherPython"),
        ("py", "patcher_python", "PatcherPython"),
        ("go", "patcher_go", "PatcherGo"),
        ("golang", "patcher_go", "PatcherGo"),
        ("rust", "patcher_rust", "PatcherRust"),
        ("rs", "patcher_rust", "PatcherRust"),
        ("cpp", "patcher_cpp", "PatcherCpp"),
        ("c", "patcher_cpp", "PatcherCpp"),
        ("typescript", "patcher_ts", "PatcherTS"),
        ("javascript", "patcher_ts", "PatcherTS"),
        ("js", "patcher_ts", "PatcherTS"),
    ],
)
def test_graph_patcher_routes_through_language_registry(
    tmp_path, language, module_suffix, class_name
):
    patcher = GraphPatcher(str(tmp_path), None, language)

    assert patcher._impl.__class__.__name__ == class_name
    assert patcher._impl.__class__.__module__.endswith(module_suffix)
    assert patcher.workspace_warmup_idle is None


def test_graph_patcher_rejects_unregistered_language(tmp_path):
    with pytest.raises(ValueError, match="Unsupported language: java"):
        GraphPatcher(str(tmp_path), None, "java")


@pytest.mark.parametrize(
    ("patcher_cls", "command", "language_id"),
    [
        (PatcherPython, ["basedpyright-langserver", "--stdio"], "python"),
        (PatcherGo, ["gopls", "serve"], "go"),
        (PatcherTS, ["typescript-language-server", "--stdio"], "typescript"),
        (PatcherCpp, ["clangd"], "cpp"),
    ],
)
def test_patcher_lsp_metadata_comes_from_language_registry(
    tmp_path, patcher_cls, command, language_id
):
    patcher = patcher_cls(str(tmp_path), CodeGraph(str(tmp_path)))

    assert patcher.get_lsp_command() == command
    assert patcher._language_id() == language_id


def test_python_patcher_lsp_command_allows_registry_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEMINER_PYTHON_LSP_CMD", "ty server")

    patcher = PatcherPython(str(tmp_path), CodeGraph(str(tmp_path)))

    assert patcher.get_lsp_command() == ["ty", "server"]


def test_rust_patcher_lsp_command_uses_registry_factory(tmp_path, monkeypatch):
    import codeminer.scip_interface.rust_analyzer as rust_analyzer

    monkeypatch.setattr(
        rust_analyzer,
        "rust_analyzer_lsp_command",
        lambda *args: ["/toolchains/stable/bin/rust-analyzer", *args],
    )

    patcher = PatcherRust(str(tmp_path), CodeGraph(str(tmp_path)))

    assert patcher.get_lsp_command() == [
        "/toolchains/stable/bin/rust-analyzer",
    ]
    assert patcher._language_id() == "rust"


def test_rust_lsp_command_resolves_fixed_analyzer_binary(tmp_path, monkeypatch):
    import codeminer.scip_interface.rust_analyzer as rust_analyzer

    binary = tmp_path / "toolchains" / "nightly" / "bin" / "rust-analyzer"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        rust_analyzer.shutil,
        "which",
        lambda command: "/usr/bin/rustup" if command == "rustup" else None,
    )
    monkeypatch.setattr(
        rust_analyzer.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=f"{binary}\n"),
    )

    assert rust_analyzer.rust_analyzer_lsp_command("--version") == [
        str(binary),
        "--version",
    ]


def test_lsp_client_command_lookup_uses_language_registry(monkeypatch):
    from codeminer.graph.incremental import lsp_client

    monkeypatch.setattr(lsp_client, "resolve_lsp_binary", lambda _binary: None)
    monkeypatch.setenv("CODEMINER_PYTHON_LSP_CMD", "ty server")

    assert lsp_client.LSPClient.get_lsp_command("python") == ["ty", "server"]
    assert lsp_client.LSPClient.get_lsp_command("go") == ["gopls", "serve"]
    assert lsp_client.LSPClient.get_lsp_command("java") == ["jdtls"]
    assert lsp_client.LSPClient.get_lsp_command("csharp") == ["csharp-ls"]
    assert lsp_client.LSPClient.get_lsp_command("ruby") == ["ruby-lsp"]
    assert lsp_client.LSPClient.get_lsp_command("php") == ["intelephense", "--stdio"]
    assert lsp_client.LSPClient.get_lsp_command("kotlin") == [
        "kotlin-language-server",
        "--stdio",
    ]


def test_lsp_binary_resolver_searches_dotnet_global_tools():
    from codeminer.graph.incremental import lsp_client

    extra_dirs = {str(dir_fn()) for dir_fn in lsp_client._EXTRA_BIN_DIRS}

    assert str(lsp_client.Path.home() / ".dotnet" / "tools") in extra_dirs


def test_lsp_process_env_exposes_user_dotnet_install(tmp_path, monkeypatch):
    from codeminer.graph.incremental import lsp_client

    dotnet_root = tmp_path / ".dotnet"
    dotnet_root.mkdir()
    (dotnet_root / "tools").mkdir()
    (dotnet_root / "dotnet").write_text("", encoding="utf-8")

    monkeypatch.delenv("DOTNET_ROOT", raising=False)
    monkeypatch.delenv("DOTNET_ROOT_X64", raising=False)
    monkeypatch.setattr(lsp_client.Path, "home", classmethod(lambda cls: tmp_path))

    env = lsp_client._lsp_process_env("csharp")

    assert env["DOTNET_ROOT"] == str(dotnet_root)
    assert env["DOTNET_ROOT_X64"] == str(dotnet_root)
    assert env["PATH"].split(os.pathsep)[:2] == [
        str(dotnet_root),
        str(dotnet_root / "tools"),
    ]


def test_rust_lsp_env_honors_project_toolchain_by_default(tmp_path, monkeypatch):
    from codeminer.graph.incremental import lsp_client

    monkeypatch.setenv("RUSTUP_TOOLCHAIN", "nightly")
    monkeypatch.delenv("CODEMINER_RUST_PROJECT_TOOLCHAIN", raising=False)

    env = lsp_client._lsp_process_env("rust", tmp_path)

    assert "RUSTUP_TOOLCHAIN" not in env


def test_rust_lsp_env_allows_explicit_project_toolchain(tmp_path, monkeypatch):
    from codeminer.graph.incremental import lsp_client

    monkeypatch.setenv("CODEMINER_RUST_PROJECT_TOOLCHAIN", "stable")

    env = lsp_client._lsp_process_env("rust", tmp_path)

    assert env["RUSTUP_TOOLCHAIN"] == "stable"


def test_lsp_process_env_uses_ruby_overlay_bundle_gemfile(tmp_path, monkeypatch):
    from codeminer.graph.incremental import lsp_client

    monkeypatch.setenv("CODEMINER_RUBY_BUNDLE_GEMFILE", ".codeminer/Gemfile")
    monkeypatch.setenv("GEM_PATH", "/tmp/global-gems")

    env = lsp_client._lsp_process_env("ruby", tmp_path)

    assert env["BUNDLE_GEMFILE"] == str(tmp_path / ".codeminer/Gemfile")
    assert "GEM_PATH" not in env


def test_patcher_applies_uniform_strict_request_budget(tmp_path, monkeypatch):
    from codeminer.graph.incremental import lsp_client, patcher_base

    clients = []

    class FakeLSPClient:
        def __init__(self, *_args, **kwargs):
            self.max_in_flight_requests = kwargs["max_in_flight_requests"]
            clients.append(self)

        def start(self, *, skip_probe=False):
            self.skip_probe = skip_probe

    monkeypatch.setattr(lsp_client, "resolve_lsp_binary", lambda _binary: None)
    monkeypatch.setattr(patcher_base, "LSPClient", FakeLSPClient)

    patcher = PatcherPython(
        str(tmp_path),
        CodeGraph(str(tmp_path)),
        reference_timeout_s=81.0,
        reference_retries=3,
        reference_concurrency=7,
        strict_lsp_requests=True,
    )
    patcher.start_lsp(skip_probe=True)

    client = clients[0]
    assert client.skip_probe is True
    assert client.strict_request_failures is True
    assert client.operation_retries == 3
    assert client.reference_retries == 3
    assert client.max_in_flight_requests == 7
    assert client.reference_timeout_s == 81.0
    assert client.document_symbol_timeout_s == 81.0
    assert client.definition_timeout_s == 81.0
    assert client.semantic_tokens_timeout_s == 81.0
