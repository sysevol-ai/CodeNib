# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Materialize retained manifest exports as strict context artifacts.

The exporter result accepted here is data, not authority.  This module first
detaches and revalidates that data, then authenticates its retained projection
and every selected bundle inside one object-retention callback.  The same
callback covers member replay, strict missing-only workspace publication, both
staged and published tree validation, and the final exact-receipt sweep.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, BinaryIO, TypeVar

from .._atomic_directory import (
    PublicationDirectoryReader,
    TreeFileRecord,
    _annotate_secondary_error,
    _OrderedAction,
    _run_context_with_cleanup_actions,
    directory_ownership_file_records,
    directory_ownership_inventory,
    lexical_directory_path,
)
from .._captured_directory import (
    PublishedWorkspaceReceiptOwner,
    WorkspaceDirectory,
    WorkspaceFile,
    WorkspacePlan,
    require_owned_workspace_publication_support,
)
from .._owned_file_publication import _CancellationSafeRLock
from .._version import package_version
from .._workspace_provider import (
    StrictWorkspaceProvider,
    StrictWorkspaceRequest,
    StrictWorkspaceSession,
    run_strict_workspace,
)
from ..artifacts.context import (
    CONTEXT_ARTIFACT_MANIFEST,
    CONTEXT_ARTIFACT_SCHEMA,
    ContextArtifactResult,
)
from ..artifacts.runtime import verify_context_artifact_reader
from ..artifacts.security import assert_publishable_tree_reader
from ..repository_source_selection import RepositorySourceSelection
from ..source_fingerprint import is_secure_source_fingerprint_v2
from ..storage.cas import BlobInfo
from ..storage.models import (
    ArtifactMember,
    NamespaceIdentity,
    ObjectRecord,
    PublishedSnapshot,
    RepositoryIdentity,
    SnapshotView,
    SourceRevision,
    StorageIntegrityError,
    StorageValidationError,
    ViewGeneration,
    ViewProfile,
    canonical_json,
)
from ..storage.protocols import ReceiptRetainingObjectStore, RetainedSnapshotCatalog
from ..storage.view_bundle import (
    DEFAULT_MAX_BUNDLE_BYTES,
    DEFAULT_MAX_BUNDLE_FILES,
    DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    VIEW_BUNDLE_MEDIA_TYPE,
    VIEW_BUNDLE_SCHEMA,
    ViewBundleRecord,
    consume_verified_view_bundle_stream,
    validate_view_bundle_physical_size,
)
from .manifest import MANIFEST_FILENAME, MANIFEST_VERSION, SUPPORTED_MANIFEST_VERSIONS
from .manifest_export import (
    RepoManifestExportReceipt,
    RepoManifestExportResult,
    RepoManifestViewExportReceipt,
    _artifact_member,
    _cross_bind_generation_record,
    _portable_export_manifest_bytes,
    _require_bundle_semantics,
    _run_with_owned_close,
    _strict_json_bytes,
    export_retained_repo_manifest_ref,
    export_retained_repo_manifest_snapshot,
)
from .manifest_import import (
    DEFAULT_MAX_CONTEXT_BYTES,
    DEFAULT_MAX_CONTEXT_FILES,
    DEFAULT_MAX_PROJECTION_BYTES,
    DEFAULT_NAMESPACE_NAME,
    DEFAULT_REF_NAME,
)
from .manifest_storage import (
    DEFAULT_MAX_MANIFEST_BYTES,
    PORTABLE_STORAGE_VIEWS,
    RepoManifestImportPlan,
    ViewImportIntent,
    plan_repo_manifest_import_bytes,
)
from .retained_manifest_contract import (
    REPO_MANIFEST_PROJECTION_MEDIA_TYPE,
    REPO_MANIFEST_PROJECTION_SCHEMA,
    REPO_MANIFEST_PROJECTION_SNAPSHOT_REACHABILITY,
    repo_manifest_generation_metadata,
    repo_manifest_projection_profile,
)
from .retained_snapshot import same_exact_json
from .snapshot_store import normalize_repo

_Result = TypeVar("_Result")

_INT64_MAX = 9_223_372_036_854_775_807
_READ_CHUNK_BYTES = 1024 * 1024
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_ENVIRONMENT_ITEMS = 4_096
_MAX_TEXT = 32_768
_PLAN_DOMAIN = "codenib-retained-context-materialization-v1"
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_REPOSITORY_RE = re.compile(
    r"[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*\Z",
    re.ASCII,
)
_MISSING = object()
_UNSET_RESULT = object()
_CALLBACK_RECOVERY_LIMIT = 64


@dataclass(frozen=True, slots=True)
class RepoManifestMaterializationResult:
    """Data result for one retained export plus filesystem materialization."""

    artifact: ContextArtifactResult
    export_receipt: RepoManifestExportReceipt

    def __post_init__(self) -> None:
        if type(self) is not RepoManifestMaterializationResult:
            raise StorageValidationError(
                "retained materialization result must use the exact model type"
            )
        if type(self.artifact) is not ContextArtifactResult:
            raise StorageValidationError(
                "retained materialization result artifact is invalid"
            )
        if type(self.export_receipt) is not RepoManifestExportReceipt:
            raise StorageValidationError(
                "retained materialization result export receipt is invalid"
            )
        if (
            self.artifact.repository != self.export_receipt.repository_key
            or self.artifact.views != self.export_receipt.views
        ):
            raise StorageIntegrityError(
                "retained materialization result identity is inconsistent"
            )


@dataclass(frozen=True, slots=True)
class _MaterializationPreflight:
    destination: Path
    environment: Mapping[str, str]
    max_manifest_bytes: int
    max_projection_bytes: int
    max_context_files: int
    max_context_bytes: int
    max_bundle_files: int
    max_bundle_bytes: int
    max_bundle_metadata_bytes: int


@dataclass(frozen=True, slots=True)
class _SemanticView:
    """Minimal shape consumed by the exporter's source-free semantic gate."""

    intent: ViewImportIntent
    members: tuple[ArtifactMember, ...]


@dataclass(frozen=True, slots=True)
class _RetainedViewPlan:
    name: str
    generation_id: str
    bundle_receipt: BlobInfo
    member_receipt_items: tuple[tuple[str, BlobInfo], ...]
    members: tuple[ArtifactMember, ...]
    output_records: tuple[TreeFileRecord, ...]


@dataclass(frozen=True, slots=True)
class _RetainedContextPlan:
    workspace: WorkspacePlan
    repository: str
    commit: str
    manifest_version: str
    source_fingerprint: str
    views: tuple[str, ...]
    view_plans: tuple[_RetainedViewPlan, ...]
    output_records: tuple[TreeFileRecord, ...]
    manifest_payload: bytes
    metadata_payload: bytes
    streaming_json_paths: tuple[str, ...]
    file_count: int
    byte_count: int


@dataclass(frozen=True, slots=True)
class _ProjectionViewAuthority:
    receipt: RepoManifestViewExportReceipt
    intent: ViewImportIntent
    members: tuple[ArtifactMember, ...]
    generation: ViewGeneration


@dataclass(frozen=True, slots=True)
class _AuthenticatedProjection:
    plan: RepoManifestImportPlan
    source: SourceRevision
    projection_generation: ViewGeneration
    view_items: tuple[_ProjectionViewAuthority, ...]


