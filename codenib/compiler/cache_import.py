# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Import compiler-cache query views through retained authority.

The compiler cache is mutable local state, so it is never handed directly to
the retained importer.  This coordinator holds one existing compiler cache
lock while it authenticates the fixed manifest and selected query-view trees,
plans the exact portable manifest, and publishes immutable view and context
generations.  Only the immutable context receipt crosses the lock boundary
into storage import.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import re
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .._atomic_directory import (
    PublicationDirectoryReader,
    TreeFileRecord,
    lexical_directory_path,
)
from .._captured_directory import (
    PublishedWorkspaceReceipt,
    PublishedWorkspaceReceiptOwner,
)
from .._workspace_provider import StrictWorkspaceProvider
from ..artifacts.context import ContextArtifactResult
from ..artifacts.portable_views import _read_bounded_json
from ..artifacts.strict_bm25 import (
    PlannedBm25View,
    _plan_recaptured_bm25_view,
    _plan_retained_bm25_publication_view,
    _publish_recaptured_bm25_view,
    _publish_retained_bm25_publication_view,
)
from ..artifacts.strict_context import (
    _canonical_json_bytes,
    _plan_context_artifact_strict_interruptibly,
    _portable_capabilities,
    _publish_planned_context_artifact_strict_interruptibly,
)
from ..artifacts.strict_vector import (
    _plan_recaptured_vector_view,
    _publish_recaptured_vector_view,
)
from ..repository_source_selection import (
    DEFAULT_REPOSITORY_SOURCE_SELECTION,
    RepositorySourceSelection,
)
from ..source_fingerprint import (
    RepositorySourceBinding,
    RepositorySourceIdentitySnapshot,
    is_secure_source_fingerprint_v2,
)
from ..storage.job_worker import (
    IndexJobExecutionContext,
    IndexJobExecutionResult,
    IndexJobStopToken,
    IndexJobViewExecutionResult,
)
from ..storage.models import (
    DEFAULT_NAMESPACE_NAME,
    IndexJobEffectiveMode,
    IndexJobRecord,
    IndexJobRequest,
    IndexJobRequestedMode,
    IndexJobStatus,
    IndexJobViewOutcome,
    IndexJobViewRecord,
    RepositoryIdentity,
    SourceRevision,
    StorageIntegrityError,
    StorageValidationError,
)
from ..storage.protocols import (
    RETAINED_IMPORT_CATALOG_CONTRACT,
    InterruptibleReceiptVerifyingObjectStore,
    InterruptibleStreamingObjectStore,
    JobPublicationCatalog,
    RetainedImportCatalog,
    RetainedImportObjectStore,
)
from ..storage.publication import IndexJobViewArtifact, publish_job_artifacts
from ..storage.view_bundle import (
    DEFAULT_MAX_BUNDLE_BYTES,
    DEFAULT_MAX_BUNDLE_FILES,
    DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    VIEW_BUNDLE_SCHEMA,
)
from .cache_lock import COMPILER_CACHE_LOCK_FILENAME, compiler_cache_lock
from .index_compiler import IndexCompiler
from .manifest import MANIFEST_FILENAME, MANIFEST_VERSION, IndexEntry, RepoManifest
from .manifest_import import (
    _CATALOG_METHODS,
    _OBJECT_STORE_METHODS,
    DEFAULT_MAX_CONTEXT_BYTES,
    DEFAULT_MAX_CONTEXT_FILES,
    DEFAULT_MAX_PROJECTION_BYTES,
    DEFAULT_REF_NAME,
    RepoManifestImportResult,
    _exact_text,
    _expected_generation,
    _expected_namespace_id,
    _positive_limit,
    _prepare_job_view_artifacts_inside_authority,
    _require_static_methods,
    _snapshot_environment,
    _snapshot_forbidden_paths,
    import_retained_repo_manifest,
)
from .manifest_storage import (
    CURRENT_PORTABLE_BUILDER_SCHEMAS,
    DEFAULT_MAX_MANIFEST_BYTES,
    RepoManifestImportPlan,
    _manifest_limit,
    _preflight_json_bytes,
    _strict_json_loads,
    plan_repo_manifest_import_bytes,
)
from .snapshot_store import normalize_repo

_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_SUPPORTED_CACHE_VIEWS = ("bm25", "vector")
_CATALOG_INT64_MAX = 9_223_372_036_854_775_807

# Keep the established module seams patchable in focused compiler-cache tests
# while the public strict-context functions retain their stable signatures.
plan_context_artifact_strict = _plan_context_artifact_strict_interruptibly
publish_planned_context_artifact_strict = (
    _publish_planned_context_artifact_strict_interruptibly
)


class _CompilerCacheJobStopped(RuntimeError):
    """Internal cooperative stop signal for prepare-only worker work."""


def _compiler_cache_job_stop_check(
    stop_token: IndexJobStopToken | None,
) -> Callable[[], None] | None:
    if stop_token is None:
        return None
    if not isinstance(stop_token, IndexJobStopToken):
        raise TypeError("compiler cache job stop token is invalid")

    stopped: _CompilerCacheJobStopped | None = None

    def check_cancelled() -> None:
        nonlocal stopped
        if stop_token.is_set():
            if stopped is None:
                stopped = _CompilerCacheJobStopped(
                    "compiler cache job preparation stopped"
                )
            raise stopped

    return check_cancelled


def _interruptible_chunks(
    chunks: Iterable[bytes],
    check_cancelled: Callable[[], None] | None,
) -> Iterable[bytes]:
    iterator = iter(chunks)
    while True:
        if check_cancelled is not None:
            check_cancelled()
        try:
            chunk = next(iterator)
        except StopIteration:
            return
        yield chunk


class CompilerCacheTopologyGuard(Protocol):
    """Trusted application-level checks around one compiler-cache mutation."""

    def capture(self, cache: Path) -> None:
        """Capture topology after the cache lease binds its directory."""

    def verify(self, cache: Path) -> None:
        """Reject topology drift while the same cache lease remains held."""


@dataclass(frozen=True, slots=True)
class CompilerCacheViewRecaptureResult:
    """Detached evidence for one mutable-to-immutable view recapture."""

    view_type: str
    source_view: Path
    output_view: Path
    source_records: tuple[TreeFileRecord, ...]
    output_records: tuple[TreeFileRecord, ...]

    def __post_init__(self) -> None:
        if type(self) is not CompilerCacheViewRecaptureResult:
            raise TypeError("compiler cache view recapture must use the exact type")
        if (
            type(self.view_type) is not str
            or self.view_type not in _SUPPORTED_CACHE_VIEWS
        ):
            raise ValueError("compiler cache view recapture type is unsupported")
        if type(self.source_view) is not type(Path()) or type(
            self.output_view
        ) is not type(Path()):
            raise TypeError("compiler cache view recapture paths must be exact paths")
        source_records = tuple(self.source_records)
        output_records = tuple(self.output_records)
        for records, label in (
            (source_records, "source"),
            (output_records, "output"),
        ):
            if not records or any(
                type(record) is not TreeFileRecord for record in records
            ):
                raise TypeError(
                    f"compiler cache view recapture {label} records are invalid"
                )
            if records != tuple(sorted(records, key=lambda item: item.path)):
                raise ValueError(
                    f"compiler cache view recapture {label} records are not canonical"
                )
            if len({record.path for record in records}) != len(records):
                raise ValueError(
                    f"compiler cache view recapture {label} records repeat a path"
                )
        if self.view_type == "bm25":
            expected = {"bm25_metadata.json", "documents.json"}
            if {record.path for record in source_records} != expected or {
                record.path for record in output_records
            } != expected:
                raise ValueError("compiler cache BM25 recapture records are incomplete")
        object.__setattr__(self, "source_records", source_records)
        object.__setattr__(self, "output_records", output_records)

    @property
    def output_file_fingerprints(self) -> dict[str, dict[str, Any]]:
        """Return detached output fingerprints keyed by portable path."""

        return _fingerprints_for_records(self.output_records)


@dataclass(frozen=True, slots=True)
class CompilerCacheBm25RecaptureResult:
    """Detached evidence for the mutable-to-immutable BM25 recapture."""

    source_view: Path
    output_view: Path
    source_records: tuple[TreeFileRecord, ...]
    output_records: tuple[TreeFileRecord, ...]
    canonical_manifest_bytes: bytes

    def __post_init__(self) -> None:
        if type(self) is not CompilerCacheBm25RecaptureResult:
            raise TypeError("compiler cache recapture must use the exact result type")
        if type(self.source_view) is not type(Path()) or type(
            self.output_view
        ) is not type(Path()):
            raise TypeError("compiler cache recapture paths must be exact paths")
        source_records = tuple(self.source_records)
        output_records = tuple(self.output_records)
        for records, label in (
            (source_records, "source"),
            (output_records, "output"),
        ):
            if any(type(record) is not TreeFileRecord for record in records):
                raise TypeError(f"compiler cache recapture {label} records are invalid")
            if records != tuple(sorted(records, key=lambda item: item.path)):
                raise ValueError(
                    f"compiler cache recapture {label} records are not canonical"
                )
            if {record.path for record in records} != {
                "bm25_metadata.json",
                "documents.json",
            }:
                raise ValueError(
                    f"compiler cache recapture {label} records are incomplete"
                )
        if type(self.canonical_manifest_bytes) is not bytes:
            raise TypeError("compiler cache portable manifest must be exact bytes")
        object.__setattr__(self, "source_records", source_records)
        object.__setattr__(self, "output_records", output_records)

    @property
    def manifest_digest(self) -> str:
        return hashlib.sha256(self.canonical_manifest_bytes).hexdigest()

    @property
    def artifact_file_fingerprints(self) -> dict[str, dict[str, Any]]:
        return _fingerprints_for_records(self.output_records)


@dataclass(frozen=True, slots=True)
class CompilerCacheImportResult:
    """Pure-data result of one compiler-cache BM25 retained import."""

    recapture: CompilerCacheBm25RecaptureResult
    import_plan: RepoManifestImportPlan
    context_artifact: ContextArtifactResult
    import_result: RepoManifestImportResult

    def __post_init__(self) -> None:
        if type(self) is not CompilerCacheImportResult:
            raise TypeError("compiler cache import must use the exact result type")
        if type(self.recapture) is not CompilerCacheBm25RecaptureResult:
            raise TypeError("compiler cache import recapture result is invalid")
        if type(self.import_plan) is not RepoManifestImportPlan:
            raise TypeError("compiler cache import plan is invalid")
        if type(self.context_artifact) is not ContextArtifactResult:
            raise TypeError("compiler cache context artifact result is invalid")
        if type(self.import_result) is not RepoManifestImportResult:
            raise TypeError("compiler cache retained import result is invalid")
        if (
            self.import_plan.selection.selected_views != ("bm25",)
            or self.context_artifact.views != ("bm25",)
            or self.import_result.views != ("bm25",)
            or self.recapture.canonical_manifest_bytes
            != _pretty_manifest_bytes(self.import_plan.manifest)
        ):
            raise StorageIntegrityError(
                "compiler cache import result identities are inconsistent"
            )


@dataclass(frozen=True, slots=True)
class CompilerCacheJobPublicationResult:
    """Exact data returned after one fenced BM25 job publication."""

    manifest: RepoManifest
    import_plan: RepoManifestImportPlan
    recapture: CompilerCacheBm25RecaptureResult
    job: IndexJobRecord

    def __post_init__(self) -> None:
        if type(self) is not CompilerCacheJobPublicationResult:
            raise TypeError(
                "compiler cache job publication must use the exact result type"
            )
        if type(self.manifest) is not RepoManifest:
            raise TypeError("compiler cache job manifest is invalid")
        if type(self.import_plan) is not RepoManifestImportPlan:
            raise TypeError("compiler cache job import plan is invalid")
        if type(self.recapture) is not CompilerCacheBm25RecaptureResult:
            raise TypeError("compiler cache job recapture is invalid")
        if type(self.job) is not IndexJobRecord:
            raise TypeError("compiler cache job record is invalid")
        try:
            manifest = RepoManifest.from_dict(copy.deepcopy(self.manifest.to_dict()))
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageIntegrityError(
                "compiler cache job manifest cannot be detached"
            ) from exc
        plan_views = self.import_plan.views
        request_views = _index_job_request(self.job).view_requests
        expected_source = SourceRevision.dirty(
            self.job.repository_id,
            source_fingerprint=self.import_plan.source.fingerprint,
            commit_sha=None,
        )
        if (
            self.import_plan.selection.selected_views != ("bm25",)
            or len(plan_views) != 1
            or plan_views[0].view_type != "bm25"
            or len(request_views) != 1
            or request_views[0].view_type != "bm25"
            or request_views[0].profile_id != plan_views[0].profile_id
            or request_views[0].requested_mode is not IndexJobRequestedMode.FULL
            or request_views[0].required is not True
            or self.job.source_revision_id != expected_source.source_revision_id
            or self.job.status is not IndexJobStatus.SUCCEEDED
            or self.job.cancel_requested
            or self.job.result_snapshot_id is None
            or manifest.to_dict() != self.import_plan.manifest.to_dict()
            or self.recapture.canonical_manifest_bytes
            != _pretty_manifest_bytes(manifest)
        ):
            raise StorageIntegrityError(
                "compiler cache job publication result identities are inconsistent"
            )
        object.__setattr__(self, "manifest", manifest)


