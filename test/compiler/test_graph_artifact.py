# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from unittest.mock import patch

import pytest

import codenib.compiler.graph_artifact as graph_artifact_module
from codenib.compiler.artifact_fingerprints import regular_file_fingerprint
from codenib.compiler.graph_artifact import (
    authenticated_graph_artifact_matches,
    load_authenticated_graph_artifact,
)
from codenib.compiler.manifest import IndexEntry
from codenib.graph.code_graph import CodeGraph
from codenib.scip_interface.lsp_occurrence_index import (
    SCIPOccurrence,
    SCIPOccurrenceIndex,
)


def _entry(root, graph_path, occurrence_path=None) -> IndexEntry:
    config = {
        "graph_artifact": {
            "relative_path": "graph.pkl",
            **regular_file_fingerprint(graph_path),
        },
        "lsp_occurrence_artifact": None,
    }
    if occurrence_path is not None:
        config["lsp_occurrence_artifact"] = {
            "relative_path": "lsp_index.pkl",
            **regular_file_fingerprint(occurrence_path),
        }
    return IndexEntry(
        index_type="symbol_graph",
        path=str(root),
        built_at="2026-08-20T00:00:00Z",
        built_at_epoch=0.0,
        status="fresh",
        config=config,
    )


def _write_graph(path, *, file_path: str) -> None:
    graph = CodeGraph(path.parent)
    graph.add_root_node(".")
    graph.add_file_node(file_path)
    graph.save_graph(path)


def _write_occurrence_index(path, *, file_path: str, symbol: str) -> None:
    SCIPOccurrenceIndex([SCIPOccurrence(file_path, 1, 2, 1, 8, symbol, 1)]).save(path)


def test_load_authenticated_graph_artifact_uses_receipted_generation(tmp_path):
    root = tmp_path / "symbol_graph"
    root.mkdir()
    graph_path = root / "graph.pkl"
    _write_graph(graph_path, file_path="src/current.py")

    loaded = load_authenticated_graph_artifact(_entry(root, graph_path))

    assert "src/current.py" in loaded.graph.vs["name"]


def test_load_authenticated_graph_artifact_accepts_legacy_direct_graph_path(
    tmp_path,
):
    root = tmp_path / "symbol_graph"
    root.mkdir()
    graph_path = root / "graph.pkl"
    _write_graph(graph_path, file_path="src/legacy.py")
    entry = _entry(root, graph_path)
    entry.path = str(graph_path)

    loaded = load_authenticated_graph_artifact(entry)

    assert "src/legacy.py" in loaded.graph.vs["name"]
    assert authenticated_graph_artifact_matches(entry) is True


def test_load_authenticated_graph_artifact_attaches_receipted_occurrence_index(
    tmp_path,
):
    root = tmp_path / "symbol_graph"
    root.mkdir()
    graph_path = root / "graph.pkl"
    occurrence_path = root / "lsp_index.pkl"
    _write_graph(graph_path, file_path="src/current.py")
    _write_occurrence_index(
        occurrence_path,
        file_path="src/current.py",
        symbol="scip-python fixture current().",
    )

    loaded = load_authenticated_graph_artifact(
        _entry(root, graph_path, occurrence_path)
    )

    occurrence = loaded.lsp_occurrence_index.occurrence_at(
        "src/current.py",
        1,
        3,
    )
    assert occurrence is not None
    assert occurrence.symbol == "scip-python fixture current()."


def test_load_authenticated_graph_artifact_ignores_unreceipted_occurrence_index(
    tmp_path,
):
    root = tmp_path / "symbol_graph"
    root.mkdir()
    graph_path = root / "graph.pkl"
    occurrence_path = root / "lsp_index.pkl"
    _write_graph(graph_path, file_path="src/current.py")
    _write_occurrence_index(
        occurrence_path,
        file_path="private/ambient.py",
        symbol="scip-python fixture ambient().",
    )

    with patch.object(
        SCIPOccurrenceIndex,
        "load_stream",
        side_effect=AssertionError("unreceipted occurrence pickle must not be loaded"),
    ):
        loaded = load_authenticated_graph_artifact(_entry(root, graph_path))

    assert not hasattr(loaded, "lsp_occurrence_index")


