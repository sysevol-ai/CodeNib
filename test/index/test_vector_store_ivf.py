# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the configurable FAISS index type (flat vs IVF).

A deterministic stub embedding (no model download, no GPU) lets these run in
the fast unit job. IVF correctness checks rely on the fact that an exact-match
query (query text == an indexed chunk's content) produces the identical
normalized vector, so it quantizes to the same IVF cell as that chunk and is
found even at ``nprobe=1``.
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

import faiss
import numpy as np
import pytest

from codenib.index.embedding.vector_store import CodeVectorStore

_DIM = 16


class _FakeEmbedding:
    """Deterministic, content-addressed unit-norm embedding (no real model)."""

    def __init__(self, dim: int = _DIM):
        self.dim = dim

    def _vec(self, text: str) -> list:
        seed = int.from_bytes(
            hashlib.sha256(text.encode("utf-8")).digest()[:8], "little"
        )
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(self.dim).astype(np.float32)
        v /= np.linalg.norm(v) + 1e-12  # unit norm -> IP == cosine
        return v.tolist()

    def embed_query(self, text: str) -> list:
        return self._vec(text)

    def embed_documents(self, texts) -> list:
        return [self._vec(t) for t in texts]


def _make_store(**kwargs) -> CodeVectorStore:
    """Build a CodeVectorStore with the embedding model stubbed out."""
    with patch.object(
        CodeVectorStore,
        "_initialize_embedding_model",
        return_value=_FakeEmbedding(_DIM),
    ):
        return CodeVectorStore(dimension=_DIM, **kwargs)


def _chunks(n: int) -> list:
    return [
        {
            "content": f"def f{i}(x):\n    return x + {i}",
            "chunk_type": "function",
            "name": f"f{i}",
            "file": f"m{i}.py",
            "start_line": 0,
            "end_line": 1,
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Construction / validation
# ---------------------------------------------------------------------------


def test_flat_is_default():
    store = _make_store()
    assert store.index_type == "flat"
    assert isinstance(store.l2_index, faiss.IndexFlatIP)


def test_invalid_index_type_raises():
    with pytest.raises(ValueError, match="Must be 'flat' or 'ivf'"):
        _make_store(index_type="hnsw")


def test_ivf_index_built_untrained():
    store = _make_store(index_type="ivf", ivf_nlist=4, ivf_nprobe=4)
    assert isinstance(store.l2_index, faiss.IndexIVFFlat)
    assert store.l2_index.is_trained is False
    assert store.l2_index.ntotal == 0


def test_l2_metric_for_ivf():
    store = _make_store(index_type="ivf", index_metric="l2", ivf_nlist=4)
    assert isinstance(store.l2_index, faiss.IndexIVFFlat)
    assert store.l2_index.metric_type == faiss.METRIC_L2


# ---------------------------------------------------------------------------
# Lazy training + nlist/nprobe clamping
# ---------------------------------------------------------------------------


def test_ivf_trains_on_first_add():
    store = _make_store(index_type="ivf", ivf_nlist=4, ivf_nprobe=4)
    store.add_code_chunks(_chunks(8))
    assert store.l2_index.is_trained is True
    assert store.l2_index.ntotal == 8
    assert store.l2_index.nlist == 4


def test_ivf_nlist_clamped_to_small_corpus():
    # 5 chunks but nlist=100: FAISS k-means needs >= nlist points, so nlist is
    # clamped down to the batch size and nprobe follows it.
    store = _make_store(index_type="ivf", ivf_nlist=100, ivf_nprobe=8)
    store.add_code_chunks(_chunks(5))
    assert store.l2_index.ntotal == 5
    assert store.l2_index.nlist == 5
    assert store.l2_index.nprobe == 5


def test_ivf_nprobe_clamped_to_nlist():
    store = _make_store(index_type="ivf", ivf_nlist=2, ivf_nprobe=8)
    store.add_code_chunks(_chunks(6))
    assert store.l2_index.nlist == 2
    assert store.l2_index.nprobe == 2


# ---------------------------------------------------------------------------
# Search correctness + persistence
# ---------------------------------------------------------------------------


def test_ivf_search_returns_exact_top_hit():
    store = _make_store(index_type="ivf", ivf_nlist=5, ivf_nprobe=5)
    chunks = _chunks(5)
    store.add_code_chunks(chunks)
    target = chunks[2]
    results = store.search(target["content"], top_k=3)
    assert results
    assert results[0].node_name == target["name"]


def test_flat_search_still_works():
    store = _make_store(index_type="flat")
    chunks = _chunks(5)
    store.add_code_chunks(chunks)
    assert store.l2_index.ntotal == 5
    results = store.search(chunks[0]["content"], top_k=3)
    assert results and results[0].node_name == chunks[0]["name"]


def test_ivf_save_load_roundtrip(tmp_path):
    path = str(tmp_path / "vs")
    store = _make_store(index_type="ivf", ivf_nlist=5, ivf_nprobe=5)
    chunks = _chunks(5)
    store.add_code_chunks(chunks)
    store.save(path)

    loaded = _make_store(index_type="ivf", ivf_nlist=5, ivf_nprobe=5, store_path=path)
    loaded.load(path)
    assert loaded.l2_index.ntotal == 5
    res = loaded.search(chunks[1]["content"], top_k=3)
    assert res and res[0].node_name == chunks[1]["name"]


def test_save_removes_persisted_level_after_last_document_is_deleted(tmp_path):
    path = tmp_path / "vs"
    store = _make_store(embedding_model="test/model")
    store.add_code_chunks(_chunks(2))
    store.save(str(path))
    assert (path / "l2" / "index_test__model.faiss").exists()

    store.clear("l2")
    store.save(str(path))

    assert not (path / "l2").exists()
    config = json.loads((path / "config_test__model.json").read_text())
    assert config["l2_documents"] == 0

    loaded = _make_store(embedding_model="test/model", store_path=str(path))
    loaded.load()
    assert loaded.l2_index.ntotal == 0
    assert loaded.l2_documents == []


def test_save_rejects_misaligned_vectors_and_documents(tmp_path):
    store = _make_store(embedding_model="test/model")
    store.add_code_chunks(_chunks(2))
    store.l2_documents.pop()

    with pytest.raises(ValueError, match="2 vectors for 1 documents"):
        store.save(str(tmp_path / "vs"))


def test_load_prefers_portable_json_documents(tmp_path):
    path = tmp_path / "vs"
    store = _make_store(embedding_model="test/model")
    chunks = _chunks(2)
    store.add_code_chunks(chunks)
    store.save(str(path))

    documents_path = path / "l2" / "documents_test__model.pkl"
    documents_path.unlink()
    portable_path = documents_path.with_suffix(".json")
    portable_path.write_text(
        json.dumps(
            [
                {
                    "page_content": chunk["content"],
                    "metadata": {
                        "name": chunk["name"],
                        "file": chunk["file"],
                        "start_line": chunk["start_line"],
                        "end_line": chunk["end_line"],
                    },
                }
                for chunk in chunks
            ]
        ),
        encoding="utf-8",
    )

    loaded = _make_store(embedding_model="test/model", store_path=str(path))
    loaded.load()

    assert [document.metadata["file"] for document in loaded.l2_documents] == [
        "m0.py",
        "m1.py",
    ]


def test_load_rejects_invalid_portable_json_documents(tmp_path):
    path = tmp_path / "vs"
    store = _make_store(embedding_model="test/model")
    store.add_code_chunks(_chunks(1))
    store.save(str(path))

    documents_path = path / "l2" / "documents_test__model.pkl"
    documents_path.unlink()
    documents_path.with_suffix(".json").write_text(
        '[{"page_content": 7, "metadata": {}}]',
        encoding="utf-8",
    )

    loaded = _make_store(embedding_model="test/model", store_path=str(path))
    with pytest.raises(ValueError, match="invalid content or metadata"):
        loaded.load()


def test_load_rejects_faiss_document_count_mismatch(tmp_path):
    path = tmp_path / "vs"
    store = _make_store(embedding_model="test/model")
    store.add_code_chunks(_chunks(2))
    store.save(str(path))

    documents_path = path / "l2" / "documents_test__model.pkl"
    documents_path.unlink()
    documents_path.with_suffix(".json").write_text(
        json.dumps(
            [
                {
                    "page_content": "def f0(): pass",
                    "metadata": {"name": "f0", "file": "m0.py"},
                }
            ]
        ),
        encoding="utf-8",
    )

    loaded = _make_store(embedding_model="test/model", store_path=str(path))
    with pytest.raises(ValueError, match="2 vectors for 1 documents"):
        loaded.load()


def test_load_rejects_top_level_document_count_mismatch(tmp_path):
    path = tmp_path / "vs"
    store = _make_store(embedding_model="test/model")
    store.add_code_chunks(_chunks(2))
    store.save(str(path))

    config_path = path / "config_test__model.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["l2_documents"] = 3
    config_path.write_text(json.dumps(config), encoding="utf-8")

    loaded = _make_store(embedding_model="test/model", store_path=str(path))
    with pytest.raises(ValueError, match="config expects 3 documents, loaded 2"):
        loaded.load()


def test_zero_top_level_count_does_not_resurrect_stale_level_files(tmp_path):
    path = tmp_path / "vs"
    store = _make_store(embedding_model="test/model")
    store.add_code_chunks(_chunks(2))
    store.save(str(path))

    config_path = path / "config_test__model.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["l2_documents"] = 0
    config_path.write_text(json.dumps(config), encoding="utf-8")

    loaded = _make_store(embedding_model="test/model", store_path=str(path))
    loaded.load()

    assert loaded.l2_index.ntotal == 0
    assert loaded.l2_documents == []


def test_failed_swap_preserves_the_serving_index(tmp_path):
    current = _make_store(embedding_model="test/model")
    current_chunks = _chunks(2)
    current.add_code_chunks(current_chunks)

    replacement_path = tmp_path / "replacement"
    replacement = _make_store(embedding_model="test/model")
    replacement.add_code_chunks(_chunks(1))
    replacement.save(str(replacement_path))
    (replacement_path / "l2" / "documents_test__model.pkl").unlink()

    with pytest.raises(ValueError, match="document store"):
        current.swap_index(str(replacement_path))

    assert current.l2_index.ntotal == 2
    assert len(current.l2_documents) == 2
    results = current.search(current_chunks[0]["content"], top_k=1)
    assert results[0].node_name == current_chunks[0]["name"]


def test_successful_swap_replaces_all_levels(tmp_path):
    current = _make_store(embedding_model="test/model")
    current.add_code_chunks(_chunks(2), level="l0")
    current.add_code_chunks(_chunks(2), level="l2")

    replacement_path = tmp_path / "replacement"
    replacement = _make_store(embedding_model="test/model")
    replacement_chunk = _chunks(1)[0]
    replacement_chunk["name"] = "replacement"
    replacement_chunk["content"] = "def replacement():\n    return 42"
    replacement.add_code_chunks([replacement_chunk], level="l2")
    replacement.save(str(replacement_path))

    current.swap_index(str(replacement_path))

    assert current.l0_index.ntotal == 0
    assert current.l0_documents == []
    assert current.l2_index.ntotal == 1
    results = current.search(replacement_chunk["content"], top_k=1)
    assert results[0].node_name == "replacement"


def test_load_rejects_faiss_dimension_mismatch(tmp_path):
    path = tmp_path / "vs"
    model = "test/model"
    store = _make_store(embedding_model=model)
    store.add_code_chunks(_chunks(2))
    store.save(str(path))

    config_path = path / "config_test__model.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["dimension"] = 8
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with patch.object(
        CodeVectorStore,
        "_initialize_embedding_model",
        return_value=_FakeEmbedding(8),
    ):
        loaded = CodeVectorStore(
            embedding_model=model,
            dimension=8,
            store_path=str(path),
        )

    with pytest.raises(ValueError, match="FAISS dimension mismatch"):
        loaded.load()
