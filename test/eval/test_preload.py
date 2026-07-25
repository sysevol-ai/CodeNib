# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for pre-load context assembly helpers."""

from __future__ import annotations

from types import SimpleNamespace

from codenib.eval.agent_runner.preload import (
    assemble_preload,
    interleave_ranked,
    prepare_preload_query,
    snippet_for_node,
)


class FakeVectorStore:
    def __init__(self, nodes):
        self.nodes = nodes
        self.calls = []

    def search_with_content(self, *, query, top_k, level):
        self.calls.append((query, top_k, level))
        return self.nodes[:top_k]


class FakeBm25:
    def __init__(self, nodes):
        self.nodes = nodes
        self.calls = []

    def search(self, query, top_k):
        self.calls.append((query, top_k))
        return self.nodes[:top_k]


def _node(path: str, start: int, end: int, score: float, content: str = ""):
    return SimpleNamespace(
        node_id=f"{path}:symbol",
        file="/ignored/build/path.py",
        start_line=start,
        end_line=end,
        score=score,
        content=content,
    )


def test_interleave_ranked_round_robins_retriever_results():
    assert interleave_ranked([["e1", "e2"], ["b1"], ["g1", "g2"]]) == [
        "e1",
        "b1",
        "g1",
        "e2",
        "g2",
    ]


def test_snippet_for_node_keeps_first_nonblank_lines():
    text = "\n\nline 1\n\nline 2\nline 3\nline 4\nline 5\n"

    assert snippet_for_node(SimpleNamespace(content=text)) == (
        "    line 1\n    line 2\n    line 3\n    line 4"
    )


def test_assemble_preload_interleaves_and_returns_candidate_spans():
    embed = _node("pkg/embed.py", 0, 2, 0.9, "def embed():\n    pass")
    bm25 = _node("pkg/bm.py", 9, 11, 0.1, "def bm():\n    pass")
    contexts = {
        "retrieve": SimpleNamespace(
            vector_store=FakeVectorStore([embed]),
            bm25=FakeBm25([bm25]),
        )
    }

    preamble, candidates = assemble_preload(
        contexts,
        "where is auth?",
        recipe={"retrievers": ["embedding", "bm25"], "top_k": 2, "level": "l2"},
    )

    assert "UNVERIFIED retrieval hints" in preamble
    assert "pkg/embed.py:1-3" in preamble
    assert "pkg/bm.py:10-12" in preamble
    assert preamble.index("pkg/embed.py") < preamble.index("pkg/bm.py")
    assert candidates == [
        {"file": "pkg/embed.py", "start": 1, "end": 3},
        {"file": "pkg/bm.py", "start": 10, "end": 12},
    ]


def test_assemble_preload_eager_mode_uses_terse_preamble():
    node = _node("pkg/edit.py", 4, 8, 0.5)
    contexts = {
        "retrieve": SimpleNamespace(
            vector_store=FakeVectorStore([node]),
            bm25=None,
        )
    }

    preamble, candidates = assemble_preload(
        contexts,
        "where is auth?",
        recipe={"retrievers": ["embedding"], "top_k": 1, "mode": "eager"},
    )

    assert "ranked retrieval hits" in preamble
    assert "UNVERIFIED retrieval hints" not in preamble
    assert candidates == [{"file": "pkg/edit.py", "start": 5, "end": 9}]


def test_prepare_preload_query_inline_candidates():
    node = _node("pkg/edit.py", 4, 8, 0.5)
    contexts = {
        "retrieve": SimpleNamespace(
            vector_store=FakeVectorStore([node]),
            bm25=None,
        )
    }

    plan = prepare_preload_query(
        contexts,
        "where is auth?",
        recipe={"retrievers": ["embedding"], "top_k": 1, "mode": "eager"},
    )

    assert plan.query.startswith("where is auth?\n\n# Candidate locations")
    assert plan.candidates == [{"file": "pkg/edit.py", "start": 5, "end": 9}]
    assert plan.scatter is False
    assert plan.gated_skip is False


def test_prepare_preload_query_scatter_keeps_original_query():
    node = _node("pkg/edit.py", 4, 8, 0.5)
    contexts = {
        "retrieve": SimpleNamespace(
            vector_store=FakeVectorStore([node]),
            bm25=None,
        )
    }

    plan = prepare_preload_query(
        contexts,
        "where is auth?",
        recipe={"retrievers": ["embedding"], "top_k": 1, "mode": "scatter"},
    )

    assert plan.query == "where is auth?"
    assert plan.candidates == [{"file": "pkg/edit.py", "start": 5, "end": 9}]
    assert plan.scatter is True


def test_prepare_preload_query_gated_skip_avoids_candidate_retrieval():
    contexts = {
        "retrieve": SimpleNamespace(
            vector_store=FakeVectorStore([_node("pkg/edit.py", 4, 8, 0.5)]),
            bm25=None,
        )
    }

    plan = prepare_preload_query(
        contexts,
        "button is broken",
        recipe={"retrievers": ["embedding"], "top_k": 1, "mode": "eager_gated"},
        gate_call=lambda messages: "GREP",
    )

    assert plan.query == "button is broken"
    assert plan.candidates == []
    assert plan.scatter is False
    assert plan.gated_skip is True
    assert contexts["retrieve"].vector_store.calls == []