class _RetentionCallbackGate:
    """One-shot retained callback revoked synchronously before API return."""

    __slots__ = (
        "_active",
        "_called",
        "_lock",
        "_misused",
        "_operation",
        "_outcome",
        "_owner_pid",
    )

    def __init__(self, operation: Callable[[], _Result]) -> None:
        self._active = True
        self._called = False
        self._lock = _CancellationSafeRLock()
        self._misused = False
        self._operation = operation
        self._outcome: tuple[object, BaseException | None] | object = _UNSET_RESULT
        self._owner_pid = os.getpid()

    def _require_owner_pid(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError("object retention callback cannot cross a PID boundary")

    def __call__(self) -> _Result:
        self._require_owner_pid()

        def invoke() -> _Result:
            if not self._active:
                self._misused = True
                raise RuntimeError("object retention callback is no longer active")
            if self._called:
                self._misused = True
                raise RuntimeError("object retention callback is one-shot")
            self._called = True
            try:
                result = self._operation()
                self._record_outcome(result, None)
                return result
            except BaseException as operation_error:  # noqa: B036
                self._record_outcome(_UNSET_RESULT, operation_error)
                raise

        return self._lock.run(invoke)

    def _record_outcome(
        self,
        result: object,
        operation_error: BaseException | None,
    ) -> None:
        primary_error = operation_error
        outcome = (result, operation_error)
        for _attempt in range(_CALLBACK_RECOVERY_LIMIT):
            try:
                self._outcome = outcome
                if self._outcome is not outcome:
                    raise RuntimeError(
                        "object retention callback outcome changed during commit"
                    )
            except BaseException as transition_error:  # noqa: B036
                if primary_error is None:
                    primary_error = transition_error
                    outcome = (_UNSET_RESULT, primary_error)
                elif transition_error is not primary_error:
                    try:
                        _annotate_secondary_error(
                            primary_error,
                            "object retention callback outcome commit also failed",
                            transition_error,
                        )
                    except BaseException:  # noqa: B036 - diagnostic only
                        pass
                continue
            if primary_error is not None:
                raise primary_error.with_traceback(primary_error.__traceback__)
            return
        recovery_error = RuntimeError(
            "object retention callback outcome commit did not converge"
        )
        if primary_error is not None:
            try:
                _annotate_secondary_error(
                    primary_error,
                    "object retention callback outcome recovery also failed",
                    recovery_error,
                )
            except BaseException:  # noqa: B036 - diagnostic only
                pass
            raise primary_error.with_traceback(primary_error.__traceback__)
        raise recovery_error

    def _deactivate(self) -> None:
        self._active = False

    def revoke(self) -> None:
        self._require_owner_pid()
        deferred: BaseException | None = None
        for _attempt in range(_CALLBACK_RECOVERY_LIMIT):
            try:
                self._lock.run(self._deactivate)
                inactive = self._lock.run(lambda: not self._active)
            except BaseException as interruption:  # noqa: B036
                if deferred is None:
                    deferred = interruption
                elif interruption is not deferred:
                    try:
                        _annotate_secondary_error(
                            deferred,
                            "object retention callback revocation also failed",
                            interruption,
                        )
                    except BaseException:  # noqa: B036 - diagnostic only
                        pass
                continue
            if inactive:
                if deferred is not None:
                    raise deferred.with_traceback(deferred.__traceback__)
                return
        recovery_error = RuntimeError(
            "object retention callback revocation did not converge"
        )
        if deferred is not None:
            raise recovery_error from deferred
        raise recovery_error

    @property
    def outcome(self) -> tuple[bool, object, BaseException | None, bool]:
        self._require_owner_pid()

        def read() -> tuple[bool, object, BaseException | None, bool]:
            outcome = self._outcome
            if outcome is _UNSET_RESULT:
                return False, _UNSET_RESULT, None, self._misused
            result, operation_error = outcome  # type: ignore[misc]
            return True, result, operation_error, self._misused

        return self._lock.run(read)

    @property
    def inactive(self) -> bool:
        self._require_owner_pid()
        return self._lock.run(lambda: not self._active)


class _RetentionCallbackRevocationOwner:
    """Expose only callback revocation, never the callable authority itself."""

    __slots__ = ("_gate",)

    def __init__(self, gate: _RetentionCallbackGate) -> None:
        self._gate = gate

    @property
    def closed(self) -> bool:
        """Expose revocation completion to the ordered-cleanup boundary."""

        return self._gate.inactive

    def close(self) -> None:
        """Retry synchronous revocation through an attached cleanup owner."""

        self._gate.revoke()


def _run_retention_callback(
    object_store: ReceiptRetainingObjectStore,
    receipts: tuple[BlobInfo, ...],
    callback: Callable[[], _Result],
) -> _Result:
    gate = _RetentionCallbackGate(callback)
    revoke_owner = _RetentionCallbackRevocationOwner(gate)
    revoke_action = _OrderedAction(
        label="object retention callback revocation also failed",
        action=revoke_owner.close,
        complete=lambda: revoke_owner.closed,
        retry_incomplete="cancellation",
        incomplete_owner=revoke_owner,
    )
    backend_result: object = _UNSET_RESULT
    primary_error: BaseException | None = None
    try:
        with _run_context_with_cleanup_actions((revoke_action,)):
            try:
                backend_result = object_store.retain_receipts(receipts, gate)
            except BaseException as error:  # noqa: B036 - revoke before propagation
                primary_error = error
            revoke_owner.close()
    except BaseException as revoke_error:  # noqa: B036 - adjudicate after revocation
        if primary_error is None:
            primary_error = revoke_error
        elif revoke_error is not primary_error:
            try:
                _annotate_secondary_error(
                    primary_error,
                    "object retention callback revocation also failed",
                    revoke_error,
                )
            except BaseException:  # noqa: B036 - diagnostic only
                pass

    outcome_read_error: BaseException | None = None
    for _attempt in range(_CALLBACK_RECOVERY_LIMIT):
        try:
            callback_completed, callback_result, callback_error, misused = gate.outcome
        except BaseException as read_error:  # noqa: B036
            if outcome_read_error is None:
                outcome_read_error = read_error
            elif read_error is not outcome_read_error:
                try:
                    _annotate_secondary_error(
                        outcome_read_error,
                        "object retention callback outcome read failed again",
                        read_error,
                    )
                except BaseException:  # noqa: B036 - diagnostic only
                    pass
            continue
        break
    else:
        recovery_error = RuntimeError(
            "object retention callback outcome read did not converge"
        )
        if primary_error is not None:
            try:
                _annotate_secondary_error(
                    primary_error,
                    "object retention callback outcome recovery also failed",
                    outcome_read_error or recovery_error,
                )
            except BaseException:  # noqa: B036 - diagnostic only
                pass
            raise primary_error.with_traceback(primary_error.__traceback__)
        if outcome_read_error is not None:
            raise outcome_read_error.with_traceback(outcome_read_error.__traceback__)
        raise recovery_error

    if callback_error is not None:
        if primary_error is not None and primary_error is not callback_error:
            try:
                _annotate_secondary_error(
                    callback_error,
                    "object store also failed after its retention callback",
                    primary_error,
                )
            except BaseException:  # noqa: B036 - diagnostic only
                pass
        raise callback_error.with_traceback(callback_error.__traceback__)
    if primary_error is not None:
        raise primary_error.with_traceback(primary_error.__traceback__)
    if outcome_read_error is not None:
        raise outcome_read_error.with_traceback(outcome_read_error.__traceback__)
    if misused:
        raise RuntimeError("object store misused the one-shot retention callback")
    if not callback_completed:
        raise RuntimeError("object store did not invoke its retention callback")
    if backend_result is _UNSET_RESULT:
        raise RuntimeError("object store returned no retention callback result")
    if backend_result is not callback_result:
        raise RuntimeError("object store substituted the retention callback result")
    return callback_result  # type: ignore[return-value]


def _positive_limit(value: object, label: str) -> int:
    if type(value) is not int or not 0 < value <= (64 * 1024 * 1024 * 1024):
        raise StorageValidationError(
            f"{label} must be a positive integer no larger than 64 GiB"
        )
    return value


def _require_static_methods(value: object, *, label: str, names: Sequence[str]) -> None:
    if value is None:
        raise TypeError(f"{label} is required")
    for name in names:
        candidate = inspect.getattr_static(value, name, _MISSING)
        if isinstance(candidate, (classmethod, staticmethod)):
            candidate = candidate.__func__
        if candidate is _MISSING or not callable(candidate):
            raise TypeError(f"{label} must provide {name}()")


def _snapshot_environment(value: Mapping[str, str] | None) -> Mapping[str, str]:
    source = os.environ if value is None else value
    if not isinstance(source, Mapping):
        raise TypeError("retained materialization environment must be a mapping")
    try:
        iterator = iter(source.items())
        snapshot: dict[str, str] = {}
        for index in range(_MAX_ENVIRONMENT_ITEMS + 1):
            try:
                item = next(iterator)
            except StopIteration:
                break
            if index == _MAX_ENVIRONMENT_ITEMS:
                raise StorageValidationError(
                    "retained materialization environment is too large"
                )
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
            ):
                raise StorageValidationError(
                    "retained materialization environment must contain exact "
                    "text pairs"
                )
            key, item_value = item
            if (
                not key
                or len(key) > 4_096
                or "\x00" in key
                or key in snapshot
                or len(item_value) > _MAX_TEXT
                or "\x00" in item_value
            ):
                raise StorageValidationError(
                    "retained materialization environment is not canonical"
                )
            snapshot[key] = item_value
    except StorageValidationError:
        raise
    except Exception as exc:
        raise StorageValidationError(
            "retained materialization environment could not be snapshotted"
        ) from exc
    return MappingProxyType(snapshot)


