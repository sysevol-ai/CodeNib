# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for rerank ops."""

from __future__ import annotations

from codenib.ops.rerank import (
    rerank_by_cross_encoder,
    rerank_by_embedding,
    rerank_by_indexed_embedding,
)
from codenib.types import NodeInfo, QueriedNode


class _FakeEmbedding:
    def __init__(self, vectors):
        self.vectors = vectors

    def embed_query(self, text):
        return self.vectors[text]

    def embed_documents(self, texts):
        return [self.vectors[text] for text in texts]


class _FakeStore:
    def __init__(self, *, vectors, index_metric="ip"):
        self.embedding = _FakeEmbedding(vectors)
        self.index_metric = index_metric


def test_rerank_by_embedding_sorts_by_inner_product():
    store = _FakeStore(
        vectors={
            "query": [1.0, 0.0],
            "best": [0.9, 0.0],
            "other": [0.1, 1.0],
        }
    )
    candidates = [
        QueriedNode(node_name="other", type="function", content="other"),
        QueriedNode(node_name="best", type="function", content="best"),
    ]

    result = rerank_by_embedding("query", candidates, store, top_k=2)

    assert [node.node_name for node in result] == ["best", "other"]
    assert result[0].score > result[1].score


def test_rerank_by_embedding_sorts_by_negative_l2_distance():
    store = _FakeStore(
        vectors={
            "query": [1.0, 0.0],
            "near": [1.1, 0.0],
            "far": [5.0, 0.0],
        },
        index_metric="l2",
    )
    candidates = [
        QueriedNode(node_name="far", type="function", content="far"),
        QueriedNode(node_name="near", type="function", content="near"),
    ]

    result = rerank_by_embedding("query", candidates, store, top_k=1)

    assert [node.node_name for node in result] == ["near"]


def test_rerank_by_embedding_skips_candidates_without_content():
    store = _FakeStore(
        vectors={
            "query": [1.0, 0.0],
            "best": [1.0, 0.0],
        }
    )
    candidates = [
        QueriedNode(node_name="empty", type="function"),
        QueriedNode(node_name="best", type="function", content="best"),
    ]

    result = rerank_by_embedding("query", candidates, store, top_k=5)

    assert [node.node_name for node in result] == ["best"]


def test_rerank_by_indexed_embedding_reuses_index_and_appends_unmatched():
    candidates = [
        QueriedNode(node_name="dense", node_id="a.py:dense", file="a.py"),
        QueriedNode(node_name="graph", node_id="b.py:graph", file="b.py"),
        QueriedNode(node_name="missing", node_id="c.py:missing", file="c.py"),
    ]

    class FakeIndexedStore:
        def search_within_ids(self, query, mask_node_ids, top_k, level):
            assert query == "query"
            assert mask_node_ids == {
                "a.py:dense",
                "b.py:graph",
                "c.py:missing",
            }
            assert top_k == 3
            assert level == "l2"
            return [
                NodeInfo(
                    node_name="graph",
                    node_id="b.py:graph",
                    file="b.py",
                    score=0.9,
                ),
                NodeInfo(
                    node_name="graph duplicate",
                    node_id="b.py:graph",
                    file="b.py",
                    score=0.8,
                ),
                NodeInfo(
                    node_name="dense",
                    node_id="a.py:dense",
                    file="a.py",
                    score=0.7,
                ),
            ]

    result = rerank_by_indexed_embedding(
        "query", candidates, FakeIndexedStore(), top_k=3
    )

    assert [node.node_id for node in result] == [
        "b.py:graph",
        "a.py:dense",
        "c.py:missing",
    ]


def test_rerank_by_indexed_embedding_does_not_expand_bare_names():
    candidates = [
        QueriedNode(node_name="same", node_id="a.py:same", file="a.py"),
        QueriedNode(node_name="same", file="b.py"),
    ]

    class FakeIndexedStore:
        def search_within_ids(self, query, mask_node_ids, top_k, level):
            assert mask_node_ids == {"a.py:same"}
            return [
                NodeInfo(
                    node_name="same",
                    node_id="a.py:same",
                    file="a.py",
                    score=0.9,
                )
            ]

    result = rerank_by_indexed_embedding("query", candidates, FakeIndexedStore())

    assert [(node.file, node.node_id) for node in result] == [
        ("a.py", "a.py:same"),
        ("b.py", None),
    ]


def test_rerank_by_cross_encoder_scores_content_and_preserves_metadata():
    candidates = [
        QueriedNode(
            node_name="first",
            node_id="a.py:first",
            file="a.py",
            content="first body",
        ),
        QueriedNode(
            node_name="second",
            node_id="b.py:second",
            file="b.py",
            content="second body",
        ),
    ]

    class FakeContext:
        def score(self, query, docs):
            assert query == "query"
            assert docs == ["first body", "second body"]
            return [0.1, 0.9]

    result = rerank_by_cross_encoder("query", candidates, FakeContext(), top_k=1)

    assert len(result) == 1
    assert result[0].node_id == "b.py:second"
    assert result[0].content == "second body"
    assert result[0].score == 0.9


def test_rerank_by_cross_encoder_retains_candidates_without_content():
    candidates = [
        QueriedNode(node_name="missing", node_id="a.py:missing"),
        QueriedNode(node_name="present", node_id="b.py:present", content="body"),
    ]

    class FakeContext:
        def score(self, query, docs):
            assert docs == ["body"]
            return [0.7]

    result = rerank_by_cross_encoder("query", candidates, FakeContext())

    assert [node.node_id for node in result] == ["b.py:present", "a.py:missing"]
    assert result[0].score == 0.7


def test_rerank_by_cross_encoder_rejects_wrong_score_count():
    candidate = QueriedNode(node_name="only", content="body")

    class FakeContext:
        def score(self, query, docs):
            return []

    try:
        rerank_by_cross_encoder("query", [candidate], FakeContext())
    except RuntimeError as exc:
        assert "scores for 1 candidates" in str(exc)
    else:
        raise AssertionError("expected score-count validation to fail")
