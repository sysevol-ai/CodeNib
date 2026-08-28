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
from datetime import datetime, timezone
from enum import Enum
from itertools import islice
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from .._secret_fields import SecretFieldError
from .._secret_fields import assert_no_secret_fields as _assert_no_secret_fields

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_CATALOG_INT64_MAX = 9_223_372_036_854_775_807
DEFAULT_NAMESPACE_ID = "ns_default"
DEFAULT_NAMESPACE_NAME = "default"
INDEX_JOB_REQUEST_CONTRACT = "codenib.index-job-request.v1"
INDEX_JOB_PUBLICATION_CONTRACT = "codenib.index-job-publication.v1"
INDEX_JOB_SUPPORTING_VIEW_PREFIX = "codenib.internal."
INDEX_JOB_EVENT_PAYLOAD_MAX_DEPTH = 16
INDEX_JOB_EVENT_PAYLOAD_MAX_NODES = 1_024
INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS = 16 * 1_024
INDEX_JOB_EVENT_PAYLOAD_MAX_KEY_CHARS = 128
MAX_INDEX_JOB_EVENTS_PER_ATTEMPT = 256
VIEW_GENERATION_MEMBERS_METADATA_KEY = "_codenib_member_object_digests"
# One retained-import summary represents every member twice: once in canonical
# generation metadata and once as its identity-closed object envelope. Keep
# this producer bound comfortably below the 250k-node response capability.
MAX_VIEW_GENERATION_MEMBERS = 32_768


def is_index_job_supporting_view(view_type: object) -> bool:
    """Return whether a view belongs to the reserved job-support namespace."""

    return type(view_type) is str and view_type.startswith(
        INDEX_JOB_SUPPORTING_VIEW_PREFIX
    )


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


def canonical_utc_timestamp(value: object, field: str = "timestamp") -> str:
    """Require the exact timezone-aware UTC ISO-8601 storage representation."""

    if type(value) is not str:
        raise StorageValidationError(f"{field} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StorageValidationError(
            f"{field} must be a canonical UTC timestamp"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timezone.utc.utcoffset(parsed)
        or parsed.isoformat() != value
    ):
        raise StorageValidationError(f"{field} must be a canonical UTC timestamp")
    return value


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


