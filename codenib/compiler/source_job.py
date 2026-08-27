# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Prepare one FULL BM25 job directly from retained repository source.

The adapter deliberately composes the existing compiler-cache preparation
boundary.  It builds one caller-owned private cache generation, writes the
exact single-view manifest expected by that boundary, and delegates strict
recapture, context publication, and CAS ingestion.  It receives no catalog or
ref mutation authority; the durable worker remains the only publisher.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .._atomic_directory import lexical_directory_path
from .._captured_directory import PublishedWorkspaceReceiptOwner
from .._workspace_provider import StrictWorkspaceProvider
from ..repository_source_selection import RepositorySourceSelection
from ..source_fingerprint import (
    RepositorySourceBinding,
    RepositorySourceIdentitySnapshot,
)
from ..storage.job_worker import (
    IndexJobExecutionContext,
    IndexJobExecutionResult,
    IndexJobStopReason,
    IndexJobStopToken,
    IndexJobViewExecutionResult,
)
from ..storage.models import (
    DEFAULT_NAMESPACE_NAME,
    IndexJobEffectiveMode,
    IndexJobViewOutcome,
    StorageIntegrityError,
    StorageValidationError,
    ViewProfile,
    canonical_json,
)
from ..storage.protocols import RetainedImportObjectStore
from ..storage.view_bundle import (
    DEFAULT_MAX_BUNDLE_BYTES,
    DEFAULT_MAX_BUNDLE_FILES,
    DEFAULT_MAX_BUNDLE_METADATA_BYTES,
)
from .cache_import import (
    _compiler_cache_job_stop_check,
    _preflight_cache_job_preparation_operation,
    _require_missing,
    prepare_compiler_cache_job_view,
)
from .cache_lock import compiler_cache_lock
from .index_builders import BM25IndexBuilder
from .manifest import MANIFEST_FILENAME, MANIFEST_VERSION, IndexEntry, RepoManifest
from .manifest_import import (
    DEFAULT_MAX_CONTEXT_BYTES,
    DEFAULT_MAX_CONTEXT_FILES,
    _snapshot_environment,
    _snapshot_forbidden_paths,
)
from .manifest_storage import (
    DEFAULT_MAX_MANIFEST_BYTES,
    REPO_MANIFEST_PROFILE_NAME,
    _profile_config,
)
from .resources import IndexState, IndexStatus

_DISPLAY_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_BM25_VIEW = "bm25"
_BM25_SCOPE = "current_repo"


@dataclass(frozen=True, slots=True)
class _BoundSourceJobStopToken:
    """Carry one exact source-job stop exception into cache preparation."""

    token: IndexJobStopToken
    check_cancelled: Callable[[], None]

    @property
    def reason(self) -> IndexJobStopReason | None:
        return self.token.reason

    def is_set(self) -> bool:
        self.check_cancelled()
        return False

    def wait(self, timeout: float | None = None) -> bool:
        return self.token.wait(timeout)


@dataclass(frozen=True, slots=True)
class _BM25BuilderConfiguration:
    languages: tuple[str, ...]
    max_k: int
    max_lines_per_chunk: int
    additional_ignore_dirs: tuple[str, ...]
    source_selection: RepositorySourceSelection

    def builder(self) -> BM25IndexBuilder:
        return BM25IndexBuilder(
            languages=list(self.languages),
            max_k=self.max_k,
            max_lines_per_chunk=self.max_lines_per_chunk,
            additional_ignore_dirs=list(self.additional_ignore_dirs),
            source_selection=RepositorySourceSelection(
                self.source_selection.exclude_subtrees
            ),
        )

    def profile(self) -> ViewProfile:
        identity = self.builder().artifact_identity()
        # Reuse the manifest planner's profile classifier so this source
        # adapter cannot drift from imported compiler-cache identities.
        entry = IndexEntry(
            index_type=_BM25_VIEW,
            path=f"views/{_BM25_VIEW}",
            built_at="",
            built_at_epoch=0.0,
            status="fresh",
            config=copy.deepcopy(identity),
            metadata=copy.deepcopy(identity),
            source_selection_digest=self.source_selection.digest,
        )
        config = _profile_config(
            entry,
            view_type=_BM25_VIEW,
            manifest_version=MANIFEST_VERSION,
        )
        return ViewProfile.create(
            _BM25_VIEW,
            config,
            name=REPO_MANIFEST_PROFILE_NAME,
        )