@dataclass(frozen=True, slots=True)
class CompilerCacheVectorJobPublicationResult:
    """Exact data returned after one fenced vector job publication."""

    manifest: RepoManifest
    import_plan: RepoManifestImportPlan
    recapture: CompilerCacheViewRecaptureResult
    job: IndexJobRecord

    def __post_init__(self) -> None:
        if type(self) is not CompilerCacheVectorJobPublicationResult:
            raise TypeError(
                "compiler cache vector job publication must use the exact result type"
            )
        if type(self.manifest) is not RepoManifest:
            raise TypeError("compiler cache vector job manifest is invalid")
        if type(self.import_plan) is not RepoManifestImportPlan:
            raise TypeError("compiler cache vector job import plan is invalid")
        if type(self.recapture) is not CompilerCacheViewRecaptureResult:
            raise TypeError("compiler cache vector job recapture is invalid")
        if type(self.job) is not IndexJobRecord:
            raise TypeError("compiler cache vector job record is invalid")
        try:
            manifest = RepoManifest.from_dict(copy.deepcopy(self.manifest.to_dict()))
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageIntegrityError(
                "compiler cache vector job manifest cannot be detached"
            ) from exc
        plan_views = self.import_plan.views
        request_views = _index_job_request(self.job).view_requests
        expected_source = SourceRevision.dirty(
            self.job.repository_id,
            source_fingerprint=self.import_plan.source.fingerprint,
            commit_sha=None,
        )
        if (
            self.import_plan.selection.selected_views != ("vector",)
            or len(plan_views) != 1
            or plan_views[0].view_type != "vector"
            or plan_views[0].profile.config.get("builder_schema") != 8
            or len(request_views) != 1
            or request_views[0].view_type != "vector"
            or request_views[0].profile_id != plan_views[0].profile_id
            or request_views[0].requested_mode is not IndexJobRequestedMode.FULL
            or request_views[0].required is not True
            or self.job.source_revision_id != expected_source.source_revision_id
            or self.job.status is not IndexJobStatus.SUCCEEDED
            or self.job.cancel_requested
            or self.job.result_snapshot_id is None
            or manifest.to_dict() != self.import_plan.manifest.to_dict()
            or self.recapture.view_type != "vector"
            or manifest.indexes["vector"].path != "views/vector"
        ):
            raise StorageIntegrityError(
                "compiler cache vector job publication identities are inconsistent"
            )
        object.__setattr__(self, "manifest", manifest)


@dataclass(frozen=True, slots=True)
class CompilerCacheJobPreparationResult:
    """Exact prepare-only output for one worker-owned compiler-cache view."""

    job: IndexJobRecord
    view: IndexJobViewRecord
    manifest: RepoManifest
    import_plan: RepoManifestImportPlan
    recapture: CompilerCacheViewRecaptureResult
    context_artifact: ContextArtifactResult
    artifact: IndexJobViewArtifact

    def __post_init__(self) -> None:
        if type(self) is not CompilerCacheJobPreparationResult:
            raise TypeError(
                "compiler cache job preparation must use the exact result type"
            )
        if type(self.job) is not IndexJobRecord:
            raise TypeError("compiler cache prepared job record is invalid")
        if type(self.view) is not IndexJobViewRecord:
            raise TypeError("compiler cache prepared job view is invalid")
        if type(self.manifest) is not RepoManifest:
            raise TypeError("compiler cache prepared manifest is invalid")
        if type(self.import_plan) is not RepoManifestImportPlan:
            raise TypeError("compiler cache prepared import plan is invalid")
        if type(self.recapture) is not CompilerCacheViewRecaptureResult:
            raise TypeError("compiler cache prepared recapture is invalid")
        if type(self.context_artifact) is not ContextArtifactResult:
            raise TypeError("compiler cache prepared context artifact is invalid")
        if type(self.artifact) is not IndexJobViewArtifact:
            raise TypeError("compiler cache prepared view artifact is invalid")
        try:
            job = _detach_index_job_record(self.job)
            view = _detach_index_job_views((self.view,))[0]
            manifest = RepoManifest.from_dict(copy.deepcopy(self.manifest.to_dict()))
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageIntegrityError(
                "compiler cache job preparation cannot be detached"
            ) from exc
        plan_views = self.import_plan.views
        expected_source = SourceRevision.dirty(
            job.repository_id,
            source_fingerprint=self.import_plan.source.fingerprint,
            commit_sha=None,
        )
        if (
            job.status is not IndexJobStatus.RUNNING
            or job.cancel_requested
            or job.attempt_count < 1
            or job.started_at_ms is None
            or _index_job_request(job).view_requests != (view,)
            or view.requested_mode is not IndexJobRequestedMode.FULL
            or view.required is not True
            or self.import_plan.selection.selected_views != (view.view_type,)
            or len(plan_views) != 1
            or plan_views[0].view_type != view.view_type
            or plan_views[0].profile_id != view.profile_id
            or job.source_revision_id != expected_source.source_revision_id
            or manifest.to_dict() != self.import_plan.manifest.to_dict()
            or tuple(manifest.indexes) != (view.view_type,)
            or manifest.indexes[view.view_type].path != f"views/{view.view_type}"
            or self.recapture.view_type != view.view_type
            or self.context_artifact.views != (view.view_type,)
            or self.context_artifact.commit != manifest.commit
            or self.artifact.view_type != view.view_type
            or self.artifact.profile_id != view.profile_id
            or self.artifact.schema_version != VIEW_BUNDLE_SCHEMA
        ):
            raise StorageIntegrityError(
                "compiler cache job preparation identities are inconsistent"
            )
        object.__setattr__(self, "job", job)
        object.__setattr__(self, "view", view)
        object.__setattr__(self, "manifest", manifest)


@dataclass(frozen=True, slots=True)
class CompilerCacheMultiViewImportResult:
    """Pure-data result of one atomic compiler-cache view-set import."""

    recaptures: tuple[CompilerCacheViewRecaptureResult, ...]
    canonical_manifest_bytes: bytes
    import_plan: RepoManifestImportPlan
    context_artifact: ContextArtifactResult
    import_result: RepoManifestImportResult

    def __post_init__(self) -> None:
        if type(self) is not CompilerCacheMultiViewImportResult:
            raise TypeError(
                "compiler cache multi-view import must use the exact result type"
            )
        recaptures = tuple(self.recaptures)
        if not recaptures or any(
            type(item) is not CompilerCacheViewRecaptureResult for item in recaptures
        ):
            raise TypeError("compiler cache multi-view recaptures are invalid")
        views = tuple(item.view_type for item in recaptures)
        if views != tuple(view for view in _SUPPORTED_CACHE_VIEWS if view in views):
            raise ValueError("compiler cache multi-view recaptures are not canonical")
        if len(set(views)) != len(views):
            raise ValueError("compiler cache multi-view recaptures repeat a view")
        if type(self.canonical_manifest_bytes) is not bytes:
            raise TypeError("compiler cache portable manifest must be exact bytes")
        if type(self.import_plan) is not RepoManifestImportPlan:
            raise TypeError("compiler cache multi-view import plan is invalid")
        if type(self.context_artifact) is not ContextArtifactResult:
            raise TypeError("compiler cache context artifact result is invalid")
        if type(self.import_result) is not RepoManifestImportResult:
            raise TypeError("compiler cache retained import result is invalid")
        if (
            self.import_plan.selection.selected_views != views
            or self.context_artifact.views != views
            or self.import_result.views != views
            or self.canonical_manifest_bytes
            != _pretty_manifest_bytes(self.import_plan.manifest)
        ):
            raise StorageIntegrityError(
                "compiler cache multi-view result identities are inconsistent"
            )
        object.__setattr__(self, "recaptures", recaptures)

    @property
    def views(self) -> tuple[str, ...]:
        return tuple(item.view_type for item in self.recaptures)

    @property
    def recapture_map(self) -> dict[str, CompilerCacheViewRecaptureResult]:
        return {item.view_type: item for item in self.recaptures}


@dataclass(frozen=True, slots=True)
class CompilerRetainedPublicationResult:
    """Result of updating compiler views and atomically retaining them."""

    manifest: RepoManifest
    retained_import: CompilerCacheMultiViewImportResult

    def __post_init__(self) -> None:
        if type(self) is not CompilerRetainedPublicationResult:
            raise TypeError("compiler retained publication must use the exact type")
        if type(self.manifest) is not RepoManifest:
            raise TypeError("compiler retained publication manifest is invalid")
        if type(self.retained_import) is not CompilerCacheMultiViewImportResult:
            raise TypeError("compiler retained publication import result is invalid")
        views = self.retained_import.views
        for view in views:
            _require_current_view(self.manifest, view=view)
        portable = self.retained_import.import_plan.manifest
        if (
            portable.commit != self.manifest.commit
            or portable.source_fingerprint != self.manifest.source_fingerprint
            or portable.file_count != self.manifest.file_count
            or portable.source_selection != self.manifest.source_selection
            or portable.last_indexed_commit != self.manifest.last_indexed_commit
            or portable.last_indexed_source_fingerprint
            != self.manifest.last_indexed_source_fingerprint
            or portable.last_indexed_source_selection_digest
            != self.manifest.last_indexed_source_selection_digest
        ):
            raise StorageIntegrityError(
                "compiler retained publication identities are inconsistent"
            )

    @property
    def views(self) -> tuple[str, ...]:
        return self.retained_import.views

    @property
    def import_result(self) -> RepoManifestImportResult:
        return self.retained_import.import_result


@dataclass(frozen=True, slots=True)
class _ImportPreflight:
    repository_key: str
    namespace_name: str
    ref_name: str
    expected_generation: int
    max_manifest_bytes: int
    max_context_files: int
    max_context_bytes: int
    max_bundle_files: int
    max_bundle_bytes: int
    max_bundle_metadata_bytes: int
    max_projection_bytes: int
    forbidden_paths: tuple[Path, ...]
    environment: Mapping[str, str]


@dataclass(slots=True)
class _ImportOperation:
    cache: Path
    view_owners: dict[str, PublishedWorkspaceReceiptOwner]
    view_outputs: dict[str, Path]
    context_output: Path
    inputs: _ImportPreflight
    fixed_source_views: dict[str, Path]
    policy_forbidden: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _PreparedCompilerCacheImport:
    manifest: RepoManifest
    recaptures: tuple[CompilerCacheViewRecaptureResult, ...]
    canonical_manifest_bytes: bytes
    import_plan: RepoManifestImportPlan
    context_artifact: ContextArtifactResult


@dataclass(frozen=True, slots=True)
class _CompilerCacheJobBinding:
    job: IndexJobRecord
    view: IndexJobViewRecord
    source_snapshot: RepositorySourceIdentitySnapshot
    source_identity: SourceRevision


def _path_relation(path: Path, boundary: Path) -> str:
    if path == boundary:
        return "same"
    if boundary in path.parents:
        return "descendant"
    if path in boundary.parents:
        return "ancestor"
    return "disjoint"


def _resolved_path(path: Path, *, strict: bool, label: str) -> Path:
    try:
        return path.resolve(strict=strict)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} cannot be authenticated") from exc


def _require_missing(path: Path, *, label: str) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(f"{label} cannot be inspected safely") from exc
    raise FileExistsError(f"{label} must be missing")


def _preflight_authorities(
    *,
    views: tuple[str, ...],
    repository_source: RepositorySourceBinding,
    view_output_owners: dict[str, PublishedWorkspaceReceiptOwner],
    view_destinations: dict[str, Path],
    context_output_owner: PublishedWorkspaceReceiptOwner,
    context_destination: Path,
) -> None:
    if type(views) is not tuple:
        raise TypeError("compiler cache views must be an exact tuple")
    if not views or any(type(view) is not str for view in views):
        raise ValueError("compiler cache views must contain exact view names")
    canonical_views = tuple(
        view for view in _SUPPORTED_CACHE_VIEWS if view in set(views)
    )
    if views != canonical_views:
        raise ValueError(
            "compiler cache views must be a non-empty canonical portable subset"
        )
    if type(view_output_owners) is not dict:
        raise TypeError("compiler cache view output owners must be an exact dict")
    if type(view_destinations) is not dict:
        raise TypeError("compiler cache view destinations must be an exact dict")
    if any(type(view) is not str for view in view_output_owners) or any(
        type(view) is not str for view in view_destinations
    ):
        raise TypeError("compiler cache view mapping keys must be exact text")
    if tuple(view_output_owners) != views or tuple(view_destinations) != views:
        raise ValueError(
            "compiler cache view mappings must have the exact selected keys and order"
        )
    if type(repository_source) is not RepositorySourceBinding:
        raise TypeError("compiler cache source must be an exact source binding")
    owners = tuple(view_output_owners.values())
    if (
        any(type(owner) is not PublishedWorkspaceReceiptOwner for owner in owners)
        or type(context_output_owner) is not PublishedWorkspaceReceiptOwner
    ):
        raise TypeError("compiler cache outputs must be exact receipt owners")
    if len({id(owner) for owner in (*owners, context_output_owner)}) != len(owners) + 1:
        raise ValueError("compiler cache outputs must use distinct receipt owners")
    if any(type(path) is not type(Path()) for path in view_destinations.values()):
        raise TypeError("compiler cache view destinations must be exact paths")
    if type(context_destination) is not type(Path()):
        raise TypeError("compiler cache context destination must be an exact path")
    if not repository_source.usable:
        raise RuntimeError("compiler cache repository source is not usable")
    if any(owner.state != "empty" for owner in owners) or (
        context_output_owner.state != "empty"
    ):
        raise RuntimeError("compiler cache output receipt owners must be empty")


def _preflight_workspace_provider(
    workspace_provider: StrictWorkspaceProvider,
) -> None:
    for member in ("require_support", "run_workspace"):
        try:
            candidate = inspect.getattr_static(workspace_provider, member)
        except AttributeError as exc:
            raise TypeError(
                "compiler cache workspace provider has an invalid contract"
            ) from exc
        if isinstance(candidate, (classmethod, staticmethod)):
            candidate = candidate.__func__
        if not callable(candidate):
            raise TypeError("compiler cache workspace provider has an invalid contract")
    workspace_provider.require_support()


def _preflight_cache_topology_guard(
    guard: CompilerCacheTopologyGuard | None,
) -> None:
    if guard is None:
        return
    for member in ("capture", "verify"):
        try:
            candidate = inspect.getattr_static(guard, member)
        except AttributeError as exc:
            raise TypeError(
                "compiler cache topology guard has an invalid contract"
            ) from exc
        if isinstance(candidate, (classmethod, staticmethod)):
            candidate = candidate.__func__
        if not callable(candidate):
            raise TypeError("compiler cache topology guard has an invalid contract")