def normalize_view_generation_metadata(
    object_digest: str,
    metadata: Mapping[str, Any] | None = None,
    *,
    member_object_digests: Sequence[str] = (),
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Return canonical metadata and reachability members for one generation.

    The reserved member list is identity-bearing catalog metadata.  Keeping
    this normalization beside :class:`ViewGeneration` lets planners compute
    the exact schema-v4 generation ID without reproducing SQLite-private
    behavior.
    """

    primary = normalize_digest(object_digest)
    if isinstance(member_object_digests, (str, bytes)):
        raise StorageValidationError("member object digests must be a sequence")
    normalized_members_list: list[str] = []
    for value in member_object_digests:
        if len(normalized_members_list) >= MAX_VIEW_GENERATION_MEMBERS:
            raise StorageValidationError("view generation has too many member objects")
        if type(value) is not str:
            raise StorageValidationError("member object digests must be exact strings")
        normalized_members_list.append(normalize_digest(value))
    normalized_members = tuple(sorted(normalized_members_list))
    if len(normalized_members) != len(set(normalized_members)):
        raise StorageValidationError("duplicate member object digests")
    if primary in normalized_members:
        raise StorageValidationError(
            "the primary object must not also be a member object"
        )
    if metadata is not None and not isinstance(metadata, Mapping):
        raise StorageValidationError("view generation metadata must be a mapping")
    normalized_metadata = dict(metadata or {})
    if VIEW_GENERATION_MEMBERS_METADATA_KEY in normalized_metadata:
        raise StorageValidationError(
            f"{VIEW_GENERATION_MEMBERS_METADATA_KEY} is reserved catalog metadata"
        )
    if normalized_members:
        normalized_metadata[VIEW_GENERATION_MEMBERS_METADATA_KEY] = list(
            normalized_members
        )
    # Round-trip through the shared canonicalizer so callers receive detached,
    # JSON-compatible values rather than references to mutable input objects.
    return json.loads(canonical_json(normalized_metadata)), normalized_members


def view_generation_member_digests(
    object_digest: str,
    metadata: Mapping[str, Any],
) -> tuple[str, ...]:
    if not isinstance(metadata, Mapping):
        raise StorageValidationError("view generation metadata must be a mapping")
    if VIEW_GENERATION_MEMBERS_METADATA_KEY not in metadata:
        return ()
    raw_members = metadata[VIEW_GENERATION_MEMBERS_METADATA_KEY]
    if not isinstance(raw_members, list) or not raw_members:
        raise StorageValidationError(
            "view generation member metadata must be a nonempty digest list"
        )
    if len(raw_members) > MAX_VIEW_GENERATION_MEMBERS:
        raise StorageValidationError("view generation has too many member objects")
    if any(type(value) is not str for value in raw_members):
        raise StorageValidationError(
            "view generation member metadata must contain exact digest strings"
        )
    normalized = tuple(normalize_digest(value) for value in raw_members)
    if tuple(raw_members) != normalized:
        raise StorageValidationError(
            "view generation member metadata must use canonical bare digests"
        )
    if normalized != tuple(sorted(set(normalized))):
        raise StorageValidationError(
            "view generation member metadata must be sorted and unique"
        )
    if normalize_digest(object_digest) in normalized:
        raise StorageValidationError(
            "the primary object must not also be a member object"
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


def _bounded_text(value: str, field: str, *, max_length: int) -> str:
    normalized = _required_text(value, field)
    if "\x00" in normalized:
        raise StorageValidationError(f"{field} must not contain NUL")
    if len(normalized) > max_length:
        raise StorageValidationError(f"{field} must not exceed {max_length} characters")
    return normalized


def _optional_bounded_text(
    value: str | None, field: str, *, max_length: int
) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field, max_length=max_length)


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


def _nonnegative_integer(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageValidationError(f"{field} must be an integer")
    if value < 0:
        raise StorageValidationError(f"{field} must not be negative")
    return value


def _exact_nonnegative_integer(value: int, field: str) -> int:
    """Validate new catalog counters without accepting ``int`` subclasses."""

    if type(value) is not int or value < 0:
        raise StorageValidationError(f"{field} must be an exact non-negative integer")
    return value


def _exact_nonnegative_int64(value: int, field: str) -> int:
    normalized = _exact_nonnegative_integer(value, field)
    if normalized > _CATALOG_INT64_MAX:
        raise StorageValidationError(f"{field} is outside catalog range")
    return normalized


def _optional_nonnegative_integer(value: int | None, field: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_integer(value, field)


def _canonical_json_object(value: str, field: str) -> tuple[str, dict[str, Any]]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageValidationError(f"{field} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise StorageValidationError(f"{field} must be a JSON object")
    return canonical_json(parsed), parsed


def assert_no_secret_fields(value: Any, *, source: str = "value") -> None:
    """Apply the shared credential policy with a storage validation error.

    The classifier lives outside storage so artifacts do not depend on the
    catalog layer; this wrapper preserves the public storage exception contract.
    """

    try:
        _assert_no_secret_fields(value, source=source)
    except SecretFieldError as exc:
        raise StorageValidationError(str(exc)) from exc


def _reject_secret_fields(value: Any, *, path: str = "request") -> None:
    assert_no_secret_fields(value, source="index job request")


def snapshot_index_job_event_payload(value: object) -> dict[str, Any]:
    """Detach one bounded, exact-JSON, secret-free job event payload."""

    nodes = 0
    text_size = 0

    def snapshot_mapping(
        current: Mapping[object, object], depth: int
    ) -> dict[str, Any]:
        nonlocal text_size
        try:
            before_length = len(current)
            iterator = iter(current)
        except Exception as exc:
            raise StorageValidationError(
                "index job event payload could not be snapshotted"
            ) from exc
        if before_length > INDEX_JOB_EVENT_PAYLOAD_MAX_NODES - nodes:
            raise StorageValidationError(
                "index job event payload exceeds its node limit"
            )

        result: dict[str, Any] = {}
        observed: set[str] = set()
        try:
            while True:
                try:
                    key = next(iterator)
                except StopIteration:
                    break
                if nodes >= INDEX_JOB_EVENT_PAYLOAD_MAX_NODES:
                    raise StorageValidationError(
                        "index job event payload exceeds its node limit"
                    )
                if (
                    type(key) is not str
                    or not key
                    or len(key) > INDEX_JOB_EVENT_PAYLOAD_MAX_KEY_CHARS
                    or "\x00" in key
                ):
                    raise StorageValidationError(
                        "index job event payload contains an invalid object key"
                    )
                if key in observed:
                    raise StorageValidationError(
                        "index job event payload contains a duplicate object key"
                    )
                observed.add(key)
                text_size += len(key)
                if text_size > INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS:
                    raise StorageValidationError(
                        "index job event payload contains invalid text"
                    )
                result[key] = snapshot(current[key], depth + 1)
            after_length = len(current)
        except StorageValidationError:
            raise
        except Exception as exc:
            raise StorageValidationError(
                "index job event payload could not be snapshotted"
            ) from exc
        if before_length != after_length or after_length != len(observed):
            raise StorageValidationError(
                "index job event payload changed while snapshotted"
            )
        return result

    def snapshot_list(current: list[object], depth: int) -> list[Any]:
        try:
            before_length = len(current)
            iterator = iter(current)
        except Exception as exc:
            raise StorageValidationError(
                "index job event payload could not be snapshotted"
            ) from exc
        if before_length > INDEX_JOB_EVENT_PAYLOAD_MAX_NODES - nodes:
            raise StorageValidationError(
                "index job event payload exceeds its node limit"
            )
        result: list[Any] = []
        try:
            while True:
                try:
                    child = next(iterator)
                except StopIteration:
                    break
                if nodes >= INDEX_JOB_EVENT_PAYLOAD_MAX_NODES:
                    raise StorageValidationError(
                        "index job event payload exceeds its node limit"
                    )
                result.append(snapshot(child, depth + 1))
            after_length = len(current)
        except StorageValidationError:
            raise
        except Exception as exc:
            raise StorageValidationError(
                "index job event payload could not be snapshotted"
            ) from exc
        if before_length != after_length or after_length != len(result):
            raise StorageValidationError(
                "index job event payload changed while snapshotted"
            )
        return result

    def snapshot(current: object, depth: int) -> Any:
        nonlocal nodes, text_size
        nodes += 1
        if nodes > INDEX_JOB_EVENT_PAYLOAD_MAX_NODES:
            raise StorageValidationError(
                "index job event payload exceeds its node limit"
            )
        if depth > INDEX_JOB_EVENT_PAYLOAD_MAX_DEPTH:
            raise StorageValidationError(
                "index job event payload exceeds its depth limit"
            )
        if current is None or type(current) is bool:
            return current
        if type(current) is str:
            text_size += len(current)
            if text_size > INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS or "\x00" in current:
                raise StorageValidationError(
                    "index job event payload contains invalid text"
                )
            return current
        if type(current) is int:
            if not -(2**63) <= current < 2**63:
                raise StorageValidationError(
                    "index job event payload contains an invalid integer"
                )
            return current
        if type(current) is float:
            if not math.isfinite(current):
                raise StorageValidationError(
                    "index job event payload contains a non-finite number"
                )
            return current
        if type(current) is list:
            return snapshot_list(current, depth)
        if type(current) is dict:
            return snapshot_mapping(current, depth)
        raise StorageValidationError(
            "index job event payload contains a non-exact JSON value"
        )

    if not isinstance(value, Mapping):
        raise StorageValidationError("index job event payload must be an exact object")
    nodes = 1
    payload = snapshot_mapping(value, 1)
    encoded = canonical_json(payload)
    if len(encoded) > INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS:
        raise StorageValidationError("index job event payload exceeds 16384 bytes")
    assert_no_secret_fields(payload, source="index job event payload")
    return payload


class IndexJobStatus(str, Enum):
    """Persisted lifecycle states for an index job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IndexJobCompletion(str, Enum):
    """M1 lease-release outcomes; success is reserved for M2 publication."""

    REQUEUE = "requeue"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IndexJobEventKind(str, Enum):
    """Bounded execution event shapes accepted by the control plane."""

    PROGRESS = "progress"
    VIEW_RESULT = "view_result"


class IndexJobEffectiveMode(str, Enum):
    """Effective builder behavior reported after capability resolution."""

    FULL = "full"
    INCREMENTAL = "incremental"
    REBUILD_FALLBACK = "rebuild_fallback"
    UNAVAILABLE = "unavailable"


class IndexJobViewOutcome(str, Enum):
    """One requested view's terminal attempt-local execution result."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class NamespaceIdentity:
    """Backend-neutral stable identity for one logical namespace name."""

    name: str

    def __post_init__(self) -> None:
        if type(self) is not NamespaceIdentity or type(self.name) is not str:
            raise StorageValidationError(
                "namespace identity must use exact model and text types"
            )
        if str.__len__(self.name) > 32_768 or "\x00" in self.name:
            raise StorageValidationError("namespace name is out of bounds")
        normalized = str.strip(self.name)
        if not normalized:
            raise StorageValidationError("namespace name must not be empty")
        object.__setattr__(self, "name", normalized)

    @property
    def namespace_id(self) -> str:
        if self.name == DEFAULT_NAMESPACE_NAME:
            return DEFAULT_NAMESPACE_ID
        return content_id("ns", {"name": self.name})


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


def _exact_job_output_object(record: object, *, label: str) -> ObjectRecord:
    """Detach one exact catalog object record for a job output."""

    if type(record) is not ObjectRecord:
        raise StorageValidationError(f"{label} must be an exact ObjectRecord")
    values = (record.digest, record.byte_size, record.storage_key, record.media_type)
    if tuple(type(value) for value in values) != (str, int, str, str):
        raise StorageValidationError(f"{label} fields must use exact str/int types")
    detached = ObjectRecord(
        digest=values[0],
        byte_size=values[1],
        storage_key=values[2],
        media_type=values[3],
    )
    if detached.byte_size > _CATALOG_INT64_MAX:
        raise StorageValidationError(f"{label} byte size exceeds catalog int64 range")
    _bounded_text(detached.storage_key, f"{label} storage key", max_length=4_096)
    _bounded_text(detached.media_type, f"{label} media type", max_length=256)
    if detached != record:
        raise StorageValidationError(f"{label} is not canonical")
    return detached


@dataclass(frozen=True, slots=True)
class IndexJobViewOutput:
    """One exact, receipt-verified output offered for a requested job view.

    The explicit profile binding lets the catalog reject a mismatch with the
    persisted job request. ``metadata_json`` contains caller metadata only;
    :attr:`generation_metadata` injects the canonical, identity-bearing
    compound-member digest list.
    """

    view_type: str
    profile_id: str
    object_record: ObjectRecord
    schema_version: str
    metadata_json: str = "{}"
    member_object_records: tuple[ObjectRecord, ...] = ()

    def __post_init__(self) -> None:
        if tuple(
            type(value)
            for value in (
                self.view_type,
                self.profile_id,
                self.schema_version,
                self.metadata_json,
            )
        ) != (str, str, str, str):
            raise StorageValidationError(
                "index job output text fields must use exact str values"
            )
        view_type = _bounded_text(self.view_type, "view type", max_length=128)
        profile_id = _bounded_text(self.profile_id, "profile ID", max_length=96)
        schema_version = _bounded_text(
            self.schema_version, "view schema version", max_length=128
        )
        primary = _exact_job_output_object(
            self.object_record,
            label="index job output object",
        )
        metadata_json, metadata = _canonical_json_object(
            self.metadata_json,
            "index job output metadata",
        )
        if len(metadata_json) > 65_536:
            raise StorageValidationError(
                "index job output metadata must not exceed 65536 characters"
            )
        assert_no_secret_fields(metadata, source="index job output metadata")

        if type(self.member_object_records) is not tuple:
            raise StorageValidationError(
                "index job output member objects must be an exact tuple"
            )
        if len(self.member_object_records) > MAX_VIEW_GENERATION_MEMBERS:
            raise StorageValidationError("view generation has too many member objects")
        members = tuple(
            _exact_job_output_object(
                member,
                label="index job output member object",
            )
            for member in self.member_object_records
        )
        ordered = tuple(sorted(members, key=lambda member: member.digest))
        member_digests = tuple(member.digest for member in ordered)
        # This shared normalizer rejects the reserved metadata key, duplicate
        # members, and a primary digest repeated as a member.
        normalize_view_generation_metadata(
            primary.digest,
            metadata,
            member_object_digests=member_digests,
        )

        object.__setattr__(self, "view_type", view_type)
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "object_record", primary)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "metadata_json", metadata_json)
        object.__setattr__(self, "member_object_records", ordered)

    @classmethod
    def create(
        cls,
        view_type: str,
        profile_id: str,
        object_record: ObjectRecord,
        *,
        schema_version: str,
        metadata: Mapping[str, Any] | None = None,
        member_object_records: Sequence[ObjectRecord] = (),
    ) -> IndexJobViewOutput:
        if isinstance(member_object_records, (str, bytes, bytearray)):
            raise StorageValidationError(
                "index job output member objects must be a sequence"
            )
        try:
            members = tuple(
                islice(
                    iter(member_object_records),
                    MAX_VIEW_GENERATION_MEMBERS + 1,
                )
            )
        except TypeError as exc:
            raise StorageValidationError(
                "index job output member objects must be a sequence"
            ) from exc
        if len(members) > MAX_VIEW_GENERATION_MEMBERS:
            raise StorageValidationError("view generation has too many member objects")
        return cls(
            view_type=view_type,
            profile_id=profile_id,
            object_record=object_record,
            schema_version=schema_version,
            metadata_json=canonical_json({} if metadata is None else metadata),
            member_object_records=members,
        )

    @property
    def metadata(self) -> dict[str, Any]:
        return json.loads(self.metadata_json)

    @property
    def generation_metadata(self) -> dict[str, Any]:
        normalized, _members = normalize_view_generation_metadata(
            self.object_record.digest,
            self.metadata,
            member_object_digests=tuple(
                member.digest for member in self.member_object_records
            ),
        )
        return normalized

    @property
    def identity(self) -> dict[str, Any]:
        def object_identity(record: ObjectRecord) -> dict[str, Any]:
            return {
                "digest": record.digest,
                "storage_key": record.storage_key,
                "byte_size": record.byte_size,
                "media_type": record.media_type,
            }

        return {
            "view_type": self.view_type,
            "profile_id": self.profile_id,
            "schema_version": self.schema_version,
            "metadata": self.generation_metadata,
            "object": object_identity(self.object_record),
            "member_objects": [
                object_identity(record) for record in self.member_object_records
            ],
        }


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
        view_generation_member_digests(self.object_digest, metadata)
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
        member_object_digests: Sequence[str] = (),
    ) -> ViewGeneration:
        normalized_metadata, _members = normalize_view_generation_metadata(
            object_record.digest,
            metadata,
            member_object_digests=member_object_digests,
        )
        return cls(
            repository_id=source.repository_id,
            source_revision_id=source.source_revision_id,
            profile=profile,
            object_digest=object_record.digest,
            schema_version=schema_version,
            metadata_json=canonical_json(normalized_metadata),
        )

    @property
    def profile_id(self) -> str:
        return self.profile.profile_id

    @property
    def view_type(self) -> str:
        return self.profile.view_type

    @property
    def member_object_digests(self) -> tuple[str, ...]:
        return view_generation_member_digests(
            self.object_digest,
            json.loads(self.metadata_json),
        )

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


class IndexJobRequestedMode(str, Enum):
    """Requested builder behavior before capability fallback is resolved."""

    AUTO = "auto"
    FULL = "full"
    INCREMENTAL = "incremental"


@dataclass(frozen=True, slots=True)
class IndexJobViewRecord:
    """Immutable requested profile, mode, and requirement for one job view."""

    job_id: str
    view_type: str
    profile_id: str
    requested_mode: IndexJobRequestedMode
    required: bool

    def __post_init__(self) -> None:
        try:
            mode = IndexJobRequestedMode(self.requested_mode)
        except ValueError as exc:
            raise StorageValidationError(
                f"invalid requested index mode: {self.requested_mode}"
            ) from exc
        if not isinstance(self.required, bool):
            raise StorageValidationError("index job view required must be boolean")
        object.__setattr__(
            self, "job_id", _bounded_text(self.job_id, "job ID", max_length=80)
        )
        object.__setattr__(
            self,
            "view_type",
            _bounded_text(self.view_type, "view type", max_length=128),
        )
        object.__setattr__(
            self,
            "profile_id",
            _bounded_text(self.profile_id, "profile ID", max_length=96),
        )
        object.__setattr__(self, "requested_mode", mode)

    @property
    def request(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "requested_mode": self.requested_mode.value,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class IndexJobRequest:
    """Canonical, secret-free request scoped by an idempotency key."""

    repository_id: str
    source_revision_id: str
    ref_name: str
    idempotency_key: str
    expected_ref_generation: int
    max_attempts: int
    request_json: str

    def __post_init__(self) -> None:
        repository = _bounded_text(self.repository_id, "repository ID", max_length=96)
        source = _bounded_text(
            self.source_revision_id, "source revision ID", max_length=96
        )
        ref_name = _bounded_text(self.ref_name, "ref name", max_length=512)
        idempotency_key = _bounded_text(
            self.idempotency_key, "idempotency key", max_length=256
        )
        expected = _nonnegative_integer(
            self.expected_ref_generation, "expected ref generation"
        )
        if expected > _CATALOG_INT64_MAX:
            raise StorageValidationError(
                "expected ref generation exceeds catalog int64 range"
            )
        max_attempts = _nonnegative_integer(self.max_attempts, "maximum attempts")
        if max_attempts < 1 or max_attempts > 1_000:
            raise StorageValidationError("maximum attempts must be between 1 and 1000")
        request_json, request = _canonical_json_object(
            self.request_json, "index job request"
        )
        if len(request_json) > 65_536:
            raise StorageValidationError(
                "index job request must not exceed 65536 characters"
            )
        _reject_secret_fields(request)
        if set(request) != {"contract", "views"}:
            raise StorageValidationError(
                "index job request must contain exactly contract and views"
            )
        if request.get("contract") != INDEX_JOB_REQUEST_CONTRACT:
            raise StorageValidationError(
                "index job request contract must be " f"{INDEX_JOB_REQUEST_CONTRACT!r}"
            )
        views = request.get("views")
        if not isinstance(views, dict) or not views:
            raise StorageValidationError(
                "index job request views must be a non-empty JSON object"
            )
        if len(views) > 64:
            raise StorageValidationError("index job request must not exceed 64 views")
        for view_type, view_request in views.items():
            if not isinstance(view_request, dict) or set(view_request) != {
                "profile_id",
                "requested_mode",
                "required",
            }:
                raise StorageValidationError(
                    "index job view request must contain exactly profile_id, "
                    f"requested_mode, and required: {view_type}"
                )
            normalized_view = IndexJobViewRecord(
                job_id="job_" + "0" * 64,
                view_type=view_type,
                profile_id=view_request.get("profile_id"),
                requested_mode=view_request.get("requested_mode"),
                required=view_request.get("required"),
            )
            if is_index_job_supporting_view(normalized_view.view_type):
                raise StorageValidationError(
                    "index job requests cannot include reserved supporting views: "
                    f"{view_type}"
                )
            if (
                normalized_view.view_type != view_type
                or normalized_view.profile_id != view_request["profile_id"]
                or normalized_view.requested_mode.value
                != view_request["requested_mode"]
            ):
                raise StorageValidationError(
                    f"index job view request strings must be canonical: {view_type}"
                )
        object.__setattr__(self, "repository_id", repository)
        object.__setattr__(self, "source_revision_id", source)
        object.__setattr__(self, "ref_name", ref_name)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "expected_ref_generation", expected)
        object.__setattr__(self, "max_attempts", max_attempts)
        object.__setattr__(self, "request_json", request_json)

    @classmethod
    def create(
        cls,
        repository_id: str,
        source_revision_id: str,
        idempotency_key: str,
        request: Mapping[str, Any],
        *,
        ref_name: str = "main",
        expected_ref_generation: int = 0,
        max_attempts: int = 3,
    ) -> IndexJobRequest:
        return cls(
            repository_id=repository_id,
            source_revision_id=source_revision_id,
            ref_name=ref_name,
            idempotency_key=idempotency_key,
            expected_ref_generation=expected_ref_generation,
            max_attempts=max_attempts,
            request_json=canonical_json(request),
        )

    @property
    def request(self) -> dict[str, Any]:
        return json.loads(self.request_json)

    @property
    def contract(self) -> str:
        return str(self.request["contract"])

    @property
    def view_requests(self) -> tuple[IndexJobViewRecord, ...]:
        return tuple(
            IndexJobViewRecord(
                job_id=self.job_id,
                view_type=view_type,
                profile_id=view_request["profile_id"],
                requested_mode=view_request["requested_mode"],
                required=view_request["required"],
            )
            for view_type, view_request in sorted(self.request["views"].items())
        )

    @property
    def job_id(self) -> str:
        return content_id(
            "job",
            {
                "repository_id": self.repository_id,
                "idempotency_key": self.idempotency_key,
            },
        )

    @property
    def request_digest(self) -> str:
        return content_id(
            "jobreq",
            {
                "repository_id": self.repository_id,
                "source_revision_id": self.source_revision_id,
                "ref_name": self.ref_name,
                "expected_ref_generation": self.expected_ref_generation,
                "max_attempts": self.max_attempts,
                "request": self.request,
            },
        )


@dataclass(frozen=True, slots=True)
class IndexJobRecord:
    """Backend-neutral persisted state for one index job."""

    job_id: str
    repository_id: str
    source_revision_id: str
    ref_name: str
    idempotency_key: str
    expected_ref_generation: int
    max_attempts: int
    request_json: str
    request_digest: str
    status: IndexJobStatus
    cancel_requested: bool
    attempt_count: int
    result_snapshot_id: str | None
    error_code: str | None
    error_message: str | None
    created_at_ms: int
    updated_at_ms: int
    started_at_ms: int | None
    finished_at_ms: int | None

    def __post_init__(self) -> None:
        request = IndexJobRequest(
            repository_id=self.repository_id,
            source_revision_id=self.source_revision_id,
            ref_name=self.ref_name,
            idempotency_key=self.idempotency_key,
            expected_ref_generation=self.expected_ref_generation,
            max_attempts=self.max_attempts,
            request_json=self.request_json,
        )
        try:
            status = IndexJobStatus(self.status)
        except ValueError as exc:
            raise StorageValidationError(
                f"invalid index job status: {self.status}"
            ) from exc
        if self.job_id != request.job_id:
            raise StorageValidationError(
                "job ID does not match its idempotency identity"
            )
        if self.request_digest != request.request_digest:
            raise StorageValidationError(
                "job request digest does not match its request"
            )
        snapshot = _optional_bounded_text(
            self.result_snapshot_id, "result snapshot ID", max_length=96
        )
        error_code = _optional_bounded_text(
            self.error_code, "job error code", max_length=128
        )
        error_message = _optional_bounded_text(
            self.error_message, "job error message", max_length=4_096
        )
        if error_message is not None and error_code is None:
            raise StorageValidationError("job error message requires an error code")
        terminal = status in {
            IndexJobStatus.SUCCEEDED,
            IndexJobStatus.FAILED,
            IndexJobStatus.CANCELLED,
        }
        if terminal != (self.finished_at_ms is not None):
            raise StorageValidationError("terminal index jobs require a finish time")
        if status is IndexJobStatus.SUCCEEDED and snapshot is None:
            raise StorageValidationError(
                "successful index jobs require a result snapshot"
            )
        if status is not IndexJobStatus.SUCCEEDED and snapshot is not None:
            raise StorageValidationError(
                "only successful index jobs may have a result snapshot"
            )
        if status is IndexJobStatus.FAILED and error_code is None:
            raise StorageValidationError("failed index jobs require an error code")
        if status is IndexJobStatus.SUCCEEDED and error_code is not None:
            raise StorageValidationError("successful index jobs cannot have an error")
        if status is IndexJobStatus.RUNNING and self.started_at_ms is None:
            raise StorageValidationError("running index jobs require a start time")
        if status is IndexJobStatus.CANCELLED and not self.cancel_requested:
            raise StorageValidationError("cancelled index jobs require cancellation")
        if type(self.cancel_requested) is not bool:
            raise StorageValidationError("cancel requested must be boolean")

        object.__setattr__(self, "job_id", request.job_id)
        object.__setattr__(self, "repository_id", request.repository_id)
        object.__setattr__(self, "source_revision_id", request.source_revision_id)
        object.__setattr__(self, "ref_name", request.ref_name)
        object.__setattr__(self, "idempotency_key", request.idempotency_key)
        object.__setattr__(
            self, "expected_ref_generation", request.expected_ref_generation
        )
        object.__setattr__(self, "max_attempts", request.max_attempts)
        object.__setattr__(self, "request_json", request.request_json)
        object.__setattr__(self, "request_digest", request.request_digest)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "result_snapshot_id", snapshot)
        object.__setattr__(self, "error_code", error_code)
        object.__setattr__(self, "error_message", error_message)
        object.__setattr__(
            self,
            "attempt_count",
            _exact_nonnegative_integer(self.attempt_count, "job attempt count"),
        )
        if self.attempt_count > self.max_attempts:
            raise StorageValidationError("job attempts exceed the configured maximum")
        if status is IndexJobStatus.QUEUED and (
            self.cancel_requested or self.attempt_count >= self.max_attempts
        ):
            raise StorageValidationError(
                "queued index jobs must remain uncancelled and retryable"
            )
        if (
            status
            in {
                IndexJobStatus.RUNNING,
                IndexJobStatus.SUCCEEDED,
                IndexJobStatus.FAILED,
            }
            and self.attempt_count < 1
        ):
            raise StorageValidationError(
                f"{status.value} index jobs require at least one attempt"
            )
        for field in ("created_at_ms", "updated_at_ms"):
            object.__setattr__(
                self, field, _nonnegative_integer(getattr(self, field), field)
            )
        for field in ("started_at_ms", "finished_at_ms"):
            object.__setattr__(
                self,
                field,
                _optional_nonnegative_integer(getattr(self, field), field),
            )
        if self.updated_at_ms < self.created_at_ms:
            raise StorageValidationError("job update time precedes creation")
        if self.started_at_ms is not None and (
            self.started_at_ms < self.created_at_ms
            or self.updated_at_ms < self.started_at_ms
        ):
            raise StorageValidationError("job start time is out of order")
        if self.finished_at_ms is not None:
            finish_floor = self.started_at_ms or self.created_at_ms
            if (
                self.finished_at_ms < finish_floor
                or self.updated_at_ms < self.finished_at_ms
            ):
                raise StorageValidationError("job finish time is out of order")

    @property
    def request(self) -> dict[str, Any]:
        return json.loads(self.request_json)


@dataclass(frozen=True, slots=True)
class IndexJobCurrentResult:
    """One successful job whose publication is the exact current ref state."""

    job: IndexJobRecord
    ref_generation: int
    ref_updated_at: str

    def __post_init__(self) -> None:
        if type(self) is not IndexJobCurrentResult:
            raise StorageValidationError("current job result must use the exact model")
        if type(self.job) is not IndexJobRecord:
            raise StorageValidationError(
                "current job result requires an exact job record"
            )
        job = self.job
        if (
            job.status is not IndexJobStatus.SUCCEEDED
            or job.cancel_requested
            or job.result_snapshot_id is None
            or job.started_at_ms is None
            or job.finished_at_ms is None
            or job.updated_at_ms != job.finished_at_ms
        ):
            raise StorageValidationError(
                "current job result requires an exact successful publication"
            )
        generation = _exact_nonnegative_integer(
            self.ref_generation,
            "current job result ref generation",
        )
        if generation < 1 or generation > _CATALOG_INT64_MAX:
            raise StorageValidationError(
                "current job result ref generation is outside catalog range"
            )
        expected_generations = {job.expected_ref_generation + 1}
        if job.expected_ref_generation > 0:
            expected_generations.add(job.expected_ref_generation)
        if generation not in expected_generations:
            raise StorageValidationError(
                "current job result generation differs from its publication fence"
            )
        object.__setattr__(self, "ref_generation", generation)
        object.__setattr__(
            self,
            "ref_updated_at",
            canonical_utc_timestamp(
                self.ref_updated_at,
                "current job result ref updated_at",
            ),
        )

    @property
    def snapshot_id(self) -> str:
        """Return the successful snapshot authenticated by ``job``."""

        snapshot_id = self.job.result_snapshot_id
        if snapshot_id is None:  # pragma: no cover - constructor proves success
            raise AssertionError("current job result has no snapshot")
        return snapshot_id


@dataclass(frozen=True, slots=True)
class RefJobLease:
    """Active, fenced ownership of the single publisher slot for a ref."""

    repository_id: str
    ref_name: str
    job_id: str
    owner_id: str
    fencing_token: int
    acquired_at_ms: int
    heartbeat_at_ms: int
    lease_expires_at_ms: int

    def __post_init__(self) -> None:
        token = _exact_nonnegative_integer(self.fencing_token, "lease fencing token")
        if token < 1 or token > _CATALOG_INT64_MAX:
            raise StorageValidationError("lease fencing token is outside catalog range")
        object.__setattr__(
            self,
            "repository_id",
            _bounded_text(self.repository_id, "repository ID", max_length=96),
        )
        object.__setattr__(
            self, "ref_name", _bounded_text(self.ref_name, "ref name", max_length=512)
        )
        object.__setattr__(
            self, "job_id", _bounded_text(self.job_id, "job ID", max_length=80)
        )
        object.__setattr__(
            self, "owner_id", _bounded_text(self.owner_id, "owner ID", max_length=256)
        )
        object.__setattr__(self, "fencing_token", token)
        for field in ("acquired_at_ms", "heartbeat_at_ms", "lease_expires_at_ms"):
            object.__setattr__(
                self, field, _nonnegative_integer(getattr(self, field), field)
            )
        if not self.acquired_at_ms <= self.heartbeat_at_ms < self.lease_expires_at_ms:
            raise StorageValidationError("lease timestamps are not ordered")


@dataclass(frozen=True, slots=True)
class IndexJobAttemptRecord:
    """Immutable authority captured when one fenced attempt starts."""

    job_id: str
    attempt_count: int
    repository_id: str
    ref_name: str
    request_digest: str
    owner_id: str
    fencing_token: int
    started_at_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "job_id", _bounded_text(self.job_id, "job ID", max_length=80)
        )
        object.__setattr__(
            self,
            "repository_id",
            _bounded_text(self.repository_id, "repository ID", max_length=96),
        )
        object.__setattr__(
            self, "ref_name", _bounded_text(self.ref_name, "ref name", max_length=512)
        )
        object.__setattr__(
            self,
            "request_digest",
            _bounded_text(self.request_digest, "request digest", max_length=96),
        )
        object.__setattr__(
            self, "owner_id", _bounded_text(self.owner_id, "owner ID", max_length=256)
        )
        attempt = _exact_nonnegative_integer(self.attempt_count, "job attempt count")
        token = _exact_nonnegative_integer(self.fencing_token, "fencing token")
        if attempt < 1 or attempt > 1_000:
            raise StorageValidationError("job attempt count must be between 1 and 1000")
        if token < 1 or token > _CATALOG_INT64_MAX:
            raise StorageValidationError("fencing token is outside catalog range")
        object.__setattr__(self, "attempt_count", attempt)
        object.__setattr__(self, "fencing_token", token)
        object.__setattr__(
            self,
            "started_at_ms",
            _exact_nonnegative_int64(self.started_at_ms, "attempt start time"),
        )


@dataclass(frozen=True, slots=True)
class IndexJobAttemptCompletionRecord:
    """Immutable non-success closure for one fenced attempt."""

    job_id: str
    attempt_count: int
    owner_id: str
    fencing_token: int
    outcome: IndexJobCompletion
    error_code: str
    error_message: str | None
    completed_at_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "job_id", _bounded_text(self.job_id, "job ID", max_length=80)
        )
        object.__setattr__(
            self, "owner_id", _bounded_text(self.owner_id, "owner ID", max_length=256)
        )
        attempt = _exact_nonnegative_integer(self.attempt_count, "job attempt count")
        token = _exact_nonnegative_integer(self.fencing_token, "fencing token")
        if attempt < 1 or attempt > 1_000:
            raise StorageValidationError("job attempt count must be between 1 and 1000")
        if token < 1 or token > _CATALOG_INT64_MAX:
            raise StorageValidationError("fencing token is outside catalog range")
        try:
            outcome = IndexJobCompletion(self.outcome)
        except ValueError as exc:
            raise StorageValidationError(
                f"invalid job attempt completion: {self.outcome}"
            ) from exc
        object.__setattr__(self, "attempt_count", attempt)
        object.__setattr__(self, "fencing_token", token)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(
            self,
            "error_code",
            _bounded_text(self.error_code, "job error code", max_length=128),
        )
        object.__setattr__(
            self,
            "error_message",
            _optional_bounded_text(
                self.error_message, "job error message", max_length=4_096
            ),
        )
        object.__setattr__(
            self,
            "completed_at_ms",
            _exact_nonnegative_int64(self.completed_at_ms, "attempt completion time"),
        )


@dataclass(frozen=True, slots=True)
class IndexJobAttemptHeartbeat:
    """Atomic lease renewal plus cooperative-cancellation observation."""

    job_id: str
    attempt_count: int
    cancel_requested: bool
    lease: RefJobLease

    def __post_init__(self) -> None:
        job_id = _bounded_text(self.job_id, "job ID", max_length=80)
        attempt = _exact_nonnegative_integer(self.attempt_count, "job attempt count")
        if attempt < 1 or attempt > 1_000:
            raise StorageValidationError("job attempt count must be between 1 and 1000")
        if type(self.cancel_requested) is not bool:
            raise StorageValidationError("cancel requested must be boolean")
        if type(self.lease) is not RefJobLease:
            raise StorageValidationError("attempt heartbeat requires an exact lease")
        if self.lease.job_id != job_id:
            raise StorageValidationError(
                "attempt heartbeat lease belongs to another job"
            )
        object.__setattr__(self, "job_id", job_id)
        object.__setattr__(self, "attempt_count", attempt)


@dataclass(frozen=True, slots=True)
class IndexJobEventRecord:
    """One bounded, immutable attempt-local progress or view-result event."""

    sequence: int
    job_id: str
    attempt_count: int
    event_key: str
    kind: IndexJobEventKind
    owner_id: str
    fencing_token: int
    view_type: str | None
    effective_mode: IndexJobEffectiveMode | None
    outcome: IndexJobViewOutcome | None
    payload_json: str
    created_at_ms: int

    def __post_init__(self) -> None:
        sequence = _exact_nonnegative_integer(self.sequence, "job event sequence")
        attempt = _exact_nonnegative_integer(self.attempt_count, "job attempt count")
        token = _exact_nonnegative_integer(self.fencing_token, "fencing token")
        if sequence < 1 or sequence > _CATALOG_INT64_MAX:
            raise StorageValidationError("job event sequence is outside catalog range")
        if attempt < 1 or attempt > 1_000:
            raise StorageValidationError("job attempt count must be between 1 and 1000")
        if token < 1 or token > _CATALOG_INT64_MAX:
            raise StorageValidationError("fencing token is outside catalog range")
        try:
            kind = IndexJobEventKind(self.kind)
        except ValueError as exc:
            raise StorageValidationError(
                f"invalid job event kind: {self.kind}"
            ) from exc
        view_type = _optional_bounded_text(
            self.view_type, "job event view type", max_length=128
        )
        try:
            mode = (
                None
                if self.effective_mode is None
                else IndexJobEffectiveMode(self.effective_mode)
            )
        except ValueError as exc:
            raise StorageValidationError(
                f"invalid effective index mode: {self.effective_mode}"
            ) from exc
        try:
            outcome = (
                None if self.outcome is None else IndexJobViewOutcome(self.outcome)
            )
        except ValueError as exc:
            raise StorageValidationError(
                f"invalid index job view outcome: {self.outcome}"
            ) from exc
        if kind is IndexJobEventKind.PROGRESS:
            if mode is not None or outcome is not None:
                raise StorageValidationError(
                    "progress events cannot carry a mode or view outcome"
                )
        elif view_type is None or mode is None or outcome is None:
            raise StorageValidationError(
                "view-result events require view, mode, and outcome"
            )
        if type(self.payload_json) is not str:
            raise StorageValidationError(
                "index job event payload JSON must be exact text"
            )
        if (
            not self.payload_json
            or len(self.payload_json) > INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS
            or "\x00" in self.payload_json
        ):
            raise StorageValidationError(
                "index job event payload JSON is out of bounds"
            )
        try:
            parsed = json.loads(self.payload_json)
        except (TypeError, json.JSONDecodeError, RecursionError) as exc:
            raise StorageValidationError(
                "index job event payload must be valid JSON"
            ) from exc
        payload = snapshot_index_job_event_payload(parsed)
        payload_json = canonical_json(payload)

        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(
            self, "job_id", _bounded_text(self.job_id, "job ID", max_length=80)
        )
        object.__setattr__(self, "attempt_count", attempt)
        object.__setattr__(
            self,
            "event_key",
            _bounded_text(self.event_key, "job event key", max_length=128),
        )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self, "owner_id", _bounded_text(self.owner_id, "owner ID", max_length=256)
        )
        object.__setattr__(self, "fencing_token", token)
        object.__setattr__(self, "view_type", view_type)
        object.__setattr__(self, "effective_mode", mode)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "payload_json", payload_json)
        object.__setattr__(
            self,
            "created_at_ms",
            _exact_nonnegative_int64(self.created_at_ms, "job event time"),
        )

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        job_id: str,
        attempt_count: int,
        event_key: str,
        kind: IndexJobEventKind,
        owner_id: str,
        fencing_token: int,
        payload: Mapping[str, Any] | None = None,
        view_type: str | None = None,
        effective_mode: IndexJobEffectiveMode | None = None,
        outcome: IndexJobViewOutcome | None = None,
        created_at_ms: int,
    ) -> IndexJobEventRecord:
        frozen = snapshot_index_job_event_payload({} if payload is None else payload)
        return cls(
            sequence=sequence,
            job_id=job_id,
            attempt_count=attempt_count,
            event_key=event_key,
            kind=kind,
            owner_id=owner_id,
            fencing_token=fencing_token,
            view_type=view_type,
            effective_mode=effective_mode,
            outcome=outcome,
            payload_json=canonical_json(frozen),
            created_at_ms=created_at_ms,
        )

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)


@dataclass(frozen=True, slots=True)
class IndexJobRunnableCursor:
    """Stable keyset cursor for the advisory runnable-job scan."""

    created_at_ms: int
    job_id: str

    def __post_init__(self) -> None:
        created_at_ms = _exact_nonnegative_int64(
            self.created_at_ms, "runnable cursor time"
        )
        object.__setattr__(self, "created_at_ms", created_at_ms)
        object.__setattr__(
            self, "job_id", _bounded_text(self.job_id, "job ID", max_length=80)
        )


@dataclass(frozen=True, slots=True)
class IndexJobRunnableCycle:
    """Frozen catalog insertion watermark for one bounded runnable scan cycle."""

    max_job_sequence: int

    def __post_init__(self) -> None:
        if type(self) is not IndexJobRunnableCycle:
            raise StorageValidationError("runnable cycle must use the exact model")
        object.__setattr__(
            self,
            "max_job_sequence",
            _exact_nonnegative_int64(
                self.max_job_sequence,
                "runnable cycle job sequence",
            ),
        )


@dataclass(frozen=True, slots=True)
class IndexJobRunnablePage:
    """One deterministic, advisory page of jobs that may be claimable."""

    jobs: tuple[IndexJobRecord, ...]
    next_cursor: IndexJobRunnableCursor | None

    def __post_init__(self) -> None:
        if type(self.jobs) is not tuple or any(
            type(job) is not IndexJobRecord for job in self.jobs
        ):
            raise StorageValidationError(
                "runnable job page requires an exact tuple of job records"
            )
        ordering = tuple((job.created_at_ms, job.job_id) for job in self.jobs)
        if ordering != tuple(sorted(ordering)) or len(ordering) != len(set(ordering)):
            raise StorageValidationError("runnable job page ordering is not canonical")
        if (
            self.next_cursor is not None
            and type(self.next_cursor) is not IndexJobRunnableCursor
        ):
            raise StorageValidationError("runnable job page cursor is not canonical")


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
    "DEFAULT_NAMESPACE_ID",
    "DEFAULT_NAMESPACE_NAME",
    "INDEX_JOB_REQUEST_CONTRACT",
    "INDEX_JOB_PUBLICATION_CONTRACT",
    "INDEX_JOB_SUPPORTING_VIEW_PREFIX",
    "INDEX_JOB_EVENT_PAYLOAD_MAX_DEPTH",
    "INDEX_JOB_EVENT_PAYLOAD_MAX_KEY_CHARS",
    "INDEX_JOB_EVENT_PAYLOAD_MAX_NODES",
    "INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS",
    "MAX_INDEX_JOB_EVENTS_PER_ATTEMPT",
    "MAX_VIEW_GENERATION_MEMBERS",
    "IndexJobAttemptCompletionRecord",
    "IndexJobAttemptHeartbeat",
    "IndexJobAttemptRecord",
    "IndexJobCompletion",
    "IndexJobCurrentResult",
    "IndexJobEffectiveMode",
    "IndexJobEventKind",
    "IndexJobEventRecord",
    "IndexJobRecord",
    "IndexJobRequest",
    "IndexJobRequestedMode",
    "IndexJobRunnableCycle",
    "IndexJobRunnableCursor",
    "IndexJobRunnablePage",
    "IndexJobStatus",
    "IndexJobViewOutcome",
    "IndexJobViewOutput",
    "IndexJobViewRecord",
    "NamespaceIdentity",
    "ObjectRecord",
    "PublishConflict",
    "PublishedSnapshot",
    "RefState",
    "RepositoryIdentity",
    "RefJobLease",
    "SnapshotView",
    "SourceRevision",
    "StorageError",
    "StorageIntegrityError",
    "StorageNotFound",
    "StorageValidationError",
    "VIEW_GENERATION_MEMBERS_METADATA_KEY",
    "ViewGeneration",
    "ViewProfile",
    "assert_no_secret_fields",
    "canonical_utc_timestamp",
    "canonical_json",
    "content_id",
    "is_index_job_supporting_view",
    "normalize_digest",
    "normalize_view_generation_metadata",
    "snapshot_index_job_event_payload",
    "view_generation_member_digests",
]
