# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import numpy as np
import pytest

from codenib.index.embedding.vector_store import (
    CodeVectorStore,
    _Document,
    _result_source_file,
)


class _FakeIndex:
    ntotal = 3

    def __init__(self, vectors):
        self.vectors = vectors

    def reconstruct(self, index):
        return np.asarray(self.vectors[index], dtype=np.float32)

    def search(self, query_vectors, top_k):
        vectors = np.asarray(self.vectors, dtype=np.float32)
        scores = vectors @ query_vectors[0]
        indices = np.argsort(-scores)[:top_k]
        return scores[indices].reshape(1, -1), indices.reshape(1, -1)


class _FakeEmbedding:
    def __init__(self):
        self.calls = 0

    def embed_query(self, query):
        self.calls += 1
        if query == "query":
            return [1.0, 0.0]
        if query == "different query":
            return [0.0, 1.0]
        raise AssertionError(f"unexpected query: {query}")


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            {
                "file": "/tmp/checkout/src/service.py",
                "node_id": "src/service.py:run()",
            },
            "src/service.py",
        ),
        (
            {
                "file": "/tmp/checkout/src/service.py",
                "node_id": "src/service.py",
            },
            "src/service.py",
        ),
        (
            {"file": "src/service.py", "node_id": "src/service.py:run()"},
            "src/service.py",
        ),
        (
            {
                "file": "/tmp/service.py",
                "node_id": "/tmp/service.py:run()",
            },
            "/tmp/service.py",
        ),
    ],
)
def test_result_source_file_prefers_stable_repository_identity(metadata, expected):
    assert _result_source_file(metadata) == expected


def test_search_within_ids_deduplicates_before_top_k():
    store = CodeVectorStore.__new__(CodeVectorStore)
    store.index_metric = "ip"
    store.embedding = _FakeEmbedding()
    store.l2_index = _FakeIndex([[0.5, 0.0], [0.9, 0.0], [0.8, 0.0]])
    store.l2_documents = [
        _Document("old", {"node_id": "a", "name": "a", "file": "a.py"}),
        _Document("best", {"node_id": "a", "name": "a", "file": "a.py"}),
        _Document("other", {"node_id": "b", "name": "b", "file": "b.py"}),
    ]

    result = store.search_within_ids(
        "query", mask_node_ids={"a", "b"}, top_k=2, level="l2"
    )

    assert [node.node_id for node in result] == ["a", "b"]
    assert result[0].content == "best"


def test_search_stages_reuse_the_same_query_embedding_within_request_scope():
    store = CodeVectorStore.__new__(CodeVectorStore)
    store.index_metric = "ip"
    store.embedding = _FakeEmbedding()
    store.l2_index = _FakeIndex([[0.5, 0.0], [0.9, 0.0], [0.8, 0.0]])
    store.l2_documents = [
        _Document("old", {"node_id": "a", "name": "a", "file": "a.py"}),
        _Document("best", {"node_id": "a", "name": "a", "file": "a.py"}),
        _Document("other", {"node_id": "b", "name": "b", "file": "b.py"}),
    ]

    with store.reuse_query_embedding():
        dense = store.search_with_content("query", top_k=3, level="l2")
        reranked = store.search_within_ids(
            "query", mask_node_ids={"a", "b"}, top_k=2, level="l2"
        )

    assert store.embedding.calls == 1
    assert [node.score for node in dense] == pytest.approx([0.9, 0.8, 0.5])
    assert [node.node_id for node in reranked] == ["a", "b"]

    store.search_with_content("different query", top_k=3, level="l2")
    assert store.embedding.calls == 2


def test_vector_search_apis_return_repository_relative_files():
    store = CodeVectorStore.__new__(CodeVectorStore)
    store.index_metric = "ip"
    store.embedding = _FakeEmbedding()
    store.l2_index = _FakeIndex([[1.0, 0.0], [0.5, 0.0], [0.25, 0.0]])
    store.l2_documents = [
        _Document(
            "target",
            {
                "node_id": "src/service.py:run()",
                "name": "run",
                "file": "/tmp/checkout/src/service.py",
            },
        ),
        _Document("other", {"node_id": "b", "name": "b", "file": "b.py"}),
        _Document("last", {"node_id": "c", "name": "c", "file": "c.py"}),
    ]

    assert store.search("query", top_k=1)[0].file == "src/service.py"
    assert store.search_with_content("query", top_k=1)[0].file == "src/service.py"
    assert (
        store.search_within_ids("query", {"src/service.py:run()"}, top_k=1)[0].file
        == "src/service.py"
    )


def test_repeated_requests_do_not_reuse_query_embeddings():
    store = CodeVectorStore.__new__(CodeVectorStore)
    store.index_metric = "ip"
    store.embedding = _FakeEmbedding()
    store.l2_index = _FakeIndex([[0.5, 0.0], [0.9, 0.0], [0.8, 0.0]])
    store.l2_documents = [
        _Document("a", {"node_id": "a", "name": "a", "file": "a.py"}),
        _Document("b", {"node_id": "b", "name": "b", "file": "b.py"}),
        _Document("c", {"node_id": "c", "name": "c", "file": "c.py"}),
    ]

    store.search_with_content("query", top_k=3, level="l2")
    store.search_with_content("query", top_k=3, level="l2")

    assert store.embedding.calls == 2