def _cache_directory_identity(cache: Path) -> tuple[int, int]:
    try:
        metadata = cache.lstat()
    except OSError as exc:
        raise RuntimeError("compiler cache directory cannot be authenticated") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError("compiler cache path is not a real directory")
    return (metadata.st_dev, metadata.st_ino)


def _run_cache_topology_guard(
    guard: CompilerCacheTopologyGuard | None,
    member: str,
    cache: Path,
) -> None:
    if guard is None:
        return
    result = getattr(guard, member)(cache)
    if result is not None:
        raise TypeError(f"compiler cache topology guard {member} must return None")


def _preflight_import_inputs(
    *,
    repository_key: str,
    catalog: RetainedImportCatalog,
    object_store: RetainedImportObjectStore,
    namespace_name: str,
    ref_name: str,
    expected_generation: int,
    max_manifest_bytes: int,
    max_context_files: int,
    max_context_bytes: int,
    max_bundle_files: int,
    max_bundle_bytes: int,
    max_bundle_metadata_bytes: int,
    max_projection_bytes: int,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str] | None,
) -> _ImportPreflight:
    repository = _exact_text(repository_key, "repository key")
    try:
        normalized_repository = normalize_repo(repository)
    except ValueError as exc:
        raise StorageValidationError("repository key is not canonical") from exc
    if normalized_repository != repository:
        raise StorageValidationError("repository key is not canonical")
    namespace = _exact_text(namespace_name, "namespace name")
    ref = _exact_text(ref_name, "ref name")
    expected = _expected_generation(expected_generation)
    manifest_limit = _manifest_limit(max_manifest_bytes)
    context_files = _positive_limit(max_context_files, "context file limit")
    context_bytes = _positive_limit(max_context_bytes, "context byte limit")
    bundle_files = _positive_limit(max_bundle_files, "bundle file limit")
    bundle_bytes = _positive_limit(max_bundle_bytes, "bundle byte limit")
    bundle_metadata_bytes = _positive_limit(
        max_bundle_metadata_bytes,
        "bundle metadata byte limit",
    )
    projection_bytes = _positive_limit(max_projection_bytes, "projection byte limit")
    environment = _snapshot_environment(environ)
    forbidden = _snapshot_forbidden_paths(forbidden_paths)

    if not isinstance(catalog, RetainedImportCatalog):
        raise TypeError("compiler cache import catalog lacks required capabilities")
    if not isinstance(object_store, RetainedImportObjectStore):
        raise TypeError(
            "compiler cache import object store lacks required capabilities"
        )
    _require_static_methods(
        object_store,
        label="retained import object store",
        names=_OBJECT_STORE_METHODS,
    )
    _require_static_methods(
        catalog,
        label="retained import catalog",
        names=_CATALOG_METHODS,
    )
    namespace_id = _expected_namespace_id(namespace)
    RepositoryIdentity(namespace_id=namespace_id, repository_key=repository)
    return _ImportPreflight(
        repository_key=repository,
        namespace_name=namespace,
        ref_name=ref,
        expected_generation=expected,
        max_manifest_bytes=manifest_limit,
        max_context_files=context_files,
        max_context_bytes=context_bytes,
        max_bundle_files=bundle_files,
        max_bundle_bytes=bundle_bytes,
        max_bundle_metadata_bytes=bundle_metadata_bytes,
        max_projection_bytes=projection_bytes,
        forbidden_paths=forbidden,
        environment=environment,
    )


def _preflight_catalog_contract(catalog: RetainedImportCatalog) -> None:
    contract = catalog.retained_import_contract()
    if type(contract) is not str or contract != RETAINED_IMPORT_CATALOG_CONTRACT:
        raise TypeError("compiler cache import catalog contract is incompatible")


def _index_job_request(job: IndexJobRecord) -> IndexJobRequest:
    return IndexJobRequest(
        repository_id=job.repository_id,
        source_revision_id=job.source_revision_id,
        ref_name=job.ref_name,
        idempotency_key=job.idempotency_key,
        expected_ref_generation=job.expected_ref_generation,
        max_attempts=job.max_attempts,
        request_json=job.request_json,
    )


def _detach_index_job_record(value: object) -> IndexJobRecord:
    if type(value) is not IndexJobRecord:
        raise StorageIntegrityError("catalog returned an invalid index job record")
    text_fields = (
        "job_id",
        "repository_id",
        "source_revision_id",
        "ref_name",
        "idempotency_key",
        "request_json",
        "request_digest",
    )
    optional_text_fields = (
        "result_snapshot_id",
        "error_code",
        "error_message",
    )
    integer_fields = (
        "expected_ref_generation",
        "max_attempts",
        "attempt_count",
        "created_at_ms",
        "updated_at_ms",
    )
    optional_integer_fields = ("started_at_ms", "finished_at_ms")
    if (
        any(type(getattr(value, field)) is not str for field in text_fields)
        or any(
            getattr(value, field) is not None and type(getattr(value, field)) is not str
            for field in optional_text_fields
        )
        or any(type(getattr(value, field)) is not int for field in integer_fields)
        or any(
            getattr(value, field) is not None and type(getattr(value, field)) is not int
            for field in optional_integer_fields
        )
        or type(value.status) is not IndexJobStatus
        or type(value.cancel_requested) is not bool
    ):
        raise StorageIntegrityError("catalog index job fields are not exact")
    detached = IndexJobRecord(
        job_id=value.job_id,
        repository_id=value.repository_id,
        source_revision_id=value.source_revision_id,
        ref_name=value.ref_name,
        idempotency_key=value.idempotency_key,
        expected_ref_generation=value.expected_ref_generation,
        max_attempts=value.max_attempts,
        request_json=value.request_json,
        request_digest=value.request_digest,
        status=value.status,
        cancel_requested=value.cancel_requested,
        attempt_count=value.attempt_count,
        result_snapshot_id=value.result_snapshot_id,
        error_code=value.error_code,
        error_message=value.error_message,
        created_at_ms=value.created_at_ms,
        updated_at_ms=value.updated_at_ms,
        started_at_ms=value.started_at_ms,
        finished_at_ms=value.finished_at_ms,
    )
    if detached != value:
        raise StorageIntegrityError("catalog index job record is not canonical")
    return detached


def _detach_index_job_views(value: object) -> tuple[IndexJobViewRecord, ...]:
    if type(value) is not tuple or any(
        type(item) is not IndexJobViewRecord for item in value
    ):
        raise StorageIntegrityError("catalog returned invalid index job view rows")
    if any(
        type(item.job_id) is not str
        or type(item.view_type) is not str
        or type(item.profile_id) is not str
        or type(item.requested_mode) is not IndexJobRequestedMode
        or type(item.required) is not bool
        for item in value
    ):
        raise StorageIntegrityError("catalog index job view fields are not exact")
    detached = tuple(
        IndexJobViewRecord(
            job_id=item.job_id,
            view_type=item.view_type,
            profile_id=item.profile_id,
            requested_mode=item.requested_mode,
            required=item.required,
        )
        for item in value
    )
    if detached != value:
        raise StorageIntegrityError("catalog index job view rows are not canonical")
    return detached


def _compiler_cache_job_binding(
    job_value: object,
    views_value: object,
    *,
    view_type: str,
    repository_source: RepositorySourceBinding,
    repository_key: str,
    namespace_name: str,
    allow_succeeded: bool,
    check_cancelled: Callable[[], None] | None = None,
) -> _CompilerCacheJobBinding:
    if type(view_type) is not str or view_type not in _SUPPORTED_CACHE_VIEWS:
        raise TypeError("compiler cache job view type is invalid")
    if type(allow_succeeded) is not bool:
        raise TypeError("compiler cache job replay policy must be a boolean")
    source_snapshot = (
        repository_source.authenticated_identity_snapshot()
        if check_cancelled is None
        else repository_source.authenticated_identity_snapshot(
            check_cancelled=check_cancelled,
        )
    )
    if type(source_snapshot) is not RepositorySourceIdentitySnapshot:
        raise TypeError("compiler cache repository identity has an invalid type")
    repository_identity = RepositoryIdentity(
        namespace_id=_expected_namespace_id(namespace_name),
        repository_key=repository_key,
    )
    source_identity = SourceRevision.dirty(
        repository_identity.repository_id,
        source_fingerprint=source_snapshot.fingerprint,
        commit_sha=None,
    )
    job = _detach_index_job_record(job_value)
    views = _detach_index_job_views(views_value)
    expected_views = _index_job_request(job).view_requests
    if views != expected_views:
        raise StorageIntegrityError("job views differ from the canonical request")
    if (
        job.repository_id != repository_identity.repository_id
        or job.source_revision_id != source_identity.source_revision_id
    ):
        raise StorageValidationError(
            "compiler cache job repository or source identity does not match"
        )
    if (
        len(views) != 1
        or views[0].job_id != job.job_id
        or views[0].view_type != view_type
        or views[0].requested_mode is not IndexJobRequestedMode.FULL
        or views[0].required is not True
    ):
        raise StorageValidationError(
            "compiler cache job must request exactly one required FULL "
            f"{view_type} view"
        )
    if job.cancel_requested:
        raise StorageValidationError("compiler cache job is cancelled")
    allowed_statuses = {IndexJobStatus.RUNNING}
    if allow_succeeded:
        allowed_statuses.add(IndexJobStatus.SUCCEEDED)
    if job.status not in allowed_statuses:
        raise StorageValidationError(
            "compiler cache job must be active"
            + (" or an exact successful replay" if allow_succeeded else "")
        )
    if job.attempt_count < 1 or job.started_at_ms is None:
        raise StorageValidationError(
            "compiler cache job has no acquired publication attempt"
        )
    return _CompilerCacheJobBinding(
        job=job,
        view=views[0],
        source_snapshot=source_snapshot,
        source_identity=source_identity,
    )


def _read_compiler_cache_job_binding(
    job_id: str,
    *,
    view_type: str,
    catalog: JobPublicationCatalog,
    repository_source: RepositorySourceBinding,
    repository_key: str,
    namespace_name: str,
) -> _CompilerCacheJobBinding:
    job = _detach_index_job_record(catalog.get_job(job_id))
    if job.job_id != job_id:
        raise StorageIntegrityError("catalog returned a different index job")
    views = _detach_index_job_views(catalog.get_job_views(job_id))
    return _compiler_cache_job_binding(
        job,
        views,
        view_type=view_type,
        repository_source=repository_source,
        repository_key=repository_key,
        namespace_name=namespace_name,
        allow_succeeded=True,
    )


def _require_compiler_cache_job_profile(
    binding: _CompilerCacheJobBinding,
    plan: RepoManifestImportPlan,
    *,
    view_type: str,
) -> None:
    if type(view_type) is not str or view_type not in _SUPPORTED_CACHE_VIEWS:
        raise TypeError("compiler cache job profile view type is invalid")
    if (
        type(plan) is not RepoManifestImportPlan
        or plan.selection.selected_views != (view_type,)
        or len(plan.views) != 1
        or plan.views[0].view_type != view_type
    ):
        raise StorageIntegrityError(
            f"compiler cache {view_type} import plan is inconsistent"
        )
    if binding.view.profile_id != plan.views[0].profile_id:
        raise StorageValidationError(
            f"compiler cache {view_type} profile does not match the job request"
        )


def _preflight_cache_job_operation(
    cache_dir: str | Path,
    *,
    view_type: str,
    job_id: str,
    owner_id: str,
    fencing_token: int,
    repository_source: RepositorySourceBinding,
    view_output_owner: PublishedWorkspaceReceiptOwner,
    context_output_owner: PublishedWorkspaceReceiptOwner,
    view_destination: Path,
    context_destination: Path,
    workspace_provider: StrictWorkspaceProvider,
    repository_key: str,
    catalog: JobPublicationCatalog,
    object_store: RetainedImportObjectStore,
    namespace_name: str,
    max_manifest_bytes: int,
    max_context_files: int,
    max_context_bytes: int,
    max_bundle_files: int,
    max_bundle_bytes: int,
    max_bundle_metadata_bytes: int,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str] | None,
) -> tuple[_ImportOperation, _CompilerCacheJobBinding, str, str, int]:
    if type(view_type) is not str or view_type not in _SUPPORTED_CACHE_VIEWS:
        raise TypeError("compiler cache job view type is invalid")
    normalized_job_id = _exact_text(job_id, "job ID", max_length=80)
    normalized_owner_id = _exact_text(owner_id, "lease owner ID", max_length=256)
    if (
        type(fencing_token) is not int
        or fencing_token < 1
        or fencing_token > _CATALOG_INT64_MAX
    ):
        raise StorageValidationError("fencing token must be a positive catalog int64")
    _preflight_authorities(
        views=(view_type,),
        repository_source=repository_source,
        view_output_owners={view_type: view_output_owner},
        view_destinations={view_type: view_destination},
        context_output_owner=context_output_owner,
        context_destination=context_destination,
    )
    repository = _exact_text(repository_key, "repository key")
    try:
        normalized_repository = normalize_repo(repository)
    except ValueError as exc:
        raise StorageValidationError("repository key is not canonical") from exc
    if normalized_repository != repository:
        raise StorageValidationError("repository key is not canonical")
    namespace = _exact_text(namespace_name, "namespace name")
    manifest_limit = _manifest_limit(max_manifest_bytes)
    context_files = _positive_limit(max_context_files, "context file limit")
    context_bytes = _positive_limit(max_context_bytes, "context byte limit")
    bundle_files = _positive_limit(max_bundle_files, "bundle file limit")
    bundle_bytes = _positive_limit(max_bundle_bytes, "bundle byte limit")
    bundle_metadata_bytes = _positive_limit(
        max_bundle_metadata_bytes,
        "bundle metadata byte limit",
    )
    environment = _snapshot_environment(environ)
    forbidden = _snapshot_forbidden_paths(forbidden_paths)
    if not isinstance(catalog, JobPublicationCatalog):
        raise TypeError("compiler cache job catalog lacks publication capabilities")
    _require_static_methods(
        catalog,
        label="compiler cache job catalog",
        names=("get_job", "get_job_views", "publish_job_outputs"),
    )
    if not isinstance(object_store, RetainedImportObjectStore):
        raise TypeError(
            "compiler cache job object store lacks retained streaming capabilities"
        )
    _require_static_methods(
        object_store,
        label="compiler cache job object store",
        names=_OBJECT_STORE_METHODS,
    )
    _preflight_workspace_provider(workspace_provider)
    binding = _read_compiler_cache_job_binding(
        normalized_job_id,
        view_type=view_type,
        catalog=catalog,
        repository_source=repository_source,
        repository_key=repository,
        namespace_name=namespace,
    )
    inputs = _ImportPreflight(
        repository_key=repository,
        namespace_name=namespace,
        ref_name=binding.job.ref_name,
        expected_generation=binding.job.expected_ref_generation,
        max_manifest_bytes=manifest_limit,
        max_context_files=context_files,
        max_context_bytes=context_bytes,
        max_bundle_files=bundle_files,
        max_bundle_bytes=bundle_bytes,
        max_bundle_metadata_bytes=bundle_metadata_bytes,
        max_projection_bytes=DEFAULT_MAX_PROJECTION_BYTES,
        forbidden_paths=forbidden,
        environment=environment,
    )
    cache = lexical_directory_path(Path(cache_dir))
    view_output = lexical_directory_path(view_destination)
    context_output = lexical_directory_path(context_destination)
    fixed_source_views = {view_type: lexical_directory_path(cache / view_type)}
    policy_forbidden = _snapshot_forbidden_paths(
        (
            *inputs.forbidden_paths,
            cache,
            *fixed_source_views.values(),
            view_output,
            context_output,
        )
    )
    return (
        _ImportOperation(
            cache=cache,
            view_owners={view_type: view_output_owner},
            view_outputs={view_type: view_output},
            context_output=context_output,
            inputs=inputs,
            fixed_source_views=fixed_source_views,
            policy_forbidden=policy_forbidden,
        ),
        binding,
        normalized_job_id,
        normalized_owner_id,
        fencing_token,
    )


