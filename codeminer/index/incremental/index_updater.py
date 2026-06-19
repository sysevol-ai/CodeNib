# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Incremental index update orchestrator.

Ties together git diff detection, per-file rechunking, embedding cache
lookup, and FAISS index rebuild so that only genuinely changed code chunks
are sent to the embedding model.

Typical usage
-------------
::

    updater = IncrementalIndexUpdater(
        chunker=CodeChunker(language="python", repo_config=...),
        embedding_model=vector_store.embedding,
        diff_detector=GitDiffDetector(),
    )
    result = updater.update(
        repo_path="/path/to/repo",
        vector_store=vector_store,
        chunk_store=chunk_store,
        embeddings_cache=embeddings_cache,
        last_commit="abc123",
    )
    print(f"Re-embedded {result.chunks_reembedded} / {result.total_chunks} chunks")
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Tuple

import numpy as np

from ...log_utils import get_logger
from ..embedding.vector_store import _Document
from .chunk_store import IncrementalChunkStore, VersionedChunk
from .embeddings_cache import EmbeddingsCache
from .git_diff import GitDiffDetector, RepoChanges

if TYPE_CHECKING:
    from ...code_chunker import CodeChunker
    from ..embedding.vector_store import CodeVectorStore

logger = get_logger(__name__)


@dataclass
class UpdateResult:
    """Summary statistics from a single incremental update run."""

    files_changed: int = 0
    chunks_added: int = 0
    chunks_removed: int = 0
    chunks_reembedded: int = 0
    chunks_from_cache: int = 0
    total_chunks: int = 0
    new_commit: str = ""
    duration_seconds: float = 0.0

    @property
    def cache_hit_rate(self) -> float:
        if self.total_chunks == 0:
            return 0.0
        return self.chunks_from_cache / self.total_chunks


