# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Retained RepoManifest import into immutable catalog snapshots.

This module is the authority-bearing counterpart to
``codenib.compiler.manifest_storage``.  The planner remains pure and inert;
this adapter accepts a retained strict-workspace receipt plus an independently
retained repository-source binding, validates every selected portable view,
streams immutable objects into CAS, and publishes the resulting snapshot/ref.

The API is intentionally one-shot.  No caller-visible "prepared" value can be
forged or published before the retained workspace callback and its namespace
postflight have completed.  Object-store writes may leave unreachable objects
after a failure, but catalog writes cannot begin until all artifact authority
checks have returned successfully, and the ref update remains the catalog's
last operation.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import struct
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from .._atomic_directory import PublicationDirectoryReader
from .._captured_directory import (
    PublishedWorkspaceReceipt,
    PublishedWorkspaceReceiptOwner,
    WorkspaceDirectory,
    WorkspaceFile,
    WorkspacePlan,
)
from ..artifacts.portable_views import validate_content_bound_portable_query_view_reader
from ..artifacts.runtime import verify_context_artifact_reader
from ..source_fingerprint import RepositorySourceBinding
from ..storage.cas import BlobInfo
from ..storage.models import (
    DEFAULT_NAMESPACE_NAME,
    VIEW_GENERATION_MEMBERS_METADATA_KEY,
    NamespaceIdentity,
    ObjectRecord,
    PublishConflict,
    PublishedSnapshot,
    RepositoryIdentity,
    SnapshotView,
    SourceRevision,
    StorageIntegrityError,
    StorageValidationError,
    ViewGeneration,
    ViewProfile,
    canonical_json,
    canonical_utc_timestamp,
)
from ..storage.protocols import (
    RETAINED_IMPORT_CATALOG_CONTRACT,
    RetainedImportCatalog,
    RetainedImportObjectStore,
    snapshot_retained_import_response,
)
from ..storage.view_bundle import (
    DEFAULT_MAX_BUNDLE_BYTES,
    DEFAULT_MAX_BUNDLE_FILES,
    DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    VIEW_BUNDLE_MEDIA_TYPE,
    VIEW_BUNDLE_SCHEMA,
    PlannedViewBundle,
    consume_planned_view_bundle,
    plan_view_bundle_reader,
)
from .manifest import MANIFEST_VERSION
from .manifest_storage import (
    RepoManifestImportPlan,
    SourceIntent,
    ViewImportIntent,
    ViewSelection,
)

REPO_MANIFEST_IMPORT_GENERATION_CONTRACT = "codenib.repo-manifest-import-generation.v2"
REPO_MANIFEST_PROJECTION_VIEW = "codenib.internal.repo-manifest"
REPO_MANIFEST_PROJECTION_SCHEMA = "codenib.internal.repo-manifest.v2"
REPO_MANIFEST_PROJECTION_MEDIA_TYPE = (
    "application/vnd.codenib.repo-manifest-projection.v2+json"
)
REPO_MANIFEST_PROJECTION_PROFILE_NAME = "canonical"

DEFAULT_REF_NAME = "main"
DEFAULT_MAX_CONTEXT_FILES = 100_000
DEFAULT_MAX_CONTEXT_BYTES = 64 * 1024 * 1024 * 1024
DEFAULT_MAX_PROJECTION_BYTES = 16 * 1024 * 1024

_MAX_TEXT = 32_768
_MAX_ENVIRONMENT_ITEMS = 4_096
_MAX_FORBIDDEN_PATHS = 256
_INT64_MAX = 9_223_372_036_854_775_807
_MEMBER_MEDIA_TYPE = "application/octet-stream"
_MISSING = object()
_SNAPSHOT_ID_RE = re.compile(r"snapshot_[0-9a-f]{64}\Z", re.ASCII)

_OBJECT_STORE_METHODS = (
    "put_bytes",
    "put_file",
    "has",
    "open",
    "read_bytes",
    "verify",
    "materialize",
    "put_chunks",
    "verify_receipt",
)
_CATALOG_METHODS = (
    "retained_import_contract",
    "create_namespace",
    "create_repository",
    "create_source_revision",
    "create_view_profile",
    "register_object",
    "stage_view_generation",
    "publish_snapshot",
    "resolve_ref",
    "get_manifest_summary",
)


@dataclass(frozen=True, slots=True)
class RepoManifestImportResult:
    """One immutable snapshot publication result."""

    repository_id: str
    source_revision_id: str
    snapshot_id: str
    ref_name: str
    generation: int
    changed: bool
    views: tuple[str, ...]
    skipped_items: tuple[tuple[str, str], ...]
    view_generation_items: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if type(self) is not RepoManifestImportResult:
            raise StorageValidationError(
                "manifest import result must use the exact result type"
            )
        for value, label in (
            (self.repository_id, "repository ID"),
            (self.source_revision_id, "source revision ID"),
            (self.snapshot_id, "snapshot ID"),
            (self.ref_name, "ref name"),
        ):
            _exact_text(value, label)
        if type(self.generation) is not int or not 0 < self.generation <= _INT64_MAX:
            raise StorageValidationError("published generation is invalid")
        if type(self.changed) is not bool:
            raise StorageValidationError("published changed flag is invalid")
        _validate_result_items(self)

    @property
    def skipped_views(self) -> Mapping[str, str]:
        return MappingProxyType(dict(self.skipped_items))

    @property
    def view_generation_ids(self) -> Mapping[str, str]:
        return MappingProxyType(dict(self.view_generation_items))


@dataclass(frozen=True, slots=True)
class _StoredObject:
    receipt: BlobInfo
    record: ObjectRecord


@dataclass(frozen=True, slots=True)
class _PreparedView:
    intent: ViewImportIntent
    bundle: _StoredObject
    members: tuple[tuple[str, int, int, _StoredObject], ...]
    generation: ViewGeneration


@dataclass(frozen=True, slots=True)
class _PreparedImport:
    artifact_plan_digest: str
    views: tuple[_PreparedView, ...]
    projection: _StoredObject
    projection_profile: ViewProfile
    projection_generation: ViewGeneration

    @property
    def stored_objects(self) -> tuple[_StoredObject, ...]:
        values: list[_StoredObject] = []
        for view in self.views:
            values.append(view.bundle)
            values.extend(member[3] for member in view.members)
        values.append(self.projection)
        return _unique_stored_objects(values)


