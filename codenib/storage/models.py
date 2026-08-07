# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Backend-neutral identities for immutable index storage."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class StorageError(RuntimeError):
    """Base class for storage contract failures."""


class StorageIntegrityError(StorageError):
    """Stored bytes or metadata do not match their immutable identity."""


class PublishConflict(StorageError):
    """A compare-and-swap publication precondition no longer holds."""


class StorageNotFound(StorageError):
    """A requested catalog or object-store record does not exist."""


class StorageValidationError(StorageError, ValueError):
    """Input cannot form a valid storage identity."""


def canonical_json(value: Mapping[str, Any]) -> str:
    """Return the deterministic JSON representation used by content IDs."""

    if not isinstance(value, Mapping):
        raise StorageValidationError("canonical JSON payload must be an object")
    try:
        return json.dumps(
            _normalize_json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StorageValidationError(f"payload is not canonical JSON: {exc}") from exc


def _normalize_json_value(value: Any) -> Any:
    """Normalize JSON-compatible values without lossy object-key coercion."""

    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise StorageValidationError(
                    "canonical JSON object keys must be strings"
                )
            normalized[key] = _normalize_json_value(child)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json_value(child) for child in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StorageValidationError("canonical JSON numbers must be finite")
        return value
    raise StorageValidationError(
        f"canonical JSON does not support {type(value).__name__} values"
    )


def content_id(prefix: str, value: Mapping[str, Any]) -> str:
    """Return a namespaced SHA-256 identity for a canonical JSON object."""

    normalized_prefix = _required_text(prefix, "content ID prefix")
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{normalized_prefix}_{digest}"


def normalize_digest(value: str) -> str:
    """Normalize an optional ``sha256:`` prefix to a bare lowercase digest."""

    normalized = _required_text(value, "digest").lower()
    if normalized.startswith("sha256:"):
        normalized = normalized[7:]
    if _DIGEST_RE.fullmatch(normalized) is None:
        raise StorageValidationError(
            "digest must be 64 lowercase hexadecimal characters"
        )
    return normalized


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StorageValidationError(f"{field} must not be empty")
    return value.strip()


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StorageValidationError(f"{field} must be text or null")
    normalized = value.strip()
    return normalized or None


def _canonical_relative_path(value: str, field: str) -> str:
    path = _required_text(value, field)
    if "\\" in path or "\x00" in path:
        raise StorageValidationError(f"{field} must use safe POSIX separators")
    normalized = PurePosixPath(path)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise StorageValidationError(f"{field} must be relative and contained")
    if path != normalized.as_posix() or path in {".", ""}:
        raise StorageValidationError(f"{field} is not canonical")
    return normalized.as_posix()


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """A repository key scoped to a non-null logical namespace."""

    namespace_id: str
    repository_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "namespace_id", _required_text(self.namespace_id, "namespace ID")
        )
        object.__setattr__(
            self,
            "repository_key",
            _required_text(self.repository_key, "repository key"),
        )

    @property
    def repository_id(self) -> str:
        return content_id(
            "repo",
            {
                "namespace_id": self.namespace_id,
                "repository_key": self.repository_key,
            },
        )


