# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for registry-driven GraphPatcher routing."""

from __future__ import annotations

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
        "rust_analyzer_command",
        lambda *args: ["rustup", "run", "stable", "rust-analyzer", *args],
    )

    patcher = PatcherRust(str(tmp_path), CodeGraph(str(tmp_path)))

    assert patcher.get_lsp_command() == [
        "rustup",
        "run",
        "stable",
        "rust-analyzer",
    ]
    assert patcher._language_id() == "rust"


def test_lsp_client_command_lookup_uses_language_registry(monkeypatch):
    from codeminer.graph.incremental import lsp_client

    monkeypatch.setattr(lsp_client, "resolve_lsp_binary", lambda _binary: None)
    monkeypatch.setenv("CODEMINER_PYTHON_LSP_CMD", "ty server")

    assert lsp_client.LSPClient.get_lsp_command("python") == ["ty", "server"]
    assert lsp_client.LSPClient.get_lsp_command("go") == ["gopls", "serve"]
    assert lsp_client.LSPClient.get_lsp_command("java") is None
