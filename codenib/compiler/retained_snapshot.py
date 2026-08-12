# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared read-only attestation for retained catalog snapshots.

The values returned here are internal, point-in-time observations.  They are
not object-store pins, leases, filesystem ownership tokens, or publication
authority.  Callers that read object bytes must still authenticate those bytes
inside the read and revalidate receipts at their final metadata boundary.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from ..storage.models import (
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
    canonical_utc_timestamp,
)
from ..storage.protocols import snapshot_retained_import_response


@dataclass(frozen=True, slots=True)
class _RetainedSnapshotView:
    view_type: str
    summary: dict[str, Any]
    profile: ViewProfile
    generation: ViewGeneration
    primary: ObjectRecord
    members: tuple[ObjectRecord, ...]


@dataclass(frozen=True, slots=True)
class _RetainedSnapshot:
    summary: dict[str, Any]
    namespace: NamespaceIdentity
    repository: RepositoryIdentity
    source: SourceRevision
    snapshot: PublishedSnapshot
    published_at: str
    view_items: tuple[tuple[str, _RetainedSnapshotView], ...]

    @property
    def views(self) -> Mapping[str, _RetainedSnapshotView]:
        return MappingProxyType(dict(self.view_items))


def same_exact_json(left: object, right: object) -> bool:
    """Compare detached JSON without bool/int/float coercions."""

    if type(left) is not type(right):
        return False
    if type(left) is dict:
        if left.keys() != right.keys():  # type: ignore[union-attr]
            return False
        return all(
            same_exact_json(value, right[key])  # type: ignore[index]
            for key, value in left.items()  # type: ignore[union-attr]
        )
    if type(left) is list:
        if len(left) != len(right):  # type: ignore[arg-type]
            return False
        return all(
            same_exact_json(left_value, right_value)
            for left_value, right_value in zip(  # type: ignore[arg-type]
                left,
                right,
                strict=True,
            )
        )
    if type(left) is float:
        return struct.pack(">d", left) == struct.pack(  # type: ignore[arg-type]
            ">d", right
        )
    return left == right


def has_exact_json_core(observed: object, expected: object) -> bool:
    """Match identity fields exactly while allowing additive object fields."""

    if type(observed) is not type(expected):
        return False
    if type(expected) is dict:
        return all(
            key in observed  # type: ignore[operator]
            and has_exact_json_core(observed[key], value)  # type: ignore[index]
            for key, value in expected.items()  # type: ignore[union-attr]
        )
    if type(expected) is list:
        if len(observed) != len(expected):  # type: ignore[arg-type]
            return False
        return all(
            has_exact_json_core(observed_value, expected_value)
            for observed_value, expected_value in zip(  # type: ignore[arg-type]
                observed,
                expected,
                strict=True,
            )
        )
    return same_exact_json(observed, expected)


def identity_closed_object(value: object, *, label: str) -> ObjectRecord:
    """Rebuild one canonical object identity while permitting extensions."""

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
    if not same_exact_json(
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


def attest_retained_snapshot(
    value: object,
    *,
    label: str,
    expected_repository_id: str | None = None,
    expected_snapshot_id: str | None = None,
) -> _RetainedSnapshot:
    """Detach one bounded response and rebuild its complete identity closure."""

    summary = snapshot_retained_import_response(value, label=label)
    return attest_detached_retained_snapshot(
        summary,
        label=label,
        expected_repository_id=expected_repository_id,
        expected_snapshot_id=expected_snapshot_id,
    )


def attest_detached_retained_snapshot(
    summary: object,
    *,
    label: str,
    expected_repository_id: str | None = None,
    expected_snapshot_id: str | None = None,
) -> _RetainedSnapshot:
    """Rebuild an already detached exact-JSON snapshot summary."""

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
        raise StorageIntegrityError(f"{label} shape is invalid")
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
        raise StorageIntegrityError(f"{label} core is invalid")
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
            f"{label} published_at",
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError(f"{label} identity is invalid") from exc
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
        or (
            expected_repository_id is not None
            and repository.repository_id != expected_repository_id
        )
    ):
        raise StorageIntegrityError(f"{label} identity is inconsistent")

    snapshot_views: list[SnapshotView] = []
    view_items: list[tuple[str, _RetainedSnapshotView]] = []
    for view_type, view_value in views_value.items():
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
            or type(view_value) is not dict
            or not view_required <= set(view_value)
            or type(view_value["metadata"]) is not dict
            or type(view_value["profile"]) is not dict
            or not {"profile_id", "name", "config"} <= set(view_value["profile"])
            or type(view_value["profile"]["config"]) is not dict
            or type(view_value["member_objects"]) is not list
        ):
            raise StorageIntegrityError(f"{label} view shape is invalid")
        try:
            profile = ViewProfile.create(
                view_type,
                view_value["profile"]["config"],
                name=view_value["profile"]["name"],
            )
            primary = identity_closed_object(
                view_value["object"],
                label=f"{label} {view_type!r} primary object",
            )
            generation = ViewGeneration(
                repository_id=repository.repository_id,
                source_revision_id=source.source_revision_id,
                profile=profile,
                object_digest=primary.digest,
                schema_version=view_value["schema_version"],
                metadata_json=canonical_json(view_value["metadata"]),
            )
            members = tuple(
                identity_closed_object(
                    member,
                    label=f"{label} {view_type!r} member object",
                )
                for member in view_value["member_objects"]
            )
        except StorageValidationError as exc:
            raise StorageIntegrityError(
                f"{label} {view_type!r} identity is invalid"
            ) from exc
        if (
            profile.view_type != view_type
            or view_value["profile"]["name"] != profile.name
            or view_value["profile"]["profile_id"] != profile.profile_id
            or not same_exact_json(view_value["profile"]["config"], profile.config)
            or view_value["schema_version"] != generation.schema_version
            or view_value["view_generation_id"] != generation.view_generation_id
            or tuple(member.digest for member in members)
            != generation.member_object_digests
        ):
            raise StorageIntegrityError(
                f"{label} {view_type!r} identity is inconsistent"
            )
        snapshot_views.append(SnapshotView(generation))
        view_items.append(
            (
                view_type,
                _RetainedSnapshotView(
                    view_type=view_type,
                    summary=view_value,
                    profile=profile,
                    generation=generation,
                    primary=primary,
                    members=members,
                ),
            )
        )
    try:
        snapshot = PublishedSnapshot(
            repository.repository_id,
            source.source_revision_id,
            tuple(snapshot_views),
        )
    except StorageValidationError as exc:
        raise StorageIntegrityError(f"{label} snapshot is invalid") from exc
    if summary["snapshot_id"] != snapshot.snapshot_id or (
        expected_snapshot_id is not None
        and snapshot.snapshot_id != expected_snapshot_id
    ):
        raise StorageIntegrityError(f"{label} snapshot identity is inconsistent")
    return _RetainedSnapshot(
        summary=summary,
        namespace=namespace,
        repository=repository,
        source=source,
        snapshot=snapshot,
        published_at=published_at,
        view_items=tuple(sorted(view_items)),
    )


__all__ = [
    "attest_retained_snapshot",
    "has_exact_json_core",
    "identity_closed_object",
    "same_exact_json",
]
