# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the agent-friendly find_related_code graph skill (#133)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codeminer.agent.skills.find_related_code.executor import (
    _candidates,
    _resolve,
    create_executor,
)


class _FakeGraph:
    """Minimal CodeGraph stand-in: name->vertex + successors/predecessors."""

    def __init__(self):
        self.name_to_vertex = {
            "pkg.mod.doWatch": 0,
            "pkg.mod.WatchHandle": 1,
            "pkg.other.caller": 2,
        }
        self._attrs = {
            0: {
                "name": "pkg.mod.doWatch",
                "type": "function",
                "file": "mod.py",
                "start_line": 10,
            },
            1: {
                "name": "pkg.mod.WatchHandle",
                "type": "class",
                "file": "mod.py",
                "start_line": 99,
            },
            2: {
                "name": "pkg.other.caller",
                "type": "function",
                "file": "other.py",
                "start_line": 5,
            },
        }
        self._succ = {"pkg.mod.doWatch": [1]}  # doWatch calls WatchHandle
        self._pred = {"pkg.mod.doWatch": [2]}  # caller calls doWatch

    def get_node_info_by_id(self, vid):
        return self._attrs.get(vid)

    def get_successors(self, name):
        return self._succ.get(name, [])

    def get_predecessors(self, name):
        return self._pred.get(name, [])


def test_fuzzy_resolve_bare_name():
    g = _FakeGraph()
    # bare "doWatch" resolves to the qualified name by suffix match
    assert _resolve(g, "doWatch") == "pkg.mod.doWatch"
    assert _resolve(g, "pkg.mod.doWatch") == "pkg.mod.doWatch"


def test_candidates_for_ambiguous():
    g = _FakeGraph()
    assert _candidates(g, "nonexistent") == []


def test_callers_and_callees_compact():
    g = _FakeGraph()
    ex = create_executor(SimpleNamespace(code_graph=g))
    res = ex(symbol="doWatch", relation="both", hops=1)
    names = {n.node_name for n in res}
    assert "pkg.mod.WatchHandle" in names  # callee
    assert "pkg.other.caller" in names  # caller
    # compact: relation marker, no code body, repo-relative file + node_id
    by = {n.node_name: n for n in res}
    assert by["pkg.other.caller"].content == "caller of pkg.mod.doWatch"
    assert by["pkg.other.caller"].file == "other.py"
    assert by["pkg.other.caller"].node_id == "other.py:pkg.other.caller"
    assert all(n.content and "of " in n.content for n in res)


def test_callers_only():
    g = _FakeGraph()
    ex = create_executor(SimpleNamespace(code_graph=g))
    res = ex(symbol="doWatch", relation="callers")
    assert {n.node_name for n in res} == {"pkg.other.caller"}


def test_unknown_symbol_raises_informative():
    g = _FakeGraph()
    ex = create_executor(SimpleNamespace(code_graph=g))
    with pytest.raises(ValueError, match="not found"):
        ex(symbol="totally_unknown_xyz")
