# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for rerank ops."""

from __future__ import annotations

from codeminer.ops.rerank import rerank_by_embedding
from codeminer.types import QueriedNode


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
