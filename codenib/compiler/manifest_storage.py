# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pure retained-import planning for :class:`RepoManifest` v1.1 documents.

This module deliberately stops before filesystem or storage authority begins.
It parses one manifest with bounded, unambiguous JSON rules and turns it into
an immutable import *intent*.  The plan contains no catalog IDs, object-store
receipts, source-verification claims, or finalized projection envelope.

Later adapter stages may use the plan only after separately authenticating the
repository source and each referenced artifact.  In particular, a source
fingerprint v2 value is content-bound input that is eligible for that later
verification; it is not proof that verification has already happened.  A v1
fingerprint remains available for diagnostics but makes the plan inert.
"""

from __future__ import annotations

import codecs
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .._bounded_json import validate_bounded_json_stream
from .._secret_fields import SecretFieldError, assert_no_secret_fields
from ..index.embedding.model_policy import (
    resolve_embedding_artifact_load_policy_from_options,
)
from ..provider_routes import resolve_embedding_artifact_route
from ..source_fingerprint import source_fingerprint_version
from ..storage.models import StorageValidationError, ViewProfile, canonical_json
from .manifest import MANIFEST_VERSION, IndexEntry, RepoManifest

REPO_MANIFEST_IMPORT_PLAN_SCHEMA = "codenib.repo-manifest-import-plan.v1"
REPO_MANIFEST_PROFILE_CONTRACT = "codenib.repo-manifest-profile.v1"
REPO_MANIFEST_PROFILE_NAME = "repo-manifest-explicit"
REPO_MANIFEST_BM25_GENERATION_CONTRACT = "codenib.repo-manifest-bm25-generation.v1"
REPO_MANIFEST_VECTOR_GENERATION_CONTRACT = "codenib.repo-manifest-vector-generation.v1"

PORTABLE_STORAGE_VIEWS = frozenset({"bm25", "vector"})
CURRENT_PORTABLE_BUILDER_SCHEMAS = {"bm25": 8, "vector": 6}

DEFAULT_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 250_000
_MAX_JSON_LEXICAL_TOKENS = 500_000
_MAX_JSON_KEY_BYTES = 4_096
_MAX_JSON_ATOM_BYTES = 128
_MAX_INDEXES = 128
_MAX_LANGUAGES = 256
_MAX_CAPABILITIES = 256
_MAX_TEXT = 32_768
_MAX_NAME = 256
_MAX_VIEW_NAME = 128
_MAX_CONFIG_BYTES = 16 * 1024 * 1024
_MAX_DOCUMENTS_BYTES = 256 * 1024 * 1024
_INT64_MAX = 9_223_372_036_854_775_807
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_DIAGNOSTIC_FIELD_NAMES = frozenset(
    {"error", "errormessage", "stalereason", "traceback", "exception"}
)
_DIAGNOSTIC_FIELD_FRAGMENTS = (
    "diagnostic",
    "error",
    "exception",
    "failuremessage",
    "failurereason",
    "stacktrace",
    "stalereason",
    "traceback",
)
_AUTHORITY_CLAIM_FIELD_NAMES = frozenset(
    {
        "repositoryid",
        "namespaceid",
        "sourcerevisionid",
        "profileid",
        "generationid",
        "viewgenerationid",
        "objectid",
        "object",
        "storagekey",
        "snapshotid",
        "catalogsnapshotid",
        "receipt",
        "pinid",
        "sourceverified",
        "commitverified",
        "checkoutverified",
        "verificationscope",
        "nativeindexauthorization",
        "authority",
        "ownership",
        "repositorysourcebinding",
        "sourcebinding",
        "workspaceauthority",
        "verified",
        "isverified",
        "verifiedsource",
        "authorized",
        "isauthorized",
        "durable",
        "pinned",
    }
)
_AUTHORITY_CLAIM_FIELD_FRAGMENTS = (
    "authority",
    "authorization",
    "authorized",
    "catalogid",
    "canonicalpath",
    "directoryfd",
    "directoryhandle",
    "durable",
    "filedescriptor",
    "filehandle",
    "generationid",
    "namespaceid",
    "objectid",
    "ownership",
    "pinid",
    "pinned",
    "profileid",
    "realpath",
    "receipt",
    "repositoryid",
    "resolvedpath",
    "snapshotid",
    "sourcebinding",
    "sourcerevisionid",
    "storagekey",
    "verification",
    "verified",
    "workspacebinding",
)
_SELECTION_SKIP_REASONS = frozenset(
    {"not_current", "incomplete_profile", "incomplete_generation_record"}
)
_MISSING_ENTRY_FIELD = object()

BM25_PROFILE_AXES = (
    "builder_schema",
    "languages",
    "max_k",
    "max_lines_per_chunk",
    "chunking_failure_policy",
    "include_header_epilogue",
    "repository_filter_policy",
)
VECTOR_PROFILE_AXES = (
    "builder_schema",
    "embedding_model",
    "embedding_provider",
    "embedding_dimension",
    "dimension",
    "embedding_endpoint",
    "embedding_kwargs",
    "embedding_route",
    "embedding_fingerprint",
    "index_metric",
    "languages",
    "levels",
    "max_lines_per_chunk",
    "chunking_failure_policy",
    "repository_filter_policy",
)

_BM25_NON_PROFILE_CONFIG_FIELDS = frozenset(
    {
        "artifact_file_fingerprints",
        "artifact_scope",
        "chunk_count",
        "file_count",
        "portable_document_format",
        "source_file_count",
    }
)
_VECTOR_NON_PROFILE_CONFIG_FIELDS = frozenset(
    {
        "artifact_scope",
        "cache_hit_rate",
        "chunks_from_cache",
        "chunks_reembedded",
        "document_count",
        "incremental_fallback_reason",
        "last_commit",
        "new_commit",
        "persistence_config_fingerprint",
        "portable_document_format",
        "update_mode",
    }
)
_VECTOR_INCREMENTAL_FIELDS = (
    "chunks_reembedded",
    "chunks_from_cache",
    "cache_hit_rate",
    "new_commit",
)
_VECTOR_FALLBACK_FIELDS = ("update_mode", "incremental_fallback_reason")
_VECTOR_UPDATE_MODES = frozenset({"full_rebuild"})
_NON_PROFILE_METADATA_FIELDS = frozenset({"build_duration_seconds"})

_TOP_LEVEL_FIELDS = frozenset(
    {"version", "repo", "indexes", "capabilities", "compiled_at", "compiled_at_epoch"}
)
_REPO_FIELDS = frozenset(
    {
        "path",
        "commit",
        "last_indexed_commit",
        "source_fingerprint",
        "last_indexed_source_fingerprint",
        "languages",
        "file_count",
    }
)
_ENTRY_FIELDS = frozenset(
    {
        "index_type",
        "path",
        "built_at",
        "built_at_epoch",
        "status",
        "config",
        "metadata",
        "commit",
        "source_fingerprint",
    }
)


class _AmbiguousJSONError(ValueError):
    """A JSON document has more than one possible decoded meaning."""


class _IncompleteProfile(ValueError):
    """A view cannot form a complete compatibility profile."""


class _IncompleteGenerationRecord(ValueError):
    """A view lacks its explicit artifact-generation fingerprint record."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return canonical_json(value).encode("ascii")


