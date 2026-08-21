# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Export retained catalog snapshots as canonical portable RepoManifest bytes.

The export result is deliberately data-only.  It does not materialize a
directory, authorize native index loading, or pin CAS objects against future
garbage collection.  Every receipt records a point-in-time observation made
while this call ran.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, BinaryIO, Callable

from .._atomic_directory import (
    _attach_publication_cleanup_owner,
    _OrderedAction,
    _run_context_with_cleanup_actions,
)
from .._bounded_json import canonical_json_array_chunks, iter_bounded_json_array
from .._secret_fields import SecretFieldError, assert_no_secret_fields
from ..artifacts.portable_views import (
    MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
    MAX_PORTABLE_FAISS_INDEX_BYTES,
    validate_portable_vector_persistence_semantics,
)
from ..index.embedding.artifact_integrity import VECTOR_PERSISTENCE_SCHEMA
from ..provider_routes import normalize_provider, resolve_embedding_artifact_route
from ..storage.cas import BlobInfo
from ..storage.models import (
    VIEW_GENERATION_MEMBERS_METADATA_KEY,
    ArtifactMember,
    ObjectRecord,
    PublishConflict,
    RepositoryIdentity,
    StorageIntegrityError,
    StorageValidationError,
    canonical_json,
    canonical_utc_timestamp,
)
from ..storage.protocols import (
    RETAINED_IMPORT_CATALOG_CONTRACT,
    ReceiptVerifyingObjectStore,
    RetainedSnapshotCatalog,
    snapshot_retained_import_response,
)
from ..storage.view_bundle import (
    DEFAULT_MAX_BUNDLE_BYTES,
    DEFAULT_MAX_BUNDLE_FILES,
    DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    VIEW_BUNDLE_MEDIA_TYPE,
    VIEW_BUNDLE_PAYLOAD,
    VIEW_BUNDLE_SCHEMA,
    ViewBundleRecord,
    consume_verified_view_bundle_stream,
    validate_view_bundle_physical_size,
)
from .manifest import MANIFEST_VERSION, SUPPORTED_MANIFEST_VERSIONS, RepoManifest
from .manifest_import import DEFAULT_MAX_PROJECTION_BYTES
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
    REPO_MANIFEST_PROJECTION_VIEW,
    repo_manifest_generation_metadata,
    repo_manifest_projection_profile,
)
from .retained_snapshot import (
    _RetainedSnapshot,
    _RetainedSnapshotView,
    attest_detached_retained_snapshot,
    same_exact_json,
)

DEFAULT_REF_NAME = "main"

_INT64_MAX = 9_223_372_036_854_775_807
_MAX_TEXT = 32_768
_READ_CHUNK_BYTES = 1024 * 1024
_MEMBER_MEDIA_TYPE = "application/octet-stream"
_MAX_CONFIG_BYTES = 16 * 1024 * 1024
_VECTOR_LEVELS = ("l0", "l2")
_VECTOR_LEVEL_CONFIG_FIELDS = {
    "embedding_model",
    "embedding_provider",
    "embedding_revision",
    "dimension",
    "index_type",
    "index_metric",
    "level",
    "num_documents",
}
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WORKSPACE_PLAN_DIGEST_RE = _DIGEST_RE
_CATALOG_METHODS = (
    "retained_import_contract",
    "resolve_ref",
    "get_manifest_summary",
)
_OBJECT_STORE_METHODS = (
    "open",
    "verify_receipt",
)
_MISSING = object()


@dataclass(frozen=True, slots=True)
class RepoManifestViewExportReceipt:
    """Point-in-time receipts for one exported portable view."""

    view_type: str
    view_generation_id: str
    bundle_receipt: BlobInfo
    member_receipt_items: tuple[tuple[str, BlobInfo], ...]

    def __post_init__(self) -> None:
        if type(self) is not RepoManifestViewExportReceipt:
            raise StorageValidationError(
                "manifest view export receipt must use the exact receipt type"
            )
        view_type = _exact_text(
            self.view_type,
            "exported view type",
            max_length=128,
        )
        if view_type not in PORTABLE_STORAGE_VIEWS:
            raise StorageValidationError("exported view type is unsupported")
        _exact_text(self.view_generation_id, "exported view generation ID")
        _require_exact_blob(self.bundle_receipt, label="exported view bundle receipt")
        if type(self.member_receipt_items) is not tuple:
            raise StorageValidationError(
                "exported view member receipts must be an exact tuple"
            )
        previous = ""
        seen: set[str] = set()
        for item in self.member_receipt_items:
            if type(item) is not tuple or len(item) != 2 or type(item[0]) is not str:
                raise StorageValidationError(
                    "exported view member receipt entries are invalid"
                )
            path = _exact_text(item[0], "exported view member path", max_length=4096)
            blob = _require_exact_blob(
                item[1],
                label="exported view member receipt",
            )
            try:
                ArtifactMember(path, blob.digest, blob.byte_size)
            except StorageValidationError as exc:
                raise StorageValidationError(
                    "exported view member receipt path is not canonical"
                ) from exc
            if path <= previous or path in seen:
                raise StorageValidationError(
                    "exported view member receipt paths are not canonical"
                )
            previous = path
            seen.add(path)

    @property
    def member_receipts(self) -> Mapping[str, BlobInfo]:
        return MappingProxyType(dict(self.member_receipt_items))


@dataclass(frozen=True, slots=True)
class RepoManifestExportReceipt:
    """Point-in-time observations supporting one manifest byte export.

    This is not a GC pin, retention lease, directory ownership token, or
    materialization authority.
    """

    namespace_id: str
    repository_id: str
    repository_key: str
    source_revision_id: str
    snapshot_id: str
    snapshot_published_at: str
    ref_name: str | None
    ref_generation: int | None
    ref_updated_at: str | None
    manifest_digest: str
    manifest_byte_size: int
    projection_generation_id: str
    projection_receipt: BlobInfo
    view_receipts: tuple[RepoManifestViewExportReceipt, ...]
    skipped_items: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if type(self) is not RepoManifestExportReceipt:
            raise StorageValidationError(
                "manifest export receipt must use the exact receipt type"
            )
        for value, label in (
            (self.namespace_id, "export namespace ID"),
            (self.repository_id, "export repository ID"),
            (self.repository_key, "export repository key"),
            (self.source_revision_id, "export source revision ID"),
            (self.snapshot_id, "export snapshot ID"),
            (self.projection_generation_id, "export projection generation ID"),
        ):
            _exact_text(value, label)
        try:
            repository = RepositoryIdentity(
                namespace_id=self.namespace_id,
                repository_key=self.repository_key,
            )
        except StorageValidationError as exc:
            raise StorageValidationError(
                "export repository identity is invalid"
            ) from exc
        if repository.repository_id != self.repository_id:
            raise StorageValidationError("export repository identity is inconsistent")
        if (
            type(self.manifest_digest) is not str
            or _DIGEST_RE.fullmatch(self.manifest_digest) is None
        ):
            raise StorageValidationError("export manifest digest is invalid")
        if (
            type(self.manifest_byte_size) is not int
            or not 0 < self.manifest_byte_size <= _INT64_MAX
        ):
            raise StorageValidationError("export manifest byte size is invalid")
        try:
            canonical_utc_timestamp(
                self.snapshot_published_at,
                "export snapshot published_at",
            )
        except StorageValidationError:
            raise
        if self.ref_name is None:
            if self.ref_generation is not None or self.ref_updated_at is not None:
                raise StorageValidationError(
                    "snapshot export receipt must not claim a ref generation"
                )
        else:
            _exact_text(self.ref_name, "export ref name")
            if (
                type(self.ref_generation) is not int
                or not 0 < self.ref_generation <= _INT64_MAX
                or type(self.ref_updated_at) is not str
            ):
                raise StorageValidationError("export ref observation is invalid")
            canonical_utc_timestamp(self.ref_updated_at, "export ref updated_at")
        _require_exact_blob(
            self.projection_receipt,
            label="export projection receipt",
        )
        if type(self.view_receipts) is not tuple or not self.view_receipts:
            raise StorageValidationError(
                "exported view receipts must be a nonempty exact tuple"
            )
        if any(
            type(value) is not RepoManifestViewExportReceipt
            for value in self.view_receipts
        ):
            raise StorageValidationError("exported view receipt entries are invalid")
        view_names = tuple(value.view_type for value in self.view_receipts)
        if view_names != tuple(sorted(set(view_names))):
            raise StorageValidationError("exported view receipts are not canonical")
        if type(self.skipped_items) is not tuple:
            raise StorageValidationError(
                "export skipped decisions must be an exact tuple"
            )
        previous_skipped = ""
        for item in self.skipped_items:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
            ):
                raise StorageValidationError("export skipped decisions are invalid")
            view_type = _exact_text(
                item[0],
                "export skipped view",
                max_length=128,
            )
            _exact_text(item[1], "export skip reason", max_length=256)
            if (
                view_type not in PORTABLE_STORAGE_VIEWS
                or view_type <= previous_skipped
                or view_type in view_names
            ):
                raise StorageValidationError(
                    "export skipped decisions are not canonical"
                )
            previous_skipped = view_type

    @property
    def views(self) -> tuple[str, ...]:
        return tuple(value.view_type for value in self.view_receipts)

    @property
    def skipped_views(self) -> Mapping[str, str]:
        return MappingProxyType(dict(self.skipped_items))