def _snapshot_builder(builder: BM25IndexBuilder) -> _BM25BuilderConfiguration:
    if type(builder) is not BM25IndexBuilder:
        raise TypeError("BM25 source job builder must use the exact builder type")
    if type(builder.languages) is not list or any(
        type(language) is not str for language in builder.languages
    ):
        raise TypeError("BM25 source job languages must be an exact text list")
    if type(builder.additional_ignore_dirs) is not list or any(
        type(path) is not str for path in builder.additional_ignore_dirs
    ):
        raise TypeError("BM25 source job ignore directories must be an exact text list")
    if type(builder.max_k) is not int or type(builder.max_lines_per_chunk) is not int:
        raise TypeError("BM25 source job numeric options must be exact integers")
    if type(builder.source_selection) is not RepositorySourceSelection:
        raise TypeError("BM25 source job selection must use the exact selection type")
    snapshot = _BM25BuilderConfiguration(
        languages=tuple(builder.languages),
        max_k=builder.max_k,
        max_lines_per_chunk=builder.max_lines_per_chunk,
        additional_ignore_dirs=tuple(builder.additional_ignore_dirs),
        source_selection=RepositorySourceSelection(
            builder.source_selection.exclude_subtrees
        ),
    )
    # Fail closed on incomplete or non-canonical compatibility axes now, not
    # after an attempt generation has been created.
    snapshot.profile()
    return snapshot


def bm25_source_job_profile(builder: BM25IndexBuilder) -> ViewProfile:
    """Return the exact portable profile requested by this source adapter."""

    return _snapshot_builder(builder).profile()


def _exact_display_commit(value: object) -> str:
    if type(value) is not str or _DISPLAY_COMMIT_RE.fullmatch(value) is None:
        raise StorageValidationError(
            "BM25 source job display commit must be a full lowercase Git SHA"
        )
    return value


def _exact_attempt_generation(value: object) -> Path:
    if type(value) is not type(Path()):
        raise TypeError("BM25 source job attempt generation must be an exact Path")
    generation = lexical_directory_path(value)
    if generation == generation.parent:
        raise ValueError(
            "BM25 source job attempt generation cannot be a filesystem root"
        )
    return generation


def _require_disjoint_attempt_generation(
    generation: Path,
    source: RepositorySourceIdentitySnapshot,
    *,
    boundaries: tuple[Path, ...],
) -> None:
    repository = lexical_directory_path(source.root)
    try:
        physical_generation = generation.resolve(strict=False)
        physical_repository = repository.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            "BM25 source job attempt topology cannot be authenticated"
        ) from exc
    inputs = ((repository, physical_repository, "repository"),)
    outputs = tuple(
        (
            lexical_directory_path(boundary),
            lexical_directory_path(boundary).resolve(strict=False),
            "output or forbidden boundary",
        )
        for boundary in boundaries
    )
    for lexical, physical, label in (*inputs, *outputs):
        if (
            generation == lexical
            or generation in lexical.parents
            or lexical in generation.parents
            or physical_generation == physical
            or physical_generation in physical.parents
            or physical in physical_generation.parents
        ):
            raise ValueError("BM25 source job attempt generation overlaps " + label)


def _built_at(status: IndexStatus) -> tuple[str, float]:
    value = status.last_built
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise StorageIntegrityError("BM25 source builder returned an invalid timestamp")
    epoch = float(value)
    try:
        timestamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise StorageIntegrityError(
            "BM25 source builder returned an invalid timestamp"
        ) from exc
    return timestamp, epoch