def _snapshot_blob(value: object, *, label: str) -> BlobInfo:
    if type(value) is not BlobInfo:
        raise StorageIntegrityError(f"{label} must be an exact BlobInfo")
    digest = value.digest
    byte_size = value.byte_size
    storage_key = value.storage_key
    if (
        type(digest) is not str
        or _DIGEST_RE.fullmatch(digest) is None
        or type(byte_size) is not int
        or not 0 <= byte_size <= _INT64_MAX
        or type(storage_key) is not str
        or not storage_key
        or storage_key != storage_key.strip()
        or "\x00" in storage_key
    ):
        raise StorageIntegrityError(f"{label} is not canonical")
    return BlobInfo(digest=digest, byte_size=byte_size, storage_key=storage_key)


def _snapshot_exported(
    exported: RepoManifestExportResult,
    *,
    max_manifest_bytes: int,
    max_bundle_files: int,
    max_context_files: int,
    max_context_bytes: int,
) -> RepoManifestExportResult:
    """Detach all forgeable frozen dataclasses before acquiring authority."""

    payload = exported.canonical_manifest_bytes
    source_receipt = exported.receipt
    if type(payload) is not bytes:
        raise StorageValidationError("exported manifest bytes must be exact bytes")
    if len(payload) > max_manifest_bytes:
        raise StorageValidationError(
            f"exported repository manifest exceeds {max_manifest_bytes} bytes"
        )
    if len(payload) > max_context_bytes:
        raise StorageValidationError(
            f"retained context exceeds {max_context_bytes} inventoried bytes"
        )
    if type(source_receipt) is not RepoManifestExportReceipt:
        raise StorageValidationError("exported manifest receipt is invalid")

    source_views = source_receipt.view_receipts
    source_skipped = source_receipt.skipped_items
    if type(source_views) is not tuple or not source_views:
        raise StorageValidationError("exported view receipts are invalid")
    if len(source_views) > len(PORTABLE_STORAGE_VIEWS):
        raise StorageValidationError("exported view receipt count is out of bounds")
    if type(source_skipped) is not tuple or len(source_skipped) > len(
        PORTABLE_STORAGE_VIEWS
    ):
        raise StorageValidationError("export skipped decisions are out of bounds")

    detached_views: list[RepoManifestViewExportReceipt] = []
    member_count = 0
    inventoried_byte_count = len(payload)
    for source_view in source_views:
        if type(source_view) is not RepoManifestViewExportReceipt:
            raise StorageValidationError("exported view receipt is invalid")
        source_members = source_view.member_receipt_items
        if type(source_members) is not tuple or not source_members:
            raise StorageValidationError("exported view member receipts are invalid")
        if len(source_members) > max_bundle_files:
            raise StorageValidationError(
                f"exported view exceeds {max_bundle_files} member receipts"
            )
        member_count += len(source_members)
        if member_count + 1 > max_context_files:
            raise StorageValidationError(
                f"exported context exceeds {max_context_files} inventoried files"
            )
        detached_members: list[tuple[str, BlobInfo]] = []
        for item in source_members:
            if type(item) is not tuple or len(item) != 2 or type(item[0]) is not str:
                raise StorageValidationError(
                    "exported view member receipt entry is invalid"
                )
            member_receipt = _snapshot_blob(
                item[1],
                label="exported view member receipt",
            )
            if member_receipt.byte_size > (max_context_bytes - inventoried_byte_count):
                raise StorageValidationError(
                    f"retained context exceeds {max_context_bytes} inventoried bytes"
                )
            inventoried_byte_count += member_receipt.byte_size
            detached_members.append((item[0], member_receipt))
        detached_views.append(
            RepoManifestViewExportReceipt(
                view_type=source_view.view_type,
                view_generation_id=source_view.view_generation_id,
                bundle_receipt=_snapshot_blob(
                    source_view.bundle_receipt,
                    label="exported view bundle receipt",
                ),
                member_receipt_items=tuple(detached_members),
            )
        )

    detached_skipped: list[tuple[str, str]] = []
    for item in source_skipped:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
        ):
            raise StorageValidationError("export skipped decision is invalid")
        detached_skipped.append((item[0], item[1]))

    receipt = RepoManifestExportReceipt(
        namespace_id=source_receipt.namespace_id,
        repository_id=source_receipt.repository_id,
        repository_key=source_receipt.repository_key,
        source_revision_id=source_receipt.source_revision_id,
        snapshot_id=source_receipt.snapshot_id,
        snapshot_published_at=source_receipt.snapshot_published_at,
        ref_name=source_receipt.ref_name,
        ref_generation=source_receipt.ref_generation,
        ref_updated_at=source_receipt.ref_updated_at,
        manifest_digest=source_receipt.manifest_digest,
        manifest_byte_size=source_receipt.manifest_byte_size,
        projection_generation_id=source_receipt.projection_generation_id,
        projection_receipt=_snapshot_blob(
            source_receipt.projection_receipt,
            label="export projection receipt",
        ),
        view_receipts=tuple(detached_views),
        skipped_items=tuple(detached_skipped),
    )
    return RepoManifestExportResult(payload, receipt)


def _unique_receipts(exported: RepoManifestExportResult) -> tuple[BlobInfo, ...]:
    receipts = [exported.receipt.projection_receipt]
    for view in exported.receipt.view_receipts:
        receipts.append(view.bundle_receipt)
        receipts.extend(receipt for _path, receipt in view.member_receipt_items)
    by_digest: dict[str, BlobInfo] = {}
    for receipt in receipts:
        previous = by_digest.get(receipt.digest)
        if previous is None:
            by_digest[receipt.digest] = receipt
        elif previous != receipt:
            raise StorageIntegrityError(
                "one retained object digest has conflicting receipt metadata"
            )
    return tuple(by_digest[digest] for digest in sorted(by_digest))


def _same_receipt(left: BlobInfo, right: BlobInfo) -> bool:
    return (
        left.digest == right.digest
        and left.byte_size == right.byte_size
        and left.storage_key == right.storage_key
    )


def _verify_receipt(
    object_store: ReceiptRetainingObjectStore,
    expected: BlobInfo,
) -> None:
    observed = object_store.verify_receipt(expected)
    observed = _snapshot_blob(observed, label="object-store receipt verification")
    if not _same_receipt(observed, expected):
        raise StorageIntegrityError("object-store receipt identity changed")


def _run_open_receipt(
    object_store: ReceiptRetainingObjectStore,
    receipt: BlobInfo,
    operation: Callable[[BinaryIO], _Result],
) -> _Result:
    def acquire() -> BinaryIO:
        try:
            return object_store.open(receipt.digest)
        except StorageIntegrityError:
            raise
        except BaseException as error:  # noqa: B036 - backend boundary
            if isinstance(error, (GeneratorExit, KeyboardInterrupt, SystemExit)):
                raise
            raise StorageIntegrityError(
                "object-store read could not be opened"
            ) from error

    return _run_with_owned_close(
        acquire,
        operation,
        complete=lambda source: source.closed is True,
        cleanup_label="object-store reader close also failed",
        close_failure="object-store reader close failed",
    )


def _iter_receipt_bytes(source: BinaryIO, receipt: BlobInfo) -> Iterator[bytes]:
    try:
        read = source.read
    except Exception as exc:
        raise StorageIntegrityError("object-store reader is invalid") from exc
    if not callable(read):
        raise StorageIntegrityError("object-store reader is invalid")
    observed = hashlib.sha256()
    remaining = receipt.byte_size
    while remaining:
        try:
            block = read(min(_READ_CHUNK_BYTES, remaining))
        except BaseException as error:  # noqa: B036 - backend boundary
            if isinstance(error, (GeneratorExit, KeyboardInterrupt, SystemExit)):
                raise
            raise StorageIntegrityError("object-store read failed") from error
        if type(block) is not bytes or not block:
            raise StorageIntegrityError("object-store read was truncated")
        if len(block) > remaining:
            raise StorageIntegrityError("object-store read exceeded its receipt")
        observed.update(block)
        remaining -= len(block)
        yield block
    try:
        extra = read(1)
    except BaseException as error:  # noqa: B036 - backend boundary
        if isinstance(error, (GeneratorExit, KeyboardInterrupt, SystemExit)):
            raise
        raise StorageIntegrityError("object-store read failed") from error
    if type(extra) is not bytes or extra:
        raise StorageIntegrityError("object-store read exceeded its receipt")
    if observed.hexdigest() != receipt.digest:
        raise StorageIntegrityError("object-store bytes do not match their receipt")


def _read_receipt_bytes(source: BinaryIO, receipt: BlobInfo) -> bytes:
    return b"".join(_iter_receipt_bytes(source, receipt))


