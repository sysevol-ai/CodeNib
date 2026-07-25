# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Index Compiler — Phase 1 of the two-phase compilation architecture.

Orchestrates index builders and produces a ``RepoManifest`` that serves
as the linking protocol for Phase 2 (query compilation).

Usage::

    builder_registry = IndexBuilderRegistry()
    register_default_builders(builder_registry, languages=["python"])

    compiler = IndexCompiler(builder_registry)
    manifest = compiler.compile_repo("/path/to/repo")
    # → writes .codenib_cache/repo_manifest.json
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional

from ..paths import REPO_INDEX_DIRNAME
from .index_builders import IndexBuilderRegistry
from .manifest import MANIFEST_FILENAME, IndexEntry, RepoManifest
from .resources import IndexStatus

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IndexCompilerConfig:
    """Configuration for the IndexCompiler."""

    cache_dir_name: str = REPO_INDEX_DIRNAME
    index_types: List[str] = field(
        default_factory=lambda: ["bm25", "vector", "symbol_graph"],
    )
    languages: List[str] = field(default_factory=lambda: ["python"])


@dataclass(slots=True)
class BuildResult:
    """Result of building a single index."""

    index_type: str
    success: bool
    status: Optional[IndexStatus] = None
    error: Optional[str] = None
    duration_seconds: float = 0.0


