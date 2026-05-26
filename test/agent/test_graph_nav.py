# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared call-graph navigation engine (#133).

Covers find_callers / find_callees (neighbors) and trace, plus fuzzy
resolution and ambiguity handling.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codeminer.agent.skills import _graphnav


class _FakeGraph:
    def __init__(self):
        self.name_to_vertex = {
            "pkg.a.doWatch": 0,
            "pkg.a.watchEffect": 1,
            "pkg.b.helper": 2,
            "pkg.c.notify": 3,  # ambiguous base "notify" with the next
            "pkg.d.notify": 4,
        }
        self._attr = {
            0: {
                "name": "pkg.a.doWatch",
                "type": "function",
                "file": "a.py",
                "start_line": 10,
            },
            1: {
                "name": "pkg.a.watchEffect",
                "type": "function",
                "file": "a.py",
                "start_line": 5,
            },
            2: {
                "name": "pkg.b.helper",
                "type": "function",
                "file": "b.py",
                "start_line": 3,
            },
            3: {
                "name": "pkg.c.notify",
                "type": "function",
                "file": "c.py",
                "start_line": 1,
            },
            4: {
                "name": "pkg.d.notify",
                "type": "function",
                "file": "d.py",
                "start_line": 1,
            },
        }
        # watchEffect -> doWatch -> helper
        self._succ = {"pkg.a.watchEffect": [0], "pkg.a.doWatch": [2]}
        self._pred = {"pkg.a.doWatch": [1], "pkg.b.helper": [0]}
        self.graph = SimpleNamespace(get_shortest_paths=self._gsp)

    def _gsp(self, v, to=None, mode="out"):
        # watchEffect(1) -> doWatch(0) -> helper(2)
        chain = {(1, 0): [[1, 0]], (1, 2): [[1, 0, 2]], (0, 2): [[0, 2]]}
        return chain.get((v, to), [[]])

    def get_node_info_by_name(self, name):
        return self._attr.get(self.name_to_vertex.get(name))

    def get_node_info_by_id(self, vid):
        return self._attr.get(vid)

    def get_successors(self, name):
        return self._succ.get(name, [])

    def get_predecessors(self, name):
        return self._pred.get(name, [])


def test_resolve_unique_and_ambiguous():
    g = _FakeGraph()
    assert _graphnav.resolve(g, "doWatch")[0] == "pkg.a.doWatch"
    name, cands = _graphnav.resolve(g, "notify")  # two *.notify -> ambiguous
    assert name is None and len(cands) == 2


def test_find_callers_compact():
    g = _FakeGraph()
    res = _graphnav.neighbors(g, "doWatch", "callers")
    assert {n.node_name for n in res} == {"pkg.a.watchEffect"}
    n = res[0]
    assert n.content == "caller of pkg.a.doWatch"
    assert n.file == "a.py" and n.node_id == "a.py:pkg.a.watchEffect"
    assert n.content is not None  # marker only, no source body field populated


def test_find_callees_compact():
    g = _FakeGraph()
    res = _graphnav.neighbors(g, "doWatch", "callees")
    assert {n.node_name for n in res} == {"pkg.b.helper"}


def test_neighbors_unresolved_raises():
    g = _FakeGraph()
    with pytest.raises(ValueError, match="not found"):
        _graphnav.neighbors(g, "totally_absent_xyz", "callers")


def test_trace_shortest_path():
    g = _FakeGraph()
    path = _graphnav.trace(g, "watchEffect", "helper")
    names = [n.node_name for n in path]
    assert names == ["pkg.a.watchEffect", "pkg.a.doWatch", "pkg.b.helper"]
    assert path[0].content.startswith("hop 0")


def test_trace_ambiguous_endpoint_raises():
    g = _FakeGraph()
    with pytest.raises(ValueError, match="unresolved"):
        _graphnav.trace(g, "notify", "helper")


class _HashNamedGraph:
    """Mimics a clang/SCIP-indexed C repo: canonical ``name`` is a content
    hash, the readable identifier lives in ``unified_name``.

    ``popGenericCommand`` (caller) -> ``addReplyNull`` (callee).
    """

    def __init__(self):
        self.name_to_vertex = {"deadbeef00": 0, "cafef00d11": 1}
        self._attr = {
            0: {
                "name": "deadbeef00",
                "unified_name": "src/t_list.c:popGenericCommand()",
                "type": "function",
                "file": "src/t_list.c",
                "start_line": 488,
            },
            1: {
                "name": "cafef00d11",
                "unified_name": "src/networking.c:addReplyNull()",
                "type": "function",
                "file": "src/networking.c",
                "start_line": 700,
            },
        }
        self._unified_to_names = {
            "src/t_list.c:popGenericCommand()": ["deadbeef00"],
            "src/networking.c:addReplyNull()": ["cafef00d11"],
        }
        self._succ = {"deadbeef00": [1]}
        self._pred = {"cafef00d11": [0]}

    def get_node_info_by_name(self, name):
        return self._attr.get(self.name_to_vertex.get(name))

    def get_node_info_by_id(self, vid):
        return self._attr.get(vid)

    def get_successors(self, name):
        return self._succ.get(name, [])

    def get_predecessors(self, name):
        return self._pred.get(name, [])


def test_display_name_prefers_unified():
    g = _HashNamedGraph()
    assert _graphnav.display_name(g, "deadbeef00") == "src/t_list.c:popGenericCommand()"


def test_resolve_bare_symbol_to_hash_canonical():
    """Agent re-seeds with a readable bare symbol; resolution returns the
    canonical hash so graph ops still work, with a readable candidate."""
    g = _HashNamedGraph()
    name, cands = _graphnav.resolve(g, "popGenericCommand")
    assert name == "deadbeef00"  # canonical identity for get_successors
    assert cands == ["src/t_list.c:popGenericCommand()"]  # readable display


def test_resolve_accepts_full_unified_name():
    g = _HashNamedGraph()
    name, _ = _graphnav.resolve(g, "src/networking.c:addReplyNull()")
    assert name == "cafef00d11"


def test_neighbors_emit_readable_names_for_hash_graph():
    g = _HashNamedGraph()
    callees = _graphnav.neighbors(g, "popGenericCommand", "callees")
    assert {n.node_name for n in callees} == {"src/networking.c:addReplyNull()"}
    assert callees[0].content == "callee of src/t_list.c:popGenericCommand()"
    # no raw content hash leaks into what the agent reads
    assert "deadbeef00" not in callees[0].node_id
    assert "cafef00d11" not in callees[0].node_id
    assert callees[0].node_id == "src/networking.c:addReplyNull()"
