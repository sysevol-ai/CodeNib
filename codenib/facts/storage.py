# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Content-addressed publication and receipt-based reuse for ``FactBatch``.

The low-level ``FactBatchReuseCache`` deliberately keeps lookup receipts in a
caller-owned mapping. Durable composition is separate: ``facts.generation``
places the same immutable objects in an identity-bound catalog generation and
records explicit member reachability without changing this artifact or reuse
key format.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, MutableMapping

from ..storage.models import canonical_json, normalize_digest
from ..storage.protocols import ObjectStore
from .model import FACT_BATCH_SCHEMA_VERSION, FactBatch, sha256_digest

FACT_BATCH_ARTIFACT_SCHEMA = "codenib.fact-batch-artifact.v1"
FACT_BATCH_ARTIFACT_MEDIA_TYPE = "application/vnd.codenib.fact-batch+json"
DEFAULT_MAX_FACT_BATCH_ARTIFACT_BYTES = 64 * 1024 * 1024

_REUSE_KEY_FIELDS = frozenset(
    {
        "schema_version",
        "file_path",
        "language",
        "content_digest",
        "profile_digest",
        "provider",
    }
)
_ARTIFACT_FIELDS = frozenset({"artifact_schema", "reuse_key", "batch_digest", "batch"})


class FactBatchArtifactError(RuntimeError):
    """A FactBatch artifact or reuse receipt failed closed validation."""


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    if value != value.strip() or "\x00" in value:
        raise ValueError(f"{field} must be canonical text")
    return value