@dataclass(frozen=True, slots=True)
class SourceIntent:
    """Manifest-declared source identity awaiting a real source authority."""

    fingerprint: str
    fingerprint_version: int
    file_count: int
    display_commit: str

    def __post_init__(self) -> None:
        if type(self) is not SourceIntent:
            raise StorageValidationError("source intent must use the exact intent type")
        try:
            if (
                type(self.fingerprint) is not str
                or type(self.display_commit) is not str
            ):
                raise StorageValidationError("source intent text fields must be exact")
            _bounded_text(
                self.fingerprint,
                "source intent fingerprint",
                max_length=128,
                allow_empty=False,
            )
            if (
                type(self.fingerprint_version) is not int
                or self.fingerprint_version not in {1, 2}
                or source_fingerprint_version(self.fingerprint)
                != self.fingerprint_version
            ):
                raise StorageValidationError(
                    "source intent fingerprint and version must be canonical"
                )
            if type(self.file_count) is not int:
                raise StorageValidationError("source intent file count must be exact")
            _nonnegative_int(self.file_count, "source intent file count")
            _bounded_text(self.display_commit, "source intent display commit")
        except StorageValidationError:
            raise
        except Exception as exc:
            raise StorageValidationError("source intent is invalid") from exc

    @property
    def disposition(self) -> str:
        return "content-bound" if self.fingerprint_version == 2 else "legacy-diagnostic"

    @property
    def eligible_for_verification(self) -> bool:
        """Whether a later authority may attempt content verification.

        This is intentionally weaker than ``source_verified`` and cannot
        authorize live source reads, native parsing, reuse, or publication.
        """

        return self.fingerprint_version == 2

    @property
    def identity_digest(self) -> str:
        """Digest only the content-bearing intent, excluding display commit."""

        return _sha256(
            _canonical_bytes(
                {
                    "contract": "codenib.repo-manifest-source-intent.v1",
                    "source_fingerprint": self.fingerprint,
                    "fingerprint_version": self.fingerprint_version,
                    "file_count": self.file_count,
                }
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": "codenib.repo-manifest-source-intent.v1",
            "disposition": self.disposition,
            "eligible_for_verification": self.eligible_for_verification,
            "fingerprint_version": self.fingerprint_version,
            "source_fingerprint": self.fingerprint,
            "file_count": self.file_count,
            "display_commit": self.display_commit,
            "identity_digest": self.identity_digest,
        }


@dataclass(frozen=True, slots=True)
class ViewImportIntent:
    """One selected view's data-only profile and generation intent."""

    view_type: str
    required: bool
    manifest_entry_json: str
    profile: ViewProfile
    generation_record_json: str

    def __post_init__(self) -> None:
        if type(self) is not ViewImportIntent:
            raise StorageValidationError(
                "view import intent must use the exact intent type"
            )
        try:
            _validate_view_import_intent(self)
        except StorageValidationError:
            raise
        except Exception as exc:
            raise StorageValidationError("view import intent is invalid") from exc

    @property
    def manifest_entry(self) -> dict[str, Any]:
        value = json.loads(self.manifest_entry_json)
        assert isinstance(value, dict)
        return value

    @property
    def manifest_entry_digest(self) -> str:
        return _sha256(self.manifest_entry_json.encode("ascii"))

    @property
    def profile_id(self) -> str:
        return self.profile.profile_id

    @property
    def generation_record(self) -> dict[str, Any]:
        value = json.loads(self.generation_record_json)
        assert isinstance(value, dict)
        return value

    @property
    def generation_record_digest(self) -> str:
        return _sha256(self.generation_record_json.encode("ascii"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "view_type": self.view_type,
            "required": self.required,
            "manifest_entry_digest": self.manifest_entry_digest,
            "profile": {
                "profile_id": self.profile_id,
                "name": self.profile.name,
                "config": self.profile.config,
            },
            "generation_record": self.generation_record,
            "generation_record_digest": self.generation_record_digest,
        }


@dataclass(frozen=True, slots=True)
class ViewSelection:
    """Canonical required/optional selection and explicit skip decisions."""

    required_views: tuple[str, ...]
    optional_views: tuple[str, ...]
    selected_views: tuple[str, ...]
    skipped_items: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if type(self) is not ViewSelection:
            raise StorageValidationError(
                "view selection must use the exact selection type"
            )
        try:
            _validate_view_selection(self)
        except StorageValidationError:
            raise
        except Exception as exc:
            raise StorageValidationError("view selection is invalid") from exc

    @property
    def skipped_views(self) -> Mapping[str, str]:
        return MappingProxyType(dict(self.skipped_items))

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_views": list(self.required_views),
            "optional_views": list(self.optional_views),
            "selected_views": list(self.selected_views),
            "skipped_views": dict(self.skipped_items),
        }


@dataclass(frozen=True, slots=True)
class RepoManifestImportPlan:
    """Immutable, non-authoritative plan for a future manifest import."""

    manifest_json: str
    source: SourceIntent
    selection: ViewSelection
    views: tuple[ViewImportIntent, ...]

    def __post_init__(self) -> None:
        if type(self) is not RepoManifestImportPlan:
            raise StorageValidationError("import plan must use the exact plan type")
        try:
            _validate_import_plan(self)
        except StorageValidationError:
            raise
        except Exception as exc:
            raise StorageValidationError("import plan is invalid") from exc

    @property
    def schema(self) -> str:
        return REPO_MANIFEST_IMPORT_PLAN_SCHEMA

    @property
    def canonical_manifest_bytes(self) -> bytes:
        return self.manifest_json.encode("ascii")

    @property
    def manifest_digest(self) -> str:
        return _sha256(self.canonical_manifest_bytes)

    @property
    def manifest(self) -> RepoManifest:
        value = json.loads(self.manifest_json)
        assert isinstance(value, dict)
        return _repo_manifest_v11_from_data(value)

    @property
    def view_map(self) -> Mapping[str, ViewImportIntent]:
        return MappingProxyType({view.view_type: view for view in self.views})

    @property
    def inert(self) -> bool:
        """Whether source compatibility forbids continuing to authority checks."""

        return not self.source.eligible_for_verification

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "manifest": {
                "version": MANIFEST_VERSION,
                "digest": self.manifest_digest,
                "byte_size": len(self.canonical_manifest_bytes),
            },
            "source": self.source.to_dict(),
            "selection": self.selection.to_dict(),
            "views": {view.view_type: view.to_dict() for view in self.views},
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())

    @property
    def plan_digest(self) -> str:
        return _sha256(self.canonical_bytes)


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _AmbiguousJSONError(f"duplicate object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise _AmbiguousJSONError(f"non-finite JSON number: {value}")


def _parse_bounded_json_int(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > 19:
        raise _AmbiguousJSONError("JSON integer exceeds the signed 64-bit limit")
    try:
        parsed = int(value)
    except (OverflowError, ValueError) as exc:
        raise _AmbiguousJSONError("JSON integer is invalid") from exc
    if parsed < -(_INT64_MAX + 1) or parsed > _INT64_MAX:
        raise _AmbiguousJSONError("JSON integer exceeds the signed 64-bit limit")
    return parsed


def _parse_bounded_json_float(value: str) -> float:
    if len(value) > 128:
        raise _AmbiguousJSONError("JSON number exceeds its lexical limit")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as exc:
        raise _AmbiguousJSONError("JSON number is invalid") from exc
    if not math.isfinite(parsed):
        raise _AmbiguousJSONError("JSON number must be finite")
    return parsed


def _positive_limit(value: int, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise StorageValidationError(f"{field} must be a positive integer")
    return value


def _manifest_limit(value: int) -> int:
    limit = _positive_limit(value, "manifest byte limit")
    if limit > DEFAULT_MAX_MANIFEST_BYTES:
        raise StorageValidationError(
            "manifest byte limit must not exceed the fixed "
            f"{DEFAULT_MAX_MANIFEST_BYTES}-byte ceiling"
        )
    return limit


def _strict_json_loads(payload: str, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_strict_json_object,
            parse_int=_parse_bounded_json_int,
            parse_float=_parse_bounded_json_float,
            parse_constant=_reject_nonfinite_json,
        )
    except (ValueError, OverflowError, RecursionError) as exc:
        raise StorageValidationError(f"{label} is invalid or ambiguous JSON") from exc
    if not isinstance(value, dict):
        raise StorageValidationError(f"{label} must be a JSON object")
    return value


def _preflight_json_bytes(payload: bytes, *, label: str, max_bytes: int) -> None:
    """Bound JSON framing before constructing a decoded object graph."""

    try:
        validate_bounded_json_stream(
            io.BytesIO(payload),
            label=label,
            max_bytes=max_bytes,
            max_nodes=_MAX_JSON_NODES,
            max_lexical_tokens=_MAX_JSON_LEXICAL_TOKENS,
            max_depth=_MAX_JSON_DEPTH,
            max_key_bytes=_MAX_JSON_KEY_BYTES,
            max_string_bytes=max_bytes,
            max_atom_bytes=_MAX_JSON_ATOM_BYTES,
        )
    except (TypeError, ValueError) as exc:
        raise StorageValidationError(
            f"{label} is invalid or ambiguous JSON or exceeds its complexity budget"
        ) from exc


def _validate_json_budget(value: Any, *, label: str) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise StorageValidationError(
                f"{label} exceeds the {_MAX_JSON_NODES}-node limit"
            )
        if depth > _MAX_JSON_DEPTH:
            raise StorageValidationError(
                f"{label} exceeds the {_MAX_JSON_DEPTH}-level depth limit"
            )
        if isinstance(current, Mapping):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise StorageValidationError(f"{label} object keys must be text")
                try:
                    encoded_key = key.encode("utf-8", errors="strict")
                except UnicodeError as exc:
                    raise StorageValidationError(
                        f"{label} object keys must be valid UTF-8 text"
                    ) from exc
                if len(encoded_key) > _MAX_JSON_KEY_BYTES:
                    raise StorageValidationError(
                        f"{label} object keys exceed the byte limit"
                    )
                stack.append((child, depth + 1))
        elif isinstance(current, (list, tuple)):
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, int) and not isinstance(current, bool):
            if current < -(_INT64_MAX + 1) or current > _INT64_MAX:
                raise StorageValidationError(
                    f"{label} integers must fit signed 64-bit storage"
                )
        elif isinstance(current, float) and not math.isfinite(current):
            raise StorageValidationError(f"{label} numbers must be finite")
        elif isinstance(current, str):
            try:
                current.encode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise StorageValidationError(
                    f"{label} strings must be valid UTF-8 text"
                ) from exc
        elif current is not None and not isinstance(current, (str, bool, int, float)):
            raise StorageValidationError(
                f"{label} contains unsupported {type(current).__name__} data"
            )


def _json_string_size(
    value: str,
    *,
    label: str,
    max_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
) -> int:
    """Return the exact ensure-ASCII JSON string size without encoding it."""

    size = 2
    if size > max_bytes:
        raise StorageValidationError(f"{label} exceeds its byte limit")
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise StorageValidationError(f"{label} strings must be valid UTF-8 text")
        if character in {'"', "\\", "\b", "\f", "\n", "\r", "\t"}:
            size += 2
        elif codepoint < 0x20 or codepoint == 0x7F or 0x80 <= codepoint <= 0xFFFF:
            size += 6
        elif codepoint > 0xFFFF:
            size += 12
        else:
            size += 1
        if size > max_bytes:
            raise StorageValidationError(f"{label} exceeds its byte limit")
    return size


def _snapshot_json_value(
    value: Any,
    *,
    label: str,
    depth: int = 1,
    state: dict[str, Any] | None = None,
    max_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
) -> tuple[Any, int]:
    """Consume caller JSON exactly once into bounded built-in containers."""

    if state is None:
        state = {
            "nodes": 0,
            "active": set(),
            # Retain the original objects, rather than only their ids, so a
            # short-lived projection cannot be freed and have its id reused.
            "seen": {},
        }

    if type(value) is RepoManifest:
        identity = id(value)
        if identity in state["seen"]:
            raise StorageValidationError(
                f"{label} contains a repeated or cyclic container"
            )
        state["seen"][identity] = value
        try:
            languages = value.languages
            indexes = value.indexes
            capabilities = value.capabilities
            if type(languages) is not list or list.__len__(languages) > _MAX_LANGUAGES:
                raise StorageValidationError(
                    "repository manifest languages must be a bounded exact list"
                )
            if type(indexes) is not dict or dict.__len__(indexes) > _MAX_INDEXES:
                raise StorageValidationError(
                    "repository manifest indexes must be a bounded exact object"
                )
            if (
                type(capabilities) is not dict
                or dict.__len__(capabilities) > _MAX_CAPABILITIES
            ):
                raise StorageValidationError(
                    "repository manifest capabilities must be a bounded exact object"
                )
            projected = {
                "version": value.version,
                "repo": {
                    "path": value.repo_path,
                    "commit": value.commit,
                    "last_indexed_commit": value.last_indexed_commit,
                    "source_fingerprint": value.source_fingerprint,
                    "last_indexed_source_fingerprint": (
                        value.last_indexed_source_fingerprint
                    ),
                    "languages": languages,
                    "file_count": value.file_count,
                },
                "indexes": indexes,
                "capabilities": capabilities,
                "compiled_at": value.compiled_at,
                "compiled_at_epoch": value.compiled_at_epoch,
            }
        except StorageValidationError:
            raise
        except Exception as exc:
            raise StorageValidationError(
                f"{label} is missing or failed to expose manifest fields"
            ) from exc
        return _snapshot_json_value(
            projected,
            label=label,
            depth=depth,
            state=state,
            max_bytes=max_bytes,
        )

    if type(value) is IndexEntry:
        identity = id(value)
        if identity in state["seen"]:
            raise StorageValidationError(
                f"{label} contains a repeated or cyclic container"
            )
        state["seen"][identity] = value
        try:
            config = value.config
            metadata = value.metadata
            if type(config) is not dict or type(metadata) is not dict:
                raise StorageValidationError(
                    "repository manifest index config and metadata must be exact objects"
                )
            projected = {
                "index_type": value.index_type,
                "path": value.path,
                "built_at": value.built_at,
                "built_at_epoch": value.built_at_epoch,
                "status": value.status,
                "config": config,
                "metadata": metadata,
                "commit": value.commit,
                "source_fingerprint": value.source_fingerprint,
            }
        except StorageValidationError:
            raise
        except Exception as exc:
            raise StorageValidationError(
                f"{label} is missing or failed to expose index fields"
            ) from exc
        return _snapshot_json_value(
            projected,
            label=label,
            depth=depth,
            state=state,
            max_bytes=max_bytes,
        )

    state["nodes"] += 1
    if state["nodes"] > _MAX_JSON_NODES:
        raise StorageValidationError(
            f"{label} exceeds the {_MAX_JSON_NODES}-node limit"
        )
    if depth > _MAX_JSON_DEPTH:
        raise StorageValidationError(
            f"{label} exceeds the {_MAX_JSON_DEPTH}-level depth limit"
        )

    if isinstance(value, Mapping):
        if max_bytes < 2:
            raise StorageValidationError(f"{label} exceeds its byte limit")
        identity = id(value)
        if identity in state["seen"]:
            raise StorageValidationError(
                f"{label} contains a repeated or cyclic container"
            )
        state["seen"][identity] = value
        state["active"].add(identity)
        result: dict[str, Any] = {}
        size = 2
        try:
            iterator = iter(value.items())
            while True:
                try:
                    item = next(iterator)
                except StopIteration:
                    break
                if type(item) is not tuple or len(item) != 2:
                    raise StorageValidationError(
                        f"{label} mapping items must be key-value pairs"
                    )
                key, child = item
                if type(key) is not str:
                    raise StorageValidationError(
                        f"{label} object keys must be exact strings"
                    )
                separator_size = 1 if result else 0
                key_budget = max_bytes - size - separator_size - 2
                key_size = _json_string_size(
                    key,
                    label=label,
                    max_bytes=key_budget,
                )
                prefix_size = separator_size + key_size + 1
                child_budget = max_bytes - size - prefix_size
                if child_budget < 1:
                    raise StorageValidationError(f"{label} exceeds its byte limit")
                if key in result:
                    raise StorageValidationError(
                        f"{label} contains a duplicate object key: {key}"
                    )
                child_snapshot, child_size = _snapshot_json_value(
                    child,
                    label=label,
                    depth=depth + 1,
                    state=state,
                    max_bytes=child_budget,
                )
                size += prefix_size + child_size
                if size > max_bytes:
                    raise StorageValidationError(f"{label} exceeds its byte limit")
                result[key] = child_snapshot
        except StorageValidationError:
            raise
        except Exception as exc:
            raise StorageValidationError(
                f"{label} changed or failed while being snapshotted"
            ) from exc
        finally:
            state["active"].discard(identity)
        return result, size

    if type(value) in {list, tuple}:
        if max_bytes < 2:
            raise StorageValidationError(f"{label} exceeds its byte limit")
        identity = id(value)
        if identity in state["seen"]:
            raise StorageValidationError(
                f"{label} contains a repeated or cyclic container"
            )
        state["seen"][identity] = value
        state["active"].add(identity)
        result_list: list[Any] = []
        size = 2
        try:
            for child in value:
                separator_size = 1 if result_list else 0
                child_budget = max_bytes - size - separator_size
                if child_budget < 1:
                    raise StorageValidationError(f"{label} exceeds its byte limit")
                child_snapshot, child_size = _snapshot_json_value(
                    child,
                    label=label,
                    depth=depth + 1,
                    state=state,
                    max_bytes=child_budget,
                )
                size += separator_size + child_size
                if size > max_bytes:
                    raise StorageValidationError(f"{label} exceeds its byte limit")
                result_list.append(child_snapshot)
        finally:
            state["active"].discard(identity)
        return result_list, size

    if value is None:
        size = 4
        if size > max_bytes:
            raise StorageValidationError(f"{label} exceeds its byte limit")
        return None, size
    if type(value) is bool:
        size = 4 if value else 5
        if size > max_bytes:
            raise StorageValidationError(f"{label} exceeds its byte limit")
        return value, size
    if type(value) is int:
        if value < -(_INT64_MAX + 1) or value > _INT64_MAX:
            raise StorageValidationError(
                f"{label} integers must fit signed 64-bit storage"
            )
        size = len(str(value))
        if size > max_bytes:
            raise StorageValidationError(f"{label} exceeds its byte limit")
        return value, size
    if type(value) is float:
        if not math.isfinite(value):
            raise StorageValidationError(f"{label} numbers must be finite")
        encoded = json.dumps(value, allow_nan=False)
        if len(encoded) > _MAX_JSON_ATOM_BYTES:
            raise StorageValidationError(f"{label} number exceeds its lexical limit")
        size = len(encoded)
        if size > max_bytes:
            raise StorageValidationError(f"{label} exceeds its byte limit")
        return value, size
    if type(value) is str:
        return value, _json_string_size(
            value,
            label=label,
            max_bytes=max_bytes,
        )
    raise StorageValidationError(
        f"{label} contains unsupported {type(value).__name__} data"
    )


def _require_exact_fields(
    value: Mapping[str, Any], expected: frozenset[str], *, label: str
) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise StorageValidationError(
            f"{label} has an invalid shape ({'; '.join(details)})"
        )


def _bounded_text(
    value: object,
    field: str,
    *,
    max_length: int = _MAX_TEXT,
    allow_empty: bool = True,
) -> str:
    if type(value) is not str:
        raise StorageValidationError(f"{field} must be text")
    if str.__len__(value) > max_length or "\x00" in value:
        raise StorageValidationError(f"{field} is not bounded text")
    if not allow_empty and not str.strip(value):
        raise StorageValidationError(f"{field} must not be empty")
    try:
        str.encode(value, "utf-8", errors="strict")
    except UnicodeError as exc:
        raise StorageValidationError(f"{field} must be valid UTF-8 text") from exc
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _INT64_MAX
    ):
        raise StorageValidationError(f"{field} must be a non-negative integer")
    return value


def _positive_int(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > _INT64_MAX
    ):
        raise _IncompleteProfile(f"{field} must be a positive integer")
    return value


def _epoch(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StorageValidationError(f"{field} must be a number")
    if isinstance(value, int):
        if value < 0 or value > _INT64_MAX:
            raise StorageValidationError(
                f"{field} must be finite, non-negative, and bounded"
            )
        return float(value)
    if not math.isfinite(value) or value < 0 or value >= float(_INT64_MAX + 1):
        raise StorageValidationError(f"{field} must be finite and non-negative")
    return value


def _text_list(
    value: object,
    field: str,
    *,
    allowed: frozenset[str] | None = None,
) -> list[str]:
    if not isinstance(value, list) or not value:
        raise _IncompleteProfile(f"{field} must be a non-empty list")
    if len(value) > _MAX_LANGUAGES:
        raise _IncompleteProfile(f"{field} has too many values")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item:
            raise _IncompleteProfile(f"{field} must contain bounded text")
        if len(item) > _MAX_NAME:
            raise _IncompleteProfile(f"{field} must contain bounded text")
        if allowed is not None and item not in allowed:
            raise _IncompleteProfile(f"{field} contains an unsupported value: {item}")
        result.append(item)
    if len(set(result)) != len(result):
        raise _IncompleteProfile(f"{field} must not contain duplicates")
    return result


def _assert_no_diagnostic_fields(value: Any, *, source: str) -> None:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                normalized = _normalized_field_name(key)
                if normalized in _DIAGNOSTIC_FIELD_NAMES or any(
                    fragment in normalized for fragment in _DIAGNOSTIC_FIELD_FRAGMENTS
                ):
                    raise StorageValidationError(
                        f"{source} must not contain diagnostic field: {key}"
                    )
                stack.append(child)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)


def _normalized_field_name(value: object) -> str:
    return "".join(
        character
        for character in str(value).casefold()
        if character.isascii() and character.isalnum()
    )


def _assert_no_authority_claim_fields(value: Any, *, source: str) -> None:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                normalized = _normalized_field_name(key)
                if normalized in _AUTHORITY_CLAIM_FIELD_NAMES or any(
                    fragment in normalized
                    for fragment in _AUTHORITY_CLAIM_FIELD_FRAGMENTS
                ):
                    raise StorageValidationError(
                        f"{source} must not contain authority claim field: {key}"
                    )
                stack.append(child)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)


