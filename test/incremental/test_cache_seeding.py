# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for Phase 1: Embeddings cache seeding from first build.

Verifies that after CodeVectorStore.get_embeddings_by_content_hash(),
the cache contains all vectors so the first incremental update achieves
~100% cache hit rate for unchanged code.
"""

from __future__ import annotations

import hashlib
from typing import List
from unittest.mock import MagicMock

import faiss
import numpy as np
import pytest

from codeminer.index.embedding.vector_store import CodeVectorStore, _Document

DIM = 8


def _make_mock_embedding():
    """Return a mock embedding model with deterministic vectors."""
    model = MagicMock()

    def embed_documents(texts: List[str]) -> List[List[float]]:
        vecs = []
        for text in texts:
            h = hash(text) % (2**16)
            vecs.append([(h + i) / (2**16) for i in range(DIM)])
        return vecs

    def embed_query(text: str) -> List[float]:
        return embed_documents([text])[0]

    model.embed_documents.side_effect = embed_documents
    model.embed_query.side_effect = embed_query
    return model


def _md5(content: str) -> str:
    return hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()


@pytest.fixture()
def populated_store():
    """
    Build a CodeVectorStore with a few documents and return it
    along with the expected content hashes.
    """
    embedding = _make_mock_embedding()

    store = CodeVectorStore.__new__(CodeVectorStore)
    store.embedding = embedding
    store.dimension = DIM
    store.index_metric = "ip"
    store.profiler = None
    store.embedding_model = "test-model"
    store.embedding_provider = "test"
    store.index_type = "flat"
    store.store_path = None

    contents = [
        "def alpha():\n    return 1\n",
        "def beta():\n    return 2\n",
        "def gamma():\n    return 3\n",
    ]

    documents = []
    vectors = []
    expected_hashes = {}

    for i, content in enumerate(contents):
        ch = _md5(content)
        expected_hashes[ch] = content
        vec = embedding.embed_documents([content])[0]
        vectors.append(vec)
        documents.append(
            _Document(
                page_content=content,
                metadata={
                    "chunk_id": i,
                    "chunk_type": "function",
                    "name": f"func_{i}",
                    "file": f"file_{i}.py",
                    "start_line": 0,
                    "end_line": 2,
                    "node_id": f"file_{i}.py:func_{i}()",
                    "level": "l2",
                    "content_hash": ch,
                },
            )
        )

    # Build raw FAISS index for L2
    l2_idx = faiss.IndexFlatIP(DIM)
    l2_idx.add(np.array(vectors, dtype=np.float32))
    store.l2_index = l2_idx
    store.l2_documents = documents

    # Empty L0
    store.l0_index = faiss.IndexFlatIP(DIM)
    store.l0_documents = []

    return store, expected_hashes


class TestCacheSeeding:
    """Verify get_embeddings_by_content_hash extracts all vectors."""

    def test_extracts_all_hashes(self, populated_store):
        store, expected_hashes = populated_store
        result = store.get_embeddings_by_content_hash(level="l2")

        assert set(result.keys()) == set(expected_hashes.keys())

    def test_vectors_are_float32(self, populated_store):
        store, _ = populated_store
        result = store.get_embeddings_by_content_hash(level="l2")

        for vec in result.values():
            assert vec.dtype == np.float32
            assert vec.shape == (DIM,)

    def test_vectors_match_original(self, populated_store):
        store, expected_hashes = populated_store
        result = store.get_embeddings_by_content_hash(level="l2")

        embedding = store.embedding
        for ch, content in expected_hashes.items():
            original = np.array(
                embedding.embed_documents([content])[0], dtype=np.float32
            )
            extracted = result[ch]
            np.testing.assert_allclose(extracted, original, atol=1e-6)

    def test_empty_index_returns_empty(self, populated_store):
        store, _ = populated_store
        # L0 is empty
        result = store.get_embeddings_by_content_hash(level="l0")
        assert result == {}

    def test_seeded_cache_gives_full_hit_rate(self, populated_store):
        """Simulate the cache seeding workflow from build()."""
        from codeminer.incremental import EmbeddingsCache

        store, expected_hashes = populated_store
        hash_to_vec = store.get_embeddings_by_content_hash(level="l2")

        cache = EmbeddingsCache()
        for ch, vec in hash_to_vec.items():
            cache.put(ch, vec)

        # Every hash should be cached
        for ch in expected_hashes:
            assert cache.has(ch), f"Expected cache hit for {ch}"

        assert cache.size() == len(expected_hashes)
