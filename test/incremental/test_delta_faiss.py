"""
Tests for Phase 4: Delta FAISS update optimization.

Verifies that CodeVectorStore.delta_update() correctly rebuilds
the index when chunks change.
"""

from __future__ import annotations

import hashlib
from typing import List
from unittest.mock import MagicMock

import faiss
import numpy as np

from codeminer.index.embedding.vector_store import CodeVectorStore, _Document

DIM = 8


def _md5(content: str) -> str:
    return hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()


def _mock_embedding():
    model = MagicMock()

    def embed_documents(texts: List[str]) -> List[List[float]]:
        return [[(hash(t) % (2**16) + i) / (2**16) for i in range(DIM)] for t in texts]

    def embed_query(text: str) -> List[float]:
        return embed_documents([text])[0]

    model.embed_documents.side_effect = embed_documents
    model.embed_query.side_effect = embed_query
    return model


def _build_store_with_docs(contents: List[str]):
    """Build a CodeVectorStore populated with the given contents at L2."""
    embedding = _mock_embedding()
    store = CodeVectorStore.__new__(CodeVectorStore)
    store.embedding = embedding
    store.dimension = DIM
    store.index_metric = "ip"
    store.profiler = None
    store.embedding_model = "test"
    store.embedding_provider = "test"
    store.index_type = "flat"
    store.store_path = None

    documents = []
    vectors = []
    hashes = {}

    for i, content in enumerate(contents):
        ch = _md5(content)
        hashes[ch] = content
        vec = embedding.embed_documents([content])[0]
        vectors.append(vec)
        documents.append(
            _Document(
                page_content=content,
                metadata={
                    "chunk_id": i,
                    "chunk_type": "function",
                    "name": f"func_{i}",
                    "file": "file.py",
                    "start_line": i * 3,
                    "end_line": i * 3 + 2,
                    "node_id": f"file.py:func_{i}()",
                    "level": "l2",
                    "content_hash": ch,
                },
            )
        )

    # Build raw FAISS index
    l2_idx = faiss.IndexFlatIP(DIM)
    l2_idx.add(np.array(vectors, dtype=np.float32))
    store.l2_index = l2_idx
    store.l2_documents = documents

    l0_idx = faiss.IndexFlatIP(DIM)
    store.l0_index = l0_idx
    store.l0_documents = []

    return store, documents, hashes


