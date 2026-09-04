# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Small immutable identities used by the H1 persistence experiment."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_MAX_TEXT_BYTES = 4096
_MAX_IDENTITY_JSON_BYTES = 1024 * 1024


class StorageError(RuntimeError):
    """Base class for experimental persistence failures."""


class StorageIntegrityError(StorageError):
    """Persisted bytes or metadata differ from their immutable identity."""


class StorageNotFound(StorageError):
    """A requested artifact, snapshot, or ref does not exist."""


class PublishConflict(StorageError):
    """A ref compare-and-swap precondition no longer holds."""


class StorageValidationError(StorageError, ValueError):
    """Input cannot form a valid persistence identity."""


def required_text(value: object, *, label: str) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise StorageValidationError(f"{label} must be non-empty text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise StorageValidationError(f"{label} must be valid Unicode") from exc
    if len(encoded) > _MAX_TEXT_BYTES:
        raise StorageValidationError(
            f"{label} exceeds its {_MAX_TEXT_BYTES}-byte limit"
        )
    return value


def normalize_digest(value: object, *, label: str = "digest") -> str:
    normalized = required_text(value, label=label).lower()
    if normalized.startswith("sha256:"):
        normalized = normalized[7:]
    if _DIGEST_RE.fullmatch(normalized) is None:
        raise StorageValidationError(
            f"{label} must be 64 lowercase hexadecimal characters"
        )
    return normalized


def canonical_json(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise StorageValidationError("identity payload must be a JSON object")
    try:
        encoded = json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise StorageValidationError("identity payload is not canonical JSON") from exc
    if len(encoded) > _MAX_IDENTITY_JSON_BYTES:
        raise StorageValidationError("identity payload exceeds its byte limit")
    return encoded.decode("ascii")


def content_id(prefix: str, value: Mapping[str, Any]) -> str:
    prefix = required_text(prefix, label="content ID prefix")
    digest = hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()
    return f"{prefix}_{digest}"


def _nonnegative_int(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise StorageValidationError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class Generation:
    """One immutable portable BM25 artifact generation."""

    generation_id: str
    repository: str
    commit: str
    source_fingerprint: str
    view_type: str
    metadata_digest: str
    archive_digest: str
    archive_size: int
    file_count: int
    byte_count: int

    def __post_init__(self) -> None:
        repository = required_text(self.repository, label="repository")
        commit = required_text(self.commit, label="commit").lower()
        if _COMMIT_RE.fullmatch(commit) is None:
            raise StorageValidationError("commit must be a full lowercase Git SHA")
        source_fingerprint = required_text(
            self.source_fingerprint,
            label="source fingerprint",
        )
        if self.view_type != "bm25":
            raise StorageValidationError("H1 supports exactly the BM25 view")
        metadata_digest = normalize_digest(
            self.metadata_digest,
            label="context metadata digest",
        )
        archive_digest = normalize_digest(
            self.archive_digest,
            label="archive digest",
        )
        archive_size = _nonnegative_int(self.archive_size, label="archive size")
        file_count = _nonnegative_int(self.file_count, label="artifact file count")
        byte_count = _nonnegative_int(self.byte_count, label="artifact byte count")
        expected_id = f"gen_{metadata_digest}"
        if self.generation_id != expected_id:
            raise StorageValidationError(
                "generation ID does not match context metadata content"
            )
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "commit", commit)
        object.__setattr__(self, "source_fingerprint", source_fingerprint)
        object.__setattr__(self, "metadata_digest", metadata_digest)
        object.__setattr__(self, "archive_digest", archive_digest)
        object.__setattr__(self, "archive_size", archive_size)
        object.__setattr__(self, "file_count", file_count)
        object.__setattr__(self, "byte_count", byte_count)

    @classmethod
    def create(
        cls,
        *,
        repository: str,
        commit: str,
        source_fingerprint: str,
        metadata_digest: str,
        archive_digest: str,
        archive_size: int,
        file_count: int,
        byte_count: int,
    ) -> "Generation":
        metadata_digest = normalize_digest(
            metadata_digest,
            label="context metadata digest",
        )
        return cls(
            generation_id=f"gen_{metadata_digest}",
            repository=repository,
            commit=commit,
            source_fingerprint=source_fingerprint,
            view_type="bm25",
            metadata_digest=metadata_digest,
            archive_digest=archive_digest,
            archive_size=archive_size,
            file_count=file_count,
            byte_count=byte_count,
        )


@dataclass(frozen=True, slots=True)
class Snapshot:
    """An immutable compatible set of generations for one source identity."""

    snapshot_id: str
    repository: str
    commit: str
    source_fingerprint: str
    generations: tuple[Generation, ...]

    def __post_init__(self) -> None:
        repository = required_text(self.repository, label="repository")
        commit = required_text(self.commit, label="commit").lower()
        source_fingerprint = required_text(
            self.source_fingerprint,
            label="source fingerprint",
        )
        generations = tuple(self.generations)
        if len(generations) != 1 or generations[0].view_type != "bm25":
            raise StorageValidationError(
                "H1 snapshots contain exactly one BM25 generation"
            )
        generation = generations[0]
        if (
            generation.repository,
            generation.commit,
            generation.source_fingerprint,
        ) != (repository, commit, source_fingerprint):
            raise StorageValidationError(
                "snapshot and generation source identities differ"
            )
        expected_id = content_id(
            "snap",
            {
                "repository": repository,
                "commit": commit,
                "source_fingerprint": source_fingerprint,
                "generations": {generation.view_type: generation.generation_id},
            },
        )
        if self.snapshot_id != expected_id:
            raise StorageValidationError(
                "snapshot ID does not match its generation set"
            )
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "commit", commit)
        object.__setattr__(self, "source_fingerprint", source_fingerprint)
        object.__setattr__(self, "generations", generations)

    @classmethod
    def create(cls, generation: Generation) -> "Snapshot":
        snapshot_id = content_id(
            "snap",
            {
                "repository": generation.repository,
                "commit": generation.commit,
                "source_fingerprint": generation.source_fingerprint,
                "generations": {generation.view_type: generation.generation_id},
            },
        )
        return cls(
            snapshot_id=snapshot_id,
            repository=generation.repository,
            commit=generation.commit,
            source_fingerprint=generation.source_fingerprint,
            generations=(generation,),
        )


@dataclass(frozen=True, slots=True)
class RefHead:
    repository: str
    ref_name: str
    snapshot_id: str
    revision: int

    def __post_init__(self) -> None:
        required_text(self.repository, label="repository")
        required_text(self.ref_name, label="ref name")
        required_text(self.snapshot_id, label="snapshot ID")
        if type(self.revision) is not int or self.revision <= 0:
            raise StorageValidationError("ref revision must be a positive integer")


@dataclass(frozen=True, slots=True)
class ResolvedSnapshot:
    snapshot: Snapshot
    ref: RefHead | None = None


__all__ = [
    "Generation",
    "PublishConflict",
    "RefHead",
    "ResolvedSnapshot",
    "Snapshot",
    "StorageError",
    "StorageIntegrityError",
    "StorageNotFound",
    "StorageValidationError",
    "canonical_json",
    "content_id",
    "normalize_digest",
    "required_text",
]