class IncrementalIndexUpdater:
    """
    Orchestrates the incremental embedding update pipeline.

    Args:
        chunker: A ``CodeChunker`` configured for the target repository.
        embedding_model: The embedding model instance (same one used by the
            ``CodeVectorStore`` so vectors are comparable).
        diff_detector: A ``GitDiffDetector`` instance to query git.
        l0_chunker: Optional separate chunker for L0 (file skeleton) chunks.
            When provided, L0 skeletons are incrementally updated alongside L2.
    """

    def __init__(
        self,
        chunker: "CodeChunker",
        embedding_model,
        diff_detector: Optional[GitDiffDetector] = None,
        l0_chunker: Optional["CodeChunker"] = None,
    ) -> None:
        self._chunker = chunker
        self._embedding_model = embedding_model
        self._diff_detector = diff_detector or GitDiffDetector()
        self._l0_chunker = l0_chunker

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        repo_path: str,
        vector_store: "CodeVectorStore",
        chunk_store: IncrementalChunkStore,
        embeddings_cache: EmbeddingsCache,
        last_commit: str,
    ) -> UpdateResult:
        """
        Run the full incremental update pipeline.

        Steps
        -----
        1. Detect changed files since *last_commit*.
        2. Rechunk added/modified files; update ``chunk_store``.
        3. Remove deleted files from ``chunk_store``.
        4. Assemble embeddings for *all* current chunks:
           - cache hit  → reuse stored vector (no model call)
           - cache miss → embed via model, store in cache
        5. Rebuild the FAISS index from assembled (document, embedding) pairs.
        6. Prune stale cache entries.

        Returns:
            ``UpdateResult`` with counts and timing.
        """
        t_start = time.monotonic()
        result = UpdateResult()

        # ---- Step 1: detect changes ------------------------------------
        changes: RepoChanges = self._diff_detector.detect_changes(
            repo_path, last_commit
        )
        result.new_commit = changes.new_commit
        result.files_changed = len(changes.affected) + len(changes.deleted)

        if changes.is_empty:
            logger.info(
                "No source files changed since %s; skipping update.", last_commit[:8]
            )
            result.total_chunks = chunk_store.chunk_count()
            result.duration_seconds = time.monotonic() - t_start
            return result

        # ---- Step 2: rechunk affected files (L2 + optional L0) ----------
        for file_path in changes.affected:
            # L2 rechunk
            try:
                new_chunks = self._chunker.chunk_file(file_path)
            except Exception as exc:
                logger.warning("Failed to L2-chunk %s: %s", file_path, exc)
                new_chunks = []

            added, removed = chunk_store.update_file(
                file_path, new_chunks, changes.new_commit, level="l2"
            )
            result.chunks_added += len(added)
            result.chunks_removed += len(removed)

            # L0 rechunk (file skeleton)
            if self._l0_chunker is not None:
                try:
                    l0_chunks = self._l0_chunker.chunk_file(
                        file_path, skeleton_mode=True
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to L0-chunk %s: %s — keeping previous L0 data",
                        file_path,
                        exc,
                    )
                    continue
                chunk_store.update_file(
                    file_path, l0_chunks, changes.new_commit, level="l0"
                )

        # ---- Step 3: handle deleted files ------------------------------
        for file_path in changes.deleted:
            removed = chunk_store.delete_file(file_path)  # removes both levels
            result.chunks_removed += len(removed)

        # ---- Step 4-5: assemble embeddings & rebuild per level ----------
        levels_to_rebuild = ["l2"]
        if self._l0_chunker is not None:
            levels_to_rebuild.append("l0")

        total_from_cache = 0
        total_reembedded = 0

        for level in levels_to_rebuild:
            level_versioned = chunk_store.get_all_versioned(level=level)

            # Partition into cache hits and misses
            hits: List[Tuple[VersionedChunk, np.ndarray]] = []
            misses: List[VersionedChunk] = []

            for vc in level_versioned:
                cached = embeddings_cache.get(vc.content_hash)
                if cached is not None:
                    hits.append((vc, cached))
                else:
                    misses.append(vc)

            total_from_cache += len(hits)
            total_reembedded += len(misses)

            # Embed cache-miss chunks
            if misses:
                miss_contents = [vc.chunk.content for vc in misses]
                raw_vectors = self._embedding_model.embed_documents(miss_contents)
                for vc, raw_vec in zip(misses, raw_vectors, strict=True):
                    vec = np.asarray(raw_vec, dtype=np.float32)
                    embeddings_cache.put(vc.content_hash, vec)
                    hits.append((vc, vec))

            # Rebuild FAISS for this level (delta when possible)
            if hits:
                documents, embeddings = self._build_doc_embedding_pairs(hits)
                changed_hashes = {vc.content_hash for vc in misses}
                vector_store.delta_update(
                    documents, embeddings, changed_hashes, level=level
                )

        result.total_chunks = chunk_store.chunk_count()
        result.chunks_from_cache = total_from_cache
        result.chunks_reembedded = total_reembedded

        logger.info(
            "Embedding: %d from cache, %d need model inference (total %d)",
            total_from_cache,
            total_reembedded,
            result.total_chunks,
        )

        # ---- Step 6: prune stale cache entries -------------------------
        pruned = embeddings_cache.prune(chunk_store.get_all_content_hashes())
        if pruned:
            logger.debug("Pruned %d stale embedding cache entries.", pruned)

        result.duration_seconds = time.monotonic() - t_start
        logger.info(
            "Incremental update complete in %.2fs — "
            "files changed: %d, chunks +%d/-%d, "
            "re-embedded: %d, from cache: %d (hit rate %.1f%%)",
            result.duration_seconds,
            result.files_changed,
            result.chunks_added,
            result.chunks_removed,
            result.chunks_reembedded,
            result.chunks_from_cache,
            result.cache_hit_rate * 100,
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_doc_embedding_pairs(
        versioned_with_embeddings: List[Tuple[VersionedChunk, np.ndarray]],
    ) -> Tuple[List[_Document], List[np.ndarray]]:
        """Convert (VersionedChunk, embedding) pairs to (Document, embedding) pairs."""
        documents: list = []
        embeddings: List[np.ndarray] = []

        for i, (vc, vec) in enumerate(versioned_with_embeddings):
            chunk = vc.chunk
            metadata = {
                "chunk_id": i,
                "chunk_type": chunk.chunk_type,
                "name": chunk.name,
                "file": chunk.file,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "node_id": chunk.node_id or "",
                "level": vc.level,
                "content_hash": vc.content_hash,
                "commit_sha": vc.commit_sha,
            }
            documents.append(_Document(page_content=chunk.content, metadata=metadata))
            embeddings.append(vec)

        return documents, embeddings