def _validate_manifest_publication_policy(value: Mapping[str, Any]) -> None:
    try:
        assert_no_secret_fields(value, source="repository manifest")
    except SecretFieldError as exc:
        raise StorageValidationError(str(exc)) from exc
    _assert_no_diagnostic_fields(value, source="repository manifest")
    _assert_no_authority_claim_fields(value, source="repository manifest")


def _validate_publishable_entry(entry: IndexEntry, *, view_type: str) -> None:
    expected_path = f"views/{view_type}"
    if entry.path != expected_path:
        raise StorageValidationError(
            f"selected view {view_type!r} path must be exactly {expected_path!r}"
        )
    for section, value in (("config", entry.config), ("metadata", entry.metadata)):
        source = f"view {view_type!r} {section}"
        try:
            assert_no_secret_fields(value, source=source)
        except SecretFieldError as exc:
            raise StorageValidationError(str(exc)) from exc
        _assert_no_diagnostic_fields(value, source=source)


def _repo_manifest_v11_from_data(data: dict[str, Any]) -> RepoManifest:
    """Validate and detach one exact RepoManifest v1.1 object."""

    _validate_json_budget(data, label="repository manifest")
    _require_exact_fields(data, _TOP_LEVEL_FIELDS, label="repository manifest")
    if data.get("version") != MANIFEST_VERSION:
        raise StorageValidationError(
            f"repository manifest version must be {MANIFEST_VERSION}"
        )

    repo = data.get("repo")
    indexes = data.get("indexes")
    capabilities = data.get("capabilities")
    if not isinstance(repo, dict):
        raise StorageValidationError("repository manifest repo must be an object")
    if not isinstance(indexes, dict):
        raise StorageValidationError("repository manifest indexes must be an object")
    if not isinstance(capabilities, dict):
        raise StorageValidationError(
            "repository manifest capabilities must be an object"
        )
    _require_exact_fields(repo, _REPO_FIELDS, label="repository manifest repo")
    if len(indexes) > _MAX_INDEXES:
        raise StorageValidationError("repository manifest has too many indexes")
    if len(capabilities) > _MAX_CAPABILITIES:
        raise StorageValidationError("repository manifest has too many capabilities")

    repo_path = _bounded_text(
        repo.get("path"), "repository manifest path", allow_empty=False
    )
    commit = _bounded_text(repo.get("commit"), "repository manifest commit")
    last_commit = _bounded_text(
        repo.get("last_indexed_commit"),
        "repository manifest last indexed commit",
    )
    source = _bounded_text(
        repo.get("source_fingerprint"),
        "repository manifest source fingerprint",
        allow_empty=False,
    )
    source_version = source_fingerprint_version(source)
    if source_version not in {1, 2}:
        raise StorageValidationError(
            "repository manifest source fingerprint must be canonical v1 or v2"
        )
    last_source = _bounded_text(
        repo.get("last_indexed_source_fingerprint"),
        "repository manifest last indexed source fingerprint",
    )
    if last_source and source_fingerprint_version(last_source) not in {1, 2}:
        raise StorageValidationError(
            "repository manifest last indexed source fingerprint is unsupported"
        )
    raw_languages = repo.get("languages")
    if not isinstance(raw_languages, list) or len(raw_languages) > _MAX_LANGUAGES:
        raise StorageValidationError("repository manifest languages must be a list")
    languages = [
        _bounded_text(
            language,
            "repository manifest language",
            max_length=_MAX_NAME,
            allow_empty=False,
        )
        for language in raw_languages
    ]
    if len(set(languages)) != len(languages):
        raise StorageValidationError(
            "repository manifest languages must not contain duplicates"
        )
    file_count = _nonnegative_int(
        repo.get("file_count"), "repository manifest file count"
    )

    entries: dict[str, IndexEntry] = {}
    for name, raw_entry in indexes.items():
        view_name = _bounded_text(
            name,
            "repository manifest view name",
            max_length=_MAX_VIEW_NAME,
            allow_empty=False,
        )
        if not isinstance(raw_entry, dict):
            raise StorageValidationError(
                f"repository manifest view {view_name!r} must be an object"
            )
        _require_exact_fields(
            raw_entry,
            _ENTRY_FIELDS,
            label=f"repository manifest view {view_name!r}",
        )
        config = raw_entry.get("config")
        metadata = raw_entry.get("metadata")
        if not isinstance(config, dict) or not isinstance(metadata, dict):
            raise StorageValidationError(
                f"repository manifest view {view_name!r} metadata must be objects"
            )
        status = _bounded_text(
            raw_entry.get("status"),
            f"view {view_name!r} status",
            allow_empty=False,
        )
        if status not in {"fresh", "stale", "failed"}:
            raise StorageValidationError(f"view {view_name!r} has an invalid status")
        entry_source = _bounded_text(
            raw_entry.get("source_fingerprint"),
            f"view {view_name!r} source fingerprint",
        )
        if entry_source and source_fingerprint_version(entry_source) not in {1, 2}:
            raise StorageValidationError(
                f"view {view_name!r} source fingerprint is unsupported"
            )
        index_type = _bounded_text(
            raw_entry.get("index_type"),
            f"view {view_name!r} index type",
            max_length=_MAX_VIEW_NAME,
            allow_empty=False,
        )
        if index_type != view_name:
            raise StorageValidationError(
                f"repository manifest view {view_name!r} index type does not match"
            )
        entries[view_name] = IndexEntry(
            index_type=index_type,
            path=_bounded_text(
                raw_entry.get("path"),
                f"view {view_name!r} path",
                allow_empty=False,
            ),
            built_at=_bounded_text(
                raw_entry.get("built_at"), f"view {view_name!r} built at"
            ),
            built_at_epoch=_epoch(
                raw_entry.get("built_at_epoch"),
                f"view {view_name!r} built_at_epoch",
            ),
            status=status,
            config=config,
            metadata=metadata,
            commit=_bounded_text(raw_entry.get("commit"), f"view {view_name!r} commit"),
            source_fingerprint=entry_source,
        )

    normalized_capabilities: dict[str, bool] = {}
    for name, enabled in capabilities.items():
        capability = _bounded_text(
            name,
            "repository manifest capability",
            max_length=_MAX_NAME,
            allow_empty=False,
        )
        if not isinstance(enabled, bool):
            raise StorageValidationError(
                "repository manifest capabilities must map text to booleans"
            )
        normalized_capabilities[capability] = enabled

    manifest = RepoManifest(
        version=MANIFEST_VERSION,
        repo_path=repo_path,
        commit=commit,
        last_indexed_commit=last_commit,
        source_fingerprint=source,
        last_indexed_source_fingerprint=last_source,
        languages=languages,
        file_count=file_count,
        indexes=entries,
        capabilities=normalized_capabilities,
        compiled_at=_bounded_text(
            data.get("compiled_at"), "repository manifest compiled_at"
        ),
        compiled_at_epoch=_epoch(
            data.get("compiled_at_epoch"), "repository manifest compiled_at_epoch"
        ),
    )
    try:
        canonical_json(manifest.to_dict())
    except StorageValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise StorageValidationError(
            "repository manifest is not canonical JSON"
        ) from exc
    return manifest