@dataclass(frozen=True, slots=True)
class SourceRevision:
    """A clean Git tree or a dirty-worktree source identity."""

    repository_id: str
    source_kind: str
    commit_sha: str | None
    tree_sha: str | None
    source_fingerprint: str

    def __post_init__(self) -> None:
        repository = _required_text(self.repository_id, "repository ID")
        kind = _required_text(self.source_kind, "source kind").lower()
        commit = _optional_text(self.commit_sha, "source commit")
        tree = _optional_text(self.tree_sha, "source tree")
        commit = commit.lower() if commit else None
        tree = tree.lower() if tree else None
        fingerprint = _required_text(self.source_fingerprint, "source fingerprint")
        if kind not in {"clean", "dirty"}:
            raise StorageValidationError("source kind must be 'clean' or 'dirty'")
        if kind == "clean":
            if not commit or not tree:
                raise StorageValidationError(
                    "clean source requires both commit and tree identity"
                )
            expected = f"git-tree:{tree}"
            if fingerprint != expected:
                raise StorageValidationError(
                    "clean source fingerprint must match its Git tree"
                )
        elif tree is not None:
            raise StorageValidationError(
                "dirty source identity must not include a base tree"
            )

        object.__setattr__(self, "repository_id", repository)
        object.__setattr__(self, "source_kind", kind)
        object.__setattr__(self, "commit_sha", commit)
        object.__setattr__(self, "tree_sha", tree)
        object.__setattr__(self, "source_fingerprint", fingerprint)

    @classmethod
    def clean(
        cls,
        repository_id: str,
        *,
        commit_sha: str,
        tree_sha: str,
    ) -> SourceRevision:
        tree = _required_text(tree_sha, "clean source tree").lower()
        return cls(
            repository_id=repository_id,
            source_kind="clean",
            commit_sha=commit_sha,
            tree_sha=tree,
            source_fingerprint=f"git-tree:{tree}",
        )

    @classmethod
    def dirty(
        cls,
        repository_id: str,
        *,
        source_fingerprint: str,
        commit_sha: str | None = None,
    ) -> SourceRevision:
        return cls(
            repository_id=repository_id,
            source_kind="dirty",
            commit_sha=commit_sha,
            tree_sha=None,
            source_fingerprint=source_fingerprint,
        )

    @property
    def source_revision_id(self) -> str:
        return content_id(
            "src",
            {
                "repository_id": self.repository_id,
                "source_kind": self.source_kind,
                "commit_sha": self.commit_sha,
                "tree_sha": self.tree_sha,
                "source_fingerprint": self.source_fingerprint,
            },
        )


@dataclass(frozen=True, slots=True)
class ViewProfile:
    """Canonical compatibility identity for one persisted view type."""

    view_type: str
    name: str
    config_json: str

    def __post_init__(self) -> None:
        view_type = _required_text(self.view_type, "view type")
        name = _required_text(self.name, "profile name")
        try:
            parsed = json.loads(self.config_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageValidationError("profile config must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise StorageValidationError("profile config must be a JSON object")
        config_json = canonical_json(parsed)
        object.__setattr__(self, "view_type", view_type)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "config_json", config_json)

    @classmethod
    def create(
        cls,
        view_type: str,
        config: Mapping[str, Any] | None = None,
        *,
        name: str = "default",
    ) -> ViewProfile:
        return cls(
            view_type=view_type,
            name=name,
            config_json=canonical_json(config or {}),
        )

    @property
    def config(self) -> dict[str, Any]:
        return json.loads(self.config_json)

    @property
    def profile_id(self) -> str:
        return content_id(
            "profile",
            {
                "view_type": self.view_type,
                "name": self.name,
                "config": self.config,
            },
        )


@dataclass(frozen=True, slots=True)
class ObjectRecord:
    """Catalog metadata for one immutable object-store payload."""

    digest: str
    byte_size: int
    storage_key: str
    media_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int):
            raise StorageValidationError("object byte size must be an integer")
        if self.byte_size < 0:
            raise StorageValidationError("object byte size must not be negative")
        object.__setattr__(self, "digest", normalize_digest(self.digest))
        object.__setattr__(
            self,
            "storage_key",
            _canonical_relative_path(self.storage_key, "storage key"),
        )
        object.__setattr__(
            self, "media_type", _required_text(self.media_type, "media type")
        )


@dataclass(frozen=True, slots=True)
class ArtifactMember:
    """One relative-path member of a multi-file immutable artifact."""

    path: str
    digest: str
    byte_size: int
    mode: int = 0o644

    def __post_init__(self) -> None:
        path = _canonical_relative_path(self.path, "artifact member path")
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int):
            raise StorageValidationError("artifact member size must be an integer")
        if self.byte_size < 0:
            raise StorageValidationError("artifact member size must not be negative")
        if isinstance(self.mode, bool) or not isinstance(self.mode, int):
            raise StorageValidationError("artifact member mode must be an integer")
        if self.mode < 0 or self.mode > 0o777:
            raise StorageValidationError("artifact member mode is out of range")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "digest", normalize_digest(self.digest))


