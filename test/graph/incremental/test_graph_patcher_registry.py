# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for registry-driven GraphPatcher routing."""

from __future__ import annotations

import os

import pytest

from codenib.graph.code_graph import CodeGraph
from codenib.graph.incremental.graph_patcher import GraphPatcher
from codenib.graph.incremental.patcher_cpp import PatcherCpp
from codenib.graph.incremental.patcher_go import PatcherGo
from codenib.graph.incremental.patcher_python import PatcherPython
from codenib.graph.incremental.patcher_rust import PatcherRust
from codenib.graph.incremental.patcher_ts import PatcherTS
from codenib.types import EDGE_TYPE_REFERENCE


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


def test_cpp_patch_rebuilds_range_indexes(tmp_path, monkeypatch):
    graph = CodeGraph(str(tmp_path))
    graph.add_file_node("target.cpp")
    graph._add_vertex(
        "target.cpp:target",
        {
            "type": "function",
            "file": "target.cpp",
            "start_line": 0,
            "end_line": 2,
            "unified_name": "target.cpp:target()",
        },
    )
    graph.symbol_ranges["target.cpp:target"] = (0, 2)
    graph.build_range_indexes()

    patcher = PatcherCpp(str(tmp_path), graph)

    idx_dir = tmp_path / ".cache" / "clangd" / "index"
    idx_dir.mkdir(parents=True)
    monkeypatch.setattr(
        patcher,
        "_reindex_changed_files",
        lambda changed_files: idx_dir,
    )

    def fake_incremental_update_idx(changed_files, *, idx_dir=None):
        assert changed_files == ["new.cpp"]
        assert idx_dir == tmp_path / ".cache" / "clangd" / "index"
        graph.add_file_node("new.cpp")
        graph._add_vertex(
            "new.cpp:caller",
            {
                "type": "function",
                "file": "new.cpp",
                "start_line": 3,
                "end_line": 8,
                "unified_name": "new.cpp:caller()",
            },
        )
        graph.symbol_ranges["new.cpp:caller"] = (3, 8)
        graph._add_edge(
            "new.cpp:caller",
            "target.cpp:target",
            EDGE_TYPE_REFERENCE,
            anchor_file="new.cpp",
            anchor_line=5,
        )
        return {"vertices_created": 2, "refs_added": 1}

    monkeypatch.setattr(
        patcher,
        "_incremental_update_idx",
        fake_incremental_update_idx,
    )

    patcher.patch_files(
        {
            "modified": [],
            "added": ["new.cpp"],
            "deleted": [],
            "renamed": [],
        }
    )

    result = graph.query_range("new.cpp", 5, 5)
    assert [node.name for node in result.defined] == ["new.cpp:caller"]
    assert len(result.outgoing) == 1
    assert result.outgoing[0].anchor_file == "new.cpp"
    assert result.outgoing[0].anchor_line == 5


@pytest.mark.parametrize("failure_stage", ["reindex", "apply"])
def test_cpp_patch_failure_preserves_original_graph(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    graph = CodeGraph(str(tmp_path))
    graph.add_file_node("source.cpp")
    graph._add_vertex(
        "source.cpp:kept",
        {
            "type": "function",
            "file": "source.cpp",
            "start_line": 1,
            "end_line": 4,
            "unified_name": "source.cpp:kept()",
        },
    )
    graph.symbol_ranges["source.cpp:kept"] = (1, 4)
    graph.build_range_indexes()
    original_names = list(graph.graph.vs["name"])
    original_ranges = dict(graph.symbol_ranges)

    patcher = PatcherCpp(str(tmp_path), graph)
    idx_dir = tmp_path / ".cache" / "clangd" / "index"
    idx_dir.mkdir(parents=True)
    monkeypatch.setattr(
        patcher,
        "_reindex_changed_files",
        lambda _changed_files: None if failure_stage == "reindex" else idx_dir,
    )
    if failure_stage == "apply":

        def fail_incremental_update(*_args, **_kwargs):
            raise RuntimeError("decode failed")

        monkeypatch.setattr(
            patcher,
            "_incremental_update_idx",
            fail_incremental_update,
        )

    with pytest.raises(RuntimeError):
        patcher.patch_files(
            {
                "modified": ["source.cpp"],
                "added": [],
                "deleted": [],
                "renamed": [],
            }
        )

    assert list(graph.graph.vs["name"]) == original_names
    assert graph.symbol_ranges == original_ranges
    result = graph.query_range("source.cpp", 2, 2)
    assert [node.name for node in result.defined] == ["source.cpp:kept"]


def test_python_patcher_lsp_command_allows_registry_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CODENIB_PYTHON_LSP_CMD", "ty server")

    patcher = PatcherPython(str(tmp_path), CodeGraph(str(tmp_path)))

    assert patcher.get_lsp_command() == ["ty", "server"]


def test_rust_patcher_lsp_command_uses_registry_factory(tmp_path, monkeypatch):
    import codenib.scip_interface.rust_analyzer as rust_analyzer

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
    from codenib.graph.incremental import lsp_client

    monkeypatch.setattr(lsp_client, "resolve_lsp_binary", lambda _binary: None)
    monkeypatch.setenv("CODENIB_PYTHON_LSP_CMD", "ty server")

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
    from codenib.graph.incremental import lsp_client

    extra_dirs = {str(dir_fn()) for dir_fn in lsp_client._EXTRA_BIN_DIRS}

    assert str(lsp_client.Path.home() / ".dotnet" / "tools") in extra_dirs


def test_lsp_process_env_exposes_user_dotnet_install(tmp_path, monkeypatch):
    from codenib.graph.incremental import lsp_client

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


def test_lsp_process_env_uses_ruby_overlay_bundle_gemfile(tmp_path, monkeypatch):
    from codenib.graph.incremental import lsp_client

    monkeypatch.setenv("CODENIB_RUBY_BUNDLE_GEMFILE", ".codenib/Gemfile")
    monkeypatch.setenv("GEM_PATH", "/tmp/global-gems")

    env = lsp_client._lsp_process_env("ruby", tmp_path)

    assert env["BUNDLE_GEMFILE"] == str(tmp_path / ".codenib/Gemfile")
    assert "GEM_PATH" not in env
