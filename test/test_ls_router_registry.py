# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for registry-driven LS router delegate selection."""

from __future__ import annotations

import pytest

from codeminer.graph.code_graph import CodeGraph
from codeminer.ls_router import LSGraphDecoder, LSIndexer


@pytest.mark.parametrize(
    ("language", "delegate_class"),
    [
        ("python", "SCIPPythonIndexer"),
        ("py", "SCIPPythonIndexer"),
        ("go", "SCIPGoIndexer"),
        ("golang", "SCIPGoIndexer"),
        ("rust", "SCIPRustIndexer"),
        ("rs", "SCIPRustIndexer"),
        ("typescript", "SCIPTypeScriptIndexer"),
        ("javascript", "SCIPTypeScriptIndexer"),
        ("cpp", "ClangdIndexer"),
        ("c", "ClangdIndexer"),
    ],
)
def test_ls_indexer_uses_registry_delegate(tmp_path, language, delegate_class):
    indexer = LSIndexer(tmp_path, language=language, output_dir=tmp_path / "out")

    assert indexer._delegate.__class__.__name__ == delegate_class


def test_ls_indexer_passes_decoder_backend_only_to_scip(tmp_path):
    py_indexer = LSIndexer(
        tmp_path / "py",
        language="python",
        output_dir=tmp_path / "py-out",
        decoder_backend="core",
    )
    assert py_indexer._delegate.decoder_backend == "core"

    cpp_indexer = LSIndexer(
        tmp_path / "cpp",
        language="cpp",
        output_dir=tmp_path / "cpp-out",
        decoder_backend="core",
    )

    assert cpp_indexer._delegate.__class__.__name__ == "ClangdIndexer"
    assert not hasattr(cpp_indexer._delegate, "decoder_backend")


@pytest.mark.parametrize(
    ("language", "delegate_class"),
    [
        ("python", "SCIPPythonGraphDecoder"),
        ("go", "SCIPGoGraphDecoder"),
        ("rust", "SCIPRustGraphDecoder"),
        ("typescript", "SCIPTypeScriptGraphDecoder"),
        ("javascript", "SCIPTypeScriptGraphDecoder"),
        ("cpp", "ClangdGraphDecoder"),
    ],
)
def test_ls_graph_decoder_uses_registry_delegate(tmp_path, language, delegate_class):
    decoder = LSGraphDecoder(
        str(tmp_path / "index.decoded"),
        project_root=str(tmp_path),
        language=language,
    )

    assert decoder._delegate.__class__.__name__ == delegate_class


def test_ls_indexer_graph_patch_constructs_graph_patcher_without_profiler_kwarg(
    tmp_path, monkeypatch
):
    indexer = LSIndexer(tmp_path, language="python", output_dir=tmp_path / "out")
    calls = {}

    from codeminer.graph.incremental import graph_patcher

    def fake_detect_changed_files(project_root, base_commit, target_commit, extensions):
        calls["detect"] = (project_root, base_commit, target_commit, extensions)
        return {"modified": [], "added": [], "deleted": [], "renamed": []}

    def fake_patch_files(self, changed_files):
        calls["patch"] = changed_files
        return {"ok": True}

    monkeypatch.setattr(
        graph_patcher.GraphPatcher,
        "detect_changed_files",
        staticmethod(fake_detect_changed_files),
    )
    monkeypatch.setattr(graph_patcher.GraphPatcher, "patch_files", fake_patch_files)

    result = indexer.graph_patch(CodeGraph(str(tmp_path)), "base", "HEAD")

    assert result == {"ok": True}
    assert calls["detect"][1:] == ("base", "HEAD", {".py"})
    assert calls["patch"] == {"modified": [], "added": [], "deleted": [], "renamed": []}