@dataclass(frozen=True, slots=True)
class ViewGeneration:
    """One immutable view output for a source revision and profile."""

    repository_id: str
    source_revision_id: str
    profile: ViewProfile
    object_digest: str
    schema_version: str
    metadata_json: str = "{}"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "repository_id", _required_text(self.repository_id, "repository ID")
        )
        object.__setattr__(
            self,
            "source_revision_id",
            _required_text(self.source_revision_id, "source revision ID"),
        )
        if not isinstance(self.profile, ViewProfile):
            raise StorageValidationError(
                "view generation profile must be a ViewProfile"
            )
        object.__setattr__(self, "object_digest", normalize_digest(self.object_digest))
        object.__setattr__(
            self,
            "schema_version",
            _required_text(self.schema_version, "schema version"),
        )
        try:
            metadata = json.loads(self.metadata_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageValidationError("view metadata must be valid JSON") from exc
        if not isinstance(metadata, dict):
            raise StorageValidationError("view metadata must be a JSON object")
        object.__setattr__(self, "metadata_json", canonical_json(metadata))

    @classmethod
    def create(
        cls,
        source: SourceRevision,
        profile: ViewProfile,
        object_record: ObjectRecord,
        *,
        schema_version: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ViewGeneration:
        return cls(
            repository_id=source.repository_id,
            source_revision_id=source.source_revision_id,
            profile=profile,
            object_digest=object_record.digest,
            schema_version=schema_version,
            metadata_json=canonical_json(metadata or {}),
        )

    @property
    def profile_id(self) -> str:
        return self.profile.profile_id

    @property
    def view_type(self) -> str:
        return self.profile.view_type

    @property
    def view_generation_id(self) -> str:
        return content_id(
            "view",
            {
                "repository_id": self.repository_id,
                "source_revision_id": self.source_revision_id,
                "profile_id": self.profile_id,
                "view_type": self.view_type,
                "object_digest": self.object_digest,
                "schema_version": self.schema_version,
                "metadata": json.loads(self.metadata_json),
            },
        )


@dataclass(frozen=True, slots=True)
class SnapshotView:
    """A view generation selected into a published snapshot."""

    generation: ViewGeneration

    @property
    def view_type(self) -> str:
        return self.generation.view_type


@dataclass(frozen=True, slots=True)
class PublishedSnapshot:
    """An immutable, source-consistent set of independently profiled views."""

    repository_id: str
    source_revision_id: str
    views: tuple[SnapshotView, ...]

    def __post_init__(self) -> None:
        repository = _required_text(self.repository_id, "repository ID")
        source = _required_text(self.source_revision_id, "source revision ID")
        views = tuple(self.views)
        if not views:
            raise StorageValidationError("a snapshot requires at least one view")
        seen: set[str] = set()
        for snapshot_view in views:
            generation = snapshot_view.generation
            if generation.repository_id != repository:
                raise StorageValidationError(
                    "all snapshot views must belong to the same repository"
                )
            if generation.source_revision_id != source:
                raise StorageValidationError(
                    "all snapshot views must share the snapshot source identity"
                )
            if generation.view_type in seen:
                raise StorageValidationError(
                    f"snapshot has duplicate view type: {generation.view_type}"
                )
            seen.add(generation.view_type)
        object.__setattr__(self, "repository_id", repository)
        object.__setattr__(self, "source_revision_id", source)
        object.__setattr__(
            self,
            "views",
            tuple(sorted(views, key=lambda item: item.view_type)),
        )

    @property
    def snapshot_id(self) -> str:
        return content_id(
            "snapshot",
            {
                "repository_id": self.repository_id,
                "source_revision_id": self.source_revision_id,
                "views": [
                    [view.view_type, view.generation.view_generation_id]
                    for view in self.views
                ],
            },
        )


@dataclass(frozen=True, slots=True)
class RefState:
    """A generation-counted repository ref resolved to one snapshot."""

    repository_id: str
    ref_name: str
    snapshot_id: str
    generation: int

    def __post_init__(self) -> None:
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise StorageValidationError("ref generation must be an integer")
        if self.generation < 1:
            raise StorageValidationError("ref generation must be positive")
        object.__setattr__(
            self, "repository_id", _required_text(self.repository_id, "repository ID")
        )
        object.__setattr__(self, "ref_name", _required_text(self.ref_name, "ref name"))
        object.__setattr__(
            self, "snapshot_id", _required_text(self.snapshot_id, "snapshot ID")
        )


__all__ = [
    "ArtifactMember",
    "ObjectRecord",
    "PublishConflict",
    "PublishedSnapshot",
    "RefState",
    "RepositoryIdentity",
    "SnapshotView",
    "SourceRevision",
    "StorageError",
    "StorageIntegrityError",
    "StorageNotFound",
    "StorageValidationError",
    "ViewGeneration",
    "ViewProfile",
    "canonical_json",
    "content_id",
    "normalize_digest",
]
