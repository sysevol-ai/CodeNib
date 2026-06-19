# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for Phase 5: Persistence migration from pickle to JSON + NPZ.

Verifies:
- JSON/NPZ save and load round-trip for both ChunkStore and EmbeddingsCache
- Backward compatibility: pickle files are loaded when JSON files are absent
- Migration: loading from pickle and saving produces JSON files
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np

from codeminer.code_chunking.base import CodeChunk
from codeminer.index.incremental.chunk_store import IncrementalChunkStore
from codeminer.index.incremental.embeddings_cache import EmbeddingsCache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk(name: str, content: str) -> CodeChunk:
    return CodeChunk(
        content=content,
        start_line=0,
        end_line=2,
        chunk_type="function",
        name=name,
        file=f"/repo/{name}.py",
        node_id=f"/repo/{name}.py:{name}()",
    )


# ---------------------------------------------------------------------------
# ChunkStore persistence tests
# ---------------------------------------------------------------------------


class TestChunkStoreJsonPersistence:
    def test_save_creates_json_file(self, tmp_path: Path):
        store = IncrementalChunkStore.from_chunks(
            [_make_chunk("alpha", "def alpha():\n    pass\n")],
            "abc123",
        )
        pkl_path = tmp_path / "chunk_store.pkl"
        store.save(pkl_path)

        json_path = tmp_path / "chunk_store.json"
        assert json_path.exists()
        # Verify it's valid JSON
        data = json.loads(json_path.read_text())
        assert isinstance(data, dict)

    def test_json_round_trip(self, tmp_path: Path):
        chunks = [
            _make_chunk("a", "def a():\n    return 1\n"),
            _make_chunk("b", "def b():\n    return 2\n"),
        ]
        store = IncrementalChunkStore.from_chunks(chunks, "sha1")
        pkl_path = tmp_path / "chunk_store.pkl"
        store.save(pkl_path)

        loaded = IncrementalChunkStore.load(pkl_path)
        assert loaded.chunk_count() == 2

        all_chunks = loaded.get_all_chunks()
        names = {c.name for c in all_chunks}
        assert names == {"a", "b"}

    def test_json_preserves_level_field(self, tmp_path: Path):
        store = IncrementalChunkStore()
        l2 = _make_chunk("f", "def f():\n    pass\n")
        l0 = _make_chunk("skel", "# skeleton\n")
        store.update_file("/f.py", [l2], "sha1", level="l2")
        store.update_file("/f.py", [l0], "sha1", level="l0")

        pkl_path = tmp_path / "store.pkl"
        store.save(pkl_path)

        loaded = IncrementalChunkStore.load(pkl_path)
        l2_vc = loaded.get_all_versioned(level="l2")
        l0_vc = loaded.get_all_versioned(level="l0")
        assert len(l2_vc) == 1
        assert len(l0_vc) == 1

    def test_backward_compat_loads_pickle(self, tmp_path: Path):
        """When only a pickle file exists (no JSON), load from pickle."""
        chunks = [_make_chunk("old", "def old():\n    pass\n")]
        store = IncrementalChunkStore.from_chunks(chunks, "sha0")

        pkl_path = tmp_path / "chunk_store.pkl"
        # Write ONLY pickle (simulate legacy)
        with open(pkl_path, "wb") as f:
            pickle.dump(store._store, f)

        # Ensure no JSON file
        json_path = tmp_path / "chunk_store.json"
        assert not json_path.exists()

        loaded = IncrementalChunkStore.load(pkl_path)
        assert loaded.chunk_count() == 1
        assert loaded.get_all_chunks()[0].name == "old"

    def test_load_or_empty_nonexistent(self, tmp_path: Path):
        store = IncrementalChunkStore.load_or_empty(tmp_path / "nope.pkl")
        assert store.chunk_count() == 0


# ---------------------------------------------------------------------------
# EmbeddingsCache persistence tests
# ---------------------------------------------------------------------------


class TestEmbeddingsCacheNpzPersistence:
    def test_save_creates_json_and_npz(self, tmp_path: Path):
        cache = EmbeddingsCache()
        cache.put("hash1", np.ones(4, dtype=np.float32))
        cache.put("hash2", np.zeros(4, dtype=np.float32))

        pkl_path = tmp_path / "cache.pkl"
        cache.save(pkl_path)

        assert (tmp_path / "cache.json").exists()
        assert (tmp_path / "cache.npz").exists()

    def test_json_npz_round_trip(self, tmp_path: Path):
        cache = EmbeddingsCache()
        v1 = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32)
        v2 = np.array([5.0, 6.0, 7.0, 8.0], dtype=np.float32)
        cache.put("aaa", v1)
        cache.put("bbb", v2)

        pkl_path = tmp_path / "emb.pkl"
        cache.save(pkl_path)

        loaded = EmbeddingsCache.load(pkl_path)
        assert loaded.size() == 2
        np.testing.assert_array_equal(loaded.get("aaa"), v1)
        np.testing.assert_array_equal(loaded.get("bbb"), v2)

    def test_vectors_are_float32_after_load(self, tmp_path: Path):
        cache = EmbeddingsCache()
        cache.put("h", np.array([1.0, 2.0], dtype=np.float64))  # store as f64

        pkl_path = tmp_path / "c.pkl"
        cache.save(pkl_path)

        loaded = EmbeddingsCache.load(pkl_path)
        vec = loaded.get("h")
        assert vec.dtype == np.float32

    def test_backward_compat_loads_pickle(self, tmp_path: Path):
        """When only a pickle file exists (no JSON/NPZ), load from pickle."""
        original = {"hash_x": np.array([1.0, 2.0], dtype=np.float32)}

        pkl_path = tmp_path / "cache.pkl"
        with open(pkl_path, "wb") as f:
            pickle.dump(original, f)

        # Ensure no JSON/NPZ
        assert not (tmp_path / "cache.json").exists()
        assert not (tmp_path / "cache.npz").exists()

        loaded = EmbeddingsCache.load(pkl_path)
        assert loaded.size() == 1
        np.testing.assert_array_equal(loaded.get("hash_x"), original["hash_x"])

    def test_empty_cache_round_trip(self, tmp_path: Path):
        cache = EmbeddingsCache()
        pkl_path = tmp_path / "empty.pkl"
        cache.save(pkl_path)

        loaded = EmbeddingsCache.load(pkl_path)
        assert loaded.size() == 0

    def test_load_or_empty_nonexistent(self, tmp_path: Path):
        cache = EmbeddingsCache.load_or_empty(tmp_path / "nope.pkl")
        assert cache.size() == 0