def _normalize_requested(
    requested: Sequence[str] | None, *, field: str
) -> tuple[str, ...]:
    if requested is None:
        return ()
    if isinstance(requested, (str, bytes)):
        raise StorageValidationError(f"{field} must be a sequence")
    values: list[str] = []
    try:
        iterator = iter(requested)
        for _index in range(_MAX_INDEXES + 1):
            try:
                value = next(iterator)
            except StopIteration:
                break
            if type(value) is not str:
                raise StorageValidationError(
                    f"{field} must contain only exact view names"
                )
            bounded_value = _bounded_text(
                value,
                f"{field} view name",
                max_length=_MAX_VIEW_NAME,
                allow_empty=False,
            )
            normalized_value = str.strip(bounded_value)
            if not normalized_value:
                raise StorageValidationError(f"{field} contains an empty view name")
            values.append(normalized_value)
        else:
            raise StorageValidationError(f"{field} has too many view names")
    except StorageValidationError:
        raise
    except Exception as exc:
        raise StorageValidationError(f"{field} must be a bounded sequence") from exc
    normalized = tuple(dict.fromkeys(values))
    unsupported = sorted(set(normalized) - PORTABLE_STORAGE_VIEWS)
    if unsupported:
        raise StorageValidationError(
            "manifest storage planning supports only bm25 and vector views: "
            + ", ".join(unsupported)
        )
    return tuple(sorted(normalized))


