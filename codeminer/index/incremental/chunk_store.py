# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Per-file versioned code chunk store for incremental indexing.

Maintains a dictionary mapping each source file to its current list of
``VersionedChunk`` objects.  Each ``VersionedChunk`` wraps a ``CodeChunk``
with a content hash and the git commit SHA at which it was last updated.

Change detection works by comparing content hashes between the old and new
chunk lists for a file — only chunks whose content actually changed need
re-embedding.
"""

from __future__ import annotations

import hashlib
import json
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ...code_chunking.base import CodeChunk
from ...log_utils import get_logger

logger = get_logger(__name__)


def _hash_content(content: str) -> str:
    """Return a stable MD5 hex digest for a chunk's content string."""
    return hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()


@dataclass
class VersionedChunk:
    """A ``CodeChunk`` annotated with version metadata."""

    chunk: CodeChunk
    content_hash: str
    commit_sha: str
    level: str = "l2"

    @classmethod
    def from_chunk(
        cls, chunk: CodeChunk, commit_sha: str, level: str = "l2"
    ) -> "VersionedChunk":
        return cls(
            chunk=chunk,
            content_hash=_hash_content(chunk.content),
            commit_sha=commit_sha,
            level=level,
        )


class IncrementalChunkStore:
    """
    Dictionary-of-lists that tracks the current chunk state per source file.

    Structure: ``Dict[file_path -> List[VersionedChunk]]``

    Typical usage
    -------------
    First build (full repo)::

        store = IncrementalChunkStore.from_chunks(all_chunks, commit_sha="abc123")
        store.save(path)

    Subsequent incremental update::

        store = IncrementalChunkStore.load(path)
        added, removed = store.update_file(file_path, new_chunks, commit_sha="def456")
        store.save(path)
    """

    def __init__(self) -> None:
        self._store: Dict[str, List[VersionedChunk]] = {}

    # ------------------------------------------------------------------
    # Key helpers (encode level into store key for L0/L2 separation)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_key(file_path: str, level: str = "l2") -> str:
        """Build a store key that separates L0 and L2 entries for the same file."""
        if level == "l2":
            return file_path  # backward compatible — L2 uses plain path
        return f"{file_path}::{level}"

    @staticmethod
    def _parse_key(key: str) -> Tuple[str, str]:
        """Return ``(file_path, level)`` from a store key."""
        if "::" in key:
            file_path, level = key.rsplit("::", 1)
            return file_path, level
        return key, "l2"

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def update_file(
        self,
        file_path: str,
        new_chunks: List[CodeChunk],
        commit_sha: str,
        level: str = "l2",
    ) -> Tuple[List[CodeChunk], List[CodeChunk]]:
        """
        Replace the chunk list for *file_path* with *new_chunks*.

        Compares content hashes between old and new lists to determine the
        delta.  A chunk is considered **added** if its content hash does not
        appear in the previous list; **removed** if its content hash
        disappears.

        Args:
            file_path: Absolute path to the source file.
            new_chunks: Fresh chunks produced by re-running the chunker.
            commit_sha: Current git HEAD SHA.
            level: Index level (``"l0"`` or ``"l2"``).

        Returns:
            ``(added_chunks, removed_chunks)`` — each list contains the plain
            ``CodeChunk`` namedtuples (without version wrapper) for callers
            that only need to know *which* chunks changed.
        """
        store_key = self._make_key(file_path, level)
        old_versioned = self._store.get(store_key, [])
        old_hashes: Set[str] = {vc.content_hash for vc in old_versioned}

        new_versioned: List[VersionedChunk] = []
        added: List[CodeChunk] = []
        new_hashes: Set[str] = set()

        for chunk in new_chunks:
            h = _hash_content(chunk.content)
            new_hashes.add(h)
            vc = VersionedChunk(
                chunk=chunk, content_hash=h, commit_sha=commit_sha, level=level
            )
            new_versioned.append(vc)
            if h not in old_hashes:
                added.append(chunk)

        removed: List[CodeChunk] = [
            vc.chunk for vc in old_versioned if vc.content_hash not in new_hashes
        ]

        self._store[store_key] = new_versioned
        logger.debug(
            "update_file %s [%s]: +%d chunks, -%d chunks",
            file_path,
            level,
            len(added),
            len(removed),
        )
        return added, removed

    def delete_file(
        self, file_path: str, level: Optional[str] = None
    ) -> List[CodeChunk]:
        """
        Remove *file_path* from the store.

        Args:
            file_path: Absolute path to the source file.
            level: If given, only delete chunks at that level.  If ``None``,
                delete chunks at **all** levels for this file.

        Returns the ``CodeChunk`` objects that were removed.
        """
        removed: List[CodeChunk] = []
        if level is not None:
            key = self._make_key(file_path, level)
            old = self._store.pop(key, [])
            removed = [vc.chunk for vc in old]
        else:
            # Remove both L2 (plain key) and any L0/other level keys
            for lvl in ("l2", "l0"):
                key = self._make_key(file_path, lvl)
                old = self._store.pop(key, [])
                removed.extend(vc.chunk for vc in old)
        logger.debug("delete_file %s: removed %d chunks", file_path, len(removed))
        return removed

    # ------------------------------------------------------------------
    # Read-only helpers
    # ------------------------------------------------------------------

    def get_chunks_for_file(self, file_path: str, level: str = "l2") -> List[CodeChunk]:
        """Return current chunks for *file_path* at *level* (empty list if unknown)."""
        key = self._make_key(file_path, level)
        return [vc.chunk for vc in self._store.get(key, [])]

    def get_all_chunks(self, level: Optional[str] = None) -> List[CodeChunk]:
        """Flatten the store to a list of ``CodeChunk`` objects.

        Args:
            level: If given, return only chunks at that level.  ``None`` returns all.
        """
        result: List[CodeChunk] = []
        for versioned_list in self._store.values():
            for vc in versioned_list:
                if level is None or vc.level == level:
                    result.append(vc.chunk)
        return result

    def get_all_versioned(self, level: Optional[str] = None) -> List[VersionedChunk]:
        """Flatten the store to a list of ``VersionedChunk`` objects.

        Args:
            level: If given, return only chunks at that level.  ``None`` returns all.
        """
        result: List[VersionedChunk] = []
        for versioned_list in self._store.values():
            for vc in versioned_list:
                if level is None or vc.level == level:
                    result.append(vc)
        return result

    def get_all_content_hashes(self) -> Set[str]:
        """Return the set of all content hashes currently live in the store.

        Used by ``EmbeddingsCache.prune()`` to evict stale cache entries.
        """
        return {
            vc.content_hash
            for versioned_list in self._store.values()
            for vc in versioned_list
        }

    def file_count(self) -> int:
        """Number of tracked source files."""
        return len(self._store)

    def chunk_count(self) -> int:
        """Total number of tracked chunks across all files."""
        return sum(len(v) for v in self._store.values())

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Save the store as JSON to *path*.

        The JSON format stores an array of versioned-chunk objects per store
        key, making the file human-readable and portable across Python
        versions.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Serialise to a JSON-friendly structure
        data: Dict[str, list] = {}
        for key, vc_list in self._store.items():
            data[key] = [
                {
                    "content": vc.chunk.content,
                    "start_line": vc.chunk.start_line,
                    "end_line": vc.chunk.end_line,
                    "chunk_type": vc.chunk.chunk_type,
                    "name": vc.chunk.name,
                    "file": vc.chunk.file,
                    "node_id": vc.chunk.node_id,
                    "content_hash": vc.content_hash,
                    "commit_sha": vc.commit_sha,
                    "level": getattr(vc, "level", "l2"),
                }
                for vc in vc_list
            ]
        json_path = path.with_suffix(".json")
        with open(json_path, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        # Also write pickle for backward compat during migration
        with open(path, "wb") as f:
            pickle.dump(self._store, f)
        logger.debug(
            "ChunkStore saved (%d files, %d chunks) → %s",
            self.file_count(),
            self.chunk_count(),
            json_path,
        )

    @classmethod
    def load(cls, path: Path) -> "IncrementalChunkStore":
        """Load from JSON if available, falling back to pickle."""
        path = Path(path)
        json_path = path.with_suffix(".json")
        store = cls()

        if json_path.exists():
            with open(json_path, "r") as f:
                data = json.load(f)
            for key, entries in data.items():
                store._store[key] = [
                    VersionedChunk(
                        chunk=CodeChunk(
                            content=e["content"],
                            start_line=e["start_line"],
                            end_line=e["end_line"],
                            chunk_type=e["chunk_type"],
                            name=e["name"],
                            file=e["file"],
                            node_id=e["node_id"],
                        ),
                        content_hash=e["content_hash"],
                        commit_sha=e["commit_sha"],
                        level=e.get("level", "l2"),
                    )
                    for e in entries
                ]
            logger.debug(
                "ChunkStore loaded from JSON (%d files, %d chunks) ← %s",
                store.file_count(),
                store.chunk_count(),
                json_path,
            )
        elif path.exists():
            # Backward compat: load legacy pickle
            with open(path, "rb") as f:
                store._store = pickle.load(f)
            logger.debug(
                "ChunkStore loaded from pickle (%d files, %d chunks) ← %s",
                store.file_count(),
                store.chunk_count(),
                path,
            )
        else:
            raise FileNotFoundError(f"No chunk store found at {json_path} or {path}")
        return store

    @classmethod
    def load_or_empty(cls, path: Path) -> "IncrementalChunkStore":
        """Load from *path* if it exists; otherwise return an empty store."""
        path = Path(path)
        json_path = path.with_suffix(".json")
        if json_path.exists() or path.exists():
            return cls.load(path)
        return cls()

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_chunks(
        cls,
        chunks: List[CodeChunk],
        commit_sha: str,
        level: str = "l2",
    ) -> "IncrementalChunkStore":
        """
        Build a store from an initial full-repository chunk list.

        Used on the very first index build to seed the store before any
        incremental updates have occurred.
        """
        store = cls()
        # Group by file
        by_file: Dict[str, List[CodeChunk]] = {}
        for chunk in chunks:
            by_file.setdefault(chunk.file, []).append(chunk)

        for file_path, file_chunks in by_file.items():
            key = cls._make_key(file_path, level)
            store._store[key] = [
                VersionedChunk.from_chunk(c, commit_sha, level=level)
                for c in file_chunks
            ]

        logger.info(
            "ChunkStore created from %d %s chunks across %d files (commit %s)",
            len(chunks),
            level,
            len(by_file),
            commit_sha[:8] if commit_sha else "unknown",
        )
        return store

    def add_chunks(
        self,
        chunks: List[CodeChunk],
        commit_sha: str,
        level: str = "l2",
    ) -> None:
        """
        Add chunks to an existing store (used to merge L0 chunks into a store
        that already has L2 chunks).
        """
        by_file: Dict[str, List[CodeChunk]] = {}
        for chunk in chunks:
            by_file.setdefault(chunk.file, []).append(chunk)

        for file_path, file_chunks in by_file.items():
            key = self._make_key(file_path, level)
            self._store[key] = [
                VersionedChunk.from_chunk(c, commit_sha, level=level)
                for c in file_chunks
            ]

    def __repr__(self) -> str:
        return (
            f"IncrementalChunkStore("
            f"files={self.file_count()}, "
            f"chunks={self.chunk_count()})"
        )
