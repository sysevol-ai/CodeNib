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

import copy
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional

from ..paths import REPO_INDEX_DIRNAME
from ..repository_filters import count_repository_files
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
            repo_path,
            index_types=index_types,
            cache_dir=cache_dir,
            existing_manifest=None,
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
        head_commit = self._get_head_commit(repo_path)
        incomplete = [
            idx_type
            for idx_type in types_to_update
            if idx_type not in existing.indexes
            or existing.indexes[idx_type].status != "fresh"
            or not self._entry_matches_builder(
                existing.indexes[idx_type],
                self._builders.get(idx_type),
            )
            or (
                bool(head_commit)
                and bool(existing.indexes[idx_type].commit)
                and existing.indexes[idx_type].commit != head_commit
            )
        ]
        if head_commit and not incomplete:
            logger.info("Indexes already at %s; nothing to update", head_commit[:8])
            return existing
        if head_commit and existing.commit == head_commit:
            logger.info(
                "Retrying incomplete indexes at %s: %s",
                head_commit[:8],
                incomplete,
            )
        else:
            logger.info(
                "Updating indexes %s -> %s",
                (existing.commit or "unknown")[:8],
                (head_commit or "HEAD")[:8],
            )
        return self._compile(
            repo_path,
            index_types=index_types,
            cache_dir=cache_dir,
            existing_manifest=existing,
        )

    def _compile(
        self,
        repo_path: str,
        *,
        index_types: Optional[List[str]],
        cache_dir: Optional[str],
        existing_manifest: Optional[RepoManifest],
    ) -> RepoManifest:
        """Build requested views while preserving independent manifest entries."""
        repo_path = os.path.abspath(repo_path)
        cache = cache_dir or os.path.join(repo_path, self._config.cache_dir_name)
        os.makedirs(cache, exist_ok=True)

        types_to_build = index_types or self._config.index_types

        head_commit = self._get_head_commit(repo_path)
        existing = existing_manifest
        languages = list(self._config.languages)
        if existing is not None:
            languages = list(dict.fromkeys([*existing.languages, *languages]))
        manifest = RepoManifest(
            repo_path=repo_path,
            commit=head_commit,
            last_indexed_commit=(
                existing.last_indexed_commit if existing is not None else head_commit
            ),
            languages=languages,
            file_count=self._count_files(repo_path),
            indexes=(copy.deepcopy(existing.indexes) if existing is not None else {}),
        )
        for entry in manifest.indexes.values():
            if (
                entry.status == "fresh"
                and entry.commit
                and head_commit
                and entry.commit != head_commit
            ):
                entry.status = "stale"

        requested_succeeded = True

        for idx_type in types_to_build:
            builder = self._builders.get(idx_type)
            if builder is None:
                logger.warning("No builder registered for '%s', skipping", idx_type)
                requested_succeeded = False
                continue

            previous_entry = (
                existing.indexes.get(idx_type) if existing is not None else None
            )
            current_entry = manifest.indexes.get(idx_type)
            if (
                current_entry is not None
                and current_entry.status == "fresh"
                and self._entry_matches_builder(
                    current_entry,
                    builder,
                )
                and (
                    not head_commit
                    or not current_entry.commit
                    or current_entry.commit == head_commit
                )
            ):
                continue

            previous_commit = ""
            previous_entry_compatible = (
                previous_entry is not None
                and previous_entry.status in {"fresh", "stale"}
                and self._entry_matches_builder(previous_entry, builder)
            )
            if previous_entry_compatible:
                previous_commit = previous_entry.commit
                if not previous_commit and existing is not None:
                    previous_commit = existing.last_indexed_commit
            incremental_from = (
                previous_commit
                if previous_commit and head_commit and previous_commit != head_commit
                else None
            )

            output_dir = os.path.join(cache, idx_type)
            result = self._build_one(
                builder,
                idx_type,
                repo_path,
                output_dir,
                last_commit=incremental_from,
            )
            if (
                idx_type == "symbol_graph"
                and result.success
                and result.status is not None
                and result.status.metadata.get("update_mode") == "incremental"
                and previous_entry is not None
            ):
                for key in ("available_languages", "failed_languages", "partial"):
                    if (
                        key not in result.status.metadata
                        and key in previous_entry.metadata
                    ):
                        result.status.metadata[key] = copy.deepcopy(
                            previous_entry.metadata[key]
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
                commit=head_commit if result.success else previous_commit,
            )
            if not result.success and result.error:
                entry.metadata["error"] = result.error

            manifest.indexes[idx_type] = entry
            if not result.success:
                requested_succeeded = False

        all_views_at_head = bool(manifest.indexes) and all(
            entry.status == "fresh"
            and (not head_commit or not entry.commit or entry.commit == head_commit)
            for entry in manifest.indexes.values()
        )
        if requested_succeeded and all_views_at_head:
            manifest.last_indexed_commit = head_commit
        else:
            manifest.last_indexed_commit = (
                existing.last_indexed_commit if existing is not None else ""
            )
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
    def _entry_matches_builder(entry: IndexEntry, builder: Any) -> bool:
        """Whether a persisted view matches the current builder contract."""

        identity_fn = getattr(builder, "artifact_identity", None)
        if not callable(identity_fn):
            return True
        try:
            expected = identity_fn()
        except Exception as exc:  # noqa: BLE001 - rebuild on uncertain identity
            logger.warning("Could not inspect builder identity: %s", exc)
            return False
        return all(entry.config.get(key) == value for key, value in expected.items())

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
        return count_repository_files(repo_path)
