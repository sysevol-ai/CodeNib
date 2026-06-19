# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for EmbeddingsCache.
"""

from pathlib import Path

import numpy as np

from codeminer.index.incremental.embeddings_cache import EmbeddingsCache


def make_vec(val: float = 1.0, dim: int = 4) -> np.ndarray:
    return np.full(dim, val, dtype=np.float32)


class TestBasicOperations:
    def test_put_and_get(self):
        cache = EmbeddingsCache()
        vec = make_vec(1.0)
        cache.put("hash1", vec)
        result = cache.get("hash1")
        assert result is not None
        np.testing.assert_array_equal(result, vec)

    def test_has_true_after_put(self):
        cache = EmbeddingsCache()
        cache.put("h", make_vec())
        assert cache.has("h")

    def test_has_false_for_missing(self):
        cache = EmbeddingsCache()
        assert not cache.has("unknown")

    def test_get_returns_none_for_missing(self):
        cache = EmbeddingsCache()
        assert cache.get("missing") is None

    def test_size_reflects_entries(self):
        cache = EmbeddingsCache()
        assert cache.size() == 0
        cache.put("h1", make_vec(1.0))
        cache.put("h2", make_vec(2.0))
        assert cache.size() == 2

    def test_put_batch(self):
        cache = EmbeddingsCache()
        hashes = ["a", "b", "c"]
        vecs = [make_vec(float(i)) for i in range(3)]
        cache.put_batch(hashes, vecs)
        assert cache.size() == 3
        for h, v in zip(hashes, vecs, strict=True):
            np.testing.assert_array_equal(cache.get(h), v)

    def test_get_batch_returns_only_cached(self):
        cache = EmbeddingsCache()
        cache.put("present", make_vec(1.0))
        result = cache.get_batch(["present", "absent"])
        assert set(result.keys()) == {"present"}

    def test_overwrite_existing_entry(self):
        cache = EmbeddingsCache()
        cache.put("h", make_vec(1.0))
        cache.put("h", make_vec(9.0))
        np.testing.assert_array_equal(cache.get("h"), make_vec(9.0))

    def test_stored_as_float32(self):
        cache = EmbeddingsCache()
        vec = np.array([1.0, 2.0], dtype=np.float64)
        cache.put("h", vec)
        assert cache.get("h").dtype == np.float32


class TestPrune:
    def test_prune_removes_stale(self):
        cache = EmbeddingsCache()
        cache.put("keep", make_vec())
        cache.put("stale", make_vec())
        pruned = cache.prune({"keep"})
        assert pruned == 1
        assert cache.has("keep")
        assert not cache.has("stale")

    def test_prune_keeps_all_active(self):
        cache = EmbeddingsCache()
        cache.put("a", make_vec())
        cache.put("b", make_vec())
        pruned = cache.prune({"a", "b"})
        assert pruned == 0
        assert cache.size() == 2

    def test_prune_empty_active_clears_all(self):
        cache = EmbeddingsCache()
        cache.put("x", make_vec())
        cache.prune(set())
        assert cache.size() == 0


class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path: Path):
        cache = EmbeddingsCache()
        cache.put("h1", make_vec(1.0))
        cache.put("h2", make_vec(2.0))
        path = tmp_path / "cache.pkl"
        cache.save(path)

        loaded = EmbeddingsCache.load(path)
        assert loaded.size() == 2
        np.testing.assert_array_equal(loaded.get("h1"), make_vec(1.0))
        np.testing.assert_array_equal(loaded.get("h2"), make_vec(2.0))

    def test_load_or_empty_missing(self, tmp_path: Path):
        cache = EmbeddingsCache.load_or_empty(tmp_path / "nonexistent.pkl")
        assert cache.size() == 0

    def test_load_or_empty_existing(self, tmp_path: Path):
        cache = EmbeddingsCache()
        cache.put("h", make_vec())
        path = tmp_path / "cache.pkl"
        cache.save(path)

        loaded = EmbeddingsCache.load_or_empty(path)
        assert loaded.size() == 1