def _retained_generation(
    source: SourceRevision,
    *,
    receipt: BlobInfo,
    media_type: str,
    profile: ViewProfile,
    schema_version: str,
    metadata: Mapping[str, Any],
    member_digests: Sequence[str],
) -> ViewGeneration:
    try:
        record = ObjectRecord(
            digest=receipt.digest,
            byte_size=receipt.byte_size,
            storage_key=receipt.storage_key,
            media_type=media_type,
        )
        return ViewGeneration.create(
            source,
            profile,
            record,
            schema_version=schema_version,
            metadata=metadata,
            member_object_digests=member_digests,
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError(
            "retained projection generation identity is invalid"
        ) from exc


def _intent_generation_metadata(intent: ViewImportIntent) -> dict[str, Any]:
    """Keep retained generation identity bound to the embedded manifest."""

    metadata = repo_manifest_generation_metadata(intent)
    metadata["manifest_version"] = intent.profile.config["manifest_version"]
    return metadata


def _authenticate_projection(
    object_store: ReceiptRetainingObjectStore,
    exported: RepoManifestExportResult,
    emitted: RepoManifestImportPlan,
    *,
    max_manifest_bytes: int,
    max_projection_bytes: int,
    max_bundle_files: int,
) -> _AuthenticatedProjection:
    """Authenticate the retained v2 projection behind a data-only export."""

    receipt = exported.receipt
    projection_receipt = receipt.projection_receipt
    if projection_receipt.byte_size > max_projection_bytes:
        raise StorageIntegrityError(
            f"repository manifest projection exceeds {max_projection_bytes} bytes"
        )
    payload = _run_open_receipt(
        object_store,
        projection_receipt,
        lambda source: _read_receipt_bytes(source, projection_receipt),
    )
    projection = _strict_json_bytes(
        payload,
        label="repository manifest projection",
    )
    if (
        set(projection)
        != {
            "schema",
            "repository_id",
            "source",
            "artifact",
            "plan",
            "selection",
            "manifest",
            "views",
        }
        or projection["schema"] != REPO_MANIFEST_PROJECTION_SCHEMA
        or projection["repository_id"] != receipt.repository_id
    ):
        raise StorageIntegrityError("repository manifest projection identity conflicts")

    manifest_value = projection["manifest"]
    if (
        type(manifest_value) is not dict
        or type(manifest_value.get("version")) is not str
        or manifest_value["version"] not in SUPPORTED_MANIFEST_VERSIONS
    ):
        raise StorageIntegrityError("projected repository manifest is invalid")
    embedded_version = manifest_value["version"]

    projected_source = projection["source"]
    expected_source_fields = {
        "source_revision_id",
        "kind",
        "source_fingerprint",
        "file_count",
        "display_commit",
        "verification_scope",
        "source_verified",
        "commit_verified",
        "checkout_state",
    }
    if embedded_version == MANIFEST_VERSION:
        expected_source_fields |= {
            "source_selection",
            "source_selection_digest",
        }
    if (
        type(projected_source) is not dict
        or set(projected_source) != expected_source_fields
    ):
        raise StorageIntegrityError("projected source shape is invalid")
    source_fingerprint = projected_source["source_fingerprint"]
    if (
        projected_source["source_revision_id"] != receipt.source_revision_id
        or projected_source["kind"] != "dirty"
        or not is_secure_source_fingerprint_v2(source_fingerprint)
        or type(projected_source["file_count"]) is not int
        or projected_source["file_count"] < 0
        or type(projected_source["display_commit"]) is not str
        or projected_source["verification_scope"] != "content-bytes"
        or projected_source["source_verified"] is not True
        or projected_source["commit_verified"] is not False
        or projected_source["checkout_state"] != "not-attested"
    ):
        raise StorageIntegrityError("projected source authority claims are invalid")
    try:
        source = SourceRevision.dirty(
            receipt.repository_id,
            source_fingerprint=source_fingerprint,
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError("projected source identity is invalid") from exc
    if source.source_revision_id != receipt.source_revision_id:
        raise StorageIntegrityError("projected source identity conflicts")

    artifact = projection["artifact"]
    if (
        type(artifact) is not dict
        or set(artifact) != {"workspace_plan_digest", "postflight"}
        or type(artifact["workspace_plan_digest"]) is not str
        or _DIGEST_RE.fullmatch(artifact["workspace_plan_digest"]) is None
        or artifact["postflight"] != "completed-before-catalog"
    ):
        raise StorageIntegrityError("projected artifact postflight is invalid")

    selection = projection["selection"]
    if type(selection) is not dict or set(selection) != {
        "required_views",
        "optional_views",
        "selected_views",
        "skipped_views",
    }:
        raise StorageIntegrityError("projected view selection shape is invalid")
    for field in ("required_views", "optional_views", "selected_views"):
        values = selection[field]
        if (
            type(values) is not list
            or any(type(item) is not str for item in values)
            or tuple(values) != tuple(sorted(set(values)))
            or any(item not in PORTABLE_STORAGE_VIEWS for item in values)
        ):
            raise StorageIntegrityError(f"projected {field} is invalid")
    if type(selection["skipped_views"]) is not dict:
        raise StorageIntegrityError("projected skipped views are invalid")

    try:
        projected_manifest = canonical_json(manifest_value).encode("ascii")
    except StorageValidationError as exc:
        raise StorageIntegrityError("projected repository manifest is invalid") from exc
    if len(projected_manifest) > max_manifest_bytes:
        raise StorageIntegrityError(
            f"projected repository manifest exceeds {max_manifest_bytes} bytes"
        )
    try:
        plan = plan_repo_manifest_import_bytes(
            projected_manifest,
            required_views=tuple(selection["required_views"]),
            optional_views=tuple(selection["optional_views"]),
            max_manifest_bytes=max_manifest_bytes,
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError(
            "projected repository manifest cannot be replanned"
        ) from exc
    expected_plan = {**plan.to_dict(), "digest": plan.plan_digest}
    if (
        not same_exact_json(projection["plan"], expected_plan)
        or not same_exact_json(selection, plan.selection.to_dict())
        or plan.source.fingerprint != source.source_fingerprint
        or plan.source.file_count != projected_source["file_count"]
        or plan.source.display_commit != projected_source["display_commit"]
        or plan.source.fingerprint_version != 2
        or plan.manifest.repo_path != "source"
    ):
        raise StorageIntegrityError("projected import plan identity conflicts")
    source_selection = plan.source.source_selection
    if embedded_version == MANIFEST_VERSION:
        if (
            source_selection is None
            or not same_exact_json(
                projected_source["source_selection"],
                source_selection.to_dict(),
            )
            or projected_source["source_selection_digest"] != source_selection.digest
        ):
            raise StorageIntegrityError("projected source selection identity conflicts")
    elif source_selection is not None:  # pragma: no cover - planner invariant
        raise StorageIntegrityError("legacy projected source has a selection")
    selected = plan.selection.selected_views
    if (
        selected != receipt.views
        or plan.selection.skipped_items != receipt.skipped_items
        or _portable_export_manifest_bytes(
            plan,
            selected=selected,
            max_manifest_bytes=max_manifest_bytes,
        )
        != exported.canonical_manifest_bytes
        or emitted.canonical_manifest_bytes != exported.canonical_manifest_bytes
        or emitted.selection.selected_views != selected
    ):
        raise StorageIntegrityError(
            "exported repository manifest conflicts with its retained projection"
        )

    projected_views = projection["views"]
    if type(projected_views) is not dict or set(projected_views) != set(selected):
        raise StorageIntegrityError("projected view closure is invalid")
    receipt_views = {view.view_type: view for view in receipt.view_receipts}
    view_authorities: list[_ProjectionViewAuthority] = []
    view_generations: list[ViewGeneration] = []
    dependency_digests: set[str] = set()
    for intent in plan.views:
        view_type = intent.view_type
        view_receipt = receipt_views.get(view_type)
        value = projected_views.get(view_type)
        if (
            view_receipt is None
            or type(value) is not dict
            or set(value) != {"generation", "generation_record", "members"}
        ):
            raise StorageIntegrityError(
                f"projected {view_type!r} view shape is invalid"
            )
        raw_members = value["members"]
        if (
            type(raw_members) is not list
            or not raw_members
            or len(raw_members) > max_bundle_files
            or len(raw_members) != len(view_receipt.member_receipt_items)
        ):
            raise StorageIntegrityError(
                f"projected {view_type!r} member inventory is invalid"
            )
        members: list[ArtifactMember] = []
        previous = ""
        for raw_member, (expected_path, expected_receipt) in zip(
            raw_members,
            view_receipt.member_receipt_items,
            strict=True,
        ):
            member = _artifact_member(raw_member, view_type=view_type)
            if (
                member.path <= previous
                or member.path != expected_path
                or member.digest != expected_receipt.digest
                or member.byte_size != expected_receipt.byte_size
            ):
                raise StorageIntegrityError(
                    f"projected {view_type!r} member differs from its receipt"
                )
            members.append(member)
            previous = member.path
        member_tuple = tuple(members)
        _cross_bind_generation_record(intent, member_tuple)
        member_digests = tuple(sorted({member.digest for member in member_tuple}))
        generation = _retained_generation(
            source,
            receipt=view_receipt.bundle_receipt,
            media_type=VIEW_BUNDLE_MEDIA_TYPE,
            profile=intent.profile,
            schema_version=VIEW_BUNDLE_SCHEMA,
            metadata=_intent_generation_metadata(intent),
            member_digests=member_digests,
        )
        generation_value = value["generation"]
        expected_generation = {
            "view_generation_id": generation.view_generation_id,
            "schema_version": generation.schema_version,
            "metadata": json.loads(generation.metadata_json),
            "profile": {
                "profile_id": generation.profile.profile_id,
                "name": generation.profile.name,
                "config": generation.profile.config,
            },
            "object": {
                "digest": view_receipt.bundle_receipt.digest,
                "byte_size": view_receipt.bundle_receipt.byte_size,
                "media_type": VIEW_BUNDLE_MEDIA_TYPE,
            },
        }
        if (
            view_receipt.view_generation_id != generation.view_generation_id
            or type(generation_value) is not dict
            or set(generation_value) != set(expected_generation)
            or not same_exact_json(generation_value, expected_generation)
            or not same_exact_json(value["generation_record"], intent.generation_record)
        ):
            raise StorageIntegrityError(
                f"projected {view_type!r} generation identity conflicts"
            )
        view_authorities.append(
            _ProjectionViewAuthority(
                receipt=view_receipt,
                intent=intent,
                members=member_tuple,
                generation=generation,
            )
        )
        view_generations.append(generation)
        dependency_digests.add(view_receipt.bundle_receipt.digest)
        dependency_digests.update(member_digests)

    projection_metadata = {
        "contract": REPO_MANIFEST_PROJECTION_SCHEMA,
        "manifest_version": plan.manifest.version,
        "manifest_digest": plan.manifest_digest,
        "plan_digest": plan.plan_digest,
        "projected_views": list(selected),
    }
    # The data-only export carries both content IDs but not the catalog's
    # reachability metadata.  Reconstruct the two contract-sanctioned forms and
    # require those IDs to authenticate exactly one of them.
    candidates: list[tuple[ViewGeneration, str]] = []
    for candidate_metadata, candidate_dependencies in (
        (projection_metadata, tuple(sorted(dependency_digests))),
        (
            {
                **projection_metadata,
                "reachability": REPO_MANIFEST_PROJECTION_SNAPSHOT_REACHABILITY,
            },
            (),
        ),
    ):
        candidate = _retained_generation(
            source,
            receipt=projection_receipt,
            media_type=REPO_MANIFEST_PROJECTION_MEDIA_TYPE,
            profile=repo_manifest_projection_profile(),
            schema_version=REPO_MANIFEST_PROJECTION_SCHEMA,
            metadata=candidate_metadata,
            member_digests=candidate_dependencies,
        )
        try:
            snapshot = PublishedSnapshot(
                repository_id=receipt.repository_id,
                source_revision_id=receipt.source_revision_id,
                views=tuple(
                    SnapshotView(generation)
                    for generation in (*view_generations, candidate)
                ),
            )
        except StorageValidationError as exc:
            raise StorageIntegrityError(
                "retained projection snapshot identity is invalid"
            ) from exc
        candidates.append((candidate, snapshot.snapshot_id))
    matches = tuple(
        candidate
        for candidate, snapshot_id in candidates
        if candidate.view_generation_id == receipt.projection_generation_id
        and snapshot_id == receipt.snapshot_id
    )
    if len(matches) != 1:
        raise StorageIntegrityError(
            "retained projection generation or snapshot identity conflicts"
        )
    projection_generation = matches[0]
    return _AuthenticatedProjection(
        plan=plan,
        source=source,
        projection_generation=projection_generation,
        view_items=tuple(view_authorities),
    )


def _canonical_json_bytes(value: Mapping[str, Any], *, label: str) -> bytes:
    try:
        payload = (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
                separators=(",", ": "),
            )
            + "\n"
        ).encode("utf-8", errors="strict")
    except (RecursionError, TypeError, ValueError) as exc:
        raise StorageValidationError(f"{label} is not canonical JSON data") from exc
    if len(payload) > _MAX_METADATA_BYTES:
        raise StorageValidationError(
            f"{label} exceeds its {_MAX_METADATA_BYTES}-byte limit"
        )
    return payload


def _output_record(
    relative: str, payload: bytes, *, mode: int = 0o600
) -> TreeFileRecord:
    return TreeFileRecord(
        path=relative,
        mode=mode,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _member_record(view: str, member: ArtifactMember) -> TreeFileRecord:
    return TreeFileRecord(
        path=(PurePosixPath("views") / view / member.path).as_posix(),
        mode=member.mode,
        size=member.byte_size,
        sha256=member.digest,
    )


def _authenticate_view_bundle(
    object_store: ReceiptRetainingObjectStore,
    view_receipt: RepoManifestViewExportReceipt,
    intent: ViewImportIntent,
    *,
    max_bundle_files: int,
    max_bundle_bytes: int,
    max_bundle_metadata_bytes: int,
) -> tuple[ArtifactMember, ...]:
    bundle = view_receipt.bundle_receipt

    def consume(source: BinaryIO) -> tuple[ArtifactMember, ...]:
        def validate(
            archive: BinaryIO,
            record: ViewBundleRecord,
        ) -> tuple[ArtifactMember, ...]:
            members = tuple(
                ArtifactMember(
                    path=member.path,
                    digest=member.digest,
                    byte_size=member.byte_size,
                    mode=member.mode,
                )
                for member in record.members
            )
            expected = view_receipt.member_receipt_items
            if len(members) != len(expected):
                raise StorageIntegrityError(
                    f"retained {view_receipt.view_type!r} bundle inventory differs "
                    "from its member receipts"
                )
            for member, (path, receipt) in zip(members, expected, strict=True):
                if (
                    member.path != path
                    or member.digest != receipt.digest
                    or member.byte_size != receipt.byte_size
                    or member.mode not in {0o644, 0o755}
                ):
                    raise StorageIntegrityError(
                        f"retained {view_receipt.view_type!r} bundle member differs "
                        "from its receipt"
                    )
            # This consumes only the already-authenticated callback-scoped ZIP
            # facade and checks BM25/vector persistence semantics without ever
            # loading native index bytes.
            _require_bundle_semantics(
                archive,
                _SemanticView(intent=intent, members=members),  # type: ignore[arg-type]
            )
            return members

        return consume_verified_view_bundle_stream(
            source,
            validate,
            expected_view_type=view_receipt.view_type,
            expected_digest=bundle.digest,
            expected_size=bundle.byte_size,
            max_files=max_bundle_files,
            max_bytes=max_bundle_bytes,
            max_metadata_bytes=max_bundle_metadata_bytes,
        )

    return _run_open_receipt(object_store, bundle, consume)


def _record_payload(record: TreeFileRecord) -> dict[str, Any]:
    return {
        "path": record.path,
        "mode": record.mode,
        "size": record.size,
        "sha256": record.sha256,
    }


def _receipt_payload(receipt: BlobInfo) -> dict[str, Any]:
    return {
        "digest": receipt.digest,
        "byte_size": receipt.byte_size,
        "storage_key": receipt.storage_key,
    }


def _workspace_directories(
    output_records: tuple[TreeFileRecord, ...],
) -> tuple[WorkspaceDirectory, ...]:
    paths: set[str] = set()
    for record in output_records:
        relative = PurePosixPath(record.path)
        for depth in range(1, len(relative.parts)):
            paths.add(PurePosixPath(*relative.parts[:depth]).as_posix())
    return tuple(
        WorkspaceDirectory(PurePosixPath(path), mode=0o700) for path in sorted(paths)
    )


def _workspace_subject(
    exported: RepoManifestExportResult,
    *,
    repository: str,
    commit: str,
    source_fingerprint: str,
    source_selection: RepositorySourceSelection | None,
    view_plans: tuple[_RetainedViewPlan, ...],
    output_records: tuple[TreeFileRecord, ...],
) -> str:
    receipt = exported.receipt
    identity = {
        "domain": _PLAN_DOMAIN,
        "schema": CONTEXT_ARTIFACT_SCHEMA,
        "repository": repository,
        "commit": commit,
        "source_fingerprint": source_fingerprint,
        "export": {
            "namespace_id": receipt.namespace_id,
            "repository_id": receipt.repository_id,
            "source_revision_id": receipt.source_revision_id,
            "snapshot_id": receipt.snapshot_id,
            "projection_generation_id": receipt.projection_generation_id,
            "projection_receipt": _receipt_payload(receipt.projection_receipt),
            "manifest_sha256": receipt.manifest_digest,
            "manifest_bytes": receipt.manifest_byte_size,
            "skipped": [list(item) for item in receipt.skipped_items],
        },
        "views": [
            {
                "name": view.name,
                "generation_id": view.generation_id,
                "bundle_receipt": _receipt_payload(view.bundle_receipt),
                "members": [
                    {
                        **_record_payload(record),
                        "storage_key": member_receipt.storage_key,
                    }
                    for record, (_path, member_receipt) in zip(
                        view.output_records,
                        view.member_receipt_items,
                        strict=True,
                    )
                ],
            }
            for view in view_plans
        ],
        "output_records": [_record_payload(record) for record in output_records],
    }
    if source_selection is not None:
        identity["source_selection"] = source_selection.to_dict()
        identity["source_selection_digest"] = source_selection.digest
    try:
        payload = canonical_json(identity).encode("ascii")
    except (RecursionError, TypeError, ValueError) as exc:
        raise StorageValidationError(
            "retained context plan identity is invalid"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _build_context_plan(
    exported: RepoManifestExportResult,
    parsed: RepoManifestImportPlan,
    authenticated_views: tuple[
        tuple[RepoManifestViewExportReceipt, tuple[ArtifactMember, ...]], ...
    ],
    *,
    max_context_files: int,
    max_context_bytes: int,
) -> _RetainedContextPlan:
    manifest = parsed.manifest
    repository = exported.receipt.repository_key
    try:
        normalized_repository = normalize_repo(repository)
    except (TypeError, ValueError) as exc:
        raise StorageIntegrityError("exported repository identity is invalid") from exc
    if (
        normalized_repository != repository
        or _REPOSITORY_RE.fullmatch(repository) is None
    ):
        raise StorageIntegrityError("exported repository identity is not canonical")
    if (
        type(manifest.commit) is not str
        or _COMMIT_RE.fullmatch(manifest.commit) is None
    ):
        raise StorageValidationError(
            "exported manifest commit must be a full lowercase Git SHA"
        )
    if not is_secure_source_fingerprint_v2(manifest.source_fingerprint):
        raise StorageValidationError(
            "legacy source-fingerprint-v1 exports are materialization-inert"
        )

    view_plans: list[_RetainedViewPlan] = []
    member_records: list[TreeFileRecord] = []
    for view_receipt, members in authenticated_views:
        records = tuple(
            _member_record(view_receipt.view_type, member) for member in members
        )
        member_records.extend(records)
        view_plans.append(
            _RetainedViewPlan(
                name=view_receipt.view_type,
                generation_id=view_receipt.view_generation_id,
                bundle_receipt=view_receipt.bundle_receipt,
                member_receipt_items=view_receipt.member_receipt_items,
                members=members,
                output_records=records,
            )
        )

    manifest_record = _output_record(
        MANIFEST_FILENAME,
        exported.canonical_manifest_bytes,
        mode=0o600,
    )
    inventoried_records = tuple(
        sorted((manifest_record, *member_records), key=lambda record: record.path)
    )
    file_count = len(inventoried_records)
    byte_count = sum(record.size for record in inventoried_records)
    if file_count > max_context_files:
        raise StorageValidationError(
            f"retained context exceeds {max_context_files} inventoried files"
        )
    if byte_count > max_context_bytes:
        raise StorageValidationError(
            f"retained context exceeds {max_context_bytes} inventoried bytes"
        )

    metadata = {
        "schema": CONTEXT_ARTIFACT_SCHEMA,
        "repository": {
            "slug": repository,
            "commit": manifest.commit,
            "source_fingerprint": manifest.source_fingerprint,
            "languages": list(manifest.languages),
        },
        "builder": {
            "codenib_version": package_version(),
            "manifest_version": manifest.version,
            "compiled_at": manifest.compiled_at,
        },
        "manifest": {
            "path": MANIFEST_FILENAME,
            "repository_path": "source",
            "paths": "artifact-relative-posix",
        },
        "source_locations": {
            "path": "repository-relative-posix",
            "line_base": 1,
            "end_line": "inclusive",
            "commit": manifest.commit,
        },
        "views": list(exported.receipt.views),
        "capabilities": dict(manifest.capabilities),
        "files": [
            {
                "path": record.path,
                "bytes": record.size,
                "sha256": record.sha256,
            }
            for record in inventoried_records
        ],
    }
    metadata_payload = _canonical_json_bytes(
        metadata,
        label="retained context metadata",
    )
    metadata_record = _output_record(
        CONTEXT_ARTIFACT_MANIFEST,
        metadata_payload,
        mode=0o600,
    )
    output_records = tuple(
        sorted((*inventoried_records, metadata_record), key=lambda record: record.path)
    )
    frozen_view_plans = tuple(view_plans)
    workspace = WorkspacePlan(
        subject_digest=_workspace_subject(
            exported,
            repository=repository,
            commit=manifest.commit,
            source_fingerprint=manifest.source_fingerprint,
            source_selection=manifest.source_selection,
            view_plans=frozen_view_plans,
            output_records=output_records,
        ),
        directories=_workspace_directories(output_records),
        files=tuple(
            WorkspaceFile(
                PurePosixPath(record.path),
                mode=record.mode,
                max_bytes=record.size,
            )
            for record in output_records
        ),
        root_mode=0o700,
    )
    streaming_json_paths = tuple(
        sorted(
            record.path
            for record in output_records
            if PurePosixPath(record.path).name.startswith("documents")
            and PurePosixPath(record.path).suffix == ".json"
        )
    )
    return _RetainedContextPlan(
        workspace=workspace,
        repository=repository,
        commit=manifest.commit,
        manifest_version=manifest.version,
        source_fingerprint=manifest.source_fingerprint,
        views=exported.receipt.views,
        view_plans=frozen_view_plans,
        output_records=output_records,
        manifest_payload=exported.canonical_manifest_bytes,
        metadata_payload=metadata_payload,
        streaming_json_paths=streaming_json_paths,
        file_count=file_count,
        byte_count=byte_count,
    )


def _candidate_inventory(
    planned: _RetainedContextPlan,
) -> tuple[tuple[str, str], ...]:
    directories = tuple(
        (directory.path.as_posix(), "directory")
        for directory in planned.workspace.directories
    )
    files = tuple((record.path, "file") for record in planned.output_records)
    return tuple(sorted((*directories, *files)))


def _validate_candidate(
    candidate: PublicationDirectoryReader,
    *,
    planned: _RetainedContextPlan,
    environment: Mapping[str, str],
    max_context_files: int,
    max_context_bytes: int,
) -> None:
    before = candidate.capture_ownership()
    records = tuple(directory_ownership_file_records(before))  # type: ignore[arg-type]
    inventory = tuple(directory_ownership_inventory(before))  # type: ignore[arg-type]
    if records != planned.output_records or inventory != _candidate_inventory(planned):
        raise RuntimeError(
            "retained context candidate differs from its exact workspace plan"
        )
    assert_publishable_tree_reader(
        candidate,
        forbidden_paths=(),
        environ=environment,
        label="retained context artifact",
        streaming_json_paths=planned.streaming_json_paths,
    )
    verified = verify_context_artifact_reader(
        candidate,
        expected_repository=planned.repository,
        expected_commit=planned.commit,
        expected_manifest_version=planned.manifest_version,
        max_files=max_context_files,
        max_bytes=max_context_bytes,
    )
    if (
        verified.views != planned.views
        or verified.source_fingerprint != planned.source_fingerprint
        or verified.file_count != planned.file_count
        or verified.byte_count != planned.byte_count
    ):
        raise RuntimeError("retained context candidate identity differs from its plan")
    if candidate.capture_ownership() != before:
        raise RuntimeError("retained context candidate changed during validation")


def _write_exact(
    session: StrictWorkspaceSession,
    record: TreeFileRecord,
    chunks: Iterator[bytes] | tuple[bytes, ...],
) -> None:
    observed = session.write_file(PurePosixPath(record.path), chunks)
    if observed != record:
        raise StorageIntegrityError(
            f"retained context write differs from its plan: {record.path}"
        )


def _publish_plan(
    destination: Path,
    *,
    planned: _RetainedContextPlan,
    retained_receipts: tuple[BlobInfo, ...],
    object_store: ReceiptRetainingObjectStore,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    environment: Mapping[str, str],
    max_context_files: int,
    max_context_bytes: int,
) -> ContextArtifactResult:
    request = StrictWorkspaceRequest(
        purpose="retained-context-materialization",
        destination=destination,
        plan=planned.workspace,
        destination_binding=None,
    )

    def operation(session: StrictWorkspaceSession) -> None:
        if session.request != request:
            raise RuntimeError("strict workspace provider changed its request")
        _write_exact(
            session,
            next(
                record
                for record in planned.output_records
                if record.path == MANIFEST_FILENAME
            ),
            (planned.manifest_payload,),
        )
        for view in planned.view_plans:
            for record, (member_path, member_receipt) in zip(
                view.output_records,
                view.member_receipt_items,
                strict=True,
            ):
                expected_path = (
                    PurePosixPath("views") / view.name / member_path
                ).as_posix()
                if record.path != expected_path:
                    raise StorageIntegrityError(
                        "retained context member plan changed during replay"
                    )

                def write_member(
                    source: BinaryIO,
                    *,
                    expected_record: TreeFileRecord = record,
                    expected_receipt: BlobInfo = member_receipt,
                ) -> None:
                    _write_exact(
                        session,
                        expected_record,
                        _iter_receipt_bytes(source, expected_receipt),
                    )

                _run_open_receipt(object_store, member_receipt, write_member)
        _write_exact(
            session,
            next(
                record
                for record in planned.output_records
                if record.path == CONTEXT_ARTIFACT_MANIFEST
            ),
            (planned.metadata_payload,),
        )

        # Revalidate the complete retained closure after every exact member
        # write and before the first rename.  A receipt failure must leave the
        # caller's owner empty and the requested destination absent.
        for retained_receipt in retained_receipts:
            _verify_receipt(object_store, retained_receipt)

        def validate(candidate: PublicationDirectoryReader) -> None:
            _validate_candidate(
                candidate,
                planned=planned,
                environment=environment,
                max_context_files=max_context_files,
                max_context_bytes=max_context_bytes,
            )

        session.publish_validated(validate)

    run_strict_workspace(
        workspace_provider,
        request,
        receipt_owner=output_receipt_owner,
        operation=operation,
    )
    if not output_receipt_owner.active:
        raise RuntimeError("retained context publication returned without a receipt")
    receipt = output_receipt_owner.receipt
    if receipt.path != destination or receipt.plan != planned.workspace:
        raise RuntimeError("retained context receipt differs from its plan")
    return ContextArtifactResult(
        output_dir=destination,
        metadata_path=destination / CONTEXT_ARTIFACT_MANIFEST,
        manifest_path=destination / MANIFEST_FILENAME,
        repository=planned.repository,
        commit=planned.commit,
        views=planned.views,
        file_count=planned.file_count,
        byte_count=planned.byte_count,
    )


def _require_missing_destination(destination: Path) -> None:
    """Early denial-only check; the provider owns the race-free assertion."""

    try:
        destination.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(
            "retained context destination cannot be inspected safely"
        ) from exc
    raise ValueError("retained context destination must be missing")


def _preflight_authorities(
    *,
    object_store: object,
    workspace_provider: object,
    output_receipt_owner: object,
) -> None:
    if type(output_receipt_owner) is not PublishedWorkspaceReceiptOwner:
        raise TypeError("retained context output receipt owner has an invalid type")
    if output_receipt_owner.state != "empty":
        raise RuntimeError("retained context output receipt owner must be empty")
    require_owned_workspace_publication_support()
    _require_static_methods(
        workspace_provider,
        label="strict workspace provider",
        names=("require_support", "run_workspace"),
    )
    if not isinstance(object_store, ReceiptRetainingObjectStore):
        raise TypeError(
            "retained context object store lacks receipt-retention capability"
        )
    _require_static_methods(
        object_store,
        label="retained context object store",
        names=("open", "verify_receipt", "retain_receipts"),
    )


def _preflight_materialization_request(
    destination: Path,
    *,
    object_store: object,
    workspace_provider: object,
    output_receipt_owner: object,
    max_manifest_bytes: object,
    max_projection_bytes: object,
    max_context_files: object,
    max_context_bytes: object,
    max_bundle_files: object,
    max_bundle_bytes: object,
    max_bundle_metadata_bytes: object,
    environ: Mapping[str, str] | None,
    probe_provider_support: bool = False,
) -> _MaterializationPreflight:
    if not isinstance(destination, Path):
        raise TypeError("retained context destination must be a Path")
    _preflight_authorities(
        object_store=object_store,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )
    if probe_provider_support:
        workspace_provider.require_support()  # type: ignore[union-attr]
    manifest_limit = _positive_limit(max_manifest_bytes, "manifest byte limit")
    projection_limit = _positive_limit(max_projection_bytes, "projection byte limit")
    context_files = _positive_limit(max_context_files, "context file limit")
    context_bytes = _positive_limit(max_context_bytes, "context byte limit")
    bundle_files = _positive_limit(max_bundle_files, "bundle file limit")
    bundle_bytes = _positive_limit(max_bundle_bytes, "bundle byte limit")
    bundle_metadata = _positive_limit(
        max_bundle_metadata_bytes,
        "bundle metadata byte limit",
    )
    environment = _snapshot_environment(environ)
    lexical_destination = lexical_directory_path(destination)
    _require_missing_destination(lexical_destination)
    return _MaterializationPreflight(
        destination=lexical_destination,
        environment=environment,
        max_manifest_bytes=manifest_limit,
        max_projection_bytes=projection_limit,
        max_context_files=context_files,
        max_context_bytes=context_bytes,
        max_bundle_files=bundle_files,
        max_bundle_bytes=bundle_bytes,
        max_bundle_metadata_bytes=bundle_metadata,
    )


def _recheck_materialization_target(
    preflight: _MaterializationPreflight,
    *,
    object_store: object,
    workspace_provider: object,
    output_receipt_owner: object,
) -> None:
    _preflight_authorities(
        object_store=object_store,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )
    _require_missing_destination(preflight.destination)


def _materialize_retained_context_artifact_preflighted(
    exported: RepoManifestExportResult,
    preflight: _MaterializationPreflight,
    *,
    object_store: ReceiptRetainingObjectStore,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
) -> ContextArtifactResult:
    max_manifest_bytes = preflight.max_manifest_bytes
    max_projection_bytes = preflight.max_projection_bytes
    max_context_files = preflight.max_context_files
    max_context_bytes = preflight.max_context_bytes
    max_bundle_files = preflight.max_bundle_files
    max_bundle_bytes = preflight.max_bundle_bytes
    max_bundle_metadata_bytes = preflight.max_bundle_metadata_bytes
    frozen = _snapshot_exported(
        exported,
        max_manifest_bytes=max_manifest_bytes,
        max_bundle_files=max_bundle_files,
        max_context_files=max_context_files,
        max_context_bytes=max_context_bytes,
    )
    try:
        parsed = plan_repo_manifest_import_bytes(
            frozen.canonical_manifest_bytes,
            required_views=frozen.receipt.views,
            max_manifest_bytes=max_manifest_bytes,
        )
    except StorageValidationError:
        raise
    except Exception as exc:
        raise StorageIntegrityError(
            "exported repository manifest could not be replanned"
        ) from exc
    if (
        parsed.canonical_manifest_bytes != frozen.canonical_manifest_bytes
        or parsed.selection.selected_views != frozen.receipt.views
        or tuple(view.view_type for view in parsed.views) != frozen.receipt.views
    ):
        raise StorageIntegrityError(
            "exported repository manifest differs from its selected-view receipt"
        )

    for view in frozen.receipt.view_receipts:
        validate_view_bundle_physical_size(
            view.bundle_receipt.byte_size,
            max_files=max_bundle_files,
            max_bytes=max_bundle_bytes,
            max_metadata_bytes=max_bundle_metadata_bytes,
        )
    if frozen.receipt.projection_receipt.byte_size > max_projection_bytes:
        raise StorageIntegrityError(
            f"repository manifest projection exceeds {max_projection_bytes} bytes"
        )
    retained_receipts = _unique_receipts(frozen)
    if not retained_receipts:  # pragma: no cover - projection receipt is mandatory
        raise StorageIntegrityError("retained export has no object receipts")

    def retained() -> ContextArtifactResult:
        projection = _authenticate_projection(
            object_store,
            frozen,
            parsed,
            max_manifest_bytes=max_manifest_bytes,
            max_projection_bytes=max_projection_bytes,
            max_bundle_files=max_bundle_files,
        )
        authenticated: list[
            tuple[RepoManifestViewExportReceipt, tuple[ArtifactMember, ...]]
        ] = []
        for projected_view in projection.view_items:
            view_receipt = projected_view.receipt
            members = _authenticate_view_bundle(
                object_store,
                view_receipt,
                projected_view.intent,
                max_bundle_files=max_bundle_files,
                max_bundle_bytes=max_bundle_bytes,
                max_bundle_metadata_bytes=max_bundle_metadata_bytes,
            )
            if members != projected_view.members:
                raise StorageIntegrityError(
                    f"retained {view_receipt.view_type!r} bundle differs from "
                    "its authenticated projection"
                )
            authenticated.append((view_receipt, members))
        planned = _build_context_plan(
            frozen,
            parsed,
            tuple(authenticated),
            max_context_files=max_context_files,
            max_context_bytes=max_context_bytes,
        )
        return _publish_plan(
            preflight.destination,
            planned=planned,
            retained_receipts=retained_receipts,
            object_store=object_store,
            workspace_provider=workspace_provider,
            output_receipt_owner=output_receipt_owner,
            environment=preflight.environment,
            max_context_files=max_context_files,
            max_context_bytes=max_context_bytes,
        )

    return _run_retention_callback(object_store, retained_receipts, retained)


def materialize_retained_context_artifact(
    exported: RepoManifestExportResult,
    destination: Path,
    *,
    object_store: ReceiptRetainingObjectStore,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_projection_bytes: int = DEFAULT_MAX_PROJECTION_BYTES,
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES,
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    environ: Mapping[str, str] | None = None,
) -> ContextArtifactResult:
    """Publish one retained export as an exact, query-compatible artifact.

    ``output_receipt_owner`` is caller-owned and must be empty.  On success it
    remains active and is the only authority for the published generation;
    this function never closes it, including after a post-publication failure.
    """

    if type(exported) is not RepoManifestExportResult:
        raise TypeError("retained context materialization requires an exact export")
    preflight = _preflight_materialization_request(
        destination,
        object_store=object_store,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
        max_manifest_bytes=max_manifest_bytes,
        max_projection_bytes=max_projection_bytes,
        max_context_files=max_context_files,
        max_context_bytes=max_context_bytes,
        max_bundle_files=max_bundle_files,
        max_bundle_bytes=max_bundle_bytes,
        max_bundle_metadata_bytes=max_bundle_metadata_bytes,
        environ=environ,
    )
    return _materialize_retained_context_artifact_preflighted(
        exported,
        preflight,
        object_store=object_store,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )


def _retained_repository_id(repository_key: str, namespace_name: str) -> str:
    if type(repository_key) is not str:
        raise TypeError("retained materialization repository key must be exact text")
    if type(namespace_name) is not str:
        raise TypeError("retained materialization namespace name must be exact text")
    if (
        not repository_key
        or len(repository_key) > _MAX_TEXT
        or "\x00" in repository_key
        or repository_key != repository_key.strip()
    ):
        raise StorageValidationError(
            "retained materialization repository key must be a canonical slug"
        )
    namespace = NamespaceIdentity(namespace_name)
    if namespace.name != namespace_name:
        raise StorageValidationError(
            "retained materialization namespace name must be canonical"
        )
    try:
        normalized_repository = normalize_repo(repository_key)
    except ValueError as exc:
        raise StorageValidationError(
            "retained materialization repository key is invalid"
        ) from exc
    if (
        normalized_repository != repository_key
        or _REPOSITORY_RE.fullmatch(repository_key) is None
    ):
        raise StorageValidationError(
            "retained materialization repository key must be a canonical slug"
        )
    return RepositoryIdentity(
        namespace_id=namespace.namespace_id,
        repository_key=repository_key,
    ).repository_id


def _retained_selector_text(value: object, label: str) -> str:
    if type(value) is not str:
        raise StorageValidationError(f"{label} must be exact text")
    if not value or value != value.strip() or "\x00" in value or len(value) > _MAX_TEXT:
        raise StorageValidationError(f"{label} is not canonical")
    return value


def _retained_expected_generation(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 < value <= _INT64_MAX:
        raise StorageValidationError(
            "expected ref generation must be a positive signed 64-bit integer"
        )
    return value


def _materialize_retained_export(
    exported: RepoManifestExportResult,
    preflight: _MaterializationPreflight,
    *,
    object_store: ReceiptRetainingObjectStore,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
) -> RepoManifestMaterializationResult:
    artifact = _materialize_retained_context_artifact_preflighted(
        exported,
        preflight,
        object_store=object_store,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )
    return RepoManifestMaterializationResult(
        artifact=artifact,
        export_receipt=exported.receipt,
    )


def materialize_retained_repo_manifest_ref(
    repository_key: str,
    destination: Path,
    *,
    catalog: RetainedSnapshotCatalog,
    object_store: ReceiptRetainingObjectStore,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    namespace_name: str = DEFAULT_NAMESPACE_NAME,
    ref_name: str = DEFAULT_REF_NAME,
    expected_generation: int | None = None,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_projection_bytes: int = DEFAULT_MAX_PROJECTION_BYTES,
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES,
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    environ: Mapping[str, str] | None = None,
) -> RepoManifestMaterializationResult:
    """Resolve one retained ref and publish its exact context generation.

    ``output_receipt_owner`` is caller-owned and must be empty.  This function
    never closes it.  Success leaves it active, and a failure after publication
    may also leave it active so the caller can settle the published authority.
    """

    repository_id = _retained_repository_id(repository_key, namespace_name)
    ref_name = _retained_selector_text(ref_name, "retained materialization ref name")
    expected_generation = _retained_expected_generation(expected_generation)
    preflight = _preflight_materialization_request(
        destination,
        object_store=object_store,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
        max_manifest_bytes=max_manifest_bytes,
        max_projection_bytes=max_projection_bytes,
        max_context_files=max_context_files,
        max_context_bytes=max_context_bytes,
        max_bundle_files=max_bundle_files,
        max_bundle_bytes=max_bundle_bytes,
        max_bundle_metadata_bytes=max_bundle_metadata_bytes,
        environ=environ,
        probe_provider_support=True,
    )
    exported = export_retained_repo_manifest_ref(
        repository_id,
        catalog=catalog,
        object_store=object_store,
        ref_name=ref_name,
        expected_generation=expected_generation,
        max_manifest_bytes=preflight.max_manifest_bytes,
        max_projection_bytes=preflight.max_projection_bytes,
        max_bundle_files=preflight.max_bundle_files,
        max_bundle_bytes=preflight.max_bundle_bytes,
        max_bundle_metadata_bytes=preflight.max_bundle_metadata_bytes,
    )
    _recheck_materialization_target(
        preflight,
        object_store=object_store,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )
    return _materialize_retained_export(
        exported,
        preflight,
        object_store=object_store,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )


def materialize_retained_repo_manifest_snapshot(
    repository_key: str,
    snapshot_id: str,
    destination: Path,
    *,
    catalog: RetainedSnapshotCatalog,
    object_store: ReceiptRetainingObjectStore,
    workspace_provider: StrictWorkspaceProvider,
    output_receipt_owner: PublishedWorkspaceReceiptOwner,
    namespace_name: str = DEFAULT_NAMESPACE_NAME,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_projection_bytes: int = DEFAULT_MAX_PROJECTION_BYTES,
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES,
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    environ: Mapping[str, str] | None = None,
) -> RepoManifestMaterializationResult:
    """Read one retained immutable snapshot and publish its exact context.

    ``output_receipt_owner`` is caller-owned and must be empty.  This function
    never closes it.  Success leaves it active, and a failure after publication
    may also leave it active so the caller can settle the published authority.
    """

    repository_id = _retained_repository_id(repository_key, namespace_name)
    snapshot_id = _retained_selector_text(
        snapshot_id,
        "retained materialization snapshot ID",
    )
    preflight = _preflight_materialization_request(
        destination,
        object_store=object_store,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
        max_manifest_bytes=max_manifest_bytes,
        max_projection_bytes=max_projection_bytes,
        max_context_files=max_context_files,
        max_context_bytes=max_context_bytes,
        max_bundle_files=max_bundle_files,
        max_bundle_bytes=max_bundle_bytes,
        max_bundle_metadata_bytes=max_bundle_metadata_bytes,
        environ=environ,
        probe_provider_support=True,
    )
    exported = export_retained_repo_manifest_snapshot(
        repository_id,
        snapshot_id,
        catalog=catalog,
        object_store=object_store,
        max_manifest_bytes=preflight.max_manifest_bytes,
        max_projection_bytes=preflight.max_projection_bytes,
        max_bundle_files=preflight.max_bundle_files,
        max_bundle_bytes=preflight.max_bundle_bytes,
        max_bundle_metadata_bytes=preflight.max_bundle_metadata_bytes,
    )
    _recheck_materialization_target(
        preflight,
        object_store=object_store,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )
    return _materialize_retained_export(
        exported,
        preflight,
        object_store=object_store,
        workspace_provider=workspace_provider,
        output_receipt_owner=output_receipt_owner,
    )


__all__ = [
    "RepoManifestMaterializationResult",
    "materialize_retained_context_artifact",
    "materialize_retained_repo_manifest_ref",
    "materialize_retained_repo_manifest_snapshot",
]
