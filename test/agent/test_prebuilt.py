# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the prebuilt-index staging helper (issue #133)."""

from __future__ import annotations

import importlib.util
import os
import pickle
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "prebuilt",
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "agent_compile"
    / "lib"
    / "prebuilt.py",
)
prebuilt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(prebuilt)


def test_model_suffix():
    assert (
        prebuilt.model_suffix("Qwen/Qwen3-Embedding-0.6B")
        == "Qwen__Qwen3-Embedding-0.6B"
    )
    assert prebuilt.model_suffix("nomic-ai/CodeRankEmbed") == "nomic-ai__CodeRankEmbed"


def _make_prebuilt(root: Path, iid: str, model: str, *, full=True):
    d = root / iid
    (d / "repo").mkdir(parents=True)
    (d / "graph.pkl").write_text("x")
    suffix = prebuilt.model_suffix(model)
    l2 = d / "l2"
    l2.mkdir()
    (l2 / f"index_{suffix}.faiss").write_text("x")
    if full:
        (l2 / f"documents_{suffix}.pkl").write_text("x")
    return d


def _write_legacy_graph(path: Path):
    from codeminer.graph.code_graph import CodeGraph

    graph = CodeGraph(project_root="repo")
    graph.add_file_node("pkg/mod.py")
    graph.add_symbol_node(
        "pkg.mod.foo",
        line=0,
        scope_start_line=0,
        scope_end_line=2,
        symbol_type="function",
    )
    graph.graph.vs[graph.name_to_vertex["pkg.mod.foo"]][
        "unified_name"
    ] = "pkg/mod.py:foo()"
    with path.open("wb") as f:
        pickle.dump(
            {
                "project_root": graph.project_root,
                "graph": graph.graph,
                "symbol_ranges": graph.symbol_ranges,
                "name_to_vertex": graph.name_to_vertex,
            },
            f,
        )


def test_has_full_indexes_true_false(tmp_path):
    model = "Qwen/Qwen3-Embedding-0.6B"
    _make_prebuilt(tmp_path, "good", model, full=True)
    _make_prebuilt(tmp_path, "partial", model, full=False)
    assert prebuilt.has_full_indexes(str(tmp_path), "good", model) is True
    assert prebuilt.has_full_indexes(str(tmp_path), "partial", model) is False
    assert prebuilt.has_full_indexes(str(tmp_path), "missing", model) is False


def test_repo_and_instance_paths(tmp_path):
    assert prebuilt.instance_dir(str(tmp_path), "i").endswith("/i")
    assert prebuilt.repo_path_for(str(tmp_path), "i").endswith("/i/repo")


def test_stage_creates_symlinks_without_bm25(tmp_path):
    model = "Qwen/Qwen3-Embedding-0.6B"
    src = _make_prebuilt(tmp_path / "prebuilt", "inst", model, full=True)
    cache = tmp_path / "cache" / "inst"
    # build_bm25=False so the test needs no real graph/index machinery
    out = prebuilt.stage_prebuilt_indexes(
        str(tmp_path / "prebuilt"), "inst", str(cache), build_bm25=False
    )
    assert out == str(cache)
    # symbol_graph/graph.pkl -> prebuilt graph.pkl
    sg = cache / "symbol_graph" / "graph.pkl"
    assert sg.is_symlink()
    assert os.path.realpath(sg) == os.path.realpath(src / "graph.pkl")
    # vector -> instance dir (so CodeVectorStore.load scans l0/l2)
    vec = cache / "vector"
    assert vec.is_symlink()
    assert os.path.realpath(vec) == os.path.realpath(src)


def test_stage_is_idempotent(tmp_path):
    model = "Qwen/Qwen3-Embedding-0.6B"
    _make_prebuilt(tmp_path / "prebuilt", "inst", model, full=True)
    cache = tmp_path / "cache" / "inst"
    prebuilt.stage_prebuilt_indexes(
        str(tmp_path / "prebuilt"), "inst", str(cache), build_bm25=False
    )
    # second call must not raise on existing symlinks
    prebuilt.stage_prebuilt_indexes(
        str(tmp_path / "prebuilt"), "inst", str(cache), build_bm25=False
    )
    assert (cache / "vector").is_symlink()


def test_stage_missing_instance_raises(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        prebuilt.stage_prebuilt_indexes(
            str(tmp_path), "nope", str(tmp_path / "c"), build_bm25=False
        )


def test_load_prebuilt_code_graph_accepts_legacy_bundle(tmp_path):
    model = "Qwen/Qwen3-Embedding-0.6B"
    src = _make_prebuilt(tmp_path / "prebuilt", "inst", model, full=True)
    _write_legacy_graph(src / "graph.pkl")

    graph = prebuilt.load_prebuilt_code_graph(str(tmp_path / "prebuilt"), "inst")

    assert graph.graph.vcount() == 2
    assert graph.name_to_vertex["pkg.mod.foo"] == 1
    assert graph._file_nodes["pkg/mod.py"]
    assert graph._unified_to_names["pkg/mod.py:foo()"] == ["pkg.mod.foo"]


def test_load_prebuilt_code_graph_accepts_direct_codegraph_pickle(tmp_path):
    from codeminer.graph.code_graph import CodeGraph

    model = "Qwen/Qwen3-Embedding-0.6B"
    src = _make_prebuilt(tmp_path / "prebuilt", "inst", model, full=True)
    graph = CodeGraph(project_root="repo")
    graph.add_file_node("pkg/direct.py")
    with (src / "graph.pkl").open("wb") as f:
        pickle.dump(graph, f)

    loaded = prebuilt.load_prebuilt_code_graph(str(tmp_path / "prebuilt"), "inst")

    assert loaded.graph.vcount() == 1
    assert loaded.name_to_vertex["pkg/direct.py"] == 0


def test_stage_normalizes_legacy_graph_for_strict_loader(tmp_path):
    from codeminer.graph.code_graph import CodeGraph

    model = "Qwen/Qwen3-Embedding-0.6B"
    src = _make_prebuilt(tmp_path / "prebuilt", "inst", model, full=True)
    _write_legacy_graph(src / "graph.pkl")
    cache = tmp_path / "cache" / "inst"

    prebuilt.stage_prebuilt_indexes(str(tmp_path / "prebuilt"), "inst", str(cache))

    staged = cache / "symbol_graph" / "graph.pkl"
    assert staged.exists()
    assert not staged.is_symlink()
    loaded = CodeGraph.load_graph(staged)
    assert loaded.graph.vcount() == 2
    assert (cache / "bm25").is_dir()