def _validate_result_items(result: RepoManifestImportResult) -> None:
    if type(result.views) is not tuple or not result.views:
        raise StorageValidationError("published views must be a nonempty tuple")
    if any(type(value) is not str for value in result.views):
        raise StorageValidationError("published view names must be exact strings")
    if result.views != tuple(sorted(set(result.views))):
        raise StorageValidationError("published view names must be canonical")
    if type(result.skipped_items) is not tuple:
        raise StorageValidationError("skipped views must be an exact tuple")
    if type(result.view_generation_items) is not tuple:
        raise StorageValidationError("view generations must be an exact tuple")
    for items, label in (
        (result.skipped_items, "skipped view"),
        (result.view_generation_items, "view generation"),
    ):
        for item in items:
            if (
                type(item) is not tuple
                or len(item) != 2
                or any(type(value) is not str for value in item)
            ):
                raise StorageValidationError(f"{label} entries are invalid")
            _exact_text(item[0], f"{label} name")
            _exact_text(item[1], f"{label} value")
        if items != tuple(sorted(items)) or len({item[0] for item in items}) != len(
            items
        ):
            raise StorageValidationError(f"{label} entries are not canonical")


def _exact_text(value: object, label: str, *, max_length: int = _MAX_TEXT) -> str:
    if type(value) is not str:
        raise StorageValidationError(f"{label} must be exact text")
    if len(value) > max_length:
        raise StorageValidationError(f"{label} exceeds {max_length} characters")
    if not value or value != value.strip() or "\x00" in value:
        raise StorageValidationError(f"{label} is not canonical")
    return value


def _exact_backend_id(value: object, expected: str, label: str) -> None:
    if type(value) is not str or value != expected:
        raise StorageIntegrityError(f"catalog returned a different {label}")


def _snapshot_backend_json(value: object, *, label: str) -> Any:
    """Detach one bounded exact-JSON backend response without virtual equality."""
    return snapshot_retained_import_response(value, label=label)


def _same_exact_json(left: object, right: object) -> bool:
    """Compare detached JSON without Python's bool/int/float coercions."""

    if type(left) is not type(right):
        return False
    if type(left) is dict:
        if left.keys() != right.keys():  # type: ignore[union-attr]
            return False
        return all(
            _same_exact_json(value, right[key])  # type: ignore[index]
            for key, value in left.items()  # type: ignore[union-attr]
        )
    if type(left) is list:
        if len(left) != len(right):  # type: ignore[arg-type]
            return False
        return all(
            _same_exact_json(left_value, right_value)
            for left_value, right_value in zip(  # type: ignore[arg-type]
                left, right, strict=True
            )
        )
    if type(left) is float:
        return struct.pack(">d", left) == struct.pack(">d", right)  # type: ignore[arg-type]
    return left == right


def _has_exact_json_core(observed: object, expected: object) -> bool:
    """Match identity fields exactly while allowing additive object fields."""

    if type(observed) is not type(expected):
        return False
    if type(expected) is dict:
        return all(
            key in observed  # type: ignore[operator]
            and _has_exact_json_core(observed[key], value)  # type: ignore[index]
            for key, value in expected.items()  # type: ignore[union-attr]
        )
    if type(expected) is list:
        if len(observed) != len(expected):  # type: ignore[arg-type]
            return False
        return all(
            _has_exact_json_core(observed_value, expected_value)
            for observed_value, expected_value in zip(  # type: ignore[arg-type]
                observed,
                expected,
                strict=True,
            )
        )
    return _same_exact_json(observed, expected)


def _positive_limit(value: object, label: str) -> int:
    if type(value) is not int or not 0 < value <= (64 * 1024 * 1024 * 1024):
        raise StorageValidationError(
            f"{label} must be a positive integer no larger than 64 GiB"
        )
    return value


def _expected_generation(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _INT64_MAX:
        raise StorageValidationError(
            "expected ref generation must be a non-negative signed 64-bit integer"
        )
    return value


def _require_static_methods(value: object, *, label: str, names: Sequence[str]) -> None:
    if value is None:
        raise TypeError(f"{label} is required")
    for name in names:
        candidate = inspect.getattr_static(value, name, _MISSING)
        if candidate is _MISSING or not callable(candidate):
            raise TypeError(f"{label} must provide {name}()")


def _snapshot_environment(value: Mapping[str, str] | None) -> Mapping[str, str]:
    if value is None:
        value = os.environ
    if not isinstance(value, Mapping):
        raise TypeError("manifest import environment must be a mapping")
    try:
        iterator = iter(value.items())
        snapshot: dict[str, str] = {}
        for index in range(_MAX_ENVIRONMENT_ITEMS + 1):
            try:
                item = next(iterator)
            except StopIteration:
                break
            if index == _MAX_ENVIRONMENT_ITEMS:
                raise StorageValidationError("manifest import environment is too large")
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
            ):
                raise StorageValidationError(
                    "manifest import environment must contain exact text pairs"
                )
            key = _exact_text(item[0], "environment variable", max_length=4_096)
            if key in snapshot:
                raise StorageValidationError(
                    "manifest import environment repeats a variable"
                )
            if len(item[1]) > _MAX_TEXT or "\x00" in item[1]:
                raise StorageValidationError(
                    "manifest import environment value is invalid"
                )
            snapshot[key] = item[1]
    except StorageValidationError:
        raise
    except Exception as exc:
        raise StorageValidationError(
            "manifest import environment could not be snapshotted"
        ) from exc
    return MappingProxyType(snapshot)


def _snapshot_forbidden_paths(values: Iterable[Path]) -> tuple[Path, ...]:
    try:
        iterator = iter(values)
        result: list[Path] = []
        for index in range(_MAX_FORBIDDEN_PATHS + 1):
            try:
                value = next(iterator)
            except StopIteration:
                break
            if index == _MAX_FORBIDDEN_PATHS:
                raise StorageValidationError("too many forbidden paths")
            if not isinstance(value, Path):
                raise TypeError("manifest import forbidden paths must be Path values")
            result.append(value)
    except (StorageValidationError, TypeError):
        raise
    except Exception as exc:
        raise StorageValidationError(
            "manifest import forbidden paths could not be snapshotted"
        ) from exc
    return tuple(result)


def _snapshot_plan(plan: RepoManifestImportPlan) -> RepoManifestImportPlan:
    """Detach every public plan value before any authority/backend work."""

    try:
        RepoManifestImportPlan.__post_init__(plan)
        source = SourceIntent(
            fingerprint=plan.source.fingerprint,
            fingerprint_version=plan.source.fingerprint_version,
            file_count=plan.source.file_count,
            display_commit=plan.source.display_commit,
        )
        selection = ViewSelection(
            required_views=tuple(plan.selection.required_views),
            optional_views=tuple(plan.selection.optional_views),
            selected_views=tuple(plan.selection.selected_views),
            skipped_items=tuple(
                (name, reason) for name, reason in plan.selection.skipped_items
            ),
        )
        views = tuple(
            ViewImportIntent(
                view_type=view.view_type,
                required=view.required,
                manifest_entry_json=view.manifest_entry_json,
                profile=ViewProfile(
                    view_type=view.profile.view_type,
                    name=view.profile.name,
                    config_json=view.profile.config_json,
                ),
                generation_record_json=view.generation_record_json,
            )
            for view in plan.views
        )
        return RepoManifestImportPlan(
            manifest_json=plan.manifest_json,
            source=source,
            selection=selection,
            views=views,
        )
    except StorageValidationError:
        raise
    except Exception as exc:
        raise StorageValidationError(
            "manifest import plan could not be snapshotted"
        ) from exc