def _entry_is_current(manifest: RepoManifest, view_type: str) -> tuple[bool, str]:
    entry = manifest.indexes.get(view_type)
    if entry is None:
        return False, "missing"
    if entry.index_type != view_type:
        return False, "mismatched_index_type"
    if entry.path != f"views/{view_type}":
        return False, "artifact_path_mismatch"
    if entry.status != "fresh":
        return False, "not_fresh"
    if entry.commit != manifest.commit or (
        entry.source_fingerprint != manifest.source_fingerprint
    ):
        return False, "source_mismatch"
    return True, ""


def _initial_selection(
    manifest: RepoManifest,
    *,
    views: Sequence[str] | None,
    required_views: Sequence[str] | None,
    optional_views: Sequence[str] | None,
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, str]]:
    if views is not None and (required_views is not None or optional_views is not None):
        raise StorageValidationError(
            "views cannot be combined with required_views or optional_views"
        )

    required: tuple[str, ...]
    optional: tuple[str, ...]
    if views is not None:
        required = _normalize_requested(views, field="views")
        optional = ()
    elif required_views is None and optional_views is None:
        bm25_current, _ = _entry_is_current(manifest, "bm25")
        vector_current, _ = _entry_is_current(manifest, "vector")
        if bm25_current:
            required = ("bm25",)
            optional = ("vector",) if vector_current else ()
        elif vector_current:
            required = ("vector",)
            optional = ()
        else:
            required = ()
            optional = ()
    else:
        required = _normalize_requested(required_views, field="required_views")
        optional = _normalize_requested(optional_views, field="optional_views")

    overlap = sorted(set(required) & set(optional))
    if overlap:
        raise StorageValidationError(
            "views cannot be both required and optional: " + ", ".join(overlap)
        )

    skipped: dict[str, str] = {}
    for view_type in required:
        current, reason = _entry_is_current(manifest, view_type)
        if not current:
            raise StorageValidationError(
                f"required view {view_type!r} is not current: {reason}"
            )
    retained_optional: list[str] = []
    for view_type in optional:
        current, _reason = _entry_is_current(manifest, view_type)
        if current:
            retained_optional.append(view_type)
        else:
            skipped[view_type] = "not_current"
    optional = tuple(retained_optional)
    if not required and not optional:
        raise StorageValidationError(
            "manifest storage planning requires a current bm25 or vector view"
        )
    return required, optional, skipped


def _reject_unknown_config_fields(
    config: Mapping[str, Any],
    *,
    view_type: str,
    profile_axes: tuple[str, ...],
    non_profile_fields: frozenset[str],
) -> None:
    unknown = sorted(set(config) - set(profile_axes) - non_profile_fields)
    if unknown:
        raise _IncompleteProfile(
            f"view {view_type!r} has unclassified config fields: " + ", ".join(unknown)
        )


def _reject_unknown_metadata_fields(
    metadata: Mapping[str, Any],
    *,
    view_type: str,
    profile_axes: tuple[str, ...],
    non_profile_fields: frozenset[str],
) -> None:
    unknown = sorted(
        set(metadata)
        - set(profile_axes)
        - non_profile_fields
        - _NON_PROFILE_METADATA_FIELDS
    )
    if unknown:
        raise _IncompleteProfile(
            f"view {view_type!r} has unclassified metadata fields: "
            + ", ".join(unknown)
        )


def _require_profile_axes(
    config: Mapping[str, Any], *, view_type: str, axes: tuple[str, ...]
) -> None:
    missing = [axis for axis in axes if axis not in config]
    if missing:
        raise _IncompleteProfile(
            f"view {view_type!r} profile is missing required axes: "
            + ", ".join(missing)
        )


def _profile_config(entry: IndexEntry, *, view_type: str) -> dict[str, Any]:
    config = entry.config
    axes: tuple[str, ...]
    non_profile: frozenset[str]
    embedding_load_policy: dict[str, Any] | None = None
    if view_type == "bm25":
        axes = BM25_PROFILE_AXES
        non_profile = _BM25_NON_PROFILE_CONFIG_FIELDS
    elif view_type == "vector":
        axes = VECTOR_PROFILE_AXES
        non_profile = _VECTOR_NON_PROFILE_CONFIG_FIELDS
    else:  # pragma: no cover - selection excludes unsupported views
        raise _IncompleteProfile(f"unsupported view profile: {view_type}")
    _require_profile_axes(config, view_type=view_type, axes=axes)
    _reject_unknown_config_fields(
        config,
        view_type=view_type,
        profile_axes=axes,
        non_profile_fields=non_profile,
    )
    _reject_unknown_metadata_fields(
        entry.metadata,
        view_type=view_type,
        profile_axes=axes,
        non_profile_fields=non_profile,
    )
    for axis in axes:
        if axis not in entry.metadata:
            continue
        try:
            config_value = _canonical_bytes({"value": config[axis]})
            metadata_value = _canonical_bytes({"value": entry.metadata[axis]})
        except (TypeError, ValueError) as exc:
            raise _IncompleteProfile(
                f"view {view_type!r} profile axis {axis!r} is not canonical JSON"
            ) from exc
        if config_value != metadata_value:
            raise _IncompleteProfile(
                f"view {view_type!r} profile axis {axis!r} conflicts with metadata"
            )

    builder_schema = _positive_int(
        config["builder_schema"], f"view {view_type!r} builder_schema"
    )
    if builder_schema != CURRENT_PORTABLE_BUILDER_SCHEMAS[view_type]:
        raise _IncompleteProfile(
            f"view {view_type!r} builder_schema is not the current portable schema"
        )
    _text_list(config["languages"], f"view {view_type!r} languages")
    _positive_int(
        config["max_lines_per_chunk"],
        f"view {view_type!r} max_lines_per_chunk",
    )
    failure_policy = config["chunking_failure_policy"]
    if (
        not isinstance(failure_policy, str)
        or not failure_policy.strip()
        or "\x00" in failure_policy
        or len(failure_policy) > _MAX_NAME
    ):
        raise _IncompleteProfile(
            f"view {view_type!r} chunking_failure_policy must be text"
        )
    _positive_int(
        config["repository_filter_policy"],
        f"view {view_type!r} repository_filter_policy",
    )

    if view_type == "bm25":
        _positive_int(config["max_k"], "view 'bm25' max_k")
        if not isinstance(config["include_header_epilogue"], bool):
            raise _IncompleteProfile(
                "view 'bm25' include_header_epilogue must be a boolean"
            )
    else:
        _positive_int(config["embedding_dimension"], "vector embedding_dimension")
        _positive_int(config["dimension"], "vector dimension")
        if config["embedding_dimension"] != config["dimension"]:
            raise _IncompleteProfile("view 'vector' embedding dimensions must agree")
        if not isinstance(config["embedding_kwargs"], Mapping):
            raise _IncompleteProfile("view 'vector' embedding_kwargs must be an object")
        if not isinstance(config["embedding_route"], Mapping):
            raise _IncompleteProfile("view 'vector' embedding_route must be an object")
        if (
            not isinstance(config["embedding_fingerprint"], str)
            or not config["embedding_fingerprint"]
        ):
            raise _IncompleteProfile("view 'vector' embedding_fingerprint must be text")
        if not isinstance(config["index_metric"], str) or config[
            "index_metric"
        ] not in {"ip", "l2"}:
            raise _IncompleteProfile("view 'vector' index_metric is unsupported")
        _text_list(
            config["levels"],
            "view 'vector' levels",
            allowed=frozenset({"l0", "l2"}),
        )
        try:
            route = resolve_embedding_artifact_route(config, environ={})
            if route.provider == "huggingface":
                policy = resolve_embedding_artifact_load_policy_from_options(
                    route.model,
                    route.compatibility_options,
                )
                embedding_load_policy = {
                    "revision": policy.revision,
                    "trust_remote_code": policy.trust_remote_code,
                }
            else:
                embedding_load_policy = {
                    "revision": None,
                    "trust_remote_code": False,
                }
        except (TypeError, ValueError) as exc:
            raise _IncompleteProfile(
                "view 'vector' embedding route or model policy is incomplete or "
                "inconsistent"
            ) from exc

    compatibility = {axis: config[axis] for axis in axes if axis != "builder_schema"}
    if embedding_load_policy is not None:
        # The load policy is derived without filesystem discovery.  Keeping it
        # explicit prevents policy defaults from becoming an identity wildcard.
        compatibility["embedding_load_policy"] = embedding_load_policy
    return {
        "contract": REPO_MANIFEST_PROFILE_CONTRACT,
        "manifest_version": MANIFEST_VERSION,
        "builder_schema": config["builder_schema"],
        "compatibility": compatibility,
    }


def _generation_nonnegative_int(value: object, field: str) -> int:
    try:
        return _nonnegative_int(value, field)
    except StorageValidationError as exc:
        raise _IncompleteGenerationRecord(str(exc)) from exc


def _generation_positive_int(value: object, field: str) -> int:
    parsed = _generation_nonnegative_int(value, field)
    if parsed == 0:
        raise _IncompleteGenerationRecord(f"{field} must be a positive integer")
    return parsed


def _generation_bounded_text(
    value: object,
    field: str,
    *,
    allow_empty: bool = True,
) -> str:
    try:
        return _bounded_text(
            value,
            field,
            max_length=_MAX_NAME,
            allow_empty=allow_empty,
        )
    except StorageValidationError as exc:
        raise _IncompleteGenerationRecord(str(exc)) from exc