def _preflight_cache_job_preparation_operation(
    cache_dir: str | Path,
    *,
    view_type: str,
    job: IndexJobRecord,
    views: tuple[IndexJobViewRecord, ...],
    repository_source: RepositorySourceBinding,
    view_output_owner: PublishedWorkspaceReceiptOwner,
    context_output_owner: PublishedWorkspaceReceiptOwner,
    view_destination: Path,
    context_destination: Path,
    workspace_provider: StrictWorkspaceProvider,
    repository_key: str,
    object_store: RetainedImportObjectStore,
    namespace_name: str,
    max_manifest_bytes: int,
    max_context_files: int,
    max_context_bytes: int,
    max_bundle_files: int,
    max_bundle_bytes: int,
    max_bundle_metadata_bytes: int,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str] | None,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[_ImportOperation, _CompilerCacheJobBinding]:
    """Validate a worker preparation without accepting catalog authority."""

    if check_cancelled is not None:
        if not callable(check_cancelled):
            raise TypeError("compiler cache cancellation check must be callable")
        if not isinstance(
            object_store,
            InterruptibleReceiptVerifyingObjectStore,
        ):
            raise TypeError(
                "cancellable compiler cache job requires interruptible receipt "
                "verification"
            )
        if not isinstance(object_store, InterruptibleStreamingObjectStore):
            raise TypeError(
                "cancellable compiler cache job requires interruptible streaming "
                "ingestion"
            )
        _require_static_methods(
            object_store,
            label="compiler cache job object store",
            names=(
                "put_chunks_interruptibly",
                "verify_receipt_interruptibly",
            ),
        )
    if type(view_type) is not str or view_type not in _SUPPORTED_CACHE_VIEWS:
        raise TypeError("compiler cache job view type is invalid")
    _preflight_authorities(
        views=(view_type,),
        repository_source=repository_source,
        view_output_owners={view_type: view_output_owner},
        view_destinations={view_type: view_destination},
        context_output_owner=context_output_owner,
        context_destination=context_destination,
    )
    repository = _exact_text(repository_key, "repository key")
    try:
        normalized_repository = normalize_repo(repository)
    except ValueError as exc:
        raise StorageValidationError("repository key is not canonical") from exc
    if normalized_repository != repository:
        raise StorageValidationError("repository key is not canonical")
    namespace = _exact_text(namespace_name, "namespace name")
    manifest_limit = _manifest_limit(max_manifest_bytes)
    context_files = _positive_limit(max_context_files, "context file limit")
    context_bytes = _positive_limit(max_context_bytes, "context byte limit")
    bundle_files = _positive_limit(max_bundle_files, "bundle file limit")
    bundle_bytes = _positive_limit(max_bundle_bytes, "bundle byte limit")
    bundle_metadata_bytes = _positive_limit(
        max_bundle_metadata_bytes,
        "bundle metadata byte limit",
    )
    environment = _snapshot_environment(environ)
    forbidden = _snapshot_forbidden_paths(forbidden_paths)
    if not isinstance(object_store, RetainedImportObjectStore):
        raise TypeError(
            "compiler cache job object store lacks retained streaming capabilities"
        )
    _require_static_methods(
        object_store,
        label="compiler cache job object store",
        names=_OBJECT_STORE_METHODS,
    )
    _preflight_workspace_provider(workspace_provider)
    binding = _compiler_cache_job_binding(
        job,
        views,
        view_type=view_type,
        repository_source=repository_source,
        repository_key=repository,
        namespace_name=namespace,
        allow_succeeded=False,
        check_cancelled=check_cancelled,
    )
    inputs = _ImportPreflight(
        repository_key=repository,
        namespace_name=namespace,
        ref_name=binding.job.ref_name,
        expected_generation=binding.job.expected_ref_generation,
        max_manifest_bytes=manifest_limit,
        max_context_files=context_files,
        max_context_bytes=context_bytes,
        max_bundle_files=bundle_files,
        max_bundle_bytes=bundle_bytes,
        max_bundle_metadata_bytes=bundle_metadata_bytes,
        max_projection_bytes=DEFAULT_MAX_PROJECTION_BYTES,
        forbidden_paths=forbidden,
        environment=environment,
    )
    cache = lexical_directory_path(Path(cache_dir))
    view_output = lexical_directory_path(view_destination)
    context_output = lexical_directory_path(context_destination)
    fixed_source_views = {view_type: lexical_directory_path(cache / view_type)}
    policy_forbidden = _snapshot_forbidden_paths(
        (
            *inputs.forbidden_paths,
            cache,
            *fixed_source_views.values(),
            view_output,
            context_output,
        )
    )
    return (
        _ImportOperation(
            cache=cache,
            view_owners={view_type: view_output_owner},
            view_outputs={view_type: view_output},
            context_output=context_output,
            inputs=inputs,
            fixed_source_views=fixed_source_views,
            policy_forbidden=policy_forbidden,
        ),
        binding,
    )


def _cache_topology(
    cache: Path,
    identity: RepositorySourceIdentitySnapshot,
) -> None:
    repository = lexical_directory_path(identity.root)
    lexical_relation = _path_relation(cache, repository)
    physical_cache = _resolved_path(cache, strict=True, label="compiler cache")
    physical_repository = _resolved_path(
        repository,
        strict=True,
        label="compiler cache repository",
    )
    physical_relation = _path_relation(physical_cache, physical_repository)
    if lexical_relation != physical_relation:
        raise ValueError(
            "compiler cache has inconsistent lexical and physical repository topology"
        )
    if lexical_relation in {"same", "ancestor"}:
        raise ValueError("compiler cache must not contain the repository source")
    if lexical_relation != "descendant":
        return
    relative = cache.relative_to(repository).as_posix()
    prefix = relative + "/"
    if any(
        record.path == relative or record.path.startswith(prefix)
        for record in identity.file_records
    ):
        raise StorageIntegrityError(
            "compiler cache is present in the authenticated repository source records"
        )


def _destination_topology(
    view_destinations: tuple[Path, ...],
    context_destination: Path,
    *,
    cache: Path,
    repository: Path,
    source_views: tuple[Path, ...],
) -> None:
    lexical_boundaries = (cache, repository, *source_views)
    physical_boundaries = tuple(
        _resolved_path(path, strict=True, label="compiler cache input")
        for path in lexical_boundaries
    )
    outputs = (*view_destinations, context_destination)
    physical_destinations = tuple(
        _resolved_path(path, strict=False, label="compiler cache destination")
        for path in outputs
    )
    for destination, physical_destination in zip(
        outputs,
        physical_destinations,
        strict=True,
    ):
        if any(
            _path_relation(destination, boundary) != "disjoint"
            for boundary in lexical_boundaries
        ) or any(
            _path_relation(physical_destination, boundary) != "disjoint"
            for boundary in physical_boundaries
        ):
            raise ValueError("compiler cache destination overlaps an input authority")
        _require_missing(destination, label="compiler cache destination")
    for index, destination in enumerate(outputs):
        physical_destination = physical_destinations[index]
        for other, physical_other in zip(
            outputs[index + 1 :],
            physical_destinations[index + 1 :],
            strict=True,
        ):
            if (
                _path_relation(destination, other) != "disjoint"
                or _path_relation(physical_destination, physical_other) != "disjoint"
            ):
                raise ValueError("compiler cache output destinations overlap")


def _read_manifest(
    cache: Path,
    *,
    max_manifest_bytes: int,
    check_cancelled: Callable[[], None] | None = None,
) -> bytes:
    return bytes(
        _read_bounded_json(
            cache / MANIFEST_FILENAME,
            label="compiler cache repository manifest",
            max_bytes=max_manifest_bytes,
            check_cancelled=check_cancelled,
        )
    )


def _parse_exact_manifest(payload: bytes, *, max_manifest_bytes: int) -> RepoManifest:
    if payload.startswith(b"\xef\xbb\xbf"):
        raise ValueError("compiler cache repository manifest must not contain a BOM")
    _preflight_json_bytes(
        payload,
        label="compiler cache repository manifest",
        max_bytes=max_manifest_bytes,
    )
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError(
            "compiler cache repository manifest is not valid UTF-8"
        ) from exc
    data = _strict_json_loads(text, label="compiler cache repository manifest")
    try:
        manifest = RepoManifest.from_dict(data)
        exact = manifest.to_dict()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("compiler cache repository manifest is invalid") from exc
    if exact != data:
        raise ValueError(
            "compiler cache repository manifest is not exact "
            f"v{manifest.version} data"
        )
    return manifest


def compiler_cache_source_selection(
    cache_dir: str | Path,
    *,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    check_cancelled: Callable[[], None] | None = None,
) -> RepositorySourceSelection:
    """Return the exact source selection recorded by one compiler cache.

    The CLI needs this identity axis before it captures the retained repository
    source.  The import coordinator authenticates the same manifest again under
    its longer-lived cache lock, so a manifest replacement between these two
    reads can only make the later import fail closed. A cooperative caller may
    supply ``check_cancelled`` to interrupt contention for the short manifest
    lock before source capture begins.
    """

    cache = lexical_directory_path(Path(cache_dir))
    bounded_limit = _manifest_limit(max_manifest_bytes)
    with compiler_cache_lock(
        cache,
        create=False,
        check_cancelled=check_cancelled,
    ):
        manifest = _parse_exact_manifest(
            _read_manifest(cache, max_manifest_bytes=bounded_limit),
            max_manifest_bytes=bounded_limit,
        )
    selection = manifest.source_selection
    if selection is None:
        return DEFAULT_REPOSITORY_SOURCE_SELECTION
    return RepositorySourceSelection(selection.exclude_subtrees)


def _require_manifest_source(
    manifest: RepoManifest,
    identity: RepositorySourceIdentitySnapshot,
) -> None:
    if manifest.version != MANIFEST_VERSION:
        raise ValueError("compiler cache repository manifest version is incompatible")
    declared_path = Path(manifest.repo_path)
    if not manifest.repo_path or not declared_path.is_absolute():
        raise ValueError("compiler cache repository path must be absolute")
    declared_repository = lexical_directory_path(declared_path)
    if declared_repository != lexical_directory_path(identity.root):
        raise StorageIntegrityError(
            "compiler cache repository manifest belongs to another repository"
        )
    if (
        not is_secure_source_fingerprint_v2(manifest.source_fingerprint)
        or manifest.source_fingerprint != identity.fingerprint
        or manifest.file_count != identity.file_count
    ):
        raise StorageIntegrityError(
            "compiler cache repository manifest differs from the retained source"
        )
    selection = manifest.source_selection
    identity_selection = identity.source_selection
    if type(selection) is not RepositorySourceSelection:
        raise ValueError(
            "compiler cache repository manifest has no exact source selection"
        )
    if (
        type(identity_selection) is not RepositorySourceSelection
        or identity_selection != selection
        or identity_selection.digest != selection.digest
    ):
        raise StorageIntegrityError(
            "compiler cache repository source selection differs from the "
            "retained source"
        )


def _view_source(cache: Path, entry: IndexEntry, *, view: str) -> Path:
    declared = Path(entry.path).expanduser()
    if not declared.is_absolute():
        declared = cache / declared
    source = lexical_directory_path(declared)
    expected = lexical_directory_path(cache / view)
    if source != expected:
        raise ValueError(f"compiler cache {view} entry does not name its fixed view")
    return source


def _require_current_view(manifest: RepoManifest, *, view: str) -> IndexEntry:
    entry = manifest.indexes.get(view)
    selection_digest = manifest.source_selection_digest
    if (
        type(entry) is not IndexEntry
        or entry.index_type != view
        or entry.status != "fresh"
        or entry.commit != manifest.commit
        or entry.source_fingerprint != manifest.source_fingerprint
        or not manifest.index_is_current(view)
        or type(selection_digest) is not str
        or entry.source_selection_digest != selection_digest
        or entry.config.get("source_selection_digest") != selection_digest
        or type(entry.config.get("builder_schema")) is not int
        or entry.config.get("builder_schema") != CURRENT_PORTABLE_BUILDER_SCHEMAS[view]
    ):
        raise ValueError(
            f"compiler cache repository manifest has no exact current {view}"
        )
    return entry