@dataclass(frozen=True, slots=True)
class RepoManifestExportResult:
    """Canonical portable ``RepoManifest`` bytes and their export receipt."""

    canonical_manifest_bytes: bytes
    receipt: RepoManifestExportReceipt

    def __post_init__(self) -> None:
        if type(self) is not RepoManifestExportResult:
            raise StorageValidationError(
                "manifest export result must use the exact result type"
            )
        if type(self.canonical_manifest_bytes) is not bytes:
            raise StorageValidationError("exported manifest bytes must be exact bytes")
        if type(self.receipt) is not RepoManifestExportReceipt:
            raise StorageValidationError("exported manifest receipt is invalid")
        if (
            len(self.canonical_manifest_bytes) != self.receipt.manifest_byte_size
            or hashlib.sha256(self.canonical_manifest_bytes).hexdigest()
            != self.receipt.manifest_digest
        ):
            raise StorageValidationError(
                "exported manifest bytes differ from their receipt"
            )

    @property
    def manifest_bytes(self) -> bytes:
        return self.canonical_manifest_bytes

    @property
    def manifest(self) -> RepoManifest:
        plan = plan_repo_manifest_import_bytes(
            self.canonical_manifest_bytes,
            required_views=self.receipt.views,
            max_manifest_bytes=max(
                DEFAULT_MAX_MANIFEST_BYTES,
                len(self.canonical_manifest_bytes),
            ),
        )
        return plan.manifest

    @property
    def repository_id(self) -> str:
        return self.receipt.repository_id

    @property
    def source_revision_id(self) -> str:
        return self.receipt.source_revision_id

    @property
    def snapshot_id(self) -> str:
        return self.receipt.snapshot_id

    @property
    def views(self) -> tuple[str, ...]:
        return self.receipt.views


@dataclass(frozen=True, slots=True)
class _ProjectedView:
    intent: ViewImportIntent
    catalog_view: _RetainedSnapshotView
    members: tuple[ArtifactMember, ...]
    member_receipt_items: tuple[tuple[str, BlobInfo], ...]


@dataclass(frozen=True, slots=True)
class _ValidatedProjection:
    manifest_bytes: bytes
    plan: RepoManifestImportPlan
    skipped_items: tuple[tuple[str, str], ...]
    projection_receipt: BlobInfo
    projection_view: _RetainedSnapshotView
    view_items: tuple[tuple[str, _ProjectedView], ...]

    @property
    def views(self) -> Mapping[str, _ProjectedView]:
        return MappingProxyType(dict(self.view_items))


class _CloseableResourceOwner:
    """Own one callback-acquired closeable across interruption boundaries."""

    __slots__ = ("_complete", "_first_close_error", "_resource")

    def __init__(self, complete: Callable[[Any], bool]) -> None:
        self._complete = complete
        self._first_close_error: BaseException | None = None
        self._resource: Any = _MISSING

    @property
    def closed(self) -> bool:
        resource = self._resource
        if resource is _MISSING:
            return True
        return self._complete(resource) is True

    @property
    def first_close_error(self) -> BaseException | None:
        return self._first_close_error

    def acquire(self, callback: Callable[[], Any]) -> Any:
        if self._resource is not _MISSING:
            raise RuntimeError("closeable resource is already acquired")
        # The owner and its cleanup action exist before this call.  As with the
        # repository's descriptor owners, only the native-return-to-STORE_ATTR
        # edge remains outside Python's control.
        self._resource = callback()
        return self._resource

    def close(self) -> None:
        resource = self._resource
        if resource is _MISSING:
            return
        try:
            resource.close()
        except BaseException as error:  # noqa: B036 - preserve exact close primary
            if self._first_close_error is None:
                self._first_close_error = error
            raise


def _run_with_owned_close(
    acquire: Callable[[], Any],
    operation: Callable[[Any], Any],
    *,
    complete: Callable[[Any], bool],
    cleanup_label: str,
    close_failure: str,
) -> Any:
    """Acquire and close one resource under the shared ordered-cleanup fence."""

    owner = _CloseableResourceOwner(complete)
    close_action = _OrderedAction(
        label=cleanup_label,
        action=owner.close,
        complete=lambda: owner.closed,
        retry_incomplete="cancellation",
        incomplete_owner=owner,
    )
    operation_succeeded = False
    try:
        with _run_context_with_cleanup_actions((close_action,)):
            resource = owner.acquire(acquire)
            result = operation(resource)
            operation_succeeded = True
            return result
    except BaseException as error:  # noqa: B036 - preserve operation primary
        if (
            operation_succeeded
            and error is owner.first_close_error
            and isinstance(error, Exception)
        ):
            wrapped = StorageIntegrityError(close_failure)
            _attach_publication_cleanup_owner(wrapped, owner)
            raise wrapped from error
        raise


def _exact_text(value: object, label: str, *, max_length: int = _MAX_TEXT) -> str:
    if type(value) is not str:
        raise StorageValidationError(f"{label} must be exact text")
    if len(value) > max_length:
        raise StorageValidationError(f"{label} exceeds {max_length} characters")
    if not value or value != value.strip() or "\x00" in value:
        raise StorageValidationError(f"{label} is not canonical")
    return value


def _positive_limit(value: object, label: str) -> int:
    if type(value) is not int or not 0 < value <= (64 * 1024 * 1024 * 1024):
        raise StorageValidationError(
            f"{label} must be a positive integer no larger than 64 GiB"
        )
    return value