def _non_profile_field(entry: IndexEntry, field: str) -> object:
    values = [
        section[field] for section in (entry.config, entry.metadata) if field in section
    ]
    if not values:
        return _MISSING_ENTRY_FIELD
    try:
        encoded = [_canonical_bytes({"value": value}) for value in values]
    except (TypeError, ValueError) as exc:
        raise _IncompleteGenerationRecord(
            f"view {entry.index_type!r} {field} is not canonical JSON"
        ) from exc
    if any(value != encoded[0] for value in encoded[1:]):
        raise _IncompleteGenerationRecord(
            f"view {entry.index_type!r} has conflicting {field} records"
        )
    return values[0]


def _validate_non_profile_config(entry: IndexEntry, *, view_type: str) -> None:
    config = entry.config
    artifact_scope = _non_profile_field(entry, "artifact_scope")
    if artifact_scope is not _MISSING_ENTRY_FIELD and artifact_scope != "query-serving":
        raise _IncompleteGenerationRecord(
            f"view {view_type!r} artifact_scope must be 'query-serving'"
        )

    portable_format = _non_profile_field(entry, "portable_document_format")
    expected_format = (
        "codenib.bm25-documents.v1"
        if view_type == "bm25"
        else "codenib.vector-documents.v1"
    )
    if (
        portable_format is not _MISSING_ENTRY_FIELD
        and portable_format != expected_format
    ):
        raise _IncompleteGenerationRecord(
            f"view {view_type!r} portable_document_format is unsupported"
        )

    if view_type == "bm25":
        counts: dict[str, int] = {}
        for field in ("chunk_count", "file_count", "source_file_count"):
            value = _non_profile_field(entry, field)
            if value is not _MISSING_ENTRY_FIELD:
                counts[field] = _generation_nonnegative_int(
                    value, f"view 'bm25' {field}"
                )
        if (
            "chunk_count" in counts
            and "file_count" in counts
            and counts["chunk_count"] != counts["file_count"]
        ):
            raise _IncompleteGenerationRecord(
                "view 'bm25' chunk_count and file_count must agree"
            )
        chunk_count = counts.get("chunk_count", counts.get("file_count"))
        if chunk_count is not None and counts.get("source_file_count", 0) > chunk_count:
            raise _IncompleteGenerationRecord(
                "view 'bm25' source_file_count exceeds its chunk count"
            )
        return

    document_count = _non_profile_field(entry, "document_count")
    if document_count is not _MISSING_ENTRY_FIELD:
        if not isinstance(document_count, Mapping):
            raise _IncompleteGenerationRecord(
                "view 'vector' document_count must be an object"
            )
        if not set(document_count) <= {"l0", "l2"}:
            raise _IncompleteGenerationRecord(
                "view 'vector' document_count has an unsupported level"
            )
        configured_levels = config.get("levels")
        if isinstance(configured_levels, list) and not set(document_count) <= set(
            configured_levels
        ):
            raise _IncompleteGenerationRecord(
                "view 'vector' document_count conflicts with configured levels"
            )
        for level, count in document_count.items():
            _generation_positive_int(count, f"view 'vector' {level} document count")

    last_commit = _non_profile_field(entry, "last_commit")
    if last_commit is not _MISSING_ENTRY_FIELD:
        last_commit = _generation_bounded_text(
            last_commit,
            "view 'vector' last_commit",
        )
        if last_commit and last_commit != entry.commit:
            raise _IncompleteGenerationRecord(
                "view 'vector' last_commit conflicts with its manifest entry commit"
            )

    incremental = {
        field: _non_profile_field(entry, field) for field in _VECTOR_INCREMENTAL_FIELDS
    }
    incremental_present = {
        field
        for field, value in incremental.items()
        if value is not _MISSING_ENTRY_FIELD
    }
    if incremental_present and incremental_present != set(_VECTOR_INCREMENTAL_FIELDS):
        raise _IncompleteGenerationRecord(
            "view 'vector' incremental provenance record is incomplete"
        )
    if incremental_present:
        _generation_nonnegative_int(
            incremental["chunks_reembedded"],
            "view 'vector' chunks_reembedded",
        )
        _generation_nonnegative_int(
            incremental["chunks_from_cache"],
            "view 'vector' chunks_from_cache",
        )
        cache_hit_rate = incremental["cache_hit_rate"]
        if (
            isinstance(cache_hit_rate, bool)
            or not isinstance(cache_hit_rate, (int, float))
            or not math.isfinite(cache_hit_rate)
            or cache_hit_rate < 0
            or cache_hit_rate > 1
        ):
            raise _IncompleteGenerationRecord(
                "view 'vector' cache_hit_rate must be finite and between 0 and 1"
            )
        new_commit = _generation_bounded_text(
            incremental["new_commit"],
            "view 'vector' new_commit",
            allow_empty=False,
        )
        if new_commit != entry.commit:
            raise _IncompleteGenerationRecord(
                "view 'vector' new_commit conflicts with its manifest entry commit"
            )
        if last_commit is not _MISSING_ENTRY_FIELD:
            raise _IncompleteGenerationRecord(
                "view 'vector' incremental provenance conflicts with last_commit"
            )
    elif last_commit is _MISSING_ENTRY_FIELD:
        raise _IncompleteGenerationRecord(
            "view 'vector' full-build provenance is missing last_commit"
        )

    fallback = {
        field: _non_profile_field(entry, field) for field in _VECTOR_FALLBACK_FIELDS
    }
    fallback_present = {
        field for field, value in fallback.items() if value is not _MISSING_ENTRY_FIELD
    }
    if fallback_present and fallback_present != set(_VECTOR_FALLBACK_FIELDS):
        raise _IncompleteGenerationRecord(
            "view 'vector' incremental fallback provenance record is incomplete"
        )
    if fallback_present:
        update_mode = _generation_bounded_text(
            fallback["update_mode"],
            "view 'vector' update_mode",
            allow_empty=False,
        )
        if update_mode not in _VECTOR_UPDATE_MODES:
            raise _IncompleteGenerationRecord(
                "view 'vector' update_mode is unsupported"
            )
        _generation_bounded_text(
            fallback["incremental_fallback_reason"],
            "view 'vector' incremental_fallback_reason",
            allow_empty=False,
        )
        if incremental_present:
            raise _IncompleteGenerationRecord(
                "view 'vector' incremental and fallback provenance conflict"
            )


def _record_from_entry(entry: IndexEntry, field: str, *, view_type: str) -> Any:
    found = [
        section[field] for section in (entry.config, entry.metadata) if field in section
    ]
    if not found:
        raise _IncompleteGenerationRecord(f"view {view_type!r} is missing {field}")
    try:
        canonical_values = [_canonical_bytes({"record": value}) for value in found]
    except (TypeError, ValueError) as exc:
        raise _IncompleteGenerationRecord(
            f"view {view_type!r} has an invalid {field} record"
        ) from exc
    if any(value != canonical_values[0] for value in canonical_values[1:]):
        raise _IncompleteGenerationRecord(
            f"view {view_type!r} has conflicting {field} records"
        )
    return found[0]


def _fingerprint_record(
    value: object,
    *,
    label: str,
    expected_file: str | None,
    max_bytes: int,
    include_file: bool,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _IncompleteGenerationRecord(f"{label} must be an object")
    expected = {"size", "sha256"} | ({"file"} if include_file else set())
    if set(value) != expected:
        raise _IncompleteGenerationRecord(f"{label} has an invalid shape")
    size = value.get("size")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or size > max_bytes
    ):
        raise _IncompleteGenerationRecord(f"{label} has an invalid size")
    digest = value.get("sha256")
    if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        raise _IncompleteGenerationRecord(f"{label} has an invalid SHA-256 digest")
    result: dict[str, Any] = {"size": size, "sha256": digest}
    if include_file:
        filename = value.get("file")
        if not isinstance(filename, str) or filename != expected_file:
            raise _IncompleteGenerationRecord(f"{label} has an invalid file name")
        result = {"file": filename, **result}
    return result


def _generation_record(entry: IndexEntry, *, view_type: str) -> dict[str, Any]:
    if view_type == "bm25":
        raw = _record_from_entry(
            entry,
            "artifact_file_fingerprints",
            view_type=view_type,
        )
        if not isinstance(raw, Mapping) or set(raw) != {
            "documents.json",
            "bm25_metadata.json",
        }:
            raise _IncompleteGenerationRecord(
                "view 'bm25' artifact fingerprints are incomplete"
            )
        files = {
            "bm25_metadata.json": _fingerprint_record(
                raw["bm25_metadata.json"],
                label="BM25 metadata fingerprint",
                expected_file=None,
                max_bytes=_MAX_CONFIG_BYTES,
                include_file=False,
            ),
            "documents.json": _fingerprint_record(
                raw["documents.json"],
                label="BM25 documents fingerprint",
                expected_file=None,
                max_bytes=_MAX_DOCUMENTS_BYTES,
                include_file=False,
            ),
        }
        return {
            "contract": REPO_MANIFEST_BM25_GENERATION_CONTRACT,
            "files": files,
        }

    raw = _record_from_entry(
        entry,
        "persistence_config_fingerprint",
        view_type=view_type,
    )
    route = resolve_embedding_artifact_route(entry.config, environ={})
    expected = f"config_{route.model.replace('/', '__')}.json"
    config_record = _fingerprint_record(
        raw,
        label="vector persistence config fingerprint",
        expected_file=expected,
        max_bytes=_MAX_CONFIG_BYTES,
        include_file=True,
    )
    return {
        "contract": REPO_MANIFEST_VECTOR_GENERATION_CONTRACT,
        "persistence_config": config_record,
    }