def _fingerprints_for_records(
    records: tuple[TreeFileRecord, ...],
) -> dict[str, dict[str, Any]]:
    return {
        record.path: {"size": record.size, "sha256": record.sha256}
        for record in records
    }


def _require_source_fingerprints(
    entry: IndexEntry,
    planned: PlannedBm25View,
) -> None:
    expected = _fingerprints_for_records(planned.source_records)
    if (
        entry.config.get("artifact_file_fingerprints") != expected
        or entry.metadata.get("artifact_file_fingerprints") != expected
    ):
        raise StorageIntegrityError(
            "compiler cache BM25 files differ from their manifest fingerprints"
        )


def _plan_cache_view(
    view: str,
    source: Path,
    destination: Path,
    *,
    repository_source: RepositorySourceBinding,
    view_config: Mapping[str, Any],
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
    check_cancelled: Callable[[], None] | None = None,
) -> Any:
    if view == "bm25":
        return _plan_recaptured_bm25_view(
            source,
            destination,
            repository_source=repository_source,
            view_config=view_config,
            forbidden_paths=forbidden_paths,
            environ=environ,
            check_cancelled=check_cancelled,
        )
    if view == "vector":
        return _plan_recaptured_vector_view(
            source,
            destination,
            repository_source=repository_source,
            view_config=view_config,
            forbidden_paths=forbidden_paths,
            environ=environ,
            check_cancelled=check_cancelled,
        )
    raise AssertionError(f"unsupported compiler cache view: {view}")


def _publish_cache_view(
    view: str,
    source: Path,
    destination: Path,
    *,
    planned: Any,
    repository_source: RepositorySourceBinding,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    view_config: Mapping[str, Any],
    forbidden_paths: tuple[Path, ...],
    environ: Mapping[str, str],
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if view == "bm25":
        return _publish_recaptured_bm25_view(
            source,
            destination,
            planned=planned,
            repository_source=repository_source,
            workspace_provider=workspace_provider,
            output_receipt_owner=output_receipt_owner,
            view_config=view_config,
            forbidden_paths=forbidden_paths,
            environ=environ,
            check_cancelled=check_cancelled,
        )
    if view == "vector":
        return _publish_recaptured_vector_view(
            source,
            destination,
            planned=planned,
            repository_source=repository_source,
            workspace_provider=workspace_provider,
            output_receipt_owner=output_receipt_owner,
            view_config=view_config,
            forbidden_paths=forbidden_paths,
            environ=environ,
            check_cancelled=check_cancelled,
        )
    raise AssertionError(f"unsupported compiler cache view: {view}")


def _planned_adjustments(view: str, planned: Any) -> dict[str, Any]:
    adjustments = planned.adjustments
    if type(adjustments) is not dict:
        raise TypeError(f"compiler cache {view} plan adjustments are invalid")
    if view == "bm25" and adjustments != _fingerprints_adjustment(
        planned.output_records
    ):
        raise StorageIntegrityError(
            "compiler cache BM25 plan adjustments differ from its output records"
        )
    if view == "vector" and set(adjustments) != {
        "artifact_scope",
        "portable_document_format",
        "persistence_config_fingerprint",
    }:
        raise StorageIntegrityError(
            "compiler cache vector plan adjustments are incomplete"
        )
    return copy.deepcopy(adjustments)


def _pretty_manifest_bytes(manifest: RepoManifest) -> bytes:
    return _canonical_json_bytes(
        manifest.to_dict(),
        label="compiler cache portable repository manifest",
    )


def _portable_manifest(
    manifest: RepoManifest,
    *,
    views: tuple[str, ...],
    planned_views: dict[str, Any],
) -> tuple[RepoManifest, bytes]:
    portable = copy.deepcopy(manifest.to_dict())
    portable["repo"]["path"] = "source"
    entries: dict[str, Any] = {}
    for view in views:
        entry = copy.deepcopy(portable["indexes"][view])
        entry["path"] = f"views/{view}"
        adjustments = _planned_adjustments(view, planned_views[view])
        for section_name in ("config", "metadata"):
            section = entry.get(section_name)
            if type(section) is not dict:
                raise ValueError(
                    f"compiler cache {view} {section_name} must be an exact object"
                )
            section.update(copy.deepcopy(adjustments))
        entries[view] = entry
    portable["indexes"] = entries
    portable["capabilities"] = _portable_capabilities(
        portable["capabilities"],
        views,
    )
    try:
        portable_manifest = RepoManifest.from_dict(portable)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("compiler cache portable manifest is invalid") from exc
    if portable_manifest.to_dict() != portable:
        raise ValueError("compiler cache portable manifest is not canonical")
    selection_digest = manifest.source_selection_digest
    if (
        portable_manifest.source_selection != manifest.source_selection
        or portable_manifest.last_indexed_source_selection_digest
        != manifest.last_indexed_source_selection_digest
        or type(selection_digest) is not str
        or any(
            portable_manifest.indexes[view].source_selection_digest != selection_digest
            or portable_manifest.indexes[view].config.get("source_selection_digest")
            != selection_digest
            for view in views
        )
    ):
        raise StorageIntegrityError(
            "compiler cache portable manifest changed its source selection"
        )
    return portable_manifest, _pretty_manifest_bytes(portable_manifest)


def _read_context_manifest(
    owner: PublishedWorkspaceReceiptOwner,
    *,
    max_manifest_bytes: int,
    check_cancelled: Callable[[], None] | None = None,
) -> bytes:
    def read(
        receipt: PublishedWorkspaceReceipt,
        publication: PublicationDirectoryReader,
    ) -> bytes:
        del receipt
        before = publication.capture_ownership(
            check_cancelled=check_cancelled,
        )
        with publication.open_authenticated_file(
            MANIFEST_FILENAME,
            max_bytes=max_manifest_bytes,
        ) as source:
            payload = b"".join(
                _interruptible_chunks(source.iter_bytes(), check_cancelled)
            )
        if (
            publication.capture_ownership(
                check_cancelled=check_cancelled,
            )
            != before
        ):
            raise StorageIntegrityError(
                "compiler cache context artifact changed while reading its manifest"
            )
        return payload

    return owner.consume(read, check_cancelled=check_cancelled)


def _preflight_cache_import_operation(
    cache_dir: str | Path,
    *,
    views: tuple[str, ...],
    repository_source: RepositorySourceBinding,
    view_output_owners: dict[str, PublishedWorkspaceReceiptOwner],
    context_output_owner: PublishedWorkspaceReceiptOwner,
    view_destinations: dict[str, Path],
    context_destination: Path,
    workspace_provider: StrictWorkspaceProvider,
    repository_key: str,
    catalog: RetainedImportCatalog,
    object_store: RetainedImportObjectStore,
    namespace_name: str,
    ref_name: str,
    expected_generation: int,
    max_manifest_bytes: int,
    max_context_files: int,
    max_context_bytes: int,
    max_bundle_files: int,
    max_bundle_bytes: int,
    max_bundle_metadata_bytes: int,
    max_projection_bytes: int,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str] | None,
) -> _ImportOperation:
    """Validate borrowed authorities without touching the compiler cache."""

    _preflight_authorities(
        views=views,
        repository_source=repository_source,
        view_output_owners=view_output_owners,
        view_destinations=view_destinations,
        context_output_owner=context_output_owner,
        context_destination=context_destination,
    )
    view_owners = dict(view_output_owners)
    destination_inputs = dict(view_destinations)
    cache = lexical_directory_path(Path(cache_dir))
    view_outputs = {
        view: lexical_directory_path(destination_inputs[view]) for view in views
    }
    context_output = lexical_directory_path(context_destination)
    inputs = _preflight_import_inputs(
        repository_key=repository_key,
        catalog=catalog,
        object_store=object_store,
        namespace_name=namespace_name,
        ref_name=ref_name,
        expected_generation=expected_generation,
        max_manifest_bytes=max_manifest_bytes,
        max_context_files=max_context_files,
        max_context_bytes=max_context_bytes,
        max_bundle_files=max_bundle_files,
        max_bundle_bytes=max_bundle_bytes,
        max_bundle_metadata_bytes=max_bundle_metadata_bytes,
        max_projection_bytes=max_projection_bytes,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )
    fixed_source_views = {view: lexical_directory_path(cache / view) for view in views}
    policy_forbidden = _snapshot_forbidden_paths(
        (
            *inputs.forbidden_paths,
            cache,
            *fixed_source_views.values(),
            *view_outputs.values(),
            context_output,
        )
    )
    _preflight_workspace_provider(workspace_provider)
    _preflight_catalog_contract(catalog)
    return _ImportOperation(
        cache=cache,
        view_owners=view_owners,
        view_outputs=view_outputs,
        context_output=context_output,
        inputs=inputs,
        fixed_source_views=fixed_source_views,
        policy_forbidden=policy_forbidden,
    )