def _optional_generation(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 < value <= _INT64_MAX:
        raise StorageValidationError(
            "expected ref generation must be a positive signed 64-bit integer"
        )
    return value


def _require_static_methods(value: object, *, label: str, names: Sequence[str]) -> None:
    if value is None:
        raise TypeError(f"{label} is required")
    for name in names:
        candidate = inspect.getattr_static(value, name, _MISSING)
        if candidate is _MISSING or not callable(candidate):
            raise TypeError(f"{label} must provide {name}()")


def _require_exact_blob(value: object, *, label: str) -> BlobInfo:
    if type(value) is not BlobInfo:
        raise StorageIntegrityError(f"{label} must be an exact BlobInfo")
    if (
        type(value.digest) is not str
        or _DIGEST_RE.fullmatch(value.digest) is None
        or type(value.byte_size) is not int
        or not 0 <= value.byte_size <= _INT64_MAX
        or type(value.storage_key) is not str
        or not value.storage_key
        or value.storage_key != value.storage_key.strip()
        or "\x00" in value.storage_key
    ):
        raise StorageIntegrityError(f"{label} is not canonical")
    return value


def _receipt(record: ObjectRecord) -> BlobInfo:
    return BlobInfo(
        digest=record.digest,
        byte_size=record.byte_size,
        storage_key=record.storage_key,
    )


def _same_record(left: ObjectRecord, right: ObjectRecord) -> bool:
    return (
        left.digest == right.digest
        and left.byte_size == right.byte_size
        and left.storage_key == right.storage_key
        and left.media_type == right.media_type
    )


def _verify_receipt(
    object_store: ReceiptVerifyingObjectStore,
    record: ObjectRecord,
) -> BlobInfo:
    expected = _receipt(record)
    observed = object_store.verify_receipt(expected)
    observed = _require_exact_blob(observed, label="object-store receipt verification")
    if (
        observed.digest != expected.digest
        or observed.byte_size != expected.byte_size
        or observed.storage_key != expected.storage_key
    ):
        raise StorageIntegrityError("object-store receipt identity changed")
    return observed


def _run_open_object(
    object_store: ReceiptVerifyingObjectStore,
    record: ObjectRecord,
    operation: Callable[[BinaryIO], Any],
) -> Any:
    _verify_receipt(object_store, record)

    def acquire() -> BinaryIO:
        try:
            return object_store.open(record.digest)
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


def _read_exact_bytes(source: BinaryIO, record: ObjectRecord) -> bytes:
    try:
        read = source.read
    except Exception as exc:
        raise StorageIntegrityError("object-store reader is invalid") from exc
    if not callable(read):
        raise StorageIntegrityError("object-store reader is invalid")
    result = bytearray()
    observed = hashlib.sha256()
    remaining = record.byte_size
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
        result.extend(block)
        remaining -= len(block)
    try:
        extra = read(1)
    except BaseException as error:  # noqa: B036 - backend boundary
        if isinstance(error, (GeneratorExit, KeyboardInterrupt, SystemExit)):
            raise
        raise StorageIntegrityError("object-store read failed") from error
    if type(extra) is not bytes or extra:
        raise StorageIntegrityError("object-store read exceeded its receipt")
    if observed.hexdigest() != record.digest:
        raise StorageIntegrityError("object-store bytes do not match their receipt")
    return bytes(result)


def _strict_json_bytes(payload: bytes, *, label: str) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        text = payload.decode("ascii", errors="strict")
        raw = json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite number: {value}")
            ),
        )
        value = snapshot_retained_import_response(raw, label=label)
        if type(value) is not dict or canonical_json(value).encode("ascii") != payload:
            raise StorageIntegrityError(f"{label} is not canonical JSON")
        return value
    except StorageIntegrityError:
        raise
    except (RecursionError, TypeError, ValueError) as exc:
        raise StorageIntegrityError(f"{label} is invalid") from exc


def _semantic_object(record: ObjectRecord) -> dict[str, Any]:
    return {
        "digest": record.digest,
        "byte_size": record.byte_size,
        "media_type": record.media_type,
    }


def _artifact_member(
    value: object,
    *,
    view_type: str,
) -> ArtifactMember:
    if type(value) is not dict or set(value) != {
        "path",
        "digest",
        "byte_size",
        "mode",
        "object",
    }:
        raise StorageIntegrityError(f"projected {view_type!r} member shape is invalid")
    try:
        member = ArtifactMember(
            path=value["path"],
            digest=value["digest"],
            byte_size=value["byte_size"],
            mode=value["mode"],
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError(
            f"projected {view_type!r} member is invalid"
        ) from exc
    semantic = value["object"]
    if (
        type(semantic) is not dict
        or set(semantic) != {"digest", "byte_size", "media_type"}
        or not same_exact_json(
            semantic,
            {
                "digest": member.digest,
                "byte_size": member.byte_size,
                "media_type": _MEMBER_MEDIA_TYPE,
            },
        )
        or not same_exact_json(
            value,
            {
                "path": member.path,
                "digest": member.digest,
                "byte_size": member.byte_size,
                "mode": member.mode,
                "object": semantic,
            },
        )
    ):
        raise StorageIntegrityError(
            f"projected {view_type!r} member identity is inconsistent"
        )
    return member


def _require_fingerprint(
    value: object,
    member: ArtifactMember | None,
    *,
    expected_file: str,
    label: str,
    keyed_by_path: bool = False,
) -> None:
    expected_fields = (
        {"size", "sha256"} if keyed_by_path else {"file", "size", "sha256"}
    )
    if (
        type(value) is not dict
        or set(value) != expected_fields
        or member is None
        or (not keyed_by_path and value["file"] != expected_file)
        or type(value["size"]) is not int
        or value["size"] != member.byte_size
        or value["sha256"] != member.digest
    ):
        raise StorageIntegrityError(f"{label} differs from its projected member")


def _cross_bind_generation_record(
    intent: ViewImportIntent,
    members: tuple[ArtifactMember, ...],
) -> None:
    by_path = {member.path: member for member in members}
    record = intent.generation_record
    if intent.view_type == "bm25":
        if set(by_path) != {"bm25_metadata.json", "documents.json"}:
            raise StorageIntegrityError(
                "projected BM25 bundle contains an undeclared artifact"
            )
        fingerprints = record.get("files")
        if type(fingerprints) is not dict or set(fingerprints) != set(by_path):
            raise StorageIntegrityError("projected BM25 generation files are invalid")
        for path in sorted(by_path):
            _require_fingerprint(
                fingerprints[path],
                by_path[path],
                expected_file=path,
                label=f"projected BM25 {path}",
                keyed_by_path=True,
            )
        return
    if intent.view_type != "vector":
        raise StorageIntegrityError("projected view type is unsupported")
    fingerprint = record.get("persistence_config")
    if type(fingerprint) is not dict:
        raise StorageIntegrityError("projected vector generation config is invalid")
    path = fingerprint.get("file")
    if type(path) is not str:
        raise StorageIntegrityError(
            "projected vector generation config path is invalid"
        )
    _require_fingerprint(
        fingerprint,
        by_path.get(path),
        expected_file=path,
        label="projected vector root config",
    )


def _read_zip_json(
    archive: zipfile.ZipFile,
    member_by_path: Mapping[str, ArtifactMember],
    path: str,
    *,
    label: str,
) -> dict[str, Any]:
    member = member_by_path.get(path)
    if member is None or member.byte_size > _MAX_CONFIG_BYTES:
        raise StorageIntegrityError(f"{label} is missing or exceeds its byte limit")

    def acquire() -> BinaryIO:
        try:
            return archive.open(f"{VIEW_BUNDLE_PAYLOAD}/{path}", mode="r")
        except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise StorageIntegrityError(f"{label} cannot be opened") from exc

    def read(source: BinaryIO) -> dict[str, Any]:
        try:
            payload = source.read(_MAX_CONFIG_BYTES + 1)
            if type(payload) is not bytes or len(payload) > _MAX_CONFIG_BYTES:
                raise StorageIntegrityError(f"{label} exceeds its byte limit")
            value = json.loads(
                payload.decode("utf-8", errors="strict"),
                object_pairs_hook=lambda pairs: _strict_object_pairs(
                    pairs,
                    label=label,
                ),
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite number: {value}")
                ),
            )
            value = snapshot_retained_import_response(value, label=label)
            if (
                type(value) is not dict
                or _portable_json_bytes(value) != payload
                or hashlib.sha256(payload).hexdigest() != member.digest
                or len(payload) != member.byte_size
            ):
                raise StorageIntegrityError(f"{label} is not canonical JSON")
            assert_no_secret_fields(value, source=label)
            return value
        except BaseException as error:  # noqa: B036 - preserve semantic primary
            if isinstance(
                error,
                (
                    StorageIntegrityError,
                    GeneratorExit,
                    KeyboardInterrupt,
                    SystemExit,
                ),
            ):
                raise
            if isinstance(
                error,
                (
                    KeyError,
                    OSError,
                    RecursionError,
                    SecretFieldError,
                    TypeError,
                    UnicodeError,
                    ValueError,
                    zipfile.BadZipFile,
                ),
            ):
                raise StorageIntegrityError(f"{label} is invalid") from error
            raise

    return _run_with_owned_close(
        acquire,
        read,
        complete=lambda source: source.closed is True,
        cleanup_label=f"{label} close also failed",
        close_failure=f"{label} close failed",
    )