def test_load_authenticated_graph_artifact_rejects_occurrence_receipt_mismatch(
    tmp_path,
):
    root = tmp_path / "symbol_graph"
    root.mkdir()
    graph_path = root / "graph.pkl"
    occurrence_path = root / "lsp_index.pkl"
    _write_graph(graph_path, file_path="src/current.py")
    _write_occurrence_index(
        occurrence_path,
        file_path="src/current.py",
        symbol="scip-python fixture expected().",
    )
    entry = _entry(root, graph_path, occurrence_path)
    _write_occurrence_index(
        occurrence_path,
        file_path="private/replacement.py",
        symbol="scip-python fixture replacement().",
    )

    with (
        patch.object(
            graph_artifact_module.CodeGraph,
            "load_graph_stream",
            side_effect=AssertionError("graph pickle must wait for all receipts"),
        ),
        patch.object(
            SCIPOccurrenceIndex,
            "load_stream",
            side_effect=AssertionError("mismatched occurrence pickle must not load"),
        ),
        pytest.raises(ValueError, match="LSP occurrence artifact.*manifest receipt"),
    ):
        load_authenticated_graph_artifact(entry)
    assert authenticated_graph_artifact_matches(entry) is False


def test_load_authenticated_graph_artifact_rejects_occurrence_generation_swap(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "symbol_graph"
    root.mkdir()
    graph_path = root / "graph.pkl"
    occurrence_path = root / "lsp_index.pkl"
    _write_graph(graph_path, file_path="src/current.py")
    _write_occurrence_index(
        occurrence_path,
        file_path="src/current.py",
        symbol="scip-python fixture expected().",
    )
    entry = _entry(root, graph_path, occurrence_path)
    real_reopen = graph_artifact_module.reopen_authenticated_directory

    def replace_then_reopen(path, ownership, callback):
        occurrence_path.unlink()
        _write_occurrence_index(
            occurrence_path,
            file_path="private/replacement.py",
            symbol="scip-python fixture replacement().",
        )
        return real_reopen(path, ownership, callback)

    monkeypatch.setattr(
        graph_artifact_module,
        "reopen_authenticated_directory",
        replace_then_reopen,
    )
    with (
        patch.object(
            graph_artifact_module.CodeGraph,
            "load_graph_stream",
            side_effect=AssertionError("graph pickle must wait for one generation"),
        ),
        patch.object(
            SCIPOccurrenceIndex,
            "load_stream",
            side_effect=AssertionError("swapped occurrence pickle must not load"),
        ),
        pytest.raises((RuntimeError, ValueError)),
    ):
        load_authenticated_graph_artifact(entry)


def test_load_authenticated_graph_artifact_rejects_replacement_before_unpickle(
    tmp_path,
):
    root = tmp_path / "symbol_graph"
    root.mkdir()
    graph_path = root / "graph.pkl"
    _write_graph(graph_path, file_path="src/expected.py")
    entry = _entry(root, graph_path)
    _write_graph(graph_path, file_path="private/replacement.py")

    with (
        patch.object(
            graph_artifact_module.CodeGraph,
            "load_graph_stream",
            side_effect=AssertionError("unverified pickle must not be loaded"),
        ),
        pytest.raises(ValueError, match="manifest receipt"),
    ):
        load_authenticated_graph_artifact(entry)


def test_load_authenticated_graph_artifact_rejects_capture_to_open_swap(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "symbol_graph"
    root.mkdir()
    graph_path = root / "graph.pkl"
    _write_graph(graph_path, file_path="src/expected.py")
    entry = _entry(root, graph_path)
    real_reopen = graph_artifact_module.reopen_authenticated_directory

    def replace_then_reopen(path, ownership, callback):
        graph_path.unlink()
        _write_graph(graph_path, file_path="private/replacement.py")
        return real_reopen(path, ownership, callback)

    monkeypatch.setattr(
        graph_artifact_module,
        "reopen_authenticated_directory",
        replace_then_reopen,
    )
    with (
        patch.object(
            graph_artifact_module.CodeGraph,
            "load_graph_stream",
            side_effect=AssertionError("swapped pickle must not be loaded"),
        ),
        pytest.raises((RuntimeError, ValueError)),
    ):
        load_authenticated_graph_artifact(entry)


def test_load_authenticated_graph_artifact_rejects_symlink(tmp_path):
    root = tmp_path / "symbol_graph"
    root.mkdir()
    target = tmp_path / "outside.pkl"
    _write_graph(target, file_path="private/outside.py")
    graph_path = root / "graph.pkl"
    graph_path.symlink_to(target)
    entry = IndexEntry(
        index_type="symbol_graph",
        path=str(root),
        built_at="2026-08-20T00:00:00Z",
        built_at_epoch=0.0,
        status="fresh",
        config={
            "graph_artifact": {
                "relative_path": "graph.pkl",
                **regular_file_fingerprint(target),
            }
        },
    )

    with pytest.raises((RuntimeError, ValueError)):
        load_authenticated_graph_artifact(entry)