def _prepare_compiler_cache_import_locked(
    operation: _ImportOperation,
    *,
    views: tuple[str, ...],
    repository_source: RepositorySourceBinding,
    context_output_owner: PublishedWorkspaceReceiptOwner,
    workspace_provider: StrictWorkspaceProvider,
    expected_manifest: RepoManifest | None = None,
    job_binding: _CompilerCacheJobBinding | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> _PreparedCompilerCacheImport:
    """Recapture selected views while the caller holds the cache lease."""

    cache = operation.cache
    inputs = operation.inputs
    if check_cancelled is not None:
        check_cancelled()
    source_manifest_bytes = _read_manifest(
        cache,
        max_manifest_bytes=inputs.max_manifest_bytes,
        check_cancelled=check_cancelled,
    )
    manifest = _parse_exact_manifest(
        source_manifest_bytes,
        max_manifest_bytes=inputs.max_manifest_bytes,
    )
    if expected_manifest is not None:
        if type(expected_manifest) is not RepoManifest:
            raise TypeError("compiler update returned an invalid repository manifest")
        if expected_manifest.to_dict() != manifest.to_dict():
            raise StorageIntegrityError(
                "compiler update result differs from its exact serialized manifest"
            )
    if _COMMIT_RE.fullmatch(manifest.commit) is None:
        raise ValueError(
            "compiler cache repository manifest commit must be a full "
            "lowercase Git SHA"
        )
    identity = (
        repository_source.authenticated_identity_snapshot()
        if check_cancelled is None
        else repository_source.authenticated_identity_snapshot(
            check_cancelled=check_cancelled,
        )
    )
    if type(identity) is not RepositorySourceIdentitySnapshot:
        raise TypeError("compiler cache repository identity has an invalid type")
    _cache_topology(cache, identity)
    _require_manifest_source(manifest, identity)
    entries = {view: _require_current_view(manifest, view=view) for view in views}
    source_views = {
        view: _view_source(cache, entries[view], view=view) for view in views
    }
    _destination_topology(
        tuple(operation.view_outputs.values()),
        operation.context_output,
        cache=cache,
        repository=lexical_directory_path(identity.root),
        source_views=tuple(source_views.values()),
    )
    if source_views != operation.fixed_source_views:  # pragma: no cover
        raise AssertionError("compiler cache fixed view sources changed")
    if check_cancelled is not None:
        check_cancelled()

    # Every raw-cache recapture plan plus the exact portable manifest and
    # retained import plan exists before the first provider call can mutate
    # any workspace destination.  The context plan is authority-dependent:
    # it is built later from the published view receipts, before context
    # workspace mutation.
    planned_views: dict[str, Any] = {}
    for view in views:
        if check_cancelled is not None:
            check_cancelled()
        planned = _plan_cache_view(
            view,
            source_views[view],
            operation.view_outputs[view],
            repository_source=repository_source,
            view_config=entries[view].config,
            forbidden_paths=operation.policy_forbidden,
            environ=inputs.environment,
            check_cancelled=check_cancelled,
        )
        if view == "bm25":
            _require_source_fingerprints(entries[view], planned)
        _planned_adjustments(view, planned)
        if check_cancelled is not None:
            check_cancelled()
        planned_views[view] = planned
    portable_manifest, canonical_manifest_bytes = _portable_manifest(
        manifest,
        views=views,
        planned_views=planned_views,
    )
    import_plan = plan_repo_manifest_import_bytes(
        canonical_manifest_bytes,
        views=views,
        max_manifest_bytes=inputs.max_manifest_bytes,
    )
    if (
        import_plan.selection.selected_views != views
        or import_plan.manifest.to_dict() != portable_manifest.to_dict()
    ):
        raise StorageIntegrityError(
            "compiler cache portable manifest changed during import planning"
        )
    if job_binding is not None:
        if type(job_binding) is not _CompilerCacheJobBinding or len(views) != 1:
            raise TypeError("compiler cache job binding is invalid")
        _require_compiler_cache_job_profile(
            job_binding,
            import_plan,
            view_type=views[0],
        )
    if check_cancelled is not None:
        check_cancelled()

    recaptures: list[CompilerCacheViewRecaptureResult] = []
    for view in views:
        if check_cancelled is not None:
            check_cancelled()
        planned = planned_views[view]
        adjustments = _publish_cache_view(
            view,
            source_views[view],
            operation.view_outputs[view],
            planned=planned,
            repository_source=repository_source,
            workspace_provider=workspace_provider,
            output_receipt_owner=operation.view_owners[view],
            view_config=entries[view].config,
            forbidden_paths=operation.policy_forbidden,
            environ=inputs.environment,
            check_cancelled=check_cancelled,
        )
        if adjustments != _planned_adjustments(view, planned):
            raise StorageIntegrityError(
                f"compiler cache {view} publication differs from its exact plan"
            )
        if check_cancelled is not None:
            check_cancelled()
        recaptures.append(
            CompilerCacheViewRecaptureResult(
                view_type=view,
                source_view=source_views[view],
                output_view=operation.view_outputs[view],
                source_records=planned.source_records,
                output_records=planned.output_records,
            )
        )

    if check_cancelled is not None:
        check_cancelled()
    planned_context = plan_context_artifact_strict(
        portable_manifest,
        repository=inputs.repository_key,
        repository_source=repository_source,
        view_generations=operation.view_owners,
        environ=inputs.environment,
        check_cancelled=check_cancelled,
    )
    if (
        planned_context.views != views
        or planned_context.manifest_payload != canonical_manifest_bytes
    ):
        raise StorageIntegrityError(
            "compiler cache context plan differs from its portable manifest"
        )
    if check_cancelled is not None:
        check_cancelled()
    context_artifact = publish_planned_context_artifact_strict(
        operation.context_output,
        planned=planned_context,
        manifest=portable_manifest,
        repository=inputs.repository_key,
        repository_source=repository_source,
        view_generations=operation.view_owners,
        workspace_provider=workspace_provider,
        output_receipt_owner=context_output_owner,
        environ=inputs.environment,
        check_cancelled=check_cancelled,
    )
    observed_manifest_bytes = _read_context_manifest(
        context_output_owner,
        max_manifest_bytes=inputs.max_manifest_bytes,
        check_cancelled=check_cancelled,
    )
    if observed_manifest_bytes != canonical_manifest_bytes:
        raise StorageIntegrityError(
            "compiler cache context manifest differs from its preplanned bytes"
        )
    final_identity = (
        repository_source.authenticated_identity_snapshot()
        if check_cancelled is None
        else repository_source.authenticated_identity_snapshot(
            check_cancelled=check_cancelled,
        )
    )
    if final_identity != identity:
        raise StorageIntegrityError(
            "compiler cache repository source changed during recapture"
        )
    if (
        _read_manifest(
            cache,
            max_manifest_bytes=inputs.max_manifest_bytes,
            check_cancelled=check_cancelled,
        )
        != source_manifest_bytes
    ):
        raise StorageIntegrityError(
            "compiler cache repository manifest changed during recapture"
        )
    result = _PreparedCompilerCacheImport(
        manifest=manifest,
        recaptures=tuple(recaptures),
        canonical_manifest_bytes=canonical_manifest_bytes,
        import_plan=import_plan,
        context_artifact=context_artifact,
    )
    if check_cancelled is not None:
        check_cancelled()
    return result


def _commit_prepared_compiler_cache_import(
    preparation: _PreparedCompilerCacheImport,
    operation: _ImportOperation,
    *,
    repository_source: RepositorySourceBinding,
    context_output_owner: PublishedWorkspaceReceiptOwner,
    catalog: RetainedImportCatalog,
    object_store: RetainedImportObjectStore,
) -> CompilerCacheMultiViewImportResult:
    """Commit immutable evidence after the mutable cache lease is released."""

    inputs = operation.inputs
    import_result = import_retained_repo_manifest(
        preparation.import_plan,
        artifact_owner=context_output_owner,
        repository_source=repository_source,
        repository_key=inputs.repository_key,
        catalog=catalog,
        object_store=object_store,
        namespace_name=inputs.namespace_name,
        ref_name=inputs.ref_name,
        expected_generation=inputs.expected_generation,
        max_context_files=inputs.max_context_files,
        max_context_bytes=inputs.max_context_bytes,
        max_bundle_files=inputs.max_bundle_files,
        max_bundle_bytes=inputs.max_bundle_bytes,
        max_bundle_metadata_bytes=inputs.max_bundle_metadata_bytes,
        max_projection_bytes=inputs.max_projection_bytes,
        forbidden_paths=operation.policy_forbidden,
        environ=inputs.environment,
    )
    return CompilerCacheMultiViewImportResult(
        recaptures=preparation.recaptures,
        canonical_manifest_bytes=preparation.canonical_manifest_bytes,
        import_plan=preparation.import_plan,
        context_artifact=preparation.context_artifact,
        import_result=import_result,
    )


def _ingest_prepared_compiler_cache_job(
    preparation: _PreparedCompilerCacheImport,
    operation: _ImportOperation,
    binding: _CompilerCacheJobBinding,
    *,
    view_type: str,
    repository_source: RepositorySourceBinding,
    context_output_owner: PublishedWorkspaceReceiptOwner,
    object_store: RetainedImportObjectStore,
    check_cancelled: Callable[[], None] | None = None,
) -> IndexJobViewArtifact:
    """Ingest one prepared cache view without granting catalog authority."""

    if type(preparation) is not _PreparedCompilerCacheImport:
        raise TypeError("compiler cache job preparation is invalid")
    if type(operation) is not _ImportOperation:
        raise TypeError("compiler cache job operation is invalid")
    if type(binding) is not _CompilerCacheJobBinding:
        raise TypeError("compiler cache job binding is invalid")
    _require_compiler_cache_job_profile(
        binding,
        preparation.import_plan,
        view_type=view_type,
    )
    if check_cancelled is not None:
        check_cancelled()
    artifacts = context_output_owner.consume(
        lambda receipt, publication: _prepare_job_view_artifacts_inside_authority(
            receipt,
            publication,
            plan=preparation.import_plan,
            repository_source=repository_source,
            repository_key=operation.inputs.repository_key,
            source_identity=binding.source_identity,
            object_store=object_store,
            environment=operation.inputs.environment,
            forbidden_paths=operation.policy_forbidden,
            max_context_files=operation.inputs.max_context_files,
            max_context_bytes=operation.inputs.max_context_bytes,
            max_bundle_files=operation.inputs.max_bundle_files,
            max_bundle_bytes=operation.inputs.max_bundle_bytes,
            max_bundle_metadata_bytes=operation.inputs.max_bundle_metadata_bytes,
            check_cancelled=check_cancelled,
        ),
        check_cancelled=check_cancelled,
    )
    if type(artifacts) is not tuple or len(artifacts) != 1:
        raise StorageIntegrityError(
            f"compiler cache {view_type} ingestion returned invalid job artifacts"
        )
    artifact = artifacts[0]
    if (
        type(artifact) is not IndexJobViewArtifact
        or artifact.view_type != binding.view.view_type
        or artifact.profile_id != binding.view.profile_id
    ):
        raise StorageIntegrityError(
            "compiler cache ingestion returned a different requested view"
        )
    final_source_snapshot = (
        repository_source.authenticated_identity_snapshot()
        if check_cancelled is None
        else repository_source.authenticated_identity_snapshot(
            check_cancelled=check_cancelled,
        )
    )
    if final_source_snapshot != binding.source_snapshot:
        raise StorageIntegrityError(
            "compiler cache repository source changed during CAS ingestion"
        )
    if check_cancelled is not None:
        check_cancelled()
    return artifact


def import_compiler_cache(
    cache_dir: str | Path,
    *,
    views: tuple[str, ...],
    repository_source: RepositorySourceBinding,
    view_output_owners: dict[str, PublishedWorkspaceReceiptOwner],
    context_output_owner: PublishedWorkspaceReceiptOwner,
    view_destinations: dict[str, Path],
    context_destination: Path,
    workspace_provider: StrictWorkspaceProvider,
    repository_key: str,
    catalog: RetainedImportCatalog,
    object_store: RetainedImportObjectStore,
    namespace_name: str = DEFAULT_NAMESPACE_NAME,
    ref_name: str = DEFAULT_REF_NAME,
    expected_generation: int = 0,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES,
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    max_projection_bytes: int = DEFAULT_MAX_PROJECTION_BYTES,
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> CompilerCacheMultiViewImportResult:
    """Recapture and import one exact current compiler-cache view set.

    All receipt owners and the retained repository binding are borrowed.  The
    caller remains responsible for closing them on success and failure.
    Published directories are immutable evidence and are never recursively
    removed when a later publication or retained import fails.
    """

    operation = _preflight_cache_import_operation(
        cache_dir,
        views=views,
        repository_source=repository_source,
        view_output_owners=view_output_owners,
        context_output_owner=context_output_owner,
        view_destinations=view_destinations,
        context_destination=context_destination,
        workspace_provider=workspace_provider,
        repository_key=repository_key,
        catalog=catalog,
        object_store=object_store,
        namespace_name=namespace_name,
        ref_name=ref_name,
        expected_generation=expected_generation,
        max_manifest_bytes=max_manifest_bytes,
        max_context_files=max_context_files,
        max_context_bytes=max_context_bytes,
        max_bundle_files=max_bundle_files,
        max_bundle_bytes=max_bundle_bytes,
        max_bundle_metadata_bytes=max_bundle_metadata_bytes,
        max_projection_bytes=max_projection_bytes,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )
    with compiler_cache_lock(operation.cache, create=False):
        preparation = _prepare_compiler_cache_import_locked(
            operation,
            views=views,
            repository_source=repository_source,
            context_output_owner=context_output_owner,
            workspace_provider=workspace_provider,
        )

    # Catalog/CAS publication deliberately occurs after releasing the mutable
    # compiler cache lock.  The context receipt remains the exact authority
    # authenticated above, so the mutable cache is no longer consulted.
    return _commit_prepared_compiler_cache_import(
        preparation,
        operation,
        repository_source=repository_source,
        context_output_owner=context_output_owner,
        catalog=catalog,
        object_store=object_store,
    )


def _retained_cache_manifest_bytes(
    publication: PublicationDirectoryReader,
    *,
    max_manifest_bytes: int,
    check_cancelled: Callable[[], None] | None,
) -> bytes:
    before = publication.capture_ownership(check_cancelled=check_cancelled)
    with publication.open_authenticated_file(
        MANIFEST_FILENAME,
        max_bytes=max_manifest_bytes,
    ) as source:
        payload = b"".join(_interruptible_chunks(source.iter_bytes(), check_cancelled))
    if publication.capture_ownership(check_cancelled=check_cancelled) != before:
        raise StorageIntegrityError(
            "retained compiler cache changed while reading its manifest"
        )
    return payload


def _prepare_retained_bm25_cache_generation(
    operation: _ImportOperation,
    binding: _CompilerCacheJobBinding,
    receipt: PublishedWorkspaceReceipt,
    publication: PublicationDirectoryReader,
    *,
    expected_manifest: RepoManifest,
    repository_source: RepositorySourceBinding,
    context_output_owner: PublishedWorkspaceReceiptOwner,
    workspace_provider: StrictWorkspaceProvider,
    check_cancelled: Callable[[], None] | None,
) -> _PreparedCompilerCacheImport:
    """Prepare BM25 while one exact cache-generation authority stays active."""

    cache = operation.cache
    inputs = operation.inputs
    if receipt.path != cache:
        raise StorageIntegrityError(
            "retained compiler cache receipt belongs to another destination"
        )
    initial_ownership = publication.capture_ownership(
        check_cancelled=check_cancelled,
    )
    expected_inventory = {
        (COMPILER_CACHE_LOCK_FILENAME, "file"),
        ("bm25", "directory"),
        ("bm25/bm25_metadata.json", "file"),
        ("bm25/documents.json", "file"),
        (MANIFEST_FILENAME, "file"),
    }
    if set(publication.inventory()) != expected_inventory:
        raise StorageIntegrityError(
            "retained compiler cache generation has an unexpected layout"
        )
    source_manifest_bytes = _retained_cache_manifest_bytes(
        publication,
        max_manifest_bytes=inputs.max_manifest_bytes,
        check_cancelled=check_cancelled,
    )
    manifest = _parse_exact_manifest(
        source_manifest_bytes,
        max_manifest_bytes=inputs.max_manifest_bytes,
    )
    if (
        type(expected_manifest) is not RepoManifest
        or expected_manifest.to_dict() != manifest.to_dict()
    ):
        raise StorageIntegrityError(
            "retained compiler cache differs from its prepared manifest"
        )
    if _COMMIT_RE.fullmatch(manifest.commit) is None:
        raise ValueError(
            "compiler cache repository manifest commit must be a full "
            "lowercase Git SHA"
        )
    identity = repository_source.authenticated_identity_snapshot(
        check_cancelled=check_cancelled,
    )
    if type(identity) is not RepositorySourceIdentitySnapshot:
        raise TypeError("compiler cache repository identity has an invalid type")
    repository = lexical_directory_path(identity.root)
    if _path_relation(cache, repository) != "disjoint":
        raise ValueError("retained compiler cache overlaps its repository")
    _require_manifest_source(manifest, identity)
    entry = _require_current_view(manifest, view="bm25")
    source_view = _view_source(cache, entry, view="bm25")
    if source_view != operation.fixed_source_views["bm25"]:
        raise AssertionError("compiler cache fixed BM25 source changed")
    for output in (*operation.view_outputs.values(), operation.context_output):
        if any(
            _path_relation(output, boundary) != "disjoint"
            for boundary in (cache, source_view, repository)
        ):
            raise ValueError(
                "compiler cache output overlaps a retained input authority"
            )
    if check_cancelled is not None:
        check_cancelled()

    source_publication = publication.subtree("bm25")
    planned = _plan_retained_bm25_publication_view(
        source_publication,
        repository_source=repository_source,
        repository_identity=identity,
        view_config=entry.config,
        forbidden_paths=operation.policy_forbidden,
        environ=inputs.environment,
        check_cancelled=check_cancelled,
    )
    _require_source_fingerprints(entry, planned)
    _planned_adjustments("bm25", planned)
    portable_manifest, canonical_manifest_bytes = _portable_manifest(
        manifest,
        views=("bm25",),
        planned_views={"bm25": planned},
    )
    import_plan = plan_repo_manifest_import_bytes(
        canonical_manifest_bytes,
        views=("bm25",),
        max_manifest_bytes=inputs.max_manifest_bytes,
    )
    if (
        import_plan.selection.selected_views != ("bm25",)
        or import_plan.manifest.to_dict() != portable_manifest.to_dict()
    ):
        raise StorageIntegrityError(
            "compiler cache portable manifest changed during import planning"
        )
    _require_compiler_cache_job_profile(
        binding,
        import_plan,
        view_type="bm25",
    )
    if check_cancelled is not None:
        check_cancelled()

    adjustments = _publish_retained_bm25_publication_view(
        source_publication,
        operation.view_outputs["bm25"],
        planned=planned,
        repository_source=repository_source,
        repository_identity=identity,
        workspace_provider=workspace_provider,
        output_receipt_owner=operation.view_owners["bm25"],
        view_config=entry.config,
        forbidden_paths=operation.policy_forbidden,
        environ=inputs.environment,
        check_cancelled=check_cancelled,
    )
    if adjustments != _planned_adjustments("bm25", planned):
        raise StorageIntegrityError(
            "compiler cache BM25 publication differs from its exact plan"
        )
    recapture = CompilerCacheViewRecaptureResult(
        view_type="bm25",
        source_view=source_view,
        output_view=operation.view_outputs["bm25"],
        source_records=planned.source_records,
        output_records=planned.output_records,
    )
    if check_cancelled is not None:
        check_cancelled()

    planned_context = plan_context_artifact_strict(
        portable_manifest,
        repository=inputs.repository_key,
        repository_source=repository_source,
        view_generations=operation.view_owners,
        environ=inputs.environment,
        check_cancelled=check_cancelled,
    )
    if (
        planned_context.views != ("bm25",)
        or planned_context.manifest_payload != canonical_manifest_bytes
    ):
        raise StorageIntegrityError(
            "compiler cache context plan differs from its portable manifest"
        )
    context_artifact = publish_planned_context_artifact_strict(
        operation.context_output,
        planned=planned_context,
        manifest=portable_manifest,
        repository=inputs.repository_key,
        repository_source=repository_source,
        view_generations=operation.view_owners,
        workspace_provider=workspace_provider,
        output_receipt_owner=context_output_owner,
        environ=inputs.environment,
        check_cancelled=check_cancelled,
    )
    if (
        _read_context_manifest(
            context_output_owner,
            max_manifest_bytes=inputs.max_manifest_bytes,
            check_cancelled=check_cancelled,
        )
        != canonical_manifest_bytes
    ):
        raise StorageIntegrityError(
            "compiler cache context manifest differs from its preplanned bytes"
        )
    final_identity = repository_source.authenticated_identity_snapshot(
        check_cancelled=check_cancelled,
    )
    if final_identity != identity:
        raise StorageIntegrityError(
            "compiler cache repository source changed during recapture"
        )
    if (
        _retained_cache_manifest_bytes(
            publication,
            max_manifest_bytes=inputs.max_manifest_bytes,
            check_cancelled=check_cancelled,
        )
        != source_manifest_bytes
        or publication.capture_ownership(check_cancelled=check_cancelled)
        != initial_ownership
    ):
        raise StorageIntegrityError("retained compiler cache changed during recapture")
    return _PreparedCompilerCacheImport(
        manifest=manifest,
        recaptures=(recapture,),
        canonical_manifest_bytes=canonical_manifest_bytes,
        import_plan=import_plan,
        context_artifact=context_artifact,
    )


def prepare_compiler_cache_job_view_from_generation(
    cache_generation: PublishedWorkspaceReceiptOwner,
    *,
    expected_manifest: RepoManifest,
    job: IndexJobRecord,
    views: tuple[IndexJobViewRecord, ...],
    repository_source: RepositorySourceBinding,
    view_output_owner: PublishedWorkspaceReceiptOwner,
    context_output_owner: PublishedWorkspaceReceiptOwner,
    view_destination: Path,
    context_destination: Path,
    workspace_provider: StrictWorkspaceProvider,
    repository_key: str,
    object_store: RetainedImportObjectStore,
    namespace_name: str = DEFAULT_NAMESPACE_NAME,
    stop_token: IndexJobStopToken | None = None,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES,
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> CompilerCacheJobPreparationResult:
    """Prepare BM25 from one retained cache generation without path reopening."""

    if type(cache_generation) is not PublishedWorkspaceReceiptOwner:
        raise TypeError("compiler cache generation must be an exact receipt owner")
    if not cache_generation.active:
        raise RuntimeError("compiler cache generation must be active")
    if (
        cache_generation is view_output_owner
        or cache_generation is context_output_owner
    ):
        raise ValueError("compiler cache generation must use a distinct receipt owner")
    check_cancelled = _compiler_cache_job_stop_check(stop_token)
    if check_cancelled is not None:
        check_cancelled()
    cache = cache_generation.receipt.path
    operation, binding = _preflight_cache_job_preparation_operation(
        cache,
        view_type="bm25",
        job=job,
        views=views,
        repository_source=repository_source,
        view_output_owner=view_output_owner,
        context_output_owner=context_output_owner,
        view_destination=view_destination,
        context_destination=context_destination,
        workspace_provider=workspace_provider,
        repository_key=repository_key,
        object_store=object_store,
        namespace_name=namespace_name,
        max_manifest_bytes=max_manifest_bytes,
        max_context_files=max_context_files,
        max_context_bytes=max_context_bytes,
        max_bundle_files=max_bundle_files,
        max_bundle_bytes=max_bundle_bytes,
        max_bundle_metadata_bytes=max_bundle_metadata_bytes,
        forbidden_paths=forbidden_paths,
        environ=environ,
        check_cancelled=check_cancelled,
    )

    def prepare(
        receipt: PublishedWorkspaceReceipt,
        publication: PublicationDirectoryReader,
    ) -> _PreparedCompilerCacheImport:
        return _prepare_retained_bm25_cache_generation(
            operation,
            binding,
            receipt,
            publication,
            expected_manifest=expected_manifest,
            repository_source=repository_source,
            context_output_owner=context_output_owner,
            workspace_provider=workspace_provider,
            check_cancelled=check_cancelled,
        )

    preparation = cache_generation.consume(
        prepare,
        check_cancelled=check_cancelled,
    )
    if check_cancelled is not None:
        check_cancelled()
    artifact = _ingest_prepared_compiler_cache_job(
        preparation,
        operation,
        binding,
        view_type="bm25",
        repository_source=repository_source,
        context_output_owner=context_output_owner,
        object_store=object_store,
        check_cancelled=check_cancelled,
    )
    result = CompilerCacheJobPreparationResult(
        job=binding.job,
        view=binding.view,
        manifest=preparation.import_plan.manifest,
        import_plan=preparation.import_plan,
        recapture=preparation.recaptures[0],
        context_artifact=preparation.context_artifact,
        artifact=artifact,
    )
    if check_cancelled is not None:
        check_cancelled()
    return result


def prepare_compiler_cache_job_view(
    cache_dir: str | Path,
    *,
    view_type: str,
    job: IndexJobRecord,
    views: tuple[IndexJobViewRecord, ...],
    repository_source: RepositorySourceBinding,
    view_output_owner: PublishedWorkspaceReceiptOwner,
    context_output_owner: PublishedWorkspaceReceiptOwner,
    view_destination: Path,
    context_destination: Path,
    workspace_provider: StrictWorkspaceProvider,
    repository_key: str,
    object_store: RetainedImportObjectStore,
    namespace_name: str = DEFAULT_NAMESPACE_NAME,
    stop_token: IndexJobStopToken | None = None,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES,
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> CompilerCacheJobPreparationResult:
    """Prepare one exact compiler-cache view for worker-owned publication.

    This adapter intentionally accepts no catalog, lease owner, or fencing
    token. It authenticates the already-claimed worker job, recaptures the
    requested FULL cache view, and stores the immutable publication closure in
    the caller-provided object store. The durable worker remains the sole
    authority that may publish the returned artifact. When supplied, the
    worker's read-only stop token makes lock waits, recapture, and CAS
    ingestion cooperatively interruptible without exposing mutation authority.
    """

    check_cancelled = _compiler_cache_job_stop_check(stop_token)
    if check_cancelled is not None:
        check_cancelled()
    operation, binding = _preflight_cache_job_preparation_operation(
        cache_dir,
        view_type=view_type,
        job=job,
        views=views,
        repository_source=repository_source,
        view_output_owner=view_output_owner,
        context_output_owner=context_output_owner,
        view_destination=view_destination,
        context_destination=context_destination,
        workspace_provider=workspace_provider,
        repository_key=repository_key,
        object_store=object_store,
        namespace_name=namespace_name,
        max_manifest_bytes=max_manifest_bytes,
        max_context_files=max_context_files,
        max_context_bytes=max_context_bytes,
        max_bundle_files=max_bundle_files,
        max_bundle_bytes=max_bundle_bytes,
        max_bundle_metadata_bytes=max_bundle_metadata_bytes,
        forbidden_paths=forbidden_paths,
        environ=environ,
        check_cancelled=check_cancelled,
    )
    if check_cancelled is not None:
        check_cancelled()
    with compiler_cache_lock(
        operation.cache,
        create=False,
        check_cancelled=check_cancelled,
    ):
        preparation = _prepare_compiler_cache_import_locked(
            operation,
            views=(view_type,),
            repository_source=repository_source,
            context_output_owner=context_output_owner,
            workspace_provider=workspace_provider,
            job_binding=binding,
            check_cancelled=check_cancelled,
        )
    if check_cancelled is not None:
        check_cancelled()
    artifact = _ingest_prepared_compiler_cache_job(
        preparation,
        operation,
        binding,
        view_type=view_type,
        repository_source=repository_source,
        context_output_owner=context_output_owner,
        object_store=object_store,
        check_cancelled=check_cancelled,
    )
    if len(preparation.recaptures) != 1:
        raise StorageIntegrityError(
            "compiler cache job preparation returned invalid recapture evidence"
        )
    result = CompilerCacheJobPreparationResult(
        job=binding.job,
        view=binding.view,
        manifest=preparation.import_plan.manifest,
        import_plan=preparation.import_plan,
        recapture=preparation.recaptures[0],
        context_artifact=preparation.context_artifact,
        artifact=artifact,
    )
    if check_cancelled is not None:
        check_cancelled()
    return result


@dataclass(frozen=True, slots=True)
class CompilerCacheJobExecutor:
    """One-shot worker executor for one current BM25 or vector cache view.

    A resolver must supply fresh receipt owners and destinations for every
    attempt. The object store must be the same retained backend configured on
    the enclosing worker. Resource cleanup remains the resolver/caller's
    responsibility after the attempt settles.
    """

    cache_dir: str | Path
    view_type: str
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
    environ: Mapping[str, str] | None = None

    def execute(
        self,
        context: IndexJobExecutionContext,
    ) -> IndexJobExecutionResult:
        """Prepare the claimed view without receiving catalog authority."""

        if type(context) is not IndexJobExecutionContext:
            raise TypeError("compiler cache executor requires an exact job context")
        prepared = prepare_compiler_cache_job_view(
            self.cache_dir,
            view_type=self.view_type,
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
            stop_token=context.control.stop_token,
            max_manifest_bytes=self.max_manifest_bytes,
            max_context_files=self.max_context_files,
            max_context_bytes=self.max_context_bytes,
            max_bundle_files=self.max_bundle_files,
            max_bundle_bytes=self.max_bundle_bytes,
            max_bundle_metadata_bytes=self.max_bundle_metadata_bytes,
            forbidden_paths=self.forbidden_paths,
            environ=self.environ,
        )
        return IndexJobExecutionResult(
            views=(
                IndexJobViewExecutionResult.create(
                    prepared.view,
                    effective_mode=IndexJobEffectiveMode.FULL,
                    outcome=IndexJobViewOutcome.SUCCEEDED,
                    artifact=prepared.artifact,
                    payload={"adapter": "compiler_cache", "prepared": True},
                ),
            ),
            retryable=False,
        )


def _publish_compiler_cache_job(
    cache_dir: str | Path,
    *,
    view_type: str,
    job_id: str,
    owner_id: str,
    fencing_token: int,
    repository_source: RepositorySourceBinding,
    view_output_owner: PublishedWorkspaceReceiptOwner,
    context_output_owner: PublishedWorkspaceReceiptOwner,
    view_destination: Path,
    context_destination: Path,
    workspace_provider: StrictWorkspaceProvider,
    repository_key: str,
    catalog: JobPublicationCatalog,
    object_store: RetainedImportObjectStore,
    namespace_name: str,
    max_manifest_bytes: int,
    max_context_files: int,
    max_context_bytes: int,
    max_bundle_files: int,
    max_bundle_bytes: int,
    max_bundle_metadata_bytes: int,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str] | None,
) -> tuple[_PreparedCompilerCacheImport, IndexJobRecord]:
    """Recapture and atomically publish one exact compiler-cache job view."""

    (
        operation,
        binding,
        normalized_job_id,
        normalized_owner_id,
        token,
    ) = _preflight_cache_job_operation(
        cache_dir,
        view_type=view_type,
        job_id=job_id,
        owner_id=owner_id,
        fencing_token=fencing_token,
        repository_source=repository_source,
        view_output_owner=view_output_owner,
        context_output_owner=context_output_owner,
        view_destination=view_destination,
        context_destination=context_destination,
        workspace_provider=workspace_provider,
        repository_key=repository_key,
        catalog=catalog,
        object_store=object_store,
        namespace_name=namespace_name,
        max_manifest_bytes=max_manifest_bytes,
        max_context_files=max_context_files,
        max_context_bytes=max_context_bytes,
        max_bundle_files=max_bundle_files,
        max_bundle_bytes=max_bundle_bytes,
        max_bundle_metadata_bytes=max_bundle_metadata_bytes,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )
    with compiler_cache_lock(operation.cache, create=False):
        preparation = _prepare_compiler_cache_import_locked(
            operation,
            views=(view_type,),
            repository_source=repository_source,
            context_output_owner=context_output_owner,
            workspace_provider=workspace_provider,
            job_binding=binding,
        )

    # Job reads are advisory fail-fast gates. The final publication transaction
    # remains authoritative for the owner, fence, expiry, cancellation, and ref
    # generation; a previously committed exact success remains replayable.
    current = _read_compiler_cache_job_binding(
        normalized_job_id,
        view_type=view_type,
        catalog=catalog,
        repository_source=repository_source,
        repository_key=operation.inputs.repository_key,
        namespace_name=operation.inputs.namespace_name,
    )
    _require_compiler_cache_job_profile(
        current,
        preparation.import_plan,
        view_type=view_type,
    )
    if (
        current.source_snapshot != binding.source_snapshot
        or current.source_identity != binding.source_identity
        or current.job.request_digest != binding.job.request_digest
    ):
        raise StorageIntegrityError(
            "compiler cache job or repository source changed before CAS ingestion"
        )

    artifact = _ingest_prepared_compiler_cache_job(
        preparation,
        operation,
        binding,
        view_type=view_type,
        repository_source=repository_source,
        context_output_owner=context_output_owner,
        object_store=object_store,
    )
    current = _read_compiler_cache_job_binding(
        normalized_job_id,
        view_type=view_type,
        catalog=catalog,
        repository_source=repository_source,
        repository_key=operation.inputs.repository_key,
        namespace_name=operation.inputs.namespace_name,
    )
    _require_compiler_cache_job_profile(
        current,
        preparation.import_plan,
        view_type=view_type,
    )
    if (
        current.source_snapshot != binding.source_snapshot
        or current.source_identity != binding.source_identity
        or current.job.request_digest != binding.job.request_digest
    ):
        raise StorageIntegrityError(
            "compiler cache job or repository source changed before publication"
        )
    completed = publish_job_artifacts(
        normalized_job_id,
        catalog=catalog,
        object_store=object_store,
        owner_id=normalized_owner_id,
        fencing_token=token,
        outputs=(artifact,),
    )
    return preparation, completed


def publish_compiler_cache_bm25_job(
    cache_dir: str | Path,
    *,
    job_id: str,
    owner_id: str,
    fencing_token: int,
    repository_source: RepositorySourceBinding,
    bm25_output_owner: PublishedWorkspaceReceiptOwner,
    context_output_owner: PublishedWorkspaceReceiptOwner,
    bm25_destination: Path,
    context_destination: Path,
    workspace_provider: StrictWorkspaceProvider,
    repository_key: str,
    catalog: JobPublicationCatalog,
    object_store: RetainedImportObjectStore,
    namespace_name: str = DEFAULT_NAMESPACE_NAME,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES,
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> CompilerCacheJobPublicationResult:
    """Publish one exact current BM25 cache into a caller-owned fenced job.

    The caller must already have registered the repository, source, and exact
    BM25 profile, created the job, and acquired its active ref lease. This
    adapter never creates, acquires, renews, or finishes a job independently.
    Mutable cache reads and strict recapture share one existing-only compiler
    cache lease. Only the retained context receipt crosses that boundary into
    bundle/member CAS ingestion, and ``publish_job_artifacts`` is the sole
    catalog mutation path.
    """

    preparation, completed = _publish_compiler_cache_job(
        cache_dir,
        view_type="bm25",
        job_id=job_id,
        owner_id=owner_id,
        fencing_token=fencing_token,
        repository_source=repository_source,
        view_output_owner=bm25_output_owner,
        context_output_owner=context_output_owner,
        view_destination=bm25_destination,
        context_destination=context_destination,
        workspace_provider=workspace_provider,
        repository_key=repository_key,
        catalog=catalog,
        object_store=object_store,
        namespace_name=namespace_name,
        max_manifest_bytes=max_manifest_bytes,
        max_context_files=max_context_files,
        max_context_bytes=max_context_bytes,
        max_bundle_files=max_bundle_files,
        max_bundle_bytes=max_bundle_bytes,
        max_bundle_metadata_bytes=max_bundle_metadata_bytes,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )
    recapture = preparation.recaptures[0]
    if recapture.view_type != "bm25":  # pragma: no cover - adapter invariant
        raise AssertionError("compiler cache BM25 job selected another view")
    return CompilerCacheJobPublicationResult(
        manifest=preparation.import_plan.manifest,
        import_plan=preparation.import_plan,
        recapture=CompilerCacheBm25RecaptureResult(
            source_view=recapture.source_view,
            output_view=recapture.output_view,
            source_records=recapture.source_records,
            output_records=recapture.output_records,
            canonical_manifest_bytes=preparation.canonical_manifest_bytes,
        ),
        job=completed,
    )


def publish_compiler_cache_vector_job(
    cache_dir: str | Path,
    *,
    job_id: str,
    owner_id: str,
    fencing_token: int,
    repository_source: RepositorySourceBinding,
    vector_output_owner: PublishedWorkspaceReceiptOwner,
    context_output_owner: PublishedWorkspaceReceiptOwner,
    vector_destination: Path,
    context_destination: Path,
    workspace_provider: StrictWorkspaceProvider,
    repository_key: str,
    catalog: JobPublicationCatalog,
    object_store: RetainedImportObjectStore,
    namespace_name: str = DEFAULT_NAMESPACE_NAME,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES,
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> CompilerCacheVectorJobPublicationResult:
    """Publish one exact current vector cache into a caller-owned fenced job.

    The caller must already have registered the repository, source, and exact
    vector profile, created the job, and acquired its active ref lease. Only
    current schema-8 portable vector bytes are accepted. Native FAISS parsing,
    embedding/model loading, and native runtime authorization are outside this
    adapter; ``publish_job_artifacts`` is its sole catalog mutation path.
    """

    preparation, completed = _publish_compiler_cache_job(
        cache_dir,
        view_type="vector",
        job_id=job_id,
        owner_id=owner_id,
        fencing_token=fencing_token,
        repository_source=repository_source,
        view_output_owner=vector_output_owner,
        context_output_owner=context_output_owner,
        view_destination=vector_destination,
        context_destination=context_destination,
        workspace_provider=workspace_provider,
        repository_key=repository_key,
        catalog=catalog,
        object_store=object_store,
        namespace_name=namespace_name,
        max_manifest_bytes=max_manifest_bytes,
        max_context_files=max_context_files,
        max_context_bytes=max_context_bytes,
        max_bundle_files=max_bundle_files,
        max_bundle_bytes=max_bundle_bytes,
        max_bundle_metadata_bytes=max_bundle_metadata_bytes,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )
    recapture = preparation.recaptures[0]
    if recapture.view_type != "vector":  # pragma: no cover - adapter invariant
        raise AssertionError("compiler cache vector job selected another view")
    return CompilerCacheVectorJobPublicationResult(
        manifest=preparation.import_plan.manifest,
        import_plan=preparation.import_plan,
        recapture=recapture,
        job=completed,
    )


def compile_and_import_repo(
    compiler: IndexCompiler,
    repo_path: str | Path,
    *,
    cache_dir: str | Path,
    views: tuple[str, ...],
    repository_source: RepositorySourceBinding,
    view_output_owners: dict[str, PublishedWorkspaceReceiptOwner],
    context_output_owner: PublishedWorkspaceReceiptOwner,
    view_destinations: dict[str, Path],
    context_destination: Path,
    workspace_provider: StrictWorkspaceProvider,
    repository_key: str,
    catalog: RetainedImportCatalog,
    object_store: RetainedImportObjectStore,
    namespace_name: str = DEFAULT_NAMESPACE_NAME,
    ref_name: str = DEFAULT_REF_NAME,
    expected_generation: int = 0,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES,
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    max_projection_bytes: int = DEFAULT_MAX_PROJECTION_BYTES,
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
    cache_topology_guard: CompilerCacheTopologyGuard | None = None,
) -> CompilerRetainedPublicationResult:
    """Update-or-create selected compiler views and retain one snapshot.

    The mutable compiler update and strict immutable recapture share one cache
    lease.  The lease is released before the retained importer invokes any
    CAS or catalog data-plane method; static contract probes may run earlier.
    ``cache_topology_guard`` is a trusted application-level denial check for
    topology that needs the newly created, lease-bound cache directory.  It is
    not an authority token: ``capture`` runs before compiler mutation and
    ``verify`` runs before retained workspace mutation and again before lease
    release.

    All source and receipt authorities are borrowed.  The caller owns their
    cleanup, including immutable evidence left by a later storage failure.
    There is intentionally no force-rebuild option: this route only performs
    the compiler's ordinary update-or-create policy.
    """

    if type(compiler) is not IndexCompiler:
        raise TypeError("compiler retained publication requires an IndexCompiler")
    _preflight_cache_topology_guard(cache_topology_guard)
    repository = lexical_directory_path(Path(repo_path))
    operation = _preflight_cache_import_operation(
        cache_dir,
        views=views,
        repository_source=repository_source,
        view_output_owners=view_output_owners,
        context_output_owner=context_output_owner,
        view_destinations=view_destinations,
        context_destination=context_destination,
        workspace_provider=workspace_provider,
        repository_key=repository_key,
        catalog=catalog,
        object_store=object_store,
        namespace_name=namespace_name,
        ref_name=ref_name,
        expected_generation=expected_generation,
        max_manifest_bytes=max_manifest_bytes,
        max_context_files=max_context_files,
        max_context_bytes=max_context_bytes,
        max_bundle_files=max_bundle_files,
        max_bundle_bytes=max_bundle_bytes,
        max_bundle_metadata_bytes=max_bundle_metadata_bytes,
        max_projection_bytes=max_projection_bytes,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )

    with compiler_cache_lock(operation.cache):
        cache_identity = _cache_directory_identity(operation.cache)
        identity = repository_source.authenticated_identity_snapshot()
        if type(identity) is not RepositorySourceIdentitySnapshot:
            raise TypeError("compiler cache repository identity has an invalid type")
        if lexical_directory_path(identity.root) != repository:
            raise StorageIntegrityError(
                "compiler retained publication source belongs to another repository"
            )
        _cache_topology(operation.cache, identity)
        _run_cache_topology_guard(
            cache_topology_guard,
            "capture",
            operation.cache,
        )
        if _cache_directory_identity(operation.cache) != cache_identity:
            raise RuntimeError(
                "compiler cache directory changed during topology capture"
            )
        compiled_manifest = compiler._update_repo_locked(
            str(repository),
            index_types=list(views),
            cache=str(operation.cache),
        )
        if _cache_directory_identity(operation.cache) != cache_identity:
            raise RuntimeError("compiler cache directory changed during compilation")
        _run_cache_topology_guard(
            cache_topology_guard,
            "verify",
            operation.cache,
        )
        if _cache_directory_identity(operation.cache) != cache_identity:
            raise RuntimeError(
                "compiler cache directory changed during topology verify"
            )
        preparation = _prepare_compiler_cache_import_locked(
            operation,
            views=views,
            repository_source=repository_source,
            context_output_owner=context_output_owner,
            workspace_provider=workspace_provider,
            expected_manifest=compiled_manifest,
        )
        if _cache_directory_identity(operation.cache) != cache_identity:
            raise RuntimeError("compiler cache directory changed during recapture")
        _run_cache_topology_guard(
            cache_topology_guard,
            "verify",
            operation.cache,
        )
        if _cache_directory_identity(operation.cache) != cache_identity:
            raise RuntimeError(
                "compiler cache directory changed during topology verify"
            )

    retained_import = _commit_prepared_compiler_cache_import(
        preparation,
        operation,
        repository_source=repository_source,
        context_output_owner=context_output_owner,
        catalog=catalog,
        object_store=object_store,
    )
    return CompilerRetainedPublicationResult(
        manifest=compiled_manifest,
        retained_import=retained_import,
    )


def import_compiler_cache_bm25(
    cache_dir: str | Path,
    *,
    repository_source: RepositorySourceBinding,
    bm25_output_owner: PublishedWorkspaceReceiptOwner,
    context_output_owner: PublishedWorkspaceReceiptOwner,
    bm25_destination: Path,
    context_destination: Path,
    workspace_provider: StrictWorkspaceProvider,
    repository_key: str,
    catalog: RetainedImportCatalog,
    object_store: RetainedImportObjectStore,
    namespace_name: str = DEFAULT_NAMESPACE_NAME,
    ref_name: str = DEFAULT_REF_NAME,
    expected_generation: int = 0,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES,
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    max_projection_bytes: int = DEFAULT_MAX_PROJECTION_BYTES,
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> CompilerCacheImportResult:
    """Compatibility wrapper for one exact current BM25 cache import."""

    result = import_compiler_cache(
        cache_dir,
        views=("bm25",),
        repository_source=repository_source,
        view_output_owners={"bm25": bm25_output_owner},
        context_output_owner=context_output_owner,
        view_destinations={"bm25": bm25_destination},
        context_destination=context_destination,
        workspace_provider=workspace_provider,
        repository_key=repository_key,
        catalog=catalog,
        object_store=object_store,
        namespace_name=namespace_name,
        ref_name=ref_name,
        expected_generation=expected_generation,
        max_manifest_bytes=max_manifest_bytes,
        max_context_files=max_context_files,
        max_context_bytes=max_context_bytes,
        max_bundle_files=max_bundle_files,
        max_bundle_bytes=max_bundle_bytes,
        max_bundle_metadata_bytes=max_bundle_metadata_bytes,
        max_projection_bytes=max_projection_bytes,
        forbidden_paths=forbidden_paths,
        environ=environ,
    )
    recapture = result.recaptures[0]
    if recapture.view_type != "bm25":  # pragma: no cover - wrapper invariant
        raise AssertionError("compiler cache BM25 wrapper selected another view")
    return CompilerCacheImportResult(
        recapture=CompilerCacheBm25RecaptureResult(
            source_view=recapture.source_view,
            output_view=recapture.output_view,
            source_records=recapture.source_records,
            output_records=recapture.output_records,
            canonical_manifest_bytes=result.canonical_manifest_bytes,
        ),
        import_plan=result.import_plan,
        context_artifact=result.context_artifact,
        import_result=result.import_result,
    )


def _fingerprints_adjustment(
    records: tuple[TreeFileRecord, ...],
) -> dict[str, Any]:
    return {"artifact_file_fingerprints": _fingerprints_for_records(records)}


__all__ = [
    "CompilerCacheBm25RecaptureResult",
    "CompilerCacheImportResult",
    "CompilerCacheJobExecutor",
    "CompilerCacheJobPreparationResult",
    "CompilerCacheJobPublicationResult",
    "CompilerCacheMultiViewImportResult",
    "CompilerCacheTopologyGuard",
    "CompilerCacheVectorJobPublicationResult",
    "CompilerCacheViewRecaptureResult",
    "CompilerRetainedPublicationResult",
    "compile_and_import_repo",
    "import_compiler_cache",
    "import_compiler_cache_bm25",
    "prepare_compiler_cache_job_view",
    "prepare_compiler_cache_job_view_from_generation",
    "publish_compiler_cache_bm25_job",
    "publish_compiler_cache_vector_job",
]