def _source_manifest(
    status: IndexStatus,
    *,
    output: Path,
    display_commit: str,
    repository_source: RepositorySourceBinding,
    expected_source: RepositorySourceIdentitySnapshot,
    builder: _BM25BuilderConfiguration,
    check_cancelled: Callable[[], None],
) -> RepoManifest:
    if type(status) is not IndexStatus:
        raise StorageIntegrityError("BM25 source builder returned an invalid status")
    if (
        status.index_type != _BM25_VIEW
        or status.state is not IndexState.FRESH
        or status.scope != _BM25_SCOPE
        or status.path != str(output)
        or type(status.metadata) is not dict
    ):
        raise StorageIntegrityError("BM25 source builder returned inconsistent status")
    expected_identity = builder.builder().artifact_identity()
    if any(
        status.metadata.get(key) != value for key, value in expected_identity.items()
    ):
        raise StorageIntegrityError(
            "BM25 source builder changed its compatibility identity"
        )
    try:
        # Detach through the canonical JSON boundary before it becomes a
        # persisted manifest input.
        metadata = json.loads(canonical_json(status.metadata))
    except (RecursionError, TypeError, ValueError) as exc:
        raise StorageIntegrityError(
            "BM25 source builder returned non-canonical metadata"
        ) from exc
    built_at, built_at_epoch = _built_at(status)
    source = repository_source.authenticated_identity_snapshot(
        check_cancelled=check_cancelled,
    )
    if source != expected_source:
        raise StorageIntegrityError("BM25 retained source changed after job preflight")
    if source.source_selection != builder.source_selection:
        raise StorageIntegrityError(
            "BM25 source builder selection changed after job preflight"
        )
    selection_digest = builder.source_selection.digest
    entry = IndexEntry(
        index_type=_BM25_VIEW,
        path=_BM25_VIEW,
        built_at=built_at,
        built_at_epoch=built_at_epoch,
        status="fresh",
        config=copy.deepcopy(metadata),
        metadata=copy.deepcopy(metadata),
        commit=display_commit,
        source_fingerprint=source.fingerprint,
        source_selection_digest=selection_digest,
    )
    manifest = RepoManifest(
        repo_path=str(source.root),
        commit=display_commit,
        last_indexed_commit=display_commit,
        source_fingerprint=source.fingerprint,
        last_indexed_source_fingerprint=source.fingerprint,
        source_selection=RepositorySourceSelection(
            builder.source_selection.exclude_subtrees
        ),
        last_indexed_source_selection_digest=selection_digest,
        languages=list(builder.languages),
        file_count=source.file_count,
        indexes={_BM25_VIEW: entry},
        compiled_at=built_at,
        compiled_at_epoch=built_at_epoch,
    )
    manifest.derive_capabilities()
    # Exercise the exact persisted validation path before writing bytes.
    return RepoManifest.from_dict(manifest.to_dict())