def _snapshot_workspace_plan_digest(receipt: PublishedWorkspaceReceipt) -> str:
    plan = receipt.plan
    if type(plan) is not WorkspacePlan:
        raise StorageIntegrityError("retained workspace plan has an invalid type")
    if (
        type(plan.subject_digest) is not str
        or type(plan.digest) is not str
        or type(plan.root_mode) is not int
        or type(plan.directories) is not tuple
        or type(plan.files) is not tuple
    ):
        raise StorageIntegrityError("retained workspace plan fields are not exact")
    directories: list[WorkspaceDirectory] = []
    files: list[WorkspaceFile] = []
    try:
        for directory in plan.directories:
            if (
                type(directory) is not WorkspaceDirectory
                or type(directory.path) is not PurePosixPath
                or type(directory.mode) is not int
            ):
                raise StorageIntegrityError(
                    "retained workspace directory fields are not exact"
                )
            directories.append(
                WorkspaceDirectory(
                    PurePosixPath(directory.path.as_posix()),
                    mode=directory.mode,
                )
            )
        for file in plan.files:
            if (
                type(file) is not WorkspaceFile
                or type(file.path) is not PurePosixPath
                or type(file.mode) is not int
                or type(file.max_bytes) is not int
            ):
                raise StorageIntegrityError(
                    "retained workspace file fields are not exact"
                )
            files.append(
                WorkspaceFile(
                    PurePosixPath(file.path.as_posix()),
                    mode=file.mode,
                    max_bytes=file.max_bytes,
                )
            )
        detached = WorkspacePlan(
            subject_digest=plan.subject_digest,
            directories=tuple(directories),
            files=tuple(files),
            root_mode=plan.root_mode,
        )
    except StorageIntegrityError:
        raise
    except Exception as exc:
        raise StorageIntegrityError("retained workspace plan is invalid") from exc
    if detached.digest != plan.digest:
        raise StorageIntegrityError("retained workspace plan digest is inconsistent")
    return detached.digest


def _expected_namespace_id(name: str) -> str:
    return NamespaceIdentity(name).namespace_id


def _projection_profile() -> ViewProfile:
    return ViewProfile.create(
        REPO_MANIFEST_PROJECTION_VIEW,
        {
            "contract": REPO_MANIFEST_PROJECTION_SCHEMA,
            "builder_schema": REPO_MANIFEST_PROJECTION_SCHEMA,
            "artifact_schema": REPO_MANIFEST_PROJECTION_SCHEMA,
        },
        name=REPO_MANIFEST_PROJECTION_PROFILE_NAME,
    )


def _generation_metadata(intent: ViewImportIntent) -> dict[str, Any]:
    return {
        "contract": REPO_MANIFEST_IMPORT_GENERATION_CONTRACT,
        "manifest_version": MANIFEST_VERSION,
        "generation_record_digest": intent.generation_record_digest,
        "verification_scope": "content-bytes",
        "native_execution": (
            "inert" if intent.view_type == "vector" else "not-required"
        ),
    }