def _canonical_file_path(value: Any) -> str:
    text = _required_text(value, "file_path")
    path = PurePosixPath(text)
    if (
        "\\" in text
        or path.is_absolute()
        or path.as_posix() != text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("file_path must be a repository-relative POSIX path")
    return text


def _fact_digest(value: Any, field: str) -> str:
    text = _required_text(value, field)
    if not text.startswith("sha256:"):
        raise ValueError(f"{field} must use canonical sha256:<digest>")
    normalized = normalize_digest(text)
    canonical = f"sha256:{normalized}"
    if text != canonical:
        raise ValueError(f"{field} must use canonical sha256:<digest>")
    return canonical


def _exact_fields(
    payload: Mapping[str, Any], expected: frozenset[str], label: str
) -> None:
    actual = frozenset(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise FactBatchArtifactError(
            f"{label} fields differ; missing={missing}, unknown={unknown}"
        )


@dataclass(frozen=True, slots=True)
class FactBatchReuseKey:
    """Safe per-file reuse identity before cross-file resolution."""

    file_path: str
    language: str
    content_digest: str
    profile_digest: str
    provider: str
    schema_version: int = FACT_BATCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != FACT_BATCH_SCHEMA_VERSION
        ):
            raise ValueError(
                f"FactBatch reuse schema {self.schema_version!r} is unsupported"
            )
        object.__setattr__(self, "file_path", _canonical_file_path(self.file_path))
        _required_text(self.language, "language")
        object.__setattr__(
            self,
            "content_digest",
            _fact_digest(self.content_digest, "content_digest"),
        )
        object.__setattr__(
            self,
            "profile_digest",
            _fact_digest(self.profile_digest, "profile_digest"),
        )
        _required_text(self.provider, "provider")

    @classmethod
    def from_batch(cls, batch: FactBatch) -> FactBatchReuseKey:
        return cls(
            schema_version=batch.schema_version,
            file_path=batch.file_path,
            language=batch.language,
            content_digest=batch.content_digest,
            profile_digest=batch.profile_digest,
            provider=batch.provider,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FactBatchReuseKey:
        if not isinstance(payload, Mapping):
            raise FactBatchArtifactError("reuse_key must be an object")
        _exact_fields(payload, _REUSE_KEY_FIELDS, "reuse_key")
        try:
            return cls(
                schema_version=payload["schema_version"],
                file_path=payload["file_path"],
                language=payload["language"],
                content_digest=payload["content_digest"],
                profile_digest=payload["profile_digest"],
                provider=payload["provider"],
            )
        except (TypeError, ValueError) as exc:
            raise FactBatchArtifactError(f"invalid reuse_key: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "file_path": self.file_path,
            "language": self.language,
            "content_digest": self.content_digest,
            "profile_digest": self.profile_digest,
            "provider": self.provider,
        }

    @property
    def digest(self) -> str:
        return sha256_digest(canonical_json(self.to_dict()))


@dataclass(frozen=True, slots=True)
class FactBatchArtifactReceipt:
    """Caller-persistable pointer from one reuse key to immutable bytes."""

    reuse_key_digest: str
    object_digest: str
    batch_digest: str
    byte_size: int
    storage_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reuse_key_digest",
            _fact_digest(self.reuse_key_digest, "reuse_key_digest"),
        )
        object.__setattr__(self, "object_digest", normalize_digest(self.object_digest))
        object.__setattr__(
            self,
            "batch_digest",
            _fact_digest(self.batch_digest, "batch_digest"),
        )
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int):
            raise ValueError("byte_size must be an integer")
        if self.byte_size <= 0:
            raise ValueError("byte_size must be positive")
        _required_text(self.storage_key, "storage_key")

    def to_dict(self) -> dict[str, Any]:
        return {
            "reuse_key_digest": self.reuse_key_digest,
            "object_digest": self.object_digest,
            "batch_digest": self.batch_digest,
            "byte_size": self.byte_size,
            "storage_key": self.storage_key,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FactBatchArtifactReceipt:
        expected = frozenset(
            {
                "reuse_key_digest",
                "object_digest",
                "batch_digest",
                "byte_size",
                "storage_key",
            }
        )
        if not isinstance(payload, Mapping):
            raise FactBatchArtifactError("FactBatch receipt must be an object")
        _exact_fields(payload, expected, "FactBatch receipt")
        try:
            return cls(**payload)
        except (TypeError, ValueError) as exc:
            raise FactBatchArtifactError(f"invalid FactBatch receipt: {exc}") from exc


def _artifact_payload(batch: FactBatch) -> dict[str, Any]:
    key = FactBatchReuseKey.from_batch(batch)
    return {
        "artifact_schema": FACT_BATCH_ARTIFACT_SCHEMA,
        "reuse_key": key.to_dict(),
        "batch_digest": batch.digest(),
        "batch": batch.to_dict(),
    }


def encode_fact_batch_artifact(batch: FactBatch) -> bytes:
    """Encode one deterministic, self-validating FactBatch object."""

    if not isinstance(batch, FactBatch):
        raise TypeError("batch must be a FactBatch")
    return canonical_json(_artifact_payload(batch)).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FactBatchArtifactError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise FactBatchArtifactError(f"non-finite JSON number is forbidden: {value}")


def decode_fact_batch_artifact(
    payload: bytes,
    *,
    expected_key: FactBatchReuseKey | None = None,
    max_bytes: int = DEFAULT_MAX_FACT_BATCH_ARTIFACT_BYTES,
) -> FactBatch:
    """Decode bytes and validate schema, identities, digest, and canonical form."""

    if not isinstance(payload, bytes):
        raise TypeError("FactBatch artifact payload must be bytes")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if not payload or len(payload) > max_bytes:
        raise FactBatchArtifactError(
            f"FactBatch artifact size must be between 1 and {max_bytes} bytes"
        )
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except FactBatchArtifactError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactBatchArtifactError(f"invalid FactBatch artifact JSON: {exc}") from exc
    if not isinstance(decoded, Mapping):
        raise FactBatchArtifactError("FactBatch artifact must be a JSON object")
    _exact_fields(decoded, _ARTIFACT_FIELDS, "FactBatch artifact")
    if decoded["artifact_schema"] != FACT_BATCH_ARTIFACT_SCHEMA:
        raise FactBatchArtifactError(
            f"unsupported FactBatch artifact schema {decoded['artifact_schema']!r}"
        )
    key = FactBatchReuseKey.from_dict(decoded["reuse_key"])
    if expected_key is not None and key != expected_key:
        raise FactBatchArtifactError("FactBatch artifact reuse key does not match")
    if not isinstance(decoded["batch"], Mapping):
        raise FactBatchArtifactError("FactBatch artifact batch must be an object")
    try:
        batch = FactBatch.from_dict(decoded["batch"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FactBatchArtifactError(f"invalid FactBatch payload: {exc}") from exc
    if FactBatchReuseKey.from_batch(batch) != key:
        raise FactBatchArtifactError(
            "FactBatch payload identity differs from reuse key"
        )
    if decoded["batch_digest"] != batch.digest():
        raise FactBatchArtifactError("FactBatch payload digest mismatch")
    if payload != encode_fact_batch_artifact(batch):
        raise FactBatchArtifactError("FactBatch artifact is not canonical JSON")
    return batch


def publish_fact_batch(
    store: ObjectStore,
    batch: FactBatch,
) -> FactBatchArtifactReceipt:
    """Publish a verified immutable artifact and return its reuse receipt."""

    payload = encode_fact_batch_artifact(batch)
    stored = store.put_bytes(payload)
    verified = store.verify(stored.digest)
    if verified != stored:
        raise FactBatchArtifactError("object-store verification receipt changed")
    observed = hashlib.sha256(payload).hexdigest()
    if stored.digest != observed or stored.byte_size != len(payload):
        raise FactBatchArtifactError("object-store publication receipt is inconsistent")
    key = FactBatchReuseKey.from_batch(batch)
    return FactBatchArtifactReceipt(
        reuse_key_digest=key.digest,
        object_digest=stored.digest,
        batch_digest=batch.digest(),
        byte_size=stored.byte_size,
        storage_key=stored.storage_key,
    )


def load_fact_batch(
    store: ObjectStore,
    receipt: FactBatchArtifactReceipt | Mapping[str, Any],
    *,
    expected_key: FactBatchReuseKey,
    max_bytes: int = DEFAULT_MAX_FACT_BATCH_ARTIFACT_BYTES,
) -> FactBatch:
    """Load one receipt and fail closed on any metadata or byte mismatch."""

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if not isinstance(receipt, FactBatchArtifactReceipt):
        receipt = FactBatchArtifactReceipt.from_dict(receipt)
    if receipt.reuse_key_digest != expected_key.digest:
        raise FactBatchArtifactError("FactBatch receipt reuse key does not match")
    if receipt.byte_size > max_bytes:
        raise FactBatchArtifactError(
            f"FactBatch artifact exceeds the {max_bytes}-byte read limit"
        )
    verified = store.verify(receipt.object_digest)
    if (
        verified.digest != receipt.object_digest
        or verified.byte_size != receipt.byte_size
        or verified.storage_key != receipt.storage_key
    ):
        raise FactBatchArtifactError("FactBatch receipt differs from object store")
    payload = store.read_bytes(receipt.object_digest)
    if len(payload) != receipt.byte_size:
        raise FactBatchArtifactError("FactBatch object size differs from receipt")
    if hashlib.sha256(payload).hexdigest() != receipt.object_digest:
        raise FactBatchArtifactError("FactBatch object digest differs from receipt")
    batch = decode_fact_batch_artifact(
        payload, expected_key=expected_key, max_bytes=max_bytes
    )
    if batch.digest() != receipt.batch_digest:
        raise FactBatchArtifactError("FactBatch semantic digest differs from receipt")
    return batch


class FactBatchReuseCache:
    """Receipt-indexed reuse over an ``ObjectStore`` without catalog claims."""

    def __init__(
        self,
        store: ObjectStore,
        receipts: (
            MutableMapping[str, FactBatchArtifactReceipt | Mapping[str, Any]] | None
        ) = None,
    ) -> None:
        self.store = store
        self.receipts = receipts if receipts is not None else {}
        self.hits = 0
        self.misses = 0
        self.publications = 0

    def lookup(self, key: FactBatchReuseKey) -> FactBatch | None:
        receipt = self.receipts.get(key.digest)
        if receipt is None:
            self.misses += 1
            return None
        batch = load_fact_batch(self.store, receipt, expected_key=key)
        self.hits += 1
        return batch

    def publish(self, batch: FactBatch) -> FactBatchArtifactReceipt:
        key = FactBatchReuseKey.from_batch(batch)
        existing = self.receipts.get(key.digest)
        if existing is not None:
            existing_batch = load_fact_batch(self.store, existing, expected_key=key)
            if existing_batch != batch:
                raise FactBatchArtifactError(
                    "reuse key already names a different FactBatch; "
                    "the profile is incomplete or the analyzer is nondeterministic"
                )
            return (
                existing
                if isinstance(existing, FactBatchArtifactReceipt)
                else FactBatchArtifactReceipt.from_dict(existing)
            )
        receipt = publish_fact_batch(self.store, batch)
        self.receipts[key.digest] = receipt
        self.publications += 1
        return receipt

    def receipt_snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            digest: (
                receipt.to_dict()
                if isinstance(receipt, FactBatchArtifactReceipt)
                else FactBatchArtifactReceipt.from_dict(receipt).to_dict()
            )
            for digest, receipt in sorted(self.receipts.items())
        }


__all__ = [
    "DEFAULT_MAX_FACT_BATCH_ARTIFACT_BYTES",
    "FACT_BATCH_ARTIFACT_MEDIA_TYPE",
    "FACT_BATCH_ARTIFACT_SCHEMA",
    "FactBatchArtifactError",
    "FactBatchArtifactReceipt",
    "FactBatchReuseCache",
    "FactBatchReuseKey",
    "decode_fact_batch_artifact",
    "encode_fact_batch_artifact",
    "load_fact_batch",
    "publish_fact_batch",
]