@dataclass(frozen=True, slots=True)
class BM25SourceJobExecutor:
    """Prepare exactly one required FULL BM25 job from retained source bytes.

    ``attempt_generation`` is a unique missing cache root owned by the caller.
    The executor never removes it, including after cancellation or failure;
    retries must supply another missing root. ``display_commit`` is provenance
    for the strict portable context only. It is never derived from a lexical
    repository read and does not alter the fingerprint-bound durable source ID.
    """

    attempt_generation: Path
    display_commit: str
    builder: InitVar[BM25IndexBuilder]
    repository_source: RepositorySourceBinding
    view_output_owner: PublishedWorkspaceReceiptOwner
    context_output_owner: PublishedWorkspaceReceiptOwner
    view_destination: Path
    context_destination: Path
    workspace_provider: StrictWorkspaceProvider
    repository_key: str
    object_store: RetainedImportObjectStore
    namespace_name: str = DEFAULT_NAMESPACE_NAME
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES
    forbidden_paths: Iterable[Path] = ()
    environ: Mapping[str, str] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _builder: _BM25BuilderConfiguration = field(init=False, repr=False)
    _profile: ViewProfile = field(init=False, repr=False)

    def __post_init__(self, builder: BM25IndexBuilder) -> None:
        configuration = _snapshot_builder(builder)
        object.__setattr__(
            self,
            "attempt_generation",
            _exact_attempt_generation(self.attempt_generation),
        )
        object.__setattr__(
            self,
            "display_commit",
            _exact_display_commit(self.display_commit),
        )
        object.__setattr__(
            self,
            "forbidden_paths",
            _snapshot_forbidden_paths(self.forbidden_paths),
        )
        object.__setattr__(self, "environ", _snapshot_environment(self.environ))
        object.__setattr__(self, "_builder", configuration)
        object.__setattr__(self, "_profile", configuration.profile())

    def execute(
        self,
        context: IndexJobExecutionContext,
    ) -> IndexJobExecutionResult:
        """Build and prepare the claimed view without publication authority."""

        if type(context) is not IndexJobExecutionContext:
            raise TypeError("BM25 source executor requires an exact job context")
        check_cancelled = _compiler_cache_job_stop_check(context.control.stop_token)
        if check_cancelled is None:  # pragma: no cover - execution context invariant
            raise AssertionError("BM25 source job context has no stop check")
        check_cancelled()
        _require_missing(
            self.attempt_generation,
            label="BM25 source job attempt generation",
        )
        operation, binding = _preflight_cache_job_preparation_operation(
            self.attempt_generation,
            view_type=_BM25_VIEW,
            job=context.job,
            views=context.views,
            repository_source=self.repository_source,
            view_output_owner=self.view_output_owner,
            context_output_owner=self.context_output_owner,
            view_destination=self.view_destination,
            context_destination=self.context_destination,
            workspace_provider=self.workspace_provider,
            repository_key=self.repository_key,
            object_store=self.object_store,
            namespace_name=self.namespace_name,
            max_manifest_bytes=self.max_manifest_bytes,
            max_context_files=self.max_context_files,
            max_context_bytes=self.max_context_bytes,
            max_bundle_files=self.max_bundle_files,
            max_bundle_bytes=self.max_bundle_bytes,
            max_bundle_metadata_bytes=self.max_bundle_metadata_bytes,
            forbidden_paths=self.forbidden_paths,
            environ=self.environ,
            check_cancelled=check_cancelled,
        )
        if binding.view.profile_id != self._profile.profile_id:
            raise StorageValidationError(
                "BM25 source job profile does not match the builder"
            )
        if binding.source_snapshot.source_selection != self._builder.source_selection:
            raise StorageValidationError(
                "BM25 source job selection does not match the retained source"
            )
        _require_disjoint_attempt_generation(
            self.attempt_generation,
            binding.source_snapshot,
            boundaries=(
                *operation.view_outputs.values(),
                operation.context_output,
                *operation.inputs.forbidden_paths,
            ),
        )
        check_cancelled()
        # Prove that all job/view/profile/source preflight remained read-only.
        _require_missing(
            self.attempt_generation,
            label="BM25 source job attempt generation",
        )

        self.attempt_generation.mkdir(mode=0o700)
        output = self.attempt_generation / _BM25_VIEW
        builder = self._builder.builder()
        with compiler_cache_lock(
            self.attempt_generation,
            check_cancelled=check_cancelled,
        ):
            status = builder.build_from_repository_source(
                _BM25_SCOPE,
                repository_source=self.repository_source,
                output_dir=str(output),
                source_selection=self._builder.source_selection,
                check_cancelled=check_cancelled,
            )
            check_cancelled()
            manifest = _source_manifest(
                status,
                output=output,
                display_commit=self.display_commit,
                repository_source=self.repository_source,
                expected_source=binding.source_snapshot,
                builder=self._builder,
                check_cancelled=check_cancelled,
            )
            if bm25_source_job_profile(builder).profile_id != self._profile.profile_id:
                raise StorageIntegrityError(
                    "BM25 source builder profile changed during execution"
                )
            manifest.save(self.attempt_generation / MANIFEST_FILENAME)
        check_cancelled()

        prepared = prepare_compiler_cache_job_view(
            self.attempt_generation,
            view_type=_BM25_VIEW,
            job=context.job,
            views=context.views,
            repository_source=self.repository_source,
            view_output_owner=self.view_output_owner,
            context_output_owner=self.context_output_owner,
            view_destination=self.view_destination,
            context_destination=self.context_destination,
            workspace_provider=self.workspace_provider,
            repository_key=self.repository_key,
            object_store=self.object_store,
            namespace_name=self.namespace_name,
            stop_token=_BoundSourceJobStopToken(
                context.control.stop_token,
                check_cancelled,
            ),
            max_manifest_bytes=self.max_manifest_bytes,
            max_context_files=self.max_context_files,
            max_context_bytes=self.max_context_bytes,
            max_bundle_files=self.max_bundle_files,
            max_bundle_bytes=self.max_bundle_bytes,
            max_bundle_metadata_bytes=self.max_bundle_metadata_bytes,
            forbidden_paths=self.forbidden_paths,
            environ=self.environ,
        )
        if (
            prepared.view != binding.view
            or prepared.artifact.profile_id != self._profile.profile_id
        ):
            raise StorageIntegrityError(
                "BM25 source preparation returned a different requested view"
            )
        return IndexJobExecutionResult(
            views=(
                IndexJobViewExecutionResult.create(
                    prepared.view,
                    effective_mode=IndexJobEffectiveMode.FULL,
                    outcome=IndexJobViewOutcome.SUCCEEDED,
                    artifact=prepared.artifact,
                    payload={"adapter": "bm25_source", "prepared": True},
                ),
            ),
            retryable=False,
        )


__all__ = ["BM25SourceJobExecutor", "bm25_source_job_profile"]