def _exact_blob_object(
    receipt: object,
    *,
    expected_digest: str,
    expected_size: int,
    media_type: str,
    label: str,
) -> _StoredObject:
    if type(receipt) is not BlobInfo:
        raise StorageIntegrityError(f"{label} returned a non-exact BlobInfo")
    if (
        type(receipt.digest) is not str
        or type(receipt.byte_size) is not int
        or type(receipt.storage_key) is not str
    ):
        raise StorageIntegrityError(f"{label} receipt fields are not exact")
    if receipt.digest != expected_digest or receipt.byte_size != expected_size:
        raise StorageIntegrityError(f"{label} receipt identity is inconsistent")
    try:
        record = ObjectRecord(
            digest=receipt.digest,
            byte_size=receipt.byte_size,
            storage_key=receipt.storage_key,
            media_type=media_type,
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError(f"{label} receipt is not canonical") from exc
    if (
        record.digest != receipt.digest
        or record.byte_size != receipt.byte_size
        or record.storage_key != receipt.storage_key
        or record.media_type != media_type
    ):
        raise StorageIntegrityError(f"{label} receipt is not canonical")
    return _StoredObject(receipt=receipt, record=record)


def _same_object(left: ObjectRecord, right: ObjectRecord) -> bool:
    return (
        left.digest == right.digest
        and left.byte_size == right.byte_size
        and left.storage_key == right.storage_key
        and left.media_type == right.media_type
    )


def _unique_stored_objects(
    values: Iterable[_StoredObject],
) -> tuple[_StoredObject, ...]:
    by_digest: dict[str, _StoredObject] = {}
    for value in values:
        existing = by_digest.get(value.record.digest)
        if existing is None:
            by_digest[value.record.digest] = value
        elif not _same_object(existing.record, value.record):
            raise StorageIntegrityError(
                "one object digest produced conflicting immutable metadata"
            )
    return tuple(by_digest[digest] for digest in sorted(by_digest))


def _verify_exact_receipt(
    object_store: RetainedImportObjectStore,
    stored: _StoredObject,
) -> None:
    observed = object_store.verify_receipt(stored.receipt)  # type: ignore[attr-defined]
    verified = _exact_blob_object(
        observed,
        expected_digest=stored.record.digest,
        expected_size=stored.record.byte_size,
        media_type=stored.record.media_type,
        label="object-store receipt verification",
    )
    if verified.receipt.storage_key != stored.receipt.storage_key:
        raise StorageIntegrityError("object-store receipt storage key changed")


def _semantic_object(record: ObjectRecord) -> dict[str, Any]:
    return {
        "digest": record.digest,
        "byte_size": record.byte_size,
        "media_type": record.media_type,
    }


def _portable_projection_manifest(
    plan: RepoManifestImportPlan,
) -> dict[str, Any]:
    manifest = json.loads(plan.manifest_json)
    if type(manifest) is not dict:
        raise StorageIntegrityError("planned manifest is not an object")
    manifest["repo"]["path"] = "source"
    return manifest


def _projection_value(
    plan: RepoManifestImportPlan,
    *,
    artifact_plan_digest: str,
    repository_id: str,
    source: SourceRevision,
    views: Sequence[_PreparedView],
) -> dict[str, Any]:
    return {
        "schema": REPO_MANIFEST_PROJECTION_SCHEMA,
        "repository_id": repository_id,
        "source": {
            "source_revision_id": source.source_revision_id,
            "kind": source.source_kind,
            "source_fingerprint": source.source_fingerprint,
            "file_count": plan.source.file_count,
            "display_commit": plan.source.display_commit,
            "verification_scope": "content-bytes",
            "source_verified": True,
            "commit_verified": False,
            "checkout_state": "not-attested",
        },
        "artifact": {
            "workspace_plan_digest": artifact_plan_digest,
            "postflight": "completed-before-catalog",
        },
        "plan": {**plan.to_dict(), "digest": plan.plan_digest},
        "selection": plan.selection.to_dict(),
        "manifest": _portable_projection_manifest(plan),
        "views": {
            view.intent.view_type: {
                "generation": {
                    "view_generation_id": view.generation.view_generation_id,
                    "schema_version": view.generation.schema_version,
                    "metadata": json.loads(view.generation.metadata_json),
                    "profile": {
                        "profile_id": view.intent.profile_id,
                        "name": view.intent.profile.name,
                        "config": view.intent.profile.config,
                    },
                    "object": _semantic_object(view.bundle.record),
                },
                "generation_record": view.intent.generation_record,
                "members": [
                    {
                        "path": path,
                        "digest": stored.record.digest,
                        "byte_size": byte_size,
                        "mode": mode,
                        "object": _semantic_object(stored.record),
                    }
                    for path, byte_size, mode, stored in view.members
                ],
            }
            for view in views
        },
    }


def _iter_canonical_json_chunks(
    value: Mapping[str, Any],
    *,
    max_bytes: int,
) -> Iterator[bytes]:
    encoder = json.JSONEncoder(
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    observed = 0
    try:
        for text in encoder.iterencode(value):
            payload = text.encode("ascii")
            observed += len(payload)
            if observed > max_bytes:
                raise StorageValidationError(
                    f"repository manifest projection exceeds {max_bytes} bytes"
                )
            yield payload
    except StorageValidationError:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise StorageIntegrityError(
            "repository manifest projection is not canonical JSON"
        ) from exc


def _measure_canonical_json(
    value: Mapping[str, Any],
    *,
    max_bytes: int,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in _iter_canonical_json_chunks(value, max_bytes=max_bytes):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _validate_context_matches_plan(
    plan: RepoManifestImportPlan,
    *,
    verified: object,
    repository_key: str,
) -> None:
    if (
        verified.repository != repository_key  # type: ignore[attr-defined]
        or verified.commit != plan.source.display_commit  # type: ignore[attr-defined]
        or verified.source_fingerprint != plan.source.fingerprint  # type: ignore[attr-defined]
        or not set(plan.selection.selected_views).issubset(  # type: ignore[attr-defined]
            verified.views
        )
        or verified.manifest.file_count != plan.source.file_count  # type: ignore[attr-defined]
        or canonical_json(verified.manifest.to_dict()) != plan.manifest_json  # type: ignore[attr-defined]
    ):
        raise StorageIntegrityError(
            "retained context artifact does not match its manifest import plan"
        )


def _drain(chunks: Iterable[bytes]) -> None:
    for _chunk in chunks:
        pass


def _prepare_import_inside_authority(
    receipt: PublishedWorkspaceReceipt,
    publication: PublicationDirectoryReader,
    *,
    plan: RepoManifestImportPlan,
    repository_source: RepositorySourceBinding,
    repository_key: str,
    source_identity: SourceRevision,
    object_store: RetainedImportObjectStore,
    environment: Mapping[str, str],
    forbidden_paths: tuple[Path, ...],
    max_context_files: int,
    max_context_bytes: int,
    max_bundle_files: int,
    max_bundle_bytes: int,
    max_bundle_metadata_bytes: int,
    max_projection_bytes: int,
) -> _PreparedImport:
    if type(receipt) is not PublishedWorkspaceReceipt:
        raise TypeError("retained context receipt has an invalid type")
    if type(publication) is not PublicationDirectoryReader:
        raise TypeError("retained context reader has an invalid type")
    artifact_plan_digest = _snapshot_workspace_plan_digest(receipt)
    verified = verify_context_artifact_reader(
        publication,
        expected_repository=repository_key,
        expected_commit=plan.source.display_commit,
        max_files=max_context_files,
        max_bytes=max_context_bytes,
    )
    _validate_context_matches_plan(
        plan,
        verified=verified,
        repository_key=repository_key,
    )

    planned: list[
        tuple[ViewImportIntent, PublicationDirectoryReader, PlannedViewBundle]
    ] = []
    # Every selected view is semantically validated and fully bundle-planned
    # before the first object-store producer is invoked.
    for intent in plan.views:
        prefix = PurePosixPath("views") / intent.view_type
        view_reader = publication.subtree(prefix)
        validate_content_bound_portable_query_view_reader(
            view_reader,
            repository_source=repository_source,
            view_type=intent.view_type,
            view_config=intent.manifest_entry["config"],
            forbidden_paths=forbidden_paths,
            environ=environment,
        )
        source_ownership = view_reader.capture_ownership()
        bundle = plan_view_bundle_reader(
            publication,
            prefix,
            source_ownership,
            view_type=intent.view_type,
            max_files=max_bundle_files,
            max_bytes=max_bundle_bytes,
            max_metadata_bytes=max_bundle_metadata_bytes,
        )
        planned.append((intent, view_reader, bundle))

    prepared_views: list[_PreparedView] = []
    stored_by_digest: dict[str, _StoredObject] = {}
    for intent, view_reader, bundle in planned:
        raw_bundle = consume_planned_view_bundle(
            publication,
            bundle,
            lambda chunks, bundle=bundle: object_store.put_chunks(  # type: ignore[attr-defined]
                chunks,
                bundle.digest,
                bundle.byte_size,
            ),
        )
        bundle_object = _exact_blob_object(
            raw_bundle,
            expected_digest=bundle.digest,
            expected_size=bundle.byte_size,
            media_type=VIEW_BUNDLE_MEDIA_TYPE,
            label=f"portable {intent.view_type} bundle ingestion",
        )
        existing_bundle = stored_by_digest.get(bundle_object.record.digest)
        if existing_bundle is not None and not _same_object(
            existing_bundle.record, bundle_object.record
        ):
            raise StorageIntegrityError("bundle object metadata conflicts")
        stored_by_digest[bundle_object.record.digest] = bundle_object

        members: list[tuple[str, int, int, _StoredObject]] = []
        for member in bundle.members:
            existing = stored_by_digest.get(member.digest)
            with view_reader.iter_authenticated_chunks(
                member.path,
                max_bytes=member.byte_size,
            ) as chunks:
                if existing is None:
                    raw_member = object_store.put_chunks(  # type: ignore[attr-defined]
                        chunks,
                        member.digest,
                        member.byte_size,
                    )
                    stored = _exact_blob_object(
                        raw_member,
                        expected_digest=member.digest,
                        expected_size=member.byte_size,
                        media_type=_MEMBER_MEDIA_TYPE,
                        label=f"portable {intent.view_type} member ingestion",
                    )
                    stored_by_digest[stored.record.digest] = stored
                else:
                    _drain(chunks)
                    if (
                        existing.record.byte_size != member.byte_size
                        or existing.record.media_type != _MEMBER_MEDIA_TYPE
                    ):
                        raise StorageIntegrityError(
                            "duplicate member digest has conflicting metadata"
                        )
                    stored = existing
            members.append((member.path, member.byte_size, member.mode, stored))

        member_digests = tuple(sorted({item[3].record.digest for item in members}))
        generation = ViewGeneration.create(
            source_identity,
            intent.profile,
            bundle_object.record,
            schema_version=VIEW_BUNDLE_SCHEMA,
            metadata=_generation_metadata(intent),
            member_object_digests=member_digests,
        )
        prepared_views.append(
            _PreparedView(
                intent=intent,
                bundle=bundle_object,
                members=tuple(members),
                generation=generation,
            )
        )

    projection_profile = _projection_profile()
    projection_value = _projection_value(
        plan,
        artifact_plan_digest=artifact_plan_digest,
        repository_id=source_identity.repository_id,
        source=source_identity,
        views=prepared_views,
    )
    projection_digest, projection_size = _measure_canonical_json(
        projection_value,
        max_bytes=max_projection_bytes,
    )
    raw_projection = object_store.put_chunks(  # type: ignore[attr-defined]
        _iter_canonical_json_chunks(
            projection_value,
            max_bytes=max_projection_bytes,
        ),
        projection_digest,
        projection_size,
    )
    projection_object = _exact_blob_object(
        raw_projection,
        expected_digest=projection_digest,
        expected_size=projection_size,
        media_type=REPO_MANIFEST_PROJECTION_MEDIA_TYPE,
        label="repository manifest projection ingestion",
    )
    dependencies = tuple(
        sorted(
            {
                dependency.record.digest
                for view in prepared_views
                for dependency in (view.bundle, *(member[3] for member in view.members))
            }
        )
    )
    projection_metadata = {
        "contract": REPO_MANIFEST_PROJECTION_SCHEMA,
        "manifest_version": MANIFEST_VERSION,
        "manifest_digest": plan.manifest_digest,
        "plan_digest": plan.plan_digest,
        "projected_views": [view.intent.view_type for view in prepared_views],
    }
    projection_generation = ViewGeneration.create(
        source_identity,
        projection_profile,
        projection_object.record,
        schema_version=REPO_MANIFEST_PROJECTION_SCHEMA,
        metadata=projection_metadata,
        member_object_digests=dependencies,
    )
    repository_source.verify_snapshot()
    return _PreparedImport(
        artifact_plan_digest=artifact_plan_digest,
        views=tuple(prepared_views),
        projection=projection_object,
        projection_profile=projection_profile,
        projection_generation=projection_generation,
    )


def _validate_catalog_publication(
    publication: object,
    *,
    expected_snapshot: PublishedSnapshot,
    repository_id: str,
    ref_name: str,
    expected_generation: int,
) -> tuple[int, bool, str]:
    publication = _snapshot_backend_json(
        publication,
        label="snapshot publication result",
    )
    required = {
        "snapshot_id",
        "repository_id",
        "ref_name",
        "generation",
        "updated_at",
        "changed",
    }
    if type(publication) is not dict or not required <= set(publication):
        raise StorageIntegrityError("snapshot publication result shape is invalid")
    generation = publication["generation"]
    changed = publication["changed"]
    updated_at = publication["updated_at"]
    if (
        type(generation) is not int
        or not 0 < generation <= _INT64_MAX
        or type(changed) is not bool
        or publication["snapshot_id"] != expected_snapshot.snapshot_id
        or publication["repository_id"] != repository_id
        or publication["ref_name"] != ref_name
        or (changed and generation != expected_generation + 1)
    ):
        raise StorageIntegrityError("snapshot publication result is invalid")
    try:
        updated_at = canonical_utc_timestamp(
            updated_at,
            "snapshot publication updated_at",
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError(
            "snapshot publication timestamp is invalid"
        ) from exc
    return generation, changed, updated_at


def _identity_closed_object(value: object, *, label: str) -> ObjectRecord:
    required = {"digest", "storage_key", "byte_size", "media_type"}
    if type(value) is not dict or not required <= set(value):
        raise StorageIntegrityError(f"{label} shape is invalid")
    try:
        record = ObjectRecord(
            digest=value["digest"],
            storage_key=value["storage_key"],
            byte_size=value["byte_size"],
            media_type=value["media_type"],
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError(f"{label} identity is invalid") from exc
    if not _same_exact_json(
        value,
        {
            **value,
            "digest": record.digest,
            "storage_key": record.storage_key,
            "byte_size": record.byte_size,
            "media_type": record.media_type,
        },
    ):
        raise StorageIntegrityError(f"{label} identity is not canonical")
    return record


def _validate_identity_closed_summary(summary: object) -> str:
    """Rebuild every backend-neutral identity in one ready summary."""

    required = {
        "snapshot_id",
        "repository_id",
        "namespace",
        "repository",
        "status",
        "published_at",
        "source",
        "views",
    }
    if type(summary) is not dict or not required <= set(summary):
        raise StorageIntegrityError("published ref manifest shape is invalid")
    namespace_value = summary["namespace"]
    repository_value = summary["repository"]
    source_value = summary["source"]
    views_value = summary["views"]
    if (
        type(namespace_value) is not dict
        or not {"namespace_id", "name"} <= set(namespace_value)
        or type(repository_value) is not dict
        or not {"namespace_id", "repository_key"} <= set(repository_value)
        or type(source_value) is not dict
        or not {
            "source_revision_id",
            "kind",
            "commit_sha",
            "tree_sha",
            "source_fingerprint",
        }
        <= set(source_value)
        or type(views_value) is not dict
        or not views_value
        or summary["status"] != "ready"
    ):
        raise StorageIntegrityError("published ref manifest core is invalid")
    try:
        namespace = NamespaceIdentity(namespace_value["name"])
        repository = RepositoryIdentity(
            namespace_id=namespace_value["namespace_id"],
            repository_key=repository_value["repository_key"],
        )
        source = SourceRevision(
            repository_id=summary["repository_id"],
            source_kind=source_value["kind"],
            commit_sha=source_value["commit_sha"],
            tree_sha=source_value["tree_sha"],
            source_fingerprint=source_value["source_fingerprint"],
        )
        published_at = canonical_utc_timestamp(
            summary["published_at"],
            "published ref manifest published_at",
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError(
            "published ref manifest identity is invalid"
        ) from exc
    if (
        namespace_value["name"] != namespace.name
        or namespace_value["namespace_id"] != namespace.namespace_id
        or repository_value["namespace_id"] != namespace.namespace_id
        or repository_value["repository_key"] != repository.repository_key
        or summary["repository_id"] != repository.repository_id
        or source.repository_id != repository.repository_id
        or source_value["source_revision_id"] != source.source_revision_id
        or source_value["kind"] != source.source_kind
        or source_value["commit_sha"] != source.commit_sha
        or source_value["tree_sha"] != source.tree_sha
        or source_value["source_fingerprint"] != source.source_fingerprint
    ):
        raise StorageIntegrityError("published ref manifest identity is inconsistent")

    snapshot_views: list[SnapshotView] = []
    for view_type, value in views_value.items():
        view_required = {
            "view_generation_id",
            "schema_version",
            "metadata",
            "profile",
            "object",
            "member_objects",
        }
        if (
            type(view_type) is not str
            or type(value) is not dict
            or not view_required <= set(value)
            or type(value["metadata"]) is not dict
            or type(value["profile"]) is not dict
            or not {"profile_id", "name", "config"} <= set(value["profile"])
            or type(value["profile"]["config"]) is not dict
            or type(value["member_objects"]) is not list
        ):
            raise StorageIntegrityError("published ref view shape is invalid")
        try:
            profile = ViewProfile.create(
                view_type,
                value["profile"]["config"],
                name=value["profile"]["name"],
            )
            primary = _identity_closed_object(
                value["object"],
                label=f"published {view_type!r} primary object",
            )
            generation = ViewGeneration(
                repository_id=repository.repository_id,
                source_revision_id=source.source_revision_id,
                profile=profile,
                object_digest=primary.digest,
                schema_version=value["schema_version"],
                metadata_json=canonical_json(value["metadata"]),
            )
            members = tuple(
                _identity_closed_object(
                    member,
                    label=f"published {view_type!r} member object",
                )
                for member in value["member_objects"]
            )
        except StorageValidationError as exc:
            raise StorageIntegrityError(
                f"published {view_type!r} identity is invalid"
            ) from exc
        if (
            profile.view_type != view_type
            or value["profile"]["name"] != profile.name
            or value["profile"]["profile_id"] != profile.profile_id
            or not _same_exact_json(value["profile"]["config"], profile.config)
            or value["schema_version"] != generation.schema_version
            or value["view_generation_id"] != generation.view_generation_id
            or tuple(member.digest for member in members)
            != generation.member_object_digests
        ):
            raise StorageIntegrityError(
                f"published {view_type!r} identity is inconsistent"
            )
        snapshot_views.append(SnapshotView(generation))
    try:
        snapshot = PublishedSnapshot(
            repository.repository_id,
            source.source_revision_id,
            tuple(snapshot_views),
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError("published ref snapshot is invalid") from exc
    if summary["snapshot_id"] != snapshot.snapshot_id:
        raise StorageIntegrityError("published ref snapshot identity is inconsistent")
    return published_at


def _validate_ref_resolution(
    resolution: object,
    *,
    repository_id: str,
    ref_name: str,
    snapshot_id: str,
    generation: int,
    updated_at: str,
) -> None:
    resolution = _snapshot_backend_json(
        resolution,
        label="published ref resolution",
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
        raise StorageIntegrityError("published ref resolution shape is invalid")
    observed_generation = resolution["generation"]
    observed_snapshot_id = resolution["snapshot_id"]
    observed_manifest = resolution["manifest"]
    if (
        type(observed_generation) is not int
        or not 0 < observed_generation <= _INT64_MAX
        or resolution["repository_id"] != repository_id
        or resolution["ref_name"] != ref_name
        or type(observed_snapshot_id) is not str
        or _SNAPSHOT_ID_RE.fullmatch(observed_snapshot_id) is None
        or type(observed_manifest) is not dict
    ):
        raise StorageIntegrityError("published ref resolution is inconsistent")
    try:
        observed_updated_at = canonical_utc_timestamp(
            resolution["updated_at"],
            "published ref updated_at",
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError("published ref resolution is inconsistent") from exc
    if (
        observed_manifest.get("snapshot_id") != observed_snapshot_id
        or observed_manifest.get("repository_id") != repository_id
    ):
        raise StorageIntegrityError("published ref resolution is inconsistent")
    try:
        _validate_identity_closed_summary(observed_manifest)
    except StorageIntegrityError as exc:
        raise StorageIntegrityError(
            "published ref resolution manifest is invalid"
        ) from exc
    if observed_generation > generation:
        raise PublishConflict("published ref was superseded before verification")
    if (
        resolution["snapshot_id"] != snapshot_id
        or observed_generation != generation
        or observed_updated_at != updated_at
    ):
        raise StorageIntegrityError("published ref resolution is inconsistent")


def _object_summary(record: ObjectRecord) -> dict[str, Any]:
    return {
        "digest": record.digest,
        "storage_key": record.storage_key,
        "byte_size": record.byte_size,
        "media_type": record.media_type,
    }


def _validate_snapshot_summary(
    summary: object,
    *,
    namespace_id: str,
    namespace_name: str,
    repository_key: str,
    source: SourceRevision,
    snapshot: PublishedSnapshot,
    objects: Mapping[str, _StoredObject],
) -> str:
    summary = _snapshot_backend_json(
        summary,
        label="published snapshot summary",
    )
    required = {
        "snapshot_id",
        "repository_id",
        "namespace",
        "repository",
        "status",
        "published_at",
        "source",
        "views",
    }
    if type(summary) is not dict or not required <= set(summary):
        raise StorageIntegrityError("published snapshot summary shape is invalid")
    views = summary["views"]
    if (
        summary["snapshot_id"] != snapshot.snapshot_id
        or summary["repository_id"] != snapshot.repository_id
        or summary["status"] != "ready"
        or not _has_exact_json_core(
            summary["namespace"],
            {"namespace_id": namespace_id, "name": namespace_name},
        )
        or not _has_exact_json_core(
            summary["repository"],
            {"namespace_id": namespace_id, "repository_key": repository_key},
        )
        or not _has_exact_json_core(
            summary["source"],
            {
                "source_revision_id": source.source_revision_id,
                "kind": source.source_kind,
                "commit_sha": source.commit_sha,
                "tree_sha": source.tree_sha,
                "source_fingerprint": source.source_fingerprint,
            },
        )
        or type(views) is not dict
        or set(views) != {view.view_type for view in snapshot.views}
    ):
        raise StorageIntegrityError("published snapshot summary is inconsistent")
    try:
        published_at = canonical_utc_timestamp(
            summary["published_at"],
            "snapshot published_at",
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError(
            "snapshot publication timestamp is invalid"
        ) from exc
    for snapshot_view in snapshot.views:
        generation = snapshot_view.generation
        primary = objects[generation.object_digest].record
        expected_members = [
            _object_summary(objects[digest].record)
            for digest in generation.member_object_digests
        ]
        expected = {
            "view_generation_id": generation.view_generation_id,
            "schema_version": generation.schema_version,
            "metadata": json.loads(generation.metadata_json),
            "profile": {
                "profile_id": generation.profile_id,
                "name": generation.profile.name,
                "config": generation.profile.config,
            },
            "object": _object_summary(primary),
            "member_objects": expected_members,
        }
        observed_view = views[generation.view_type]
        if (
            not _has_exact_json_core(observed_view, expected)
            or not _same_exact_json(
                observed_view["metadata"],  # type: ignore[index]
                expected["metadata"],
            )
            or not _same_exact_json(
                observed_view["profile"]["config"],  # type: ignore[index]
                expected["profile"]["config"],
            )
        ):
            raise StorageIntegrityError(
                f"published {generation.view_type!r} summary is inconsistent"
            )
    return published_at


def _register_and_publish(
    prepared: _PreparedImport,
    *,
    plan: RepoManifestImportPlan,
    catalog: RetainedImportCatalog,
    object_store: RetainedImportObjectStore,
    repository_key: str,
    namespace_name: str,
    ref_name: str,
    expected_generation: int,
    repository_identity: RepositoryIdentity,
    source_identity: SourceRevision,
) -> RepoManifestImportResult:
    stored_objects = prepared.stored_objects
    objects_by_digest = {stored.record.digest: stored for stored in stored_objects}
    for stored in stored_objects:
        _verify_exact_receipt(object_store, stored)

    namespace_id = repository_identity.namespace_id
    _exact_backend_id(
        catalog.create_namespace(namespace_name),  # type: ignore[attr-defined]
        namespace_id,
        "namespace ID",
    )
    _exact_backend_id(
        catalog.create_repository(  # type: ignore[attr-defined]
            repository_key,
            namespace_id=namespace_id,
        ),
        repository_identity.repository_id,
        "repository ID",
    )
    _exact_backend_id(
        catalog.create_source_revision(  # type: ignore[attr-defined]
            repository_identity.repository_id,
            commit_sha=None,
            dirty=True,
            source_fingerprint=source_identity.source_fingerprint,
        ),
        source_identity.source_revision_id,
        "source revision ID",
    )

    for stored in stored_objects:
        record = stored.record
        _exact_backend_id(
            catalog.register_object(  # type: ignore[attr-defined]
                record.digest,
                storage_key=record.storage_key,
                byte_size=record.byte_size,
                media_type=record.media_type,
            ),
            record.digest,
            "object digest",
        )

    generations = {view.intent.view_type: view.generation for view in prepared.views}
    generations[REPO_MANIFEST_PROJECTION_VIEW] = prepared.projection_generation
    profiles = {view.intent.view_type: view.intent.profile for view in prepared.views}
    profiles[REPO_MANIFEST_PROJECTION_VIEW] = prepared.projection_profile
    for view_type in sorted(generations):
        profile = profiles[view_type]
        generation = generations[view_type]
        generation_metadata = json.loads(generation.metadata_json)
        generation_metadata.pop(VIEW_GENERATION_MEMBERS_METADATA_KEY, None)
        _exact_backend_id(
            catalog.create_view_profile(  # type: ignore[attr-defined]
                view_type,
                profile.config,
                name=profile.name,
            ),
            profile.profile_id,
            "view profile ID",
        )
        _exact_backend_id(
            catalog.stage_view_generation(  # type: ignore[attr-defined]
                repository_identity.repository_id,
                source_identity.source_revision_id,
                profile.profile_id,
                view_type,
                generation.object_digest,
                schema_version=generation.schema_version,
                metadata=generation_metadata,
                member_object_digests=generation.member_object_digests,
            ),
            generation.view_generation_id,
            "view generation ID",
        )

    # Receipts are observations, not pins.  Revalidate every object at the
    # final boundary immediately before the catalog's atomic snapshot/ref CAS.
    for stored in stored_objects:
        _verify_exact_receipt(object_store, stored)

    expected_snapshot = PublishedSnapshot(
        repository_identity.repository_id,
        source_identity.source_revision_id,
        tuple(SnapshotView(generation) for generation in generations.values()),
    )
    publication = catalog.publish_snapshot(  # type: ignore[attr-defined]
        repository_identity.repository_id,
        source_identity.source_revision_id,
        [
            generations[view_type].view_generation_id
            for view_type in sorted(generations)
        ],
        ref_name=ref_name,
        expected_generation=expected_generation,
    )
    generation, changed, updated_at = _validate_catalog_publication(
        publication,
        expected_snapshot=expected_snapshot,
        repository_id=repository_identity.repository_id,
        ref_name=ref_name,
        expected_generation=expected_generation,
    )
    raw_summary = catalog.get_manifest_summary(  # type: ignore[attr-defined]
        expected_snapshot.snapshot_id
    )
    summary = _snapshot_backend_json(
        raw_summary,
        label="published snapshot summary",
    )
    published_at = _validate_snapshot_summary(
        summary,
        namespace_id=namespace_id,
        namespace_name=namespace_name,
        repository_key=repository_key,
        source=source_identity,
        snapshot=expected_snapshot,
        objects=objects_by_digest,
    )
    raw_resolution = catalog.resolve_ref(  # type: ignore[attr-defined]
        repository_identity.repository_id,
        ref_name,
    )
    resolution = _snapshot_backend_json(
        raw_resolution,
        label="published ref resolution",
    )
    _validate_ref_resolution(
        resolution,
        repository_id=repository_identity.repository_id,
        ref_name=ref_name,
        snapshot_id=expected_snapshot.snapshot_id,
        generation=generation,
        updated_at=updated_at,
    )
    resolution_manifest = resolution["manifest"]
    resolved_published_at = _validate_snapshot_summary(
        resolution_manifest,
        namespace_id=namespace_id,
        namespace_name=namespace_name,
        repository_key=repository_key,
        source=source_identity,
        snapshot=expected_snapshot,
        objects=objects_by_digest,
    )
    if resolved_published_at != published_at:
        raise StorageIntegrityError(
            "published ref manifest timestamp differs from its snapshot summary"
        )
    return RepoManifestImportResult(
        repository_id=repository_identity.repository_id,
        source_revision_id=source_identity.source_revision_id,
        snapshot_id=expected_snapshot.snapshot_id,
        ref_name=ref_name,
        generation=generation,
        changed=changed,
        views=tuple(view.intent.view_type for view in prepared.views),
        skipped_items=plan.selection.skipped_items,
        view_generation_items=tuple(
            sorted(
                (view_type, value.view_generation_id)
                for view_type, value in generations.items()
            )
        ),
    )


def import_retained_repo_manifest(
    plan: RepoManifestImportPlan,
    *,
    artifact_owner: PublishedWorkspaceReceiptOwner,
    repository_source: RepositorySourceBinding,
    repository_key: str,
    catalog: RetainedImportCatalog,
    object_store: RetainedImportObjectStore,
    namespace_name: str = DEFAULT_NAMESPACE_NAME,
    ref_name: str = DEFAULT_REF_NAME,
    expected_generation: int = 0,
    max_context_files: int = DEFAULT_MAX_CONTEXT_FILES,
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
    max_bundle_files: int = DEFAULT_MAX_BUNDLE_FILES,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_bundle_metadata_bytes: int = DEFAULT_MAX_BUNDLE_METADATA_BYTES,
    max_projection_bytes: int = DEFAULT_MAX_PROJECTION_BYTES,
    forbidden_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> RepoManifestImportResult:
    """Authenticate, ingest, and atomically publish one retained manifest.

    ``artifact_owner`` and ``repository_source`` remain caller-owned.  The
    function borrows them synchronously and never transfers or closes either
    authority.  All selected views are validated before the first CAS write;
    the retained artifact callback and its namespace postflight finish before
    the first catalog write; and ``publish_snapshot`` is the last catalog
    mutation.
    """

    if type(plan) is not RepoManifestImportPlan:
        raise TypeError("retained manifest import requires an exact import plan")
    if type(artifact_owner) is not PublishedWorkspaceReceiptOwner:
        raise TypeError("retained manifest import requires an exact receipt owner")
    if type(repository_source) is not RepositorySourceBinding:
        raise TypeError("retained manifest import requires an exact source binding")
    if not isinstance(catalog, RetainedImportCatalog):
        raise TypeError("retained manifest import catalog lacks required capabilities")
    if not isinstance(object_store, RetainedImportObjectStore):
        raise TypeError(
            "retained manifest import object store lacks required capabilities"
        )
    # Frozen dataclasses are not authority tokens.  Rebuild every nested value
    # so later caller mutation cannot change what authority/CAS/catalog see.
    plan = _snapshot_plan(plan)
    repository_key = _exact_text(repository_key, "repository key")
    namespace_name = _exact_text(namespace_name, "namespace name")
    ref_name = _exact_text(ref_name, "ref name")
    expected_generation = _expected_generation(expected_generation)
    max_context_files = _positive_limit(max_context_files, "context file limit")
    max_context_bytes = _positive_limit(max_context_bytes, "context byte limit")
    max_bundle_files = _positive_limit(max_bundle_files, "bundle file limit")
    max_bundle_bytes = _positive_limit(max_bundle_bytes, "bundle byte limit")
    max_bundle_metadata_bytes = _positive_limit(
        max_bundle_metadata_bytes,
        "bundle metadata byte limit",
    )
    max_projection_bytes = _positive_limit(
        max_projection_bytes,
        "projection byte limit",
    )
    environment = _snapshot_environment(environ)
    forbidden = _snapshot_forbidden_paths(forbidden_paths)
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
    catalog_contract = catalog.retained_import_contract()
    if (
        type(catalog_contract) is not str
        or catalog_contract != RETAINED_IMPORT_CATALOG_CONTRACT
    ):
        raise TypeError("retained manifest import catalog contract is incompatible")
    if plan.inert:
        raise StorageValidationError(
            "legacy source-fingerprint-v1 manifest plans are import-inert"
        )
    if not plan.views or plan.selection.selected_views != tuple(
        view.view_type for view in plan.views
    ):
        raise StorageValidationError("manifest import plan selection is inconsistent")
    source_snapshot = repository_source.authenticated_identity_snapshot()
    if (
        plan.source.fingerprint != source_snapshot.fingerprint
        or plan.source.file_count != source_snapshot.file_count
    ):
        raise StorageIntegrityError(
            "manifest import plan differs from the retained repository source"
        )
    if not repository_source.usable:
        raise RuntimeError("manifest import repository source is not usable")
    if not artifact_owner.active:
        raise RuntimeError("manifest import artifact receipt owner is not active")

    namespace_id = _expected_namespace_id(namespace_name)
    repository_identity = RepositoryIdentity(
        namespace_id=namespace_id,
        repository_key=repository_key,
    )
    source_identity = SourceRevision.dirty(
        repository_identity.repository_id,
        source_fingerprint=plan.source.fingerprint,
        # The manifest commit is display provenance only; no clean-tree
        # authority exists in RepoManifest v1.1.
        commit_sha=None,
    )
    prepared = artifact_owner.consume(
        lambda receipt, publication: _prepare_import_inside_authority(
            receipt,
            publication,
            plan=plan,
            repository_source=repository_source,
            repository_key=repository_key,
            source_identity=source_identity,
            object_store=object_store,
            environment=environment,
            forbidden_paths=forbidden,
            max_context_files=max_context_files,
            max_context_bytes=max_context_bytes,
            max_bundle_files=max_bundle_files,
            max_bundle_bytes=max_bundle_bytes,
            max_bundle_metadata_bytes=max_bundle_metadata_bytes,
            max_projection_bytes=max_projection_bytes,
        )
    )
    if type(prepared) is not _PreparedImport:
        raise StorageIntegrityError("retained import preparation returned invalid data")
    final_source_snapshot = repository_source.authenticated_identity_snapshot()
    if final_source_snapshot != source_snapshot:
        raise StorageIntegrityError(
            "retained repository source identity changed during import"
        )
    return _register_and_publish(
        prepared,
        plan=plan,
        catalog=catalog,
        object_store=object_store,
        repository_key=repository_key,
        namespace_name=namespace_name,
        ref_name=ref_name,
        expected_generation=expected_generation,
        repository_identity=repository_identity,
        source_identity=source_identity,
    )


__all__ = [
    "DEFAULT_MAX_CONTEXT_BYTES",
    "DEFAULT_MAX_CONTEXT_FILES",
    "DEFAULT_MAX_PROJECTION_BYTES",
    "DEFAULT_NAMESPACE_NAME",
    "DEFAULT_REF_NAME",
    "REPO_MANIFEST_IMPORT_GENERATION_CONTRACT",
    "REPO_MANIFEST_PROJECTION_MEDIA_TYPE",
    "REPO_MANIFEST_PROJECTION_PROFILE_NAME",
    "REPO_MANIFEST_PROJECTION_SCHEMA",
    "REPO_MANIFEST_PROJECTION_VIEW",
    "RepoManifestImportResult",
    "import_retained_repo_manifest",
]