def _run_zip_archive(
    archive_handle: BinaryIO,
    *,
    label: str,
    operation: Callable[[zipfile.ZipFile], Any],
) -> Any:
    def acquire() -> zipfile.ZipFile:
        try:
            return zipfile.ZipFile(archive_handle, mode="r")
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
            raise StorageIntegrityError(f"{label} cannot be opened") from exc

    return _run_with_owned_close(
        acquire,
        operation,
        complete=lambda archive: archive.fp is None,
        cleanup_label=f"{label} close also failed",
        close_failure=f"{label} close failed",
    )


def _strict_object_pairs(
    pairs: list[tuple[str, Any]],
    *,
    label: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StorageIntegrityError(f"{label} repeats a JSON key")
        result[key] = value
    return result


def _portable_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def _read_canonical_documents(
    archive: zipfile.ZipFile,
    member: ArtifactMember,
    path: str,
    *,
    label: str,
    require_source_field: bool,
) -> int:
    if member.byte_size > MAX_PORTABLE_DOCUMENTS_JSON_BYTES:
        raise StorageIntegrityError(f"{label} exceeds its byte limit")

    def acquire() -> BinaryIO:
        try:
            return archive.open(f"{VIEW_BUNDLE_PAYLOAD}/{path}", mode="r")
        except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise StorageIntegrityError(f"{label} cannot be opened") from exc

    def read(source: BinaryIO) -> int:
        count = 0
        size = 0
        digest = hashlib.sha256()

        def documents():
            nonlocal count
            for index, document in enumerate(
                iter_bounded_json_array(source, label=label)
            ):
                if (
                    type(document) is not dict
                    or set(document) != {"page_content", "metadata"}
                    or type(document["page_content"]) is not str
                    or type(document["metadata"]) is not dict
                ):
                    raise StorageIntegrityError(
                        f"{label} document {index} has an invalid shape"
                    )
                try:
                    assert_no_secret_fields(
                        document,
                        source=f"{label} document {index}",
                    )
                except SecretFieldError as exc:
                    raise StorageIntegrityError(
                        f"{label} document {index} is not publishable"
                    ) from exc
                raw_file = document["metadata"].get("file")
                if require_source_field and "file" not in document["metadata"]:
                    raise StorageIntegrityError(
                        f"{label} document {index} has no source path"
                    )
                if raw_file is not None:
                    _require_portable_source_path(
                        raw_file,
                        label=f"{label} document {index} source path",
                    )
                count += 1
                yield document

        try:
            for chunk in canonical_json_array_chunks(documents()):
                size += len(chunk)
                if size > MAX_PORTABLE_DOCUMENTS_JSON_BYTES:
                    raise StorageIntegrityError(f"{label} exceeds its byte limit")
                digest.update(chunk)
            if size != member.byte_size or digest.hexdigest() != member.digest:
                raise StorageIntegrityError(f"{label} is not canonical JSON")
        except BaseException as error:  # noqa: B036 - preserve semantic primary
            if isinstance(
                error,
                (
                    StorageIntegrityError,
                    GeneratorExit,
                    KeyboardInterrupt,
                    SystemExit,
                ),
            ):
                raise
            raise StorageIntegrityError(f"{label} is invalid") from error
        return count

    return _run_with_owned_close(
        acquire,
        read,
        complete=lambda source: source.closed is True,
        cleanup_label=f"{label} close also failed",
        close_failure=f"{label} close failed",
    )


def _require_portable_source_path(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise StorageIntegrityError(f"{label} is invalid")
    # Content-bound portable validation preserves an exact empty source path
    # for documents that intentionally carry no repository-file attribution.
    if not value:
        return value
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise StorageIntegrityError(f"{label} is invalid") from exc
    parts = value.split("/")
    normalized = PurePosixPath(value)
    if (
        len(encoded) > 4_096
        or len(parts) > 256
        or value.startswith(("/", "~"))
        or "\\" in value
        or _WINDOWS_DRIVE_RE.match(value) is not None
        or normalized.is_absolute()
        or any(part in {"", ".", "..", "~"} for part in parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or value != normalized.as_posix()
    ):
        raise StorageIntegrityError(f"{label} is not portable-relative")
    return value


def _require_vector_record(
    value: object,
    member_by_path: Mapping[str, ArtifactMember],
    *,
    relative_path: str,
    expected_file: str,
    label: str,
    max_bytes: int,
) -> None:
    member = member_by_path.get(relative_path)
    _require_fingerprint(
        value,
        member,
        expected_file=expected_file,
        label=label,
    )
    if member is None or member.byte_size > max_bytes:
        raise StorageIntegrityError(f"{label} exceeds its byte limit")


def _require_level_config(
    value: Mapping[str, Any],
    *,
    root: Mapping[str, Any],
    level: str,
    count: int,
    provider: str,
    revision: str | None,
    dimension: int,
    metric: str,
    index_type: str,
) -> None:
    if set(value) != _VECTOR_LEVEL_CONFIG_FIELDS:
        raise StorageIntegrityError(
            f"snapshot vector {level} config has an invalid shape"
        )
    try:
        observed_provider = normalize_provider(value["embedding_provider"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageIntegrityError(
            f"snapshot vector {level} config has an invalid provider"
        ) from exc
    expected = {
        "embedding_model": root.get("embedding_model"),
        "embedding_revision": revision,
        "dimension": dimension,
        "index_type": index_type,
        "index_metric": metric,
        "level": level,
        "num_documents": count,
    }
    if observed_provider != provider or any(
        type(value.get(field)) is not type(expected_value)
        or not same_exact_json(value.get(field), expected_value)
        for field, expected_value in expected.items()
    ):
        raise StorageIntegrityError(
            f"snapshot vector {level} config conflicts with its root config"
        )


def _require_bundle_semantics(
    archive_handle: BinaryIO,
    view: _ProjectedView,
) -> None:
    member_by_path = {member.path: member for member in view.members}
    if view.intent.view_type == "bm25":
        # Exact inventory was already checked; parsing both canonical JSON
        # objects prevents a coherently reclosed arbitrary byte bundle.
        def validate_bm25(archive: zipfile.ZipFile) -> None:
            metadata = _read_zip_json(
                archive,
                member_by_path,
                "bm25_metadata.json",
                label="snapshot BM25 metadata",
            )
            view_config = view.intent.manifest_entry.get("config")
            if (
                set(metadata) != {"project_root", "max_k", "language"}
                or metadata.get("project_root") != "source"
                or type(metadata.get("max_k")) is not int
                or metadata["max_k"] <= 0
                or type(view_config) is not dict
                or type(view_config.get("max_k")) is not int
                or metadata["max_k"] != view_config["max_k"]
                or type(metadata.get("language")) is not str
                or not metadata["language"].strip()
                or "\x00" in metadata["language"]
                or len(metadata["language"]) > 256
            ):
                raise StorageIntegrityError(
                    "snapshot BM25 metadata conflicts with its manifest"
                )
            member = member_by_path["documents.json"]
            _read_canonical_documents(
                archive,
                member,
                "documents.json",
                label="snapshot BM25 documents",
                require_source_field=False,
            )

        archive_handle.seek(0)
        _run_zip_archive(
            archive_handle,
            label="snapshot BM25 bundle",
            operation=validate_bm25,
        )
        return

    record = view.intent.generation_record["persistence_config"]
    root_path = record["file"]
    if (
        type(root_path) is not str
        or len(root_path) <= len("config_.json")
        or not root_path.startswith("config_")
        or not root_path.endswith(".json")
        or "/" in root_path
    ):
        raise StorageIntegrityError("snapshot vector root config path is invalid")
    model_suffix = root_path[len("config_") : -len(".json")]
    manifest_entry = view.intent.manifest_entry
    view_config = manifest_entry.get("config")
    if type(view_config) is not dict:
        raise StorageIntegrityError(
            "snapshot vector manifest entry has an invalid config"
        )

    def validate_vector(
        archive: zipfile.ZipFile,
    ) -> tuple[dict[str, int], set[str]]:
        root = _read_zip_json(
            archive,
            member_by_path,
            root_path,
            label="snapshot vector root config",
        )
        try:
            (
                suffix,
                dimension,
                revision,
                metric,
                index_type,
            ) = validate_portable_vector_persistence_semantics(
                root,
                view_config,
            )
            provider = resolve_embedding_artifact_route(
                view_config,
                environ={},
            ).provider
        except (RuntimeError, TypeError, ValueError) as exc:
            raise StorageIntegrityError(
                "snapshot vector root config conflicts with its profile"
            ) from exc
        if (
            suffix != model_suffix
            or type(root.get("persistence_schema")) is not int
            or root["persistence_schema"] != VECTOR_PERSISTENCE_SCHEMA
        ):
            raise StorageIntegrityError(
                "snapshot vector root config has an invalid schema or suffix"
            )
        committed = root.get("level_artifacts")
        if type(committed) is not dict or not set(committed) <= set(_VECTOR_LEVELS):
            raise StorageIntegrityError(
                "snapshot vector root config has invalid committed levels"
            )
        counts: dict[str, int] = {}
        expected_paths = {root_path}
        for level in _VECTOR_LEVELS:
            count = root.get(f"{level}_documents")
            if type(count) is not int or count < 0:
                raise StorageIntegrityError(
                    f"snapshot vector root config has an invalid {level} count"
                )
            counts[level] = count
            artifacts = committed.get(level)
            if count == 0:
                if level in committed:
                    raise StorageIntegrityError(
                        f"snapshot vector root config commits empty {level} artifacts"
                    )
                continue
            if type(artifacts) is not dict or set(artifacts) != {
                "documents",
                "index",
            }:
                raise StorageIntegrityError(
                    f"snapshot vector root config has invalid {level} artifacts"
                )
            documents_name = f"documents_{model_suffix}.json"
            index_name = f"index_{model_suffix}.faiss"
            documents_path = f"{level}/{documents_name}"
            index_path = f"{level}/{index_name}"
            _require_vector_record(
                artifacts["documents"],
                member_by_path,
                relative_path=documents_path,
                expected_file=documents_name,
                label=f"snapshot vector {level} documents",
                max_bytes=MAX_PORTABLE_DOCUMENTS_JSON_BYTES,
            )
            _require_vector_record(
                artifacts["index"],
                member_by_path,
                relative_path=index_path,
                expected_file=index_name,
                label=f"snapshot vector {level} index",
                max_bytes=MAX_PORTABLE_FAISS_INDEX_BYTES,
            )
            observed_count = _read_canonical_documents(
                archive,
                member_by_path[documents_path],
                documents_path,
                label=f"snapshot vector {level} documents",
                require_source_field=True,
            )
            if observed_count != count:
                raise StorageIntegrityError(
                    f"snapshot vector {level} document count conflicts"
                )
            expected_paths.update({documents_path, index_path})
            config_path = f"{level}/config_{model_suffix}.json"
            if config_path in member_by_path:
                level_config = _read_zip_json(
                    archive,
                    member_by_path,
                    config_path,
                    label=f"snapshot vector {level} config",
                )
                _require_level_config(
                    level_config,
                    root=root,
                    level=level,
                    count=count,
                    provider=provider,
                    revision=revision,
                    dimension=dimension,
                    metric=metric,
                    index_type=index_type,
                )
                expected_paths.add(config_path)
        return counts, expected_paths

    archive_handle.seek(0)
    counts, expected_paths = _run_zip_archive(
        archive_handle,
        label="snapshot vector bundle",
        operation=validate_vector,
    )
    observed_counts = view_config.get("document_count")
    positive_counts = {level: count for level, count in counts.items() if count > 0}
    if (
        not positive_counts
        or (
            observed_counts is not None
            and (
                type(observed_counts) is not dict
                or set(observed_counts) != set(positive_counts)
                or any(
                    type(observed_counts[level]) is not int
                    or observed_counts[level] != expected
                    for level, expected in positive_counts.items()
                )
            )
        )
        or set(member_by_path) != expected_paths
    ):
        raise StorageIntegrityError("snapshot vector semantic inventory conflicts")


def _portable_export_manifest_bytes(
    plan: RepoManifestImportPlan,
    *,
    selected: tuple[str, ...],
    max_manifest_bytes: int,
) -> bytes:
    """Emit the canonical selected-view manifest shared by export consumers."""

    export_manifest = json.loads(plan.manifest_json)
    export_manifest["indexes"] = {
        view_type: export_manifest["indexes"][view_type] for view_type in selected
    }
    capabilities = dict(export_manifest["capabilities"])
    capabilities.update(
        {
            "sparse_search": "bm25" in selected,
            "dense_search": "vector" in selected,
            "hybrid_search": {"bm25", "vector"} <= set(selected),
            "symbol_navigation": "symbol_graph" in selected,
        }
    )
    export_manifest["capabilities"] = capabilities
    manifest_bytes = canonical_json(export_manifest).encode("ascii")
    if len(manifest_bytes) > max_manifest_bytes:
        raise StorageIntegrityError(
            f"exported repository manifest exceeds {max_manifest_bytes} bytes"
        )
    try:
        emitted = plan_repo_manifest_import_bytes(
            manifest_bytes,
            required_views=selected,
            max_manifest_bytes=max_manifest_bytes,
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError(
            "exported repository manifest cannot be replanned"
        ) from exc
    if emitted.selection.selected_views != selected:
        raise StorageIntegrityError("exported repository manifest changed its views")
    return manifest_bytes


def _intent_generation_metadata(intent: ViewImportIntent) -> dict[str, Any]:
    """Keep retained generation identity bound to the embedded manifest."""

    metadata = repo_manifest_generation_metadata(intent)
    metadata["manifest_version"] = intent.profile.config["manifest_version"]
    return metadata


def _validate_projection(
    projection: dict[str, Any],
    *,
    retained: _RetainedSnapshot,
    max_manifest_bytes: int,
    max_bundle_files: int,
) -> _ValidatedProjection:
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
    ):
        raise StorageIntegrityError("repository manifest projection shape is invalid")
    if projection["repository_id"] != retained.repository.repository_id:
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
    source = retained.source
    if (
        source.source_kind != "dirty"
        or source.commit_sha is not None
        or source.tree_sha is not None
        or projected_source["source_revision_id"] != source.source_revision_id
        or projected_source["kind"] != "dirty"
        or projected_source["source_fingerprint"] != source.source_fingerprint
        or type(projected_source["file_count"]) is not int
        or projected_source["file_count"] < 0
        or type(projected_source["display_commit"]) is not str
        or projected_source["verification_scope"] != "content-bytes"
        or projected_source["source_verified"] is not True
        or projected_source["commit_verified"] is not False
        or projected_source["checkout_state"] != "not-attested"
    ):
        raise StorageIntegrityError("projected source authority claims are invalid")
    artifact = projection["artifact"]
    if (
        type(artifact) is not dict
        or set(artifact) != {"workspace_plan_digest", "postflight"}
        or type(artifact["workspace_plan_digest"]) is not str
        or _WORKSPACE_PLAN_DIGEST_RE.fullmatch(artifact["workspace_plan_digest"])
        is None
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
    full_manifest_bytes = canonical_json(manifest_value).encode("ascii")
    if len(full_manifest_bytes) > max_manifest_bytes:
        raise StorageIntegrityError(
            f"projected repository manifest exceeds {max_manifest_bytes} bytes"
        )
    try:
        plan = plan_repo_manifest_import_bytes(
            full_manifest_bytes,
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
    projected_views = projection["views"]
    if type(projected_views) is not dict or set(projected_views) != set(selected):
        raise StorageIntegrityError("projected view closure is invalid")
    if set(retained.views) != set(selected) | {REPO_MANIFEST_PROJECTION_VIEW}:
        raise StorageIntegrityError("snapshot retained view closure is invalid")

    projected_items: list[tuple[str, _ProjectedView]] = []
    for intent in plan.views:
        view_type = intent.view_type
        value = projected_views[view_type]
        if type(value) is not dict or set(value) != {
            "generation",
            "generation_record",
            "members",
        }:
            raise StorageIntegrityError(
                f"projected {view_type!r} view shape is invalid"
            )
        catalog_view = retained.views[view_type]
        generation_value = value["generation"]
        expected_generation = {
            "view_generation_id": catalog_view.generation.view_generation_id,
            "schema_version": catalog_view.generation.schema_version,
            "metadata": json.loads(catalog_view.generation.metadata_json),
            "profile": {
                "profile_id": catalog_view.profile.profile_id,
                "name": catalog_view.profile.name,
                "config": catalog_view.profile.config,
            },
            "object": _semantic_object(catalog_view.primary),
        }
        if (
            type(generation_value) is not dict
            or set(generation_value) != set(expected_generation)
            or not same_exact_json(generation_value, expected_generation)
            or catalog_view.generation.schema_version != VIEW_BUNDLE_SCHEMA
            or catalog_view.primary.media_type != VIEW_BUNDLE_MEDIA_TYPE
            or not same_exact_json(value["generation_record"], intent.generation_record)
            or catalog_view.profile.profile_id != intent.profile_id
            or catalog_view.profile.name != intent.profile.name
            or not same_exact_json(catalog_view.profile.config, intent.profile.config)
        ):
            raise StorageIntegrityError(
                f"projected {view_type!r} generation conflicts with the snapshot"
            )
        metadata = json.loads(catalog_view.generation.metadata_json)
        metadata.pop(VIEW_GENERATION_MEMBERS_METADATA_KEY, None)
        if not same_exact_json(metadata, _intent_generation_metadata(intent)):
            raise StorageIntegrityError(
                f"projected {view_type!r} generation metadata conflicts"
            )
        raw_members = value["members"]
        if (
            type(raw_members) is not list
            or not raw_members
            or len(raw_members) > max_bundle_files
        ):
            raise StorageIntegrityError(
                f"projected {view_type!r} member inventory is invalid"
            )
        members: list[ArtifactMember] = []
        member_receipts: list[tuple[str, BlobInfo]] = []
        catalog_members = {record.digest: record for record in catalog_view.members}
        if len(catalog_members) != len(catalog_view.members):
            raise StorageIntegrityError(
                f"catalog {view_type!r} member objects repeat a digest"
            )
        previous = ""
        for raw_member in raw_members:
            member = _artifact_member(
                raw_member,
                view_type=view_type,
            )
            if member.path <= previous:
                raise StorageIntegrityError(
                    f"projected {view_type!r} members are not canonical"
                )
            stored = catalog_members.get(member.digest)
            if (
                stored is None
                or stored.digest != member.digest
                or stored.byte_size != member.byte_size
                or stored.media_type != _MEMBER_MEDIA_TYPE
            ):
                raise StorageIntegrityError(
                    f"projected {view_type!r} member differs from its catalog object"
                )
            members.append(member)
            member_receipts.append((member.path, _receipt(stored)))
            previous = member.path
        member_tuple = tuple(members)
        if tuple(sorted({member.digest for member in member_tuple})) != tuple(
            record.digest for record in catalog_view.members
        ):
            raise StorageIntegrityError(
                f"projected {view_type!r} member reachability conflicts"
            )
        _cross_bind_generation_record(intent, member_tuple)
        projected_items.append(
            (
                view_type,
                _ProjectedView(
                    intent=intent,
                    catalog_view=catalog_view,
                    members=member_tuple,
                    member_receipt_items=tuple(member_receipts),
                ),
            )
        )

    manifest_bytes = _portable_export_manifest_bytes(
        plan,
        selected=selected,
        max_manifest_bytes=max_manifest_bytes,
    )
    return _ValidatedProjection(
        manifest_bytes=manifest_bytes,
        plan=plan,
        skipped_items=plan.selection.skipped_items,
        projection_receipt=BlobInfo("0" * 64, 0, "placeholder"),
        projection_view=retained.views[REPO_MANIFEST_PROJECTION_VIEW],
        view_items=tuple(projected_items),
    )


def _read_and_validate_projection(
    retained: _RetainedSnapshot,
    *,
    object_store: ReceiptVerifyingObjectStore,
    max_manifest_bytes: int,
    max_projection_bytes: int,
    max_bundle_files: int,
) -> _ValidatedProjection:
    projection_view = retained.views.get(REPO_MANIFEST_PROJECTION_VIEW)
    expected_profile = repo_manifest_projection_profile()
    if (
        projection_view is None
        or projection_view.generation.schema_version != REPO_MANIFEST_PROJECTION_SCHEMA
        or projection_view.profile.profile_id != expected_profile.profile_id
        or projection_view.profile.name != expected_profile.name
        or not same_exact_json(projection_view.profile.config, expected_profile.config)
        or projection_view.primary.media_type != REPO_MANIFEST_PROJECTION_MEDIA_TYPE
        or projection_view.primary.byte_size > max_projection_bytes
    ):
        raise StorageIntegrityError(
            "snapshot repository manifest projection generation is invalid"
        )
    payload = _run_open_object(
        object_store,
        projection_view.primary,
        lambda source: _read_exact_bytes(source, projection_view.primary),
    )
    projection = _strict_json_bytes(
        payload,
        label="repository manifest projection",
    )
    validated = _validate_projection(
        projection,
        retained=retained,
        max_manifest_bytes=max_manifest_bytes,
        max_bundle_files=max_bundle_files,
    )
    plan_identity = projection["plan"]["manifest"]
    metadata = json.loads(projection_view.generation.metadata_json)
    metadata.pop(VIEW_GENERATION_MEMBERS_METADATA_KEY, None)
    expected_metadata = {
        "contract": REPO_MANIFEST_PROJECTION_SCHEMA,
        "manifest_version": validated.plan.manifest.version,
        "manifest_digest": plan_identity["digest"],
        "plan_digest": validated.plan.plan_digest,
        "projected_views": list(validated.plan.selection.selected_views),
    }
    dependency_records: dict[str, ObjectRecord] = {}
    for _view_type, view in validated.view_items:
        for record in (view.catalog_view.primary, *view.catalog_view.members):
            existing = dependency_records.get(record.digest)
            if existing is None:
                dependency_records[record.digest] = record
            elif not _same_record(existing, record):
                raise StorageIntegrityError(
                    "snapshot dependency object metadata conflicts"
                )
    dependencies = tuple(sorted(dependency_records))
    projection_dependencies = {
        record.digest: record for record in projection_view.members
    }
    if (
        not same_exact_json(metadata, expected_metadata)
        or projection_view.generation.member_object_digests != dependencies
        or tuple(record.digest for record in projection_view.members) != dependencies
        or set(projection_dependencies) != set(dependency_records)
        or any(
            not _same_record(projection_dependencies[digest], record)
            for digest, record in dependency_records.items()
        )
    ):
        raise StorageIntegrityError(
            "snapshot repository manifest projection reachability conflicts"
        )
    return _ValidatedProjection(
        manifest_bytes=validated.manifest_bytes,
        plan=validated.plan,
        skipped_items=validated.skipped_items,
        projection_receipt=_receipt(projection_view.primary),
        projection_view=projection_view,
        view_items=validated.view_items,
    )


def _verify_bundle(
    object_store: ReceiptVerifyingObjectStore,
    view: _ProjectedView,
    *,
    max_files: int,
    max_bytes: int,
    max_metadata_bytes: int,
) -> BlobInfo:
    record = view.catalog_view.primary
    try:
        validate_view_bundle_physical_size(
            record.byte_size,
            max_files=max_files,
            max_bytes=max_bytes,
            max_metadata_bytes=max_metadata_bytes,
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError(
            f"snapshot {view.intent.view_type!r} bundle exceeds its limits"
        ) from exc

    def consume(source: BinaryIO) -> None:
        def compare(_archive: BinaryIO, observed: ViewBundleRecord) -> None:
            if observed.members != view.members:
                raise StorageIntegrityError(
                    f"snapshot {view.intent.view_type!r} bundle inventory conflicts"
                )
            _require_bundle_semantics(_archive, view)

        consume_verified_view_bundle_stream(
            source,
            compare,
            expected_view_type=view.intent.view_type,
            expected_digest=record.digest,
            expected_size=record.byte_size,
            max_files=max_files,
            max_bytes=max_bytes,
            max_metadata_bytes=max_metadata_bytes,
        )

    _run_open_object(object_store, record, consume)
    return _receipt(record)


def _unique_records(values: Sequence[ObjectRecord]) -> tuple[ObjectRecord, ...]:
    result: dict[str, ObjectRecord] = {}
    for record in values:
        existing = result.get(record.digest)
        if existing is None:
            result[record.digest] = record
        elif not _same_record(existing, record):
            raise StorageIntegrityError(
                "one object digest has conflicting retained metadata"
            )
    return tuple(result[digest] for digest in sorted(result))


def _export_snapshot(
    retained: _RetainedSnapshot,
    *,
    object_store: ReceiptVerifyingObjectStore,
    ref_name: str | None,
    ref_generation: int | None,
    ref_updated_at: str | None,
    max_manifest_bytes: int,
    max_projection_bytes: int,
    max_bundle_files: int,
    max_bundle_bytes: int,
    max_bundle_metadata_bytes: int,
) -> RepoManifestExportResult:
    validated = _read_and_validate_projection(
        retained,
        object_store=object_store,
        max_manifest_bytes=max_manifest_bytes,
        max_projection_bytes=max_projection_bytes,
        max_bundle_files=max_bundle_files,
    )
    receipts: list[RepoManifestViewExportReceipt] = []
    all_records: list[ObjectRecord] = [validated.projection_view.primary]
    all_records.extend(validated.projection_view.members)
    for view_type, view in validated.view_items:
        bundle_receipt = _verify_bundle(
            object_store,
            view,
            max_files=max_bundle_files,
            max_bytes=max_bundle_bytes,
            max_metadata_bytes=max_bundle_metadata_bytes,
        )
        for record in view.catalog_view.members:
            _verify_receipt(object_store, record)
        all_records.append(view.catalog_view.primary)
        all_records.extend(view.catalog_view.members)
        receipts.append(
            RepoManifestViewExportReceipt(
                view_type=view_type,
                view_generation_id=view.catalog_view.generation.view_generation_id,
                bundle_receipt=bundle_receipt,
                member_receipt_items=view.member_receipt_items,
            )
        )
    # Receipts are observations rather than pins. Recheck every unique object
    # at the final boundary immediately before returning the data-only result.
    for record in _unique_records(all_records):
        _verify_receipt(object_store, record)
    manifest_digest = hashlib.sha256(validated.manifest_bytes).hexdigest()
    receipt = RepoManifestExportReceipt(
        namespace_id=retained.namespace.namespace_id,
        repository_id=retained.repository.repository_id,
        repository_key=retained.repository.repository_key,
        source_revision_id=retained.source.source_revision_id,
        snapshot_id=retained.snapshot.snapshot_id,
        snapshot_published_at=retained.published_at,
        ref_name=ref_name,
        ref_generation=ref_generation,
        ref_updated_at=ref_updated_at,
        manifest_digest=manifest_digest,
        manifest_byte_size=len(validated.manifest_bytes),
        projection_generation_id=validated.projection_view.generation.view_generation_id,
        projection_receipt=validated.projection_receipt,
        view_receipts=tuple(receipts),
        skipped_items=validated.skipped_items,
    )
    return RepoManifestExportResult(validated.manifest_bytes, receipt)


def _preflight(
    *,
    catalog: object,
    object_store: object,
    max_manifest_bytes: object,
    max_projection_bytes: object,
    max_bundle_files: object,
    max_bundle_bytes: object,
    max_bundle_metadata_bytes: object,
) -> tuple[int, int, int, int, int]:
    if not isinstance(catalog, RetainedSnapshotCatalog):
        raise TypeError("retained manifest export catalog lacks required capabilities")
    if not isinstance(object_store, ReceiptVerifyingObjectStore):
        raise TypeError(
            "retained manifest export object store lacks required capabilities"
        )
    _require_static_methods(
        catalog,
        label="retained manifest export catalog",
        names=_CATALOG_METHODS,
    )
    _require_static_methods(
        object_store,
        label="retained manifest export object store",
        names=_OBJECT_STORE_METHODS,
    )
    limits = (
        _positive_limit(max_manifest_bytes, "manifest byte limit"),
        _positive_limit(max_projection_bytes, "projection byte limit"),
        _positive_limit(max_bundle_files, "bundle file limit"),
        _positive_limit(max_bundle_bytes, "bundle byte limit"),
        _positive_limit(max_bundle_metadata_bytes, "bundle metadata byte limit"),
    )
    contract = catalog.retained_import_contract()  # type: ignore[union-attr]
    if type(contract) is not str or contract != RETAINED_IMPORT_CATALOG_CONTRACT:
        raise TypeError("retained manifest export catalog contract is incompatible")
    return limits


def export_retained_repo_manifest_ref(
    repository_id: str,
    *,
    catalog: RetainedSnapshotCatalog,
    object_store: ReceiptVerifyingObjectStore,
    ref_name: str = DEFAULT_REF_NAME,
    expected_generation: int | None = None,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_projection_bytes: int = DEFAULT_MAX_PROJECTION_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
) -> RepoManifestExportResult:
    """Resolve one ref once and export its immutable snapshot as v1.1 bytes."""

    repository_id = _exact_text(repository_id, "export repository ID")
    ref_name = _exact_text(ref_name, "export ref name")
    expected_generation = _optional_generation(expected_generation)
    (
        manifest_limit,
        projection_limit,
        bundle_files,
        bundle_bytes,
        bundle_metadata,
    ) = _preflight(
        catalog=catalog,
        object_store=object_store,
        max_manifest_bytes=max_manifest_bytes,
        max_projection_bytes=max_projection_bytes,
        max_bundle_files=max_bundle_files,
        max_bundle_bytes=max_bundle_bytes,
        max_bundle_metadata_bytes=max_bundle_metadata_bytes,
    )
    raw = catalog.resolve_ref(repository_id, ref_name)
    # An expected-generation mismatch is a caller-visible optimistic-lock
    # conflict, not a request to authenticate or read the resolved snapshot.
    # Inspect only the exact outer built-in mapping before detaching its
    # potentially large manifest so this precondition cannot trigger CAS I/O.
    if type(raw) is not dict:
        raise StorageIntegrityError("resolved retained manifest ref shape is invalid")
    raw_generation = raw.get("generation")
    raw_repository_id = raw.get("repository_id")
    raw_ref_name = raw.get("ref_name")
    if (
        type(raw_generation) is not int
        or not 0 < raw_generation <= _INT64_MAX
        or type(raw_repository_id) is not str
        or raw_repository_id != repository_id
        or type(raw_ref_name) is not str
        or raw_ref_name != ref_name
    ):
        raise StorageIntegrityError("resolved retained manifest ref is invalid")
    if expected_generation is not None and raw_generation != expected_generation:
        raise PublishConflict(
            "resolved repository ref generation differs from expected_generation"
        )
    resolution = snapshot_retained_import_response(
        raw,
        label="resolved retained manifest ref",
    )
    required = {
        "repository_id",
        "ref_name",
        "snapshot_id",
        "generation",
        "updated_at",
        "manifest",
    }
    if type(resolution) is not dict or not required <= set(resolution):
        raise StorageIntegrityError("resolved retained manifest ref shape is invalid")
    generation = resolution["generation"]
    if (
        type(generation) is not int
        or not 0 < generation <= _INT64_MAX
        or generation != raw_generation
        or resolution["repository_id"] != repository_id
        or resolution["ref_name"] != ref_name
        or type(resolution["snapshot_id"]) is not str
    ):
        raise StorageIntegrityError("resolved retained manifest ref is invalid")
    try:
        updated_at = canonical_utc_timestamp(
            resolution["updated_at"],
            "resolved retained manifest ref updated_at",
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError(
            "resolved retained manifest ref is invalid"
        ) from exc
    retained = attest_detached_retained_snapshot(
        resolution["manifest"],
        label="resolved retained manifest snapshot",
        expected_repository_id=repository_id,
        expected_snapshot_id=resolution["snapshot_id"],
    )
    return _export_snapshot(
        retained,
        object_store=object_store,
        ref_name=ref_name,
        ref_generation=generation,
        ref_updated_at=updated_at,
        max_manifest_bytes=manifest_limit,
        max_projection_bytes=projection_limit,
        max_bundle_files=bundle_files,
        max_bundle_bytes=bundle_bytes,
        max_bundle_metadata_bytes=bundle_metadata,
    )


def export_retained_repo_manifest_snapshot(
    repository_id: str,
    snapshot_id: str,
    *,
    catalog: RetainedSnapshotCatalog,
    object_store: ReceiptVerifyingObjectStore,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_projection_bytes: int = DEFAULT_MAX_PROJECTION_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
) -> RepoManifestExportResult:
    """Export one explicit immutable snapshot without resolving a ref."""

    repository_id = _exact_text(repository_id, "export repository ID")
    snapshot_id = _exact_text(snapshot_id, "export snapshot ID")
    (
        manifest_limit,
        projection_limit,
        bundle_files,
        bundle_bytes,
        bundle_metadata,
    ) = _preflight(
        catalog=catalog,
        object_store=object_store,
        max_manifest_bytes=max_manifest_bytes,
        max_projection_bytes=max_projection_bytes,
        max_bundle_files=max_bundle_files,
        max_bundle_bytes=max_bundle_bytes,
        max_bundle_metadata_bytes=max_bundle_metadata_bytes,
    )
    raw = catalog.get_manifest_summary(snapshot_id)
    summary = snapshot_retained_import_response(
        raw,
        label="retained manifest snapshot summary",
    )
    retained = attest_detached_retained_snapshot(
        summary,
        label="retained manifest snapshot summary",
        expected_repository_id=repository_id,
        expected_snapshot_id=snapshot_id,
    )
    return _export_snapshot(
        retained,
        object_store=object_store,
        ref_name=None,
        ref_generation=None,
        ref_updated_at=None,
        max_manifest_bytes=manifest_limit,
        max_projection_bytes=projection_limit,
        max_bundle_files=bundle_files,
        max_bundle_bytes=bundle_bytes,
        max_bundle_metadata_bytes=bundle_metadata,
    )


__all__ = [
    "RepoManifestExportReceipt",
    "RepoManifestExportResult",
    "RepoManifestViewExportReceipt",
    "export_retained_repo_manifest_ref",
    "export_retained_repo_manifest_snapshot",
]