class IndexCompiler:
    """
    Orchestrate Phase 1: build all indexes for a repository and write
    a manifest.

    The manifest (``repo_manifest.json``) is the linking protocol that
    Phase 2 (``AgentRunner`` + ``ResourceGuard``) reads to know
    what indexes are available at query time.
    """

    def __init__(
        self,
        builder_registry: IndexBuilderRegistry,
        config: Optional[IndexCompilerConfig] = None,
    ) -> None:
        self._builders = builder_registry
        self._config = config or IndexCompilerConfig()

    def compile_repo(
        self,
        repo_path: str,
        *,
        index_types: Optional[List[str]] = None,
        cache_dir: Optional[str] = None,
    ) -> RepoManifest:
        """
        Build all requested indexes and write the repo manifest.

        Args:
            repo_path: Absolute path to the repository root.
            index_types: Which indexes to build (default: config.index_types).
            cache_dir: Override for the cache directory
                (default: ``repo_path/.codenib_cache``).

        Returns:
            The completed ``RepoManifest``.
        """
        return self._compile(
            repo_path, index_types=index_types, cache_dir=cache_dir, last_commit=None
        )

    def update_repo(
        self,
        repo_path: str,
        *,
        index_types: Optional[List[str]] = None,
        cache_dir: Optional[str] = None,
    ) -> RepoManifest:
        """
        Advance an existing manifest to the repo's current HEAD.

        Each builder is asked for an incremental update rather than a full
        build. Builders without a real delta path fall back to a rebuild
        internally, so the result is always correct -- only the cost differs.

        Falls back to a full :meth:`compile_repo` when there is no existing
        manifest, or when the previously indexed commit cannot be determined.
        Returns the existing manifest untouched when HEAD has not moved.
        """
        repo_path = os.path.abspath(repo_path)
        cache = cache_dir or os.path.join(repo_path, self._config.cache_dir_name)
        manifest_path = os.path.join(cache, MANIFEST_FILENAME)

        if not os.path.isfile(manifest_path):
            logger.info(
                "No manifest at %s; falling back to a full build", manifest_path
            )
            return self.compile_repo(
                repo_path, index_types=index_types, cache_dir=cache_dir
            )

        try:
            existing = RepoManifest.load(manifest_path)
        except Exception as exc:  # noqa: BLE001 - unreadable manifest: rebuild
            logger.warning(
                "Manifest at %s unusable (%s); rebuilding", manifest_path, exc
            )
            return self.compile_repo(
                repo_path, index_types=index_types, cache_dir=cache_dir
            )

        types_to_update = index_types or self._config.index_types
        incomplete = [
            idx_type
            for idx_type in types_to_update
            if idx_type not in existing.indexes
            or existing.indexes[idx_type].status != "fresh"
        ]
        # An explicitly empty last_indexed_commit means the previous full build
        # never established a usable baseline. RepoManifest.from_dict() already
        # maps legacy manifests that lack this field to `commit`, so falling
        # back here would only hide a recorded failure.
        previous = existing.last_indexed_commit
        head_commit = self._get_head_commit(repo_path)
        if not previous:
            logger.info(
                "Manifest records no complete indexed commit; rebuilding %s",
                types_to_update,
            )
            return self.compile_repo(
                repo_path, index_types=index_types, cache_dir=cache_dir
            )
        if head_commit and previous == head_commit and not incomplete:
            logger.info("Indexes already at %s; nothing to update", head_commit[:8])
            return existing
        if head_commit and previous == head_commit:
            logger.info(
                "Retrying incomplete indexes at %s: %s",
                head_commit[:8],
                incomplete,
            )
            return self.compile_repo(
                repo_path, index_types=index_types, cache_dir=cache_dir
            )

        logger.info(
            "Updating indexes %s -> %s", previous[:8], (head_commit or "HEAD")[:8]
        )
        return self._compile(
            repo_path,
            index_types=index_types,
            cache_dir=cache_dir,
            last_commit=previous,
        )

    def _compile(
        self,
        repo_path: str,
        *,
        index_types: Optional[List[str]],
        cache_dir: Optional[str],
        last_commit: Optional[str],
    ) -> RepoManifest:
        """Shared build loop. ``last_commit`` selects the incremental path."""
        repo_path = os.path.abspath(repo_path)
        cache = cache_dir or os.path.join(repo_path, self._config.cache_dir_name)
        os.makedirs(cache, exist_ok=True)

        types_to_build = index_types or self._config.index_types

        head_commit = self._get_head_commit(repo_path)
        manifest = RepoManifest(
            repo_path=repo_path,
            commit=head_commit,
            last_indexed_commit=head_commit,
            languages=list(self._config.languages),
            file_count=self._count_files(repo_path),
        )
        all_succeeded = True

        for idx_type in types_to_build:
            builder = self._builders.get(idx_type)
            if builder is None:
                logger.warning("No builder registered for '%s', skipping", idx_type)
                continue

            output_dir = os.path.join(cache, idx_type)
            result = self._build_one(
                builder, idx_type, repo_path, output_dir, last_commit=last_commit
            )

            now = datetime.now(timezone.utc)
            entry = IndexEntry(
                index_type=idx_type,
                path=output_dir,
                built_at=now.isoformat(),
                built_at_epoch=now.timestamp(),
                status="fresh" if result.success else "failed",
                config=result.status.metadata if result.status else {},
                metadata={
                    **(result.status.metadata if result.status else {}),
                    "build_duration_seconds": round(result.duration_seconds, 2),
                },
            )
            if not result.success and result.error:
                entry.metadata["error"] = result.error

            manifest.indexes[idx_type] = entry
            if not result.success:
                all_succeeded = False

        # Only claim HEAD as indexed when every requested index actually reached
        # it. Otherwise update_repo() would see `last_indexed_commit == HEAD` on
        # its next run, report "nothing to update", and leave the failed index
        # stale forever -- a recoverable failure turned into a silent permanent
        # one. Leaving the previous commit recorded means the next run retries.
        if not all_succeeded:
            manifest.last_indexed_commit = last_commit or ""
            logger.warning(
                "Not all indexes reached %s; last_indexed_commit left at %r",
                (head_commit or "HEAD")[:8],
                manifest.last_indexed_commit[:8] or "(none)",
            )

        manifest.derive_capabilities()
        now = datetime.now(timezone.utc)
        manifest.compiled_at = now.isoformat()
        manifest.compiled_at_epoch = now.timestamp()

        manifest_path = os.path.join(cache, MANIFEST_FILENAME)
        manifest.save(manifest_path)
        logger.info("Manifest written to %s", manifest_path)

        return manifest

    @staticmethod
    def _build_one(
        builder: Any,
        index_type: str,
        repo_path: str,
        output_dir: str,
        *,
        last_commit: Optional[str] = None,
    ) -> BuildResult:
        """Build a single index, catching errors.

        When *last_commit* is given, the builder's ``incremental_update`` path is
        used instead of ``build``. Builders without a real delta implementation
        fall back to a full rebuild internally, so passing it is always safe.
        """
        start = time.monotonic()
        try:
            if last_commit:
                status = builder.incremental_update(
                    scope="current_repo",
                    repo_path=repo_path,
                    output_dir=output_dir,
                    last_commit=last_commit,
                )
            else:
                status = builder.build(
                    scope="current_repo",
                    repo_path=repo_path,
                    output_dir=output_dir,
                )
            elapsed = time.monotonic() - start
            return BuildResult(
                index_type=index_type,
                success=True,
                status=status,
                duration_seconds=elapsed,
            )
        except Exception as e:
            elapsed = time.monotonic() - start
            logger.error("Failed to build %s: %s", index_type, e)
            return BuildResult(
                index_type=index_type,
                success=False,
                error=str(e),
                duration_seconds=elapsed,
            )

    @staticmethod
    def _get_head_commit(repo_path: str) -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    @staticmethod
    def _count_files(repo_path: str) -> int:
        count = 0
        for _, _, files in os.walk(repo_path):
            count += len(files)
        return count