def _view_import_intent(
    entry: IndexEntry, *, view_type: str, required: bool
) -> ViewImportIntent:
    _validate_publishable_entry(entry, view_type=view_type)
    profile_config = _profile_config(entry, view_type=view_type)
    try:
        profile = ViewProfile.create(
            view_type,
            profile_config,
            name=REPO_MANIFEST_PROFILE_NAME,
        )
    except (TypeError, ValueError) as exc:
        raise _IncompleteProfile(
            f"view {view_type!r} profile is not canonical JSON"
        ) from exc
    _validate_non_profile_config(entry, view_type=view_type)
    generation = _generation_record(entry, view_type=view_type)
    return ViewImportIntent(
        view_type=view_type,
        required=required,
        manifest_entry_json=canonical_json(entry.to_dict()),
        profile=profile,
        generation_record_json=canonical_json(generation),
    )


def _canonical_object_json(payload: object, *, label: str) -> dict[str, Any]:
    if type(payload) is not str or payload.startswith("\ufeff"):
        raise StorageValidationError(f"{label} must be canonical JSON text")
    try:
        encoded = _encode_bounded_manifest_text(
            payload,
            limit=DEFAULT_MAX_MANIFEST_BYTES,
        )
    except UnicodeError as exc:
        raise StorageValidationError(f"{label} must be valid UTF-8 text") from exc
    _preflight_json_bytes(
        encoded,
        label=label,
        max_bytes=DEFAULT_MAX_MANIFEST_BYTES,
    )
    value = _strict_json_loads(payload, label=label)
    _validate_json_budget(value, label=label)
    if canonical_json(value) != payload:
        raise StorageValidationError(f"{label} is not canonical JSON")
    return value


def _intent_entry_from_data(data: dict[str, Any], *, view_type: str) -> IndexEntry:
    _require_exact_fields(data, _ENTRY_FIELDS, label="view import manifest entry")
    if not isinstance(data.get("config"), dict) or not isinstance(
        data.get("metadata"), dict
    ):
        raise StorageValidationError("view import entry metadata must be objects")
    try:
        entry = IndexEntry.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageValidationError("view import entry is invalid") from exc
    if entry.to_dict() != data:
        raise StorageValidationError("view import entry shape is not canonical")
    if entry.index_type != view_type or entry.status != "fresh":
        raise StorageValidationError(
            "view import entry must be a fresh matching view type"
        )
    _bounded_text(entry.path, "view import entry path", allow_empty=False)
    expected_path = f"views/{view_type}"
    if entry.path != expected_path:
        raise StorageValidationError(
            f"view import entry path must be exactly {expected_path!r}"
        )
    _bounded_text(entry.built_at, "view import entry built_at")
    _epoch(entry.built_at_epoch, "view import entry built_at_epoch")
    _bounded_text(entry.commit, "view import entry commit")
    _bounded_text(
        entry.source_fingerprint,
        "view import entry source fingerprint",
        allow_empty=False,
    )
    if source_fingerprint_version(entry.source_fingerprint) not in {1, 2}:
        raise StorageValidationError(
            "view import entry source fingerprint is unsupported"
        )
    return entry


def _validate_view_import_intent(intent: ViewImportIntent) -> None:
    if (
        type(intent.view_type) is not str
        or str.__len__(intent.view_type) > _MAX_VIEW_NAME
        or intent.view_type not in PORTABLE_STORAGE_VIEWS
    ):
        raise StorageValidationError("view import intent has an unsupported view type")
    if type(intent.required) is not bool:
        raise StorageValidationError("view import intent required flag must be boolean")
    if type(intent.profile) is not ViewProfile:
        raise StorageValidationError("view import intent profile is invalid")
    profile_fields = (
        intent.profile.view_type,
        intent.profile.name,
        intent.profile.config_json,
    )
    if any(type(value) is not str for value in profile_fields):
        raise StorageValidationError("view import intent profile fields must be exact")
    _bounded_text(
        intent.profile.view_type,
        "view import profile view type",
        max_length=_MAX_VIEW_NAME,
        allow_empty=False,
    )
    _bounded_text(
        intent.profile.name,
        "view import profile name",
        max_length=_MAX_NAME,
        allow_empty=False,
    )
    _canonical_object_json(
        intent.profile.config_json,
        label="view import profile config",
    )

    entry_data = _canonical_object_json(
        intent.manifest_entry_json,
        label="view import manifest entry",
    )
    _validate_manifest_publication_policy({"entry": entry_data})
    entry = _intent_entry_from_data(entry_data, view_type=intent.view_type)
    try:
        expected_profile = ViewProfile.create(
            intent.view_type,
            _profile_config(entry, view_type=intent.view_type),
            name=REPO_MANIFEST_PROFILE_NAME,
        )
        _validate_non_profile_config(entry, view_type=intent.view_type)
        expected_generation = canonical_json(
            _generation_record(entry, view_type=intent.view_type)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageValidationError(
            "view import intent does not match a complete manifest entry"
        ) from exc
    if profile_fields != (
        expected_profile.view_type,
        expected_profile.name,
        expected_profile.config_json,
    ):
        raise StorageValidationError(
            "view import intent profile does not match its manifest entry"
        )
    _canonical_object_json(
        intent.generation_record_json,
        label="view import generation record",
    )
    if intent.generation_record_json != expected_generation:
        raise StorageValidationError(
            "view import generation record does not match its manifest entry"
        )


def _canonical_view_names(value: object, *, field: str) -> tuple[str, ...]:
    if (
        type(value) is not tuple
        or tuple.__len__(value) > len(PORTABLE_STORAGE_VIEWS)
        or any(
            type(item) is not str
            or str.__len__(item) > _MAX_VIEW_NAME
            or item not in PORTABLE_STORAGE_VIEWS
            for item in value
        )
    ):
        raise StorageValidationError(f"{field} must be a tuple of portable views")
    if value != tuple(sorted(set(value))):
        raise StorageValidationError(f"{field} must be sorted and unique")
    return value


def _validate_view_selection(selection: ViewSelection) -> None:
    required = _canonical_view_names(
        selection.required_views,
        field="required views",
    )
    optional = _canonical_view_names(
        selection.optional_views,
        field="optional views",
    )
    selected = _canonical_view_names(
        selection.selected_views,
        field="selected views",
    )
    if set(required) & set(optional):
        raise StorageValidationError("required and optional views must be disjoint")
    if type(selection.skipped_items) is not tuple or tuple.__len__(
        selection.skipped_items
    ) > len(PORTABLE_STORAGE_VIEWS):
        raise StorageValidationError("skipped views must be a canonical tuple")
    skipped: list[tuple[str, str]] = []
    for item in selection.skipped_items:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or str.__len__(item[0]) > _MAX_VIEW_NAME
            or item[0] not in PORTABLE_STORAGE_VIEWS
            or type(item[1]) is not str
            or str.__len__(item[1]) > _MAX_NAME
            or item[1] not in _SELECTION_SKIP_REASONS
        ):
            raise StorageValidationError("skipped view decision is invalid")
        skipped.append(item)
    if tuple(skipped) != tuple(sorted(set(skipped))):
        raise StorageValidationError("skipped views must be sorted and unique")
    skipped_names = {view for view, _reason in skipped}
    if len(skipped_names) != len(skipped):
        raise StorageValidationError("a view may have only one skip decision")
    if not skipped_names <= set(optional):
        raise StorageValidationError("only optional views may be skipped")
    expected_selected = tuple(sorted(set(required) | (set(optional) - skipped_names)))
    if not selected or selected != expected_selected:
        raise StorageValidationError(
            "selected views do not close required, optional, and skipped views"
        )


def _incomplete_view_reason(entry: IndexEntry, *, view_type: str) -> str | None:
    try:
        _profile_config(entry, view_type=view_type)
    except _IncompleteProfile:
        return "incomplete_profile"
    try:
        _validate_non_profile_config(entry, view_type=view_type)
        _generation_record(entry, view_type=view_type)
    except _IncompleteGenerationRecord:
        return "incomplete_generation_record"
    return None


