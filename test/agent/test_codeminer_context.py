# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the codeminer_context one-call graph-aware composer (#133)."""

from __future__ import annotations

from types import SimpleNamespace

from codeminer.types import QueriedNode


class _FakeGraph:
    """doWatch is called by watchEffect (caller) and calls helper (callee)."""

    def __init__(self):
        self.name_to_vertex = {
            "pkg.a.doWatch": 0,
            "pkg.a.watchEffect": 1,
            "pkg.b.helper": 2,
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
        }
        self._succ = {"pkg.a.doWatch": [2]}
        self._pred = {"pkg.a.doWatch": [1]}

    def get_node_info_by_name(self, name):
        return self._attr.get(self.name_to_vertex.get(name))

    def get_node_info_by_id(self, vid):
        return self._attr.get(vid)

    def get_successors(self, name):
        return self._succ.get(name, [])

    def get_predecessors(self, name):
        return self._pred.get(name, [])


class _FakeBM25:
    def search(self, query, top_k, **kw):
        return [
            QueriedNode(
                node_name="pkg.a.doWatch", node_id="a.py:pkg.a.doWatch", file="a.py"
            )
        ]


class _FakeVec:
    def search(self, query, top_k, level="l2", **kw):
        return [
            QueriedNode(
                node_name="pkg.a.doWatch", node_id="a.py:pkg.a.doWatch", file="a.py"
            )
        ]


def _load():
    from codeminer.agent.skills.codeminer_context.executor import create_executor

    return create_executor


def test_context_composes_search_plus_graph():
    create_executor = _load()
    retrieve = SimpleNamespace(
        bm25=_FakeBM25(), vector_store=_FakeVec(), default_level="l2"
    )
    expand = SimpleNamespace(code_graph=_FakeGraph())
    ex = create_executor({"retrieve": retrieve, "expand": expand})
    res = ex(query="watch effect bug", seeds=2, max_results=20)
    names = {n.node_name for n in res}
    assert "pkg.a.doWatch" in names  # entry-point seed
    assert "pkg.a.watchEffect" in names  # caller (graph-expanded)
    assert "pkg.b.helper" in names  # callee (graph-expanded)
    assert len(res) == len({n.node_id for n in res})  # deduped


def test_context_handles_missing_graph_gracefully():
    create_executor = _load()
    retrieve = SimpleNamespace(bm25=_FakeBM25(), vector_store=None, default_level="l2")
    ex = create_executor({"retrieve": retrieve, "expand": None})
    res = ex(query="x")
    assert any(n.node_name == "pkg.a.doWatch" for n in res)  # seed still returned