class TestDeltaUpdate:
    """Verify delta_update logic."""

    def test_small_delta_patches_index(self):
        """With 1 out of 10 chunks changed, delta path should be used."""
        contents = [f"def func_{i}():\n    return {i}\n" for i in range(10)]
        store, docs, hashes = _build_store_with_docs(contents)

        # Capture the original index object; the delta path must patch it
        # in place (identity preserved), whereas a full rebuild replaces it.
        original_index = store.l2_index
        unchanged_vec_5 = store.l2_index.reconstruct(5).copy()

        # Change one chunk
        new_content = "def func_0():\n    return 999\n"
        new_hash = _md5(new_content)

        new_docs = list(docs)
        new_embeddings = [
            np.array(
                store.embedding.embed_documents([d.page_content])[0], dtype=np.float32
            )
            for d in new_docs
        ]
        new_docs[0] = _Document(
            page_content=new_content,
            metadata={**docs[0].metadata, "content_hash": new_hash},
        )
        new_embeddings[0] = np.array(
            store.embedding.embed_documents([new_content])[0], dtype=np.float32
        )

        # 1 / 10 = 10% changed, threshold 50% → delta path taken
        changed_hashes = {new_hash}
        store.delta_update(
            new_docs, new_embeddings, changed_hashes, level="l2", threshold=0.5
        )

        # Identity of the FAISS index must be preserved — a silent fallback
        # to rebuild_from_embeddings would replace store.l2_index.
        assert (
            store.l2_index is original_index
        ), "delta_update silently fell back to full rebuild (index replaced)"
        assert store.l2_index.ntotal == 10
        assert len(store.l2_documents) == 10

        # Unchanged row vector must still be present somewhere in the index.
        reconstructed = [
            store.l2_index.reconstruct(i) for i in range(store.l2_index.ntotal)
        ]
        assert any(
            np.allclose(v, unchanged_vec_5, atol=1e-6) for v in reconstructed
        ), "unchanged vector disappeared — delta path is corrupting survivors"

        # The new content must be reflected in the docs list.
        assert any(d.page_content == new_content for d in store.l2_documents)

    def test_metadata_only_change_updates_docs(self):
        """
        When a chunk's content is unchanged but its metadata changes
        (e.g. the file was renamed or the chunk moved within the file),
        delta_update must refresh the doc entry so search results don't
        point at the stale path/line.  The FAISS vector stays the same
        because the content_hash is identical.
        """
        contents = [f"def fn_{i}():\n    return {i}\n" for i in range(6)]
        store, docs, _ = _build_store_with_docs(contents)

        original_index = store.l2_index

        # Same contents, same hashes — but fn_0's file moved and its
        # start_line shifted.  Nothing is in changed_content_hashes.
        new_docs = [
            _Document(
                page_content=d.page_content,
                metadata={**d.metadata},
            )
            for d in docs
        ]
        new_docs[0] = _Document(
            page_content=docs[0].page_content,
            metadata={
                **docs[0].metadata,
                "file": "renamed.py",
                "start_line": 42,
                "end_line": 44,
                "node_id": "renamed.py:func_0()",
            },
        )
        new_embeddings = [
            np.array(
                store.embedding.embed_documents([d.page_content])[0], dtype=np.float32
            )
            for d in new_docs
        ]

        store.delta_update(new_docs, new_embeddings, set(), level="l2", threshold=0.5)

        # Delta path must have run (empty changed set + non-empty index).
        assert (
            store.l2_index is original_index
        ), "delta_update fell back to full rebuild on metadata-only change"
        assert store.l2_index.ntotal == 6

        # Look up the doc corresponding to fn_0's content_hash and verify
        # its metadata reflects the rename, not the stale seed values.
        target_hash = docs[0].metadata["content_hash"]
        matches = [
            d
            for d in store.l2_documents
            if d.metadata.get("content_hash") == target_hash
        ]
        assert len(matches) == 1, "survivor doc was duplicated or dropped"
        assert (
            matches[0].metadata["file"] == "renamed.py"
        ), "survivor doc kept stale file path — metadata-only change ignored"
        assert matches[0].metadata["start_line"] == 42

    def test_large_delta_triggers_full_rebuild(self):
        """When > threshold fraction changed, full rebuild should happen."""
        contents = [f"def f_{i}():\n    return {i}\n" for i in range(5)]
        store, docs, hashes = _build_store_with_docs(contents)

        # Change all chunks
        new_contents = [f"def f_{i}():\n    return {i * 10}\n" for i in range(5)]
        new_docs = []
        new_embeddings = []
        changed_hashes = set()

        for i, content in enumerate(new_contents):
            ch = _md5(content)
            changed_hashes.add(ch)
            changed_hashes.add(_md5(contents[i]))
            vec = np.array(
                store.embedding.embed_documents([content])[0], dtype=np.float32
            )
            new_docs.append(
                _Document(
                    page_content=content,
                    metadata={**docs[i].metadata, "content_hash": ch},
                )
            )
            new_embeddings.append(vec)

        # threshold=0.1 → 100% changed > 10% → full rebuild
        store.delta_update(
            new_docs, new_embeddings, changed_hashes, level="l2", threshold=0.1
        )

        assert len(store.l2_documents) == 5
        for i, doc in enumerate(store.l2_documents):
            assert doc.page_content == new_contents[i]

    def test_empty_changed_set_no_op(self):
        """With no changes, delta_update should still work."""
        contents = ["def a():\n    pass\n", "def b():\n    pass\n"]
        store, docs, hashes = _build_store_with_docs(contents)

        embeddings = [
            np.array(
                store.embedding.embed_documents([d.page_content])[0], dtype=np.float32
            )
            for d in docs
        ]

        store.delta_update(docs, embeddings, set(), level="l2")
        assert len(store.l2_documents) == 2

    def test_delta_on_empty_index_does_full_rebuild(self):
        """If the index is empty, delta_update should do a full rebuild."""
        embedding = _mock_embedding()
        store = CodeVectorStore.__new__(CodeVectorStore)
        store.embedding = embedding
        store.dimension = DIM
        store.index_metric = "ip"
        store.profiler = None
        store.embedding_model = "test"
        store.embedding_provider = "test"
        store.index_type = "flat"
        store.store_path = None

        store.l2_index = faiss.IndexFlatIP(DIM)
        store.l2_documents = []
        store.l0_index = faiss.IndexFlatIP(DIM)
        store.l0_documents = []

        content = "def a():\n    pass\n"
        new_doc = _Document(
            page_content=content,
            metadata={"content_hash": _md5(content), "level": "l2"},
        )
        vec = np.array(embedding.embed_documents([content])[0], dtype=np.float32)

        store.delta_update([new_doc], [vec], {_md5(content)}, level="l2")
        assert len(store.l2_documents) == 1

    def test_seed_time_vectors_carry_content_hash(self):
        """
        Documents added via add_code_chunks (initial build) must include
        content_hash in metadata so delta_update can identify them later.

        Regression: without content_hash on seed-time vectors, the
        delta path cannot identify stale vectors and they accumulate.
        This test drives the *delta* path (not a silent rebuild fallback)
        by keeping the change ratio below the threshold.
        """
        embedding = _mock_embedding()
        store = CodeVectorStore.__new__(CodeVectorStore)
        store.embedding = embedding
        store.dimension = DIM
        store.index_metric = "ip"
        store.profiler = None
        store.embedding_model = "test"
        store.embedding_provider = "test"
        store.index_type = "flat"
        store.store_path = None

        store.l0_index = faiss.IndexFlatIP(DIM)
        store.l0_documents = []
        store.l2_index = faiss.IndexFlatIP(DIM)
        store.l2_documents = []

        # Seed three chunks; we'll replace only foo, keeping the change
        # ratio at 1/3 ≈ 0.33 < threshold 0.5 so the delta path runs.
        store.add_code_chunks(
            [
                {
                    "content": "def foo():\n    return 1\n",
                    "name": "foo",
                    "file": "a.py",
                },
                {
                    "content": "def bar():\n    return 2\n",
                    "name": "bar",
                    "file": "a.py",
                },
                {
                    "content": "def baz():\n    return 3\n",
                    "name": "baz",
                    "file": "a.py",
                },
            ],
            level="l2",
        )

        for doc in store.l2_documents:
            assert "content_hash" in doc.metadata, (
                "Seed-time document missing content_hash — delta_update "
                "will not be able to prune stale vectors"
            )

        original_index = store.l2_index

        new_foo = "def foo():\n    return 99\n"
        new_foo_hash = _md5(new_foo)
        bar_hash = _md5("def bar():\n    return 2\n")
        baz_hash = _md5("def baz():\n    return 3\n")

        all_docs = [
            _Document(
                page_content=new_foo,
                metadata={
                    "content_hash": new_foo_hash,
                    "name": "foo",
                    "file": "a.py",
                },
            ),
            _Document(
                page_content="def bar():\n    return 2\n",
                metadata={"content_hash": bar_hash, "name": "bar", "file": "a.py"},
            ),
            _Document(
                page_content="def baz():\n    return 3\n",
                metadata={"content_hash": baz_hash, "name": "baz", "file": "a.py"},
            ),
        ]
        all_embs = [
            np.array(embedding.embed_documents([d.page_content])[0], dtype=np.float32)
            for d in all_docs
        ]

        # changed set = just the new hash (what index_updater actually passes:
        # it's the set of cache misses). Size 1 / total 3 = 33% < threshold.
        store.delta_update(
            all_docs, all_embs, {new_foo_hash}, level="l2", threshold=0.5
        )

        # The delta path must have run (identity preserved), and the stale
        # foo vector must have been pruned — not left as a ghost.
        assert store.l2_index is original_index, (
            "delta_update fell back to full rebuild — regression test is "
            "no longer exercising the delta path"
        )
        assert store.l2_index.ntotal == 3, (
            f"Expected 3 vectors after delta, got {store.l2_index.ntotal} — "
            "stale seed-time vector was not removed"
        )
        # All three target hashes must be present among the docs.
        present = {d.metadata["content_hash"] for d in store.l2_documents}
        assert present == {new_foo_hash, bar_hash, baz_hash}
