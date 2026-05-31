# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the codeminer_context one-call graph-aware composer (#133)."""

from __future__ import annotations

from types import SimpleNamespace

from codeminer.agent.skills.context import ComposerContexts
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
    ex = create_executor(ComposerContexts(retrieve=retrieve, expand=expand))
    res = ex(query="watch effect bug", seeds=2, max_results=20)
    names = {n.node_name for n in res}
    assert "pkg.a.doWatch" in names  # entry-point seed
    assert "pkg.a.watchEffect" in names  # caller (graph-expanded)
    assert "pkg.b.helper" in names  # callee (graph-expanded)
    assert len(res) == len({n.node_id for n in res})  # deduped


class _ManyBM25:
    """bm25 floods 5 off-target lexical hits (mimics 'null array' matching
    response-encoding constants on a redis bug report)."""

    def search(self, query, top_k, **kw):
        return [
            QueriedNode(
                node_name=f"pkg.noise.const{i}",
                node_id=f"c.py:const{i}",
                file="c.py",
            )
            for i in range(5)
        ]


class _OnTargetVec:
    def search(self, query, top_k, level="l2", **kw):
        return [
            QueriedNode(
                node_name="pkg.a.doWatch", node_id="a.py:pkg.a.doWatch", file="a.py"
            )
        ]


def test_interleave_keeps_embedding_seed_when_bm25_floods():
    """With round-robin interleave, the lone on-target embedding hit must reach
    the seed set even though bm25 returned a full page of off-target hits.
    Regression for the redis +30 % overhead: bm25-then-embedding concatenation
    truncated the embedding seeds off the budget."""
    create_executor = _load()
    retrieve = SimpleNamespace(
        bm25=_ManyBM25(), vector_store=_OnTargetVec(), default_level="l2"
    )
    expand = SimpleNamespace(code_graph=_FakeGraph())
    ex = create_executor(ComposerContexts(retrieve=retrieve, expand=expand))
    res = ex(query="watch effect bug", seeds=2, max_results=20)
    names = {n.node_name for n in res}
    assert "pkg.a.doWatch" in names  # embedding seed survived the bm25 flood
    # and it graph-expanded (its caller/callee are present)
    assert "pkg.a.watchEffect" in names or "pkg.b.helper" in names


def test_context_handles_missing_graph_gracefully():
    create_executor = _load()
    retrieve = SimpleNamespace(bm25=_FakeBM25(), vector_store=None, default_level="l2")
    ex = create_executor(ComposerContexts(retrieve=retrieve, expand=None))
    res = ex(query="x")
    assert any(n.node_name == "pkg.a.doWatch" for n in res)  # seed still returned


def test_query_identifiers_extracts_codeish_tokens():
    """Identifier seeding (#24): pull symbol/command names the user named,
    skip plain English Titlecase + bug-report noise words."""
    from codeminer.agent.skills.codeminer_context.executor import _query_identifiers

    ids = _query_identifiers(
        "[BUG] LPOP with count returns Null Bulk instead of Null array; "
        "see popGenericCommand and `addReplyNull` in t_list.c"
    )
    assert "LPOP" in ids  # ALL_CAPS command
    assert "popGenericCommand" in ids  # camelCase symbol
    assert "addReplyNull" in ids  # backtick-quoted
    assert "BUG" not in ids  # stoplisted report-noise
    assert "Null" not in ids and "Bulk" not in ids  # plain Titlecase English
