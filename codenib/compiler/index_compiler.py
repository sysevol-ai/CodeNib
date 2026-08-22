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
from typing import Any, Dict, List, Optional

from ..paths import REPO_INDEX_DIRNAME
from ..repository_filters import repository_path_is_visible
from ..repository_source_selection import (
    DEFAULT_REPOSITORY_SOURCE_SELECTION,
    RepositorySourceSelection,
    repository_relative_source_path,
)
from ..source_fingerprint import (
    SourceFingerprint,
    fingerprint_repository,
    is_secure_source_fingerprint_v2,
)
from .cache_lock import compiler_cache_lock
from .index_builders import IndexBuilderRegistry
from .manifest import MANIFEST_FILENAME, MANIFEST_VERSION, IndexEntry, RepoManifest
from .resources import IndexState, IndexStatus

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IndexCompilerConfig:
    """Configuration for the IndexCompiler."""

    cache_dir_name: str = REPO_INDEX_DIRNAME
    index_types: List[str] = field(
        default_factory=lambda: ["bm25", "vector", "symbol_graph"],
    )
    languages: List[str] = field(default_factory=lambda: ["python"])
    source_selection: RepositorySourceSelection | None = None

    def __post_init__(self) -> None:
        if self.source_selection is None:
            return
        if type(self.source_selection) is not RepositorySourceSelection:
            raise TypeError("source_selection must be a RepositorySourceSelection")
        self.source_selection = RepositorySourceSelection(
            self.source_selection.exclude_subtrees
        )


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
        repo_path = os.path.abspath(repo_path)
        cache = self._resolve_cache_dir(repo_path, cache_dir)
        with compiler_cache_lock(cache):
            existing = self._load_existing_manifest(cache)
            source_selection = self._snapshot_source_selection(existing)
            return self._compile(
                repo_path,
                index_types=index_types,
                cache_dir=cache,
                existing_manifest=existing,
                source_selection=source_selection,
                force_rebuild=True,
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

        Requested views are rebuilt from the captured source generation.  An
        incremental delta is unsafe until the Git cleanliness/HEAD observation
        can be bound to the same pinned repository authority as the source
        fingerprint; a path-based status check can otherwise observe a
        temporary replacement repository and authorize reuse for different
        bytes.

        Falls back to a full :meth:`compile_repo` when there is no existing
        manifest, or when the previously indexed commit cannot be determined.
        Returns the existing manifest untouched when HEAD has not moved.
        """
        repo_path = os.path.abspath(repo_path)
        cache = self._resolve_cache_dir(repo_path, cache_dir)
        with compiler_cache_lock(cache):
            return self._update_repo_locked(
                repo_path,
                index_types=index_types,
                cache=cache,
            )

    def _update_repo_locked(
        self,
        repo_path: str,
        *,
        index_types: Optional[List[str]],
        cache: str,
    ) -> RepoManifest:
        """Update or create a repository while the caller holds its cache lock.

        This method deliberately does not acquire another lease.  It is the
        composition boundary used by retained publication so compilation and
        immutable recapture share one cache generation.
        """
        manifest_path = os.path.join(cache, MANIFEST_FILENAME)

        if not os.path.isfile(manifest_path):
            logger.info(
                "No manifest at %s; falling back to a full build", manifest_path
            )
            return self._compile(
                repo_path,
                index_types=index_types,
                cache_dir=cache,
                existing_manifest=None,
                source_selection=self._snapshot_source_selection(None),
                force_rebuild=True,
            )

        try:
            existing = RepoManifest.load(manifest_path)
        except Exception as exc:  # noqa: BLE001 - unreadable manifest: rebuild
            logger.warning(
                "Manifest at %s unusable (%s); rebuilding", manifest_path, exc
            )
            return self._compile(
                repo_path,
                index_types=index_types,
                cache_dir=cache,
                existing_manifest=None,
                source_selection=self._snapshot_source_selection(None),
                force_rebuild=True,
            )

        source_selection = self._snapshot_source_selection(existing)
        # The manifest identity excludes the cache tree.  Do not let the
        # update fast path accept an apparently-current manifest when a
        # caller placed that cache inside the selected repository surface.
        # The same guard also runs in ``_compile``; it must precede the
        # fingerprint/incomplete check here because that check can return the
        # existing manifest without entering ``_compile`` at all.
        self._require_cache_outside_source(repo_path, cache, source_selection)

        types_to_update = index_types or self._config.index_types
        head_commit = self._get_head_commit(repo_path)
        source = fingerprint_repository(
            repo_path,
            exclude_roots=(cache,),
            selection=source_selection,
        )
        source_selection_digest = source_selection.digest
        incomplete = [
            idx_type
            for idx_type in types_to_update
            if idx_type not in existing.indexes
            or not self._entry_matches_source(
                existing.indexes[idx_type],
                commit=head_commit,
                source_fingerprint=source.value,
                source_selection_digest=source_selection_digest,
            )
            or not self._entry_matches_builder(
                existing.indexes[idx_type],
                self._builders.get(idx_type),
            )
        ]
        if not incomplete:
            identity = head_commit[:8] if head_commit else source.value[:19]
            logger.info("Indexes already at %s; nothing to update", identity)
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
            cache_dir=cache,
            existing_manifest=existing,
            source=source,
            source_selection=source_selection,
            force_rebuild=False,
        )

    def _resolve_cache_dir(self, repo_path: str, cache_dir: Optional[str]) -> str:
        cache = cache_dir or os.path.join(repo_path, self._config.cache_dir_name)
        return os.path.abspath(cache)

    @staticmethod
    def _load_existing_manifest(cache: str) -> Optional[RepoManifest]:
        manifest_path = os.path.join(cache, MANIFEST_FILENAME)
        if not os.path.isfile(manifest_path):
            return None
        try:
            return RepoManifest.load(manifest_path)
        except Exception as exc:  # noqa: BLE001 - explicit rebuild repairs it
            logger.warning(
                "Manifest at %s unusable (%s); rebuilding requested views",
                manifest_path,
                exc,
            )
            return None

    def _compile(
        self,
        repo_path: str,
        *,
        index_types: Optional[List[str]],
        cache_dir: Optional[str],
        existing_manifest: Optional[RepoManifest],
        source_selection: RepositorySourceSelection,
        source: Optional[SourceFingerprint] = None,
        force_rebuild: bool,
    ) -> RepoManifest:
        """Build requested views while preserving independent manifest entries."""
        repo_path = os.path.abspath(repo_path)
        cache = cache_dir or os.path.join(repo_path, self._config.cache_dir_name)
        self._require_cache_outside_source(repo_path, cache, source_selection)

        types_to_build = index_types or self._config.index_types

        head_commit = self._get_head_commit(repo_path)
        source = source or fingerprint_repository(
            repo_path,
            exclude_roots=(cache,),
            selection=source_selection,
        )
        source_selection_digest = source_selection.digest
        existing = existing_manifest
        # The caller computes languages from the current selected source
        # surface. Carrying old values forward would resurrect languages that
        # disappeared or were explicitly excluded by a new selection policy.
        languages = list(dict.fromkeys(self._config.languages))
        carried_indexes = (
            copy.deepcopy(existing.indexes) if existing is not None else {}
        )
        existing_has_current_selection_identity = (
            existing is not None
            and existing.version == MANIFEST_VERSION
            and existing.source_selection is not None
        )
        if existing is not None and not existing_has_current_selection_identity:
            for entry in carried_indexes.values():
                if entry.status == "fresh":
                    entry.status = "stale"
                    entry.metadata["stale_reason"] = (
                        "manifest source-selection contract requires migration"
                    )
        manifest = RepoManifest(
            repo_path=repo_path,
            commit=head_commit,
            last_indexed_commit=(
                existing.last_indexed_commit
                if existing_has_current_selection_identity
                else (head_commit if existing is None else "")
            ),
            source_fingerprint=source.value,
            last_indexed_source_fingerprint=(
                existing.last_indexed_source_fingerprint
                if existing_has_current_selection_identity
                else (source.value if existing is None else "")
            ),
            source_selection=source_selection,
            last_indexed_source_selection_digest=(
                existing.last_indexed_source_selection_digest
                if existing_has_current_selection_identity
                else (source_selection_digest if existing is None else "")
            ),
            languages=languages,
            file_count=source.file_count,
            indexes=carried_indexes,
        )
        for entry in manifest.indexes.values():
            if entry.status == "fresh" and not self._entry_matches_source(
                entry,
                commit=head_commit,
                source_fingerprint=source.value,
                source_selection_digest=source_selection_digest,
            ):
                entry.status = "stale"

        requested_succeeded = True

        for idx_type in types_to_build:
            builder = self._builders.get(idx_type)
            if builder is None:
                logger.warning("No builder registered for '%s', skipping", idx_type)
                existing_entry = manifest.indexes.get(idx_type)
                if force_rebuild and existing_entry is not None:
                    existing_entry.status = "failed"
                    existing_entry.metadata["error"] = (
                        f"No builder registered for {idx_type!r}"
                    )
                requested_succeeded = False
                continue

            previous_entry = (
                existing.indexes.get(idx_type) if existing is not None else None
            )
            current_entry = manifest.indexes.get(idx_type)
            if (
                not force_rebuild
                and current_entry is not None
                and current_entry.status == "fresh"
                and self._entry_matches_builder(
                    current_entry,
                    builder,
                )
                and self._entry_matches_source(
                    current_entry,
                    commit=head_commit,
                    source_fingerprint=source.value,
                    source_selection_digest=source_selection_digest,
                )
            ):
                continue

            # Do not seed an incremental builder from a Git status/HEAD check
            # performed through a separately resolved path.  A repository can
            # be A for both source fingerprints, B (clean) only for that check,
            # and A again before the build.  Until Git observations share the
            # pinned source authority, a full rebuild is the only safe reuse
            # policy. A failed rebuild retains the complete previous entry
            # below so the manifest never publishes mixed provenance.
            incremental_from = None

            output_dir = os.path.join(cache, idx_type)
            result = self._build_one(
                builder,
                idx_type,
                repo_path,
                output_dir,
                last_commit=incremental_from,
                previous_artifact_config=(
                    copy.deepcopy(previous_entry.config)
                    if incremental_from and previous_entry is not None
                    else None
                ),
                source_selection=source_selection,
                expected_source_commit=head_commit,
            )
            result = self._validate_build_selection(
                result,
                source_selection=source_selection,
                expected_source_commit=head_commit,
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
            if not result.success and previous_entry is not None:
                # A failed attempt must not invent a half-new generation. Keep
                # every field that describes the last produced artifact and
                # overlay only attempt diagnostics/status.
                entry = copy.deepcopy(previous_entry)
                entry.status = "failed"
                entry.built_at = now.isoformat()
                entry.built_at_epoch = now.timestamp()
                entry.metadata = {
                    **copy.deepcopy(previous_entry.metadata),
                    "build_duration_seconds": round(result.duration_seconds, 2),
                }
            else:
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
                    commit=head_commit if result.success else "",
                    source_fingerprint=source.value if result.success else "",
                    source_selection_digest=(
                        source_selection_digest if result.success else ""
                    ),
                )
            if not result.success and result.error:
                entry.metadata["error"] = result.error

            manifest.indexes[idx_type] = entry
            if not result.success:
                requested_succeeded = False

        final_head_commit = self._get_head_commit(repo_path)
        final_source = fingerprint_repository(
            repo_path,
            exclude_roots=(cache,),
            selection=source_selection,
        )
        source_changed_during_build = (
            final_head_commit != head_commit or final_source.value != source.value
        )
        if source_changed_during_build:
            requested_succeeded = False
            for entry in manifest.indexes.values():
                if entry.status == "fresh":
                    entry.status = "stale"
                    entry.metadata["stale_reason"] = (
                        "repository source changed during index compilation"
                    )
            logger.error(
                "Repository source changed during index compilation; "
                "no view will be published as fresh"
            )

        all_views_at_head = bool(manifest.indexes) and all(
            self._entry_matches_source(
                entry,
                commit=head_commit,
                source_fingerprint=source.value,
                source_selection_digest=source_selection_digest,
            )
            for entry in manifest.indexes.values()
        )
        if requested_succeeded and all_views_at_head:
            manifest.last_indexed_commit = head_commit
            manifest.last_indexed_source_fingerprint = source.value
            manifest.last_indexed_source_selection_digest = source_selection_digest
        else:
            manifest.last_indexed_commit = (
                existing.last_indexed_commit
                if existing_has_current_selection_identity
                else ""
            )
            manifest.last_indexed_source_fingerprint = (
                existing.last_indexed_source_fingerprint
                if existing_has_current_selection_identity
                else ""
            )
            manifest.last_indexed_source_selection_digest = (
                existing.last_indexed_source_selection_digest
                if existing_has_current_selection_identity
                else ""
            )
            logger.warning(
                "Not all indexes reached source %s; "
                "last indexed identity was preserved",
                (head_commit or source.value)[:12],
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
    def _require_cache_outside_source(
        repo_path: str,
        cache: str,
        source_selection: RepositorySourceSelection,
    ) -> None:
        """Reject a cache that builders would treat as selected source."""

        if os.path.normcase(cache) == os.path.normcase(repo_path):
            raise ValueError("index cache directory cannot be the repository root")
        try:
            relative = repository_relative_source_path(repo_path, cache)
        except ValueError:
            return
        if repository_path_is_visible(relative, selection=source_selection):
            raise ValueError(
                "an in-repository index cache must be excluded by the effective "
                "repository source selection"
            )

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
    def _entry_matches_source(
        entry: IndexEntry,
        *,
        commit: str,
        source_fingerprint: str,
        source_selection_digest: str,
    ) -> bool:
        """Whether an entry was built from the exact current repository source."""

        if entry.status != "fresh":
            return False
        if entry.commit != commit:
            return False
        return (
            is_secure_source_fingerprint_v2(source_fingerprint)
            and entry.source_fingerprint == source_fingerprint
            and entry.source_selection_digest == source_selection_digest
        )

    @staticmethod
    def _validate_build_selection(
        result: BuildResult,
        *,
        source_selection: RepositorySourceSelection,
        expected_source_commit: str,
    ) -> BuildResult:
        """Require positive evidence for the complete repository source policy."""

        if not result.success or result.status is None:
            return result
        reported = result.status.metadata.get("source_selection_digest")
        expected = source_selection.digest
        if reported != expected:
            return BuildResult(
                index_type=result.index_type,
                success=False,
                error=(
                    "builder did not confirm the configured repository "
                    "source-selection identity"
                ),
                duration_seconds=result.duration_seconds,
            )
        if (
            result.index_type == "zoekt"
            and result.status.metadata.get("source_commit") != expected_source_commit
        ):
            return BuildResult(
                index_type=result.index_type,
                success=False,
                error=("Zoekt builder did not confirm the compiler source commit"),
                duration_seconds=result.duration_seconds,
            )
        return result

    @staticmethod
    def _build_one(
        builder: Any,
        index_type: str,
        repo_path: str,
        output_dir: str,
        *,
        last_commit: Optional[str] = None,
        previous_artifact_config: Optional[Dict[str, Any]] = None,
        source_selection: RepositorySourceSelection,
        expected_source_commit: str,
    ) -> BuildResult:
        """Build a single index, catching errors.

        When *last_commit* is given, the builder's ``incremental_update`` path is
        used instead of ``build``. Builders without a real delta implementation
        fall back to a full rebuild internally, so passing it is always safe.
        """
        start = time.monotonic()
        try:
            if last_commit:
                update_kwargs: Dict[str, Any] = {
                    "repo_path": repo_path,
                    "output_dir": output_dir,
                    "last_commit": last_commit,
                    "source_selection": source_selection,
                }
                if index_type == "zoekt":
                    update_kwargs["source_commit"] = expected_source_commit
                if previous_artifact_config is not None:
                    update_kwargs["previous_artifact_config"] = previous_artifact_config
                status = builder.incremental_update(
                    scope="current_repo",
                    **update_kwargs,
                )
            else:
                build_kwargs: Dict[str, Any] = {
                    "repo_path": repo_path,
                    "output_dir": output_dir,
                    "source_selection": source_selection,
                }
                if index_type == "zoekt":
                    build_kwargs["source_commit"] = expected_source_commit
                status = builder.build(scope="current_repo", **build_kwargs)
            if not isinstance(status, IndexStatus) or type(status.metadata) is not dict:
                raise TypeError("index builder must return an IndexStatus")
            if status.index_type != index_type:
                raise ValueError("index builder returned the wrong index type")
            if status.state is not IndexState.FRESH:
                raise ValueError("index builder did not return a fresh index")
            if status.scope != "current_repo":
                raise ValueError("index builder returned the wrong repository scope")
            if type(status.path) is not str or os.path.abspath(
                os.path.expanduser(status.path)
            ) != os.path.abspath(os.path.expanduser(output_dir)):
                raise ValueError("index builder returned the wrong output path")
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

    def _snapshot_source_selection(
        self,
        existing_manifest: RepoManifest | None,
    ) -> RepositorySourceSelection:
        selection = self._config.source_selection
        if selection is None:
            if (
                existing_manifest is not None
                and type(existing_manifest.source_selection)
                is RepositorySourceSelection
            ):
                selection = existing_manifest.source_selection
            else:
                selection = DEFAULT_REPOSITORY_SOURCE_SELECTION
        if type(selection) is not RepositorySourceSelection:
            raise TypeError("source_selection must be a RepositorySourceSelection")
        return RepositorySourceSelection(selection.exclude_subtrees)

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
