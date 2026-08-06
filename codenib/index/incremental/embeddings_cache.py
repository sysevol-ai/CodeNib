# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Content-hash keyed embedding vector cache.

Prevents re-embedding identical code chunks across index rebuilds.
Keyed by the MD5 content hash produced by ``IncrementalChunkStore``, so the
same logical code (regardless of which file or function name it lives in)
is never sent to the embedding model twice.
"""

from __future__ import annotations

import json
import os
import pickle
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set

import numpy as np

from ...log_utils import get_logger

logger = get_logger(__name__)


class EmbeddingsCache:
    """
    Persistent cache: ``content_hash -> np.ndarray`` (embedding vector).

    Typical usage
    -------------
    ::

        cache = EmbeddingsCache.load_or_empty(path)

        # Check before calling the model
        missing_hashes = [h for h in hashes if not cache.has(h)]
        new_embeddings = model.embed_documents([content_for[h] for h in missing_hashes])
        for h, vec in zip(missing_hashes, new_embeddings):
            cache.put(h, np.array(vec, dtype=np.float32))

        # Prune stale entries so the cache doesn't grow unboundedly
        cache.prune(chunk_store.get_all_content_hashes())
        cache.save(path)
    """

    def __init__(self) -> None:
        self._cache: Dict[str, np.ndarray] = {}

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def has(self, content_hash: str) -> bool:
        """Return True if the embedding for *content_hash* is cached."""
        return content_hash in self._cache

    def get(self, content_hash: str) -> Optional[np.ndarray]:
        """Return the cached vector, or ``None`` if not present."""
        return self._cache.get(content_hash)

    def get_batch(self, hashes: List[str]) -> Dict[str, np.ndarray]:
        """Return a dict containing only the hashes that are cached."""
        return {h: self._cache[h] for h in hashes if h in self._cache}

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def put(self, content_hash: str, embedding: np.ndarray) -> None:
        """Store an embedding vector for *content_hash*."""
        self._cache[content_hash] = np.asarray(embedding, dtype=np.float32)

    def put_batch(self, hashes: List[str], embeddings: List[np.ndarray]) -> None:
        """Store multiple (hash, embedding) pairs at once."""
        for h, vec in zip(hashes, embeddings, strict=True):
            self.put(h, vec)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune(self, active_hashes: Set[str]) -> int:
        """
        Remove cache entries whose hash is not in *active_hashes*.

        Call this after updating the ``IncrementalChunkStore`` so that
        embeddings for deleted or replaced chunks don't accumulate forever.

        Returns:
            Number of entries removed.
        """
        stale = [h for h in self._cache if h not in active_hashes]
        for h in stale:
            del self._cache[h]
        if stale:
            logger.debug("EmbeddingsCache pruned %d stale entries.", len(stale))
        return len(stale)

    def size(self) -> int:
        """Number of cached embeddings."""
        return len(self._cache)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Save cache as JSON index + NPZ vectors to *path*.

        Files created:
        - ``<path>.json`` — ordered list of content hashes (the index)
        - ``<path>.npz``  — numpy compressed archive with a single
          ``vectors`` key (shape ``[N, dim]``, ``float32``)
        - ``<path>`` (pickle) — backward-compat during migration
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        hashes = list(self._cache.keys())
        if hashes:
            vectors = np.stack([self._cache[h] for h in hashes])
        else:
            vectors = np.empty((0, 0), dtype=np.float32)

        json_path = path.with_suffix(".json")
        npz_path = path.with_suffix(".npz")

        hash_width = max((len(value) for value in hashes), default=1)
        embedded_hashes = np.asarray(hashes, dtype=f"<U{hash_width}")

        # Publish the self-describing NPZ first. If the process stops before
        # the JSON replace, the loader sees a generation mismatch instead of
        # binding old vectors to new hashes with the same cardinality.
        temp_npz = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w+b",
                dir=npz_path.parent,
                prefix=f".{npz_path.name}.",
                delete=False,
            ) as handle:
                temp_npz = Path(handle.name)
                np.savez_compressed(
                    handle,
                    vectors=vectors,
                    hashes=embedded_hashes,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_npz, npz_path)
        finally:
            if temp_npz is not None:
                temp_npz.unlink(missing_ok=True)

        temp_json = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=json_path.parent,
                prefix=f".{json_path.name}.",
                delete=False,
            ) as handle:
                temp_json = Path(handle.name)
                json.dump(hashes, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_json, json_path)
        finally:
            if temp_json is not None:
                temp_json.unlink(missing_ok=True)

        # Also write pickle for backward compat
        with open(path, "wb") as f:
            pickle.dump(self._cache, f)

        logger.debug(
            "EmbeddingsCache saved (%d entries) → %s + %s",
            self.size(),
            json_path,
            npz_path,
        )

    @classmethod
    def load(cls, path: Path) -> "EmbeddingsCache":
        """Load from JSON+NPZ if available, falling back to pickle."""
        path = Path(path)
        json_path = path.with_suffix(".json")
        npz_path = path.with_suffix(".npz")
        cache = cls()

        if json_path.exists() and npz_path.exists():
            with open(json_path, "r", encoding="utf-8") as f:
                hashes = json.load(f)
            if (
                not isinstance(hashes, list)
                or not all(isinstance(value, str) for value in hashes)
                or len(set(hashes)) != len(hashes)
            ):
                raise ValueError(
                    f"EmbeddingsCache corrupted: invalid hash index in {json_path}"
                )

            with np.load(npz_path, allow_pickle=False) as data:
                if "vectors" not in data:
                    raise ValueError(
                        f"EmbeddingsCache corrupted: vectors missing from {npz_path}"
                    )
                vectors = np.asarray(data["vectors"], dtype=np.float32).copy()
                embedded_hashes = None
                if "hashes" in data:
                    raw_hashes = data["hashes"].tolist()
                    if not isinstance(raw_hashes, list):
                        raise ValueError(
                            "EmbeddingsCache corrupted: embedded hash index "
                            f"is invalid in {npz_path}"
                        )
                    embedded_hashes = [str(value) for value in raw_hashes]

            if vectors.ndim != 2:
                raise ValueError(
                    f"EmbeddingsCache corrupted: expected a vector matrix in {npz_path}"
                )
            if len(hashes) != vectors.shape[0]:
                raise ValueError(
                    f"EmbeddingsCache corrupted: {len(hashes)} hashes "
                    f"but {vectors.shape[0]} vectors in {json_path}"
                )
            if embedded_hashes is not None and embedded_hashes != hashes:
                raise ValueError(
                    "EmbeddingsCache corrupted: JSON hash order does not match "
                    f"the vector generation in {npz_path}"
                )
            for i, h in enumerate(hashes):
                cache._cache[h] = vectors[i]
            logger.debug(
                "EmbeddingsCache loaded from JSON+NPZ (%d entries) ← %s",
                cache.size(),
                json_path,
            )
        elif path.exists():
            with open(path, "rb") as f:
                cache._cache = pickle.load(f)
            logger.debug(
                "EmbeddingsCache loaded from pickle (%d entries) ← %s",
                cache.size(),
                path,
            )
        else:
            raise FileNotFoundError(
                f"No embeddings cache found at {json_path} or {path}"
            )
        return cache

    @classmethod
    def load_or_empty(cls, path: Path) -> "EmbeddingsCache":
        """Load from *path* if it exists; otherwise return an empty cache."""
        path = Path(path)
        json_path = path.with_suffix(".json")
        npz_path = path.with_suffix(".npz")
        if (json_path.exists() and npz_path.exists()) or path.exists():
            return cls.load(path)
        return cls()

    def __repr__(self) -> str:
        return f"EmbeddingsCache(size={self.size()})"
