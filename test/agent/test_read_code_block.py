# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the read_code_block skill (LocAgent-style node read, #133)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import igraph as ig
import pytest

from codeminer.agent.skills.read_code_block.executor import create_executor
from codeminer.graph.code_graph import CodeGraph


def _graph_for(rel_file: str) -> CodeGraph:
    g = CodeGraph()
    gg = ig.Graph(directed=True)
    gg.add_vertices(1)
    gg.vs["name"] = ["hashA"]
    gg.vs["unified_name"] = [f"{rel_file}:foo()"]
    gg.vs["type"] = ["function"]
    gg.vs["file"] = [rel_file]
    gg.vs["start_line"] = [2]  # 1-based span -> lines 2..4
    gg.vs["end_line"] = [4]
    g.graph = gg
    g.name_to_vertex = {"hashA": 0}
    g._unified_to_names = {}
    return g


def test_reads_symbol_block(tmp_path, monkeypatch):
    src = tmp_path / "mod.py"
    src.write_text("L1\ndef foo():\n    return 1\n# end foo\nL5\n")
    monkeypatch.chdir(tmp_path)  # runner sets cwd to the repo
    ex = create_executor(SimpleNamespace(code_graph=_graph_for("mod.py")))
    out = ex(symbol="foo")
    assert "mod.py:2-4" in out
    assert "def foo():" in out and "return 1" in out
    assert "L1" not in out and "L5" not in out  # only the node span


def test_unresolved_symbol_raises(tmp_path, monkeypatch):
    (tmp_path / "mod.py").write_text("def foo(): pass\n")
    monkeypatch.chdir(tmp_path)
    ex = create_executor(SimpleNamespace(code_graph=_graph_for("mod.py")))
    with pytest.raises(ValueError, match="unresolved"):
        ex(symbol="not_a_symbol")


def test_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # mod.py does not exist here
    ex = create_executor(SimpleNamespace(code_graph=_graph_for("mod.py")))
    with pytest.raises(ValueError, match="file not found"):
        ex(symbol="foo")


def test_no_graph_raises():
    ex = create_executor(SimpleNamespace(code_graph=None))
    with pytest.raises(RuntimeError, match="not available"):
        ex(symbol="foo")
    assert os.getcwd()  # sanity: cwd intact