def _validate_import_plan(plan: RepoManifestImportPlan) -> None:
    manifest_data = _canonical_object_json(
        plan.manifest_json,
        label="import plan manifest",
    )
    manifest = _repo_manifest_v11_from_data(manifest_data)
    _validate_manifest_publication_policy(manifest_data)
    if not isinstance(plan.source, SourceIntent):
        raise StorageValidationError("import plan source intent is invalid")
    if type(plan.source) is not SourceIntent:
        raise StorageValidationError("import plan source intent must be exact")
    SourceIntent.__post_init__(plan.source)
    fingerprint_version = source_fingerprint_version(manifest.source_fingerprint)
    if fingerprint_version not in {1, 2}:  # pragma: no cover - manifest gate
        raise StorageValidationError("import plan source fingerprint is unsupported")
    expected_source = SourceIntent(
        fingerprint=manifest.source_fingerprint,
        fingerprint_version=fingerprint_version,
        file_count=manifest.file_count,
        display_commit=manifest.commit,
    )
    if (
        plan.source.fingerprint,
        plan.source.fingerprint_version,
        plan.source.file_count,
        plan.source.display_commit,
    ) != (
        expected_source.fingerprint,
        expected_source.fingerprint_version,
        expected_source.file_count,
        expected_source.display_commit,
    ):
        raise StorageValidationError(
            "import plan source intent does not match its manifest"
        )
    if not isinstance(plan.selection, ViewSelection):
        raise StorageValidationError("import plan selection is invalid")
    if type(plan.selection) is not ViewSelection:
        raise StorageValidationError("import plan selection must be exact")
    _validate_view_selection(plan.selection)
    if (
        type(plan.views) is not tuple
        or tuple.__len__(plan.views) > len(PORTABLE_STORAGE_VIEWS)
        or any(type(view) is not ViewImportIntent for view in plan.views)
    ):
        raise StorageValidationError("import plan views must be an intent tuple")
    for view in plan.views:
        _validate_view_import_intent(view)
    view_names = tuple(view.view_type for view in plan.views)
    if len(set(view_names)) != len(view_names):
        raise StorageValidationError("import plan contains duplicate view intents")
    if view_names != plan.selection.selected_views:
        raise StorageValidationError(
            "import plan views do not match its canonical selection"
        )
    for view_type, reason in plan.selection.skipped_items:
        current, _current_reason = _entry_is_current(manifest, view_type)
        if reason == "not_current":
            if current:
                raise StorageValidationError(
                    "import plan skip reason conflicts with a current view"
                )
            continue
        if not current:
            raise StorageValidationError(
                "import plan incomplete-view skip is not current in its manifest"
            )
        observed_reason = _incomplete_view_reason(
            manifest.indexes[view_type],
            view_type=view_type,
        )
        if observed_reason != reason:
            raise StorageValidationError(
                "import plan skip reason does not match its manifest view"
            )
    required = set(plan.selection.required_views)
    for view in plan.views:
        current, _reason = _entry_is_current(manifest, view.view_type)
        if not current:
            raise StorageValidationError(
                "import plan contains a view not current in its manifest"
            )
        if view.required != (view.view_type in required):
            raise StorageValidationError(
                "import plan view requirement does not match its selection"
            )
        expected_entry = canonical_json(manifest.indexes[view.view_type].to_dict())
        if view.manifest_entry_json != expected_entry:
            raise StorageValidationError(
                "import plan view entry does not match its manifest"
            )


def _validated_manifest_data(
    value: RepoManifest | Mapping[str, Any],
    *,
    max_bytes: int,
) -> dict[str, Any]:
    if type(value) is RepoManifest:
        raw: Any = value
    elif isinstance(value, Mapping):
        raw = value
    else:
        raise TypeError("manifest must be a RepoManifest or JSON object")
    try:
        snapshot, _size = _snapshot_json_value(
            raw,
            label="repository manifest",
            max_bytes=max_bytes,
        )
        if not isinstance(snapshot, dict):  # pragma: no cover - Mapping root
            raise StorageValidationError("repository manifest must be an object")
        normalized = _strict_json_loads(
            canonical_json(snapshot),
            label="repository manifest",
        )
    except StorageValidationError:
        raise
    except (RecursionError, TypeError, ValueError) as exc:
        raise StorageValidationError(
            "repository manifest is not canonical JSON"
        ) from exc
    return normalized


def plan_repo_manifest_import(
    manifest: RepoManifest | Mapping[str, Any],
    *,
    views: Sequence[str] | None = None,
    required_views: Sequence[str] | None = None,
    optional_views: Sequence[str] | None = None,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
) -> RepoManifestImportPlan:
    """Build a pure import plan from an in-memory manifest value.

    The returned plan is safe to inspect and hash, but it carries no authority
    to read the repository, execute a native index, write CAS objects, or
    publish catalog state.
    """

    limit = _manifest_limit(max_manifest_bytes)
    data = _validated_manifest_data(manifest, max_bytes=limit)
    parsed = _repo_manifest_v11_from_data(data)
    normalized_manifest = parsed.to_dict()
    _validate_manifest_publication_policy(normalized_manifest)
    manifest_json = canonical_json(normalized_manifest)
    if len(manifest_json.encode("ascii")) > limit:
        raise StorageValidationError(
            f"repository manifest exceeds the {limit}-byte limit"
        )

    required, optional, skipped = _initial_selection(
        parsed,
        views=views,
        required_views=required_views,
        optional_views=optional_views,
    )
    intents: list[ViewImportIntent] = []
    for view_type in sorted(set(required) | set(optional)):
        is_required = view_type in required
        try:
            intent = _view_import_intent(
                parsed.indexes[view_type],
                view_type=view_type,
                required=is_required,
            )
        except _IncompleteProfile as exc:
            if is_required:
                raise StorageValidationError(
                    f"required view {view_type!r} has an incomplete profile: {exc}"
                ) from exc
            skipped[view_type] = "incomplete_profile"
            continue
        except _IncompleteGenerationRecord as exc:
            if is_required:
                raise StorageValidationError(
                    f"required view {view_type!r} has an incomplete generation record: "
                    f"{exc}"
                ) from exc
            skipped[view_type] = "incomplete_generation_record"
            continue
        intents.append(intent)

    if not intents:
        raise StorageValidationError(
            "manifest storage planning produced no complete portable view intents"
        )
    selected = tuple(intent.view_type for intent in intents)
    selection = ViewSelection(
        required_views=required,
        optional_views=tuple(sorted(set(optional) | set(skipped))),
        selected_views=selected,
        skipped_items=tuple(sorted(skipped.items())),
    )
    fingerprint_version = source_fingerprint_version(parsed.source_fingerprint)
    assert fingerprint_version in {1, 2}
    source = SourceIntent(
        fingerprint=parsed.source_fingerprint,
        fingerprint_version=fingerprint_version,
        file_count=parsed.file_count,
        display_commit=parsed.commit,
    )
    return RepoManifestImportPlan(
        manifest_json=manifest_json,
        source=source,
        selection=selection,
        views=tuple(intents),
    )


def plan_repo_manifest_import_bytes(
    payload: bytes | bytearray | memoryview | str,
    *,
    views: Sequence[str] | None = None,
    required_views: Sequence[str] | None = None,
    optional_views: Sequence[str] | None = None,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
) -> RepoManifestImportPlan:
    """Parse bounded strict JSON bytes and build a pure import plan."""

    limit = _manifest_limit(max_manifest_bytes)
    if isinstance(payload, str):
        try:
            encoded = _encode_bounded_manifest_text(payload, limit=limit)
        except UnicodeError as exc:
            raise StorageValidationError(
                "repository manifest is not valid UTF-8"
            ) from exc
        text = encoded.decode("utf-8", errors="strict")
    elif isinstance(payload, bytes):
        if bytes.__len__(payload) > limit:
            raise StorageValidationError(
                f"repository manifest exceeds the {limit}-byte limit"
            )
        encoded = payload if type(payload) is bytes else memoryview(payload).tobytes()
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise StorageValidationError(
                "repository manifest is not valid UTF-8"
            ) from exc
    elif isinstance(payload, (bytearray, memoryview)):
        try:
            view = memoryview(payload)
            if view.nbytes > limit:
                raise StorageValidationError(
                    f"repository manifest exceeds the {limit}-byte limit"
                )
            encoded = view.tobytes()
        except StorageValidationError:
            raise
        except Exception as exc:
            raise StorageValidationError(
                "repository manifest buffer is unavailable"
            ) from exc
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise StorageValidationError(
                "repository manifest is not valid UTF-8"
            ) from exc
    else:
        raise TypeError("manifest payload must be bytes or text")
    if len(encoded) > limit:
        raise StorageValidationError(
            f"repository manifest exceeds the {limit}-byte limit"
        )
    if text.startswith("\ufeff"):
        raise StorageValidationError("repository manifest must not contain a BOM")
    _preflight_json_bytes(
        encoded,
        label="repository manifest",
        max_bytes=limit,
    )
    data = _strict_json_loads(text, label="repository manifest")
    return plan_repo_manifest_import(
        data,
        views=views,
        required_views=required_views,
        optional_views=optional_views,
        max_manifest_bytes=limit,
    )


def _encode_bounded_manifest_text(value: str, *, limit: int) -> bytes:
    """Encode text without allocating an over-limit UTF-8 copy first."""

    character_count = str.__len__(value)
    if character_count > limit:
        raise StorageValidationError(
            f"repository manifest exceeds the {limit}-byte limit"
        )
    encoder = codecs.getincrementalencoder("utf-8")(errors="strict")
    chunks: list[bytes] = []
    byte_count = 0
    for offset in range(0, character_count, 4096):
        text_chunk = str.__getitem__(value, slice(offset, offset + 4096))
        encoded_chunk = encoder.encode(text_chunk, final=False)
        byte_count += len(encoded_chunk)
        if byte_count > limit:
            raise StorageValidationError(
                f"repository manifest exceeds the {limit}-byte limit"
            )
        chunks.append(encoded_chunk)
    final_chunk = encoder.encode("", final=True)
    byte_count += len(final_chunk)
    if byte_count > limit:
        raise StorageValidationError(
            f"repository manifest exceeds the {limit}-byte limit"
        )
    chunks.append(final_chunk)
    return b"".join(chunks)


__all__ = [
    "BM25_PROFILE_AXES",
    "DEFAULT_MAX_MANIFEST_BYTES",
    "PORTABLE_STORAGE_VIEWS",
    "REPO_MANIFEST_BM25_GENERATION_CONTRACT",
    "REPO_MANIFEST_IMPORT_PLAN_SCHEMA",
    "REPO_MANIFEST_PROFILE_CONTRACT",
    "REPO_MANIFEST_PROFILE_NAME",
    "REPO_MANIFEST_VECTOR_GENERATION_CONTRACT",
    "RepoManifestImportPlan",
    "SourceIntent",
    "VECTOR_PROFILE_AXES",
    "ViewImportIntent",
    "ViewSelection",
    "plan_repo_manifest_import",
    "plan_repo_manifest_import_bytes",
]
