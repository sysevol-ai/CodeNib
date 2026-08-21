# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Repo manifest — the linking protocol between index compilation (Phase 1)
and query compilation (Phase 2).

Phase 1 (``IndexCompiler``) builds all index artifacts for a repository
and writes a ``RepoManifest`` recording what was built, where, and when.

Phase 2 (``AgentRunner`` + ``ResourceGuard``) reads the manifest via
``ManifestIndexStateStore`` to resolve index freshness at query time.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .._bounded_json import validate_bounded_json_stream
from ..repository_source_selection import (
    DEFAULT_REPOSITORY_SOURCE_SELECTION,
    RepositorySourceSelection,
)
from ..source_fingerprint import is_secure_source_fingerprint_v2
from .resources import IndexState, IndexStatus

MANIFEST_FILENAME = "repo_manifest.json"
LEGACY_MANIFEST_VERSION = "1.1"
MANIFEST_VERSION = "1.2"
SUPPORTED_MANIFEST_VERSIONS = frozenset({LEGACY_MANIFEST_VERSION, MANIFEST_VERSION})

_TOP_LEVEL_FIELDS = frozenset(
    {
        "version",
        "repo",
        "indexes",
        "capabilities",
        "compiled_at",
        "compiled_at_epoch",
    }
)
_REPO_V11_FIELDS = frozenset(
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
_REPO_V12_FIELDS = _REPO_V11_FIELDS | frozenset(
    {
        "source_selection",
        "last_indexed_source_selection_digest",
    }
)
_INDEX_V11_FIELDS = frozenset(
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
_INDEX_V12_FIELDS = _INDEX_V11_FIELDS | frozenset({"source_selection_digest"})
_SELECTION_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024


def _require_exact_object(
    value: object,
    expected_fields: frozenset[str],
    *,
    label: str,
) -> Dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be a JSON object")
    observed = set(value)
    if observed != expected_fields:
        missing = sorted(expected_fields - observed)
        unknown = sorted(observed - expected_fields)
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown fields: {', '.join(unknown)}")
        raise ValueError(f"{label} has invalid fields ({'; '.join(details)})")
    return value


def _require_string(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a string")
    return value


def _require_epoch(value: object, *, label: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _require_string_list(value: object, *, label: str) -> List[str]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise ValueError(f"{label} must be an array of strings")
    return list(value)


def _require_json_object(value: object, *, label: str) -> Dict[str, Any]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError(f"{label} must be a JSON object with string keys")
    return dict(value)


def _require_selection_digest(
    value: object,
    *,
    label: str,
    allow_empty: bool,
) -> str:
    digest = _require_string(value, label=label)
    if (allow_empty and not digest) or _SELECTION_DIGEST_RE.fullmatch(digest):
        return digest
    raise ValueError(f"{label} must be a canonical source-selection digest")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"manifest JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"manifest JSON contains non-finite number {value}")


def _validate_json_tree(value: object, *, label: str) -> None:
    """Reject non-JSON and non-finite nested values before they cross trust tiers."""

    stack: list[object] = [value]
    while stack:
        current = stack.pop()
        if type(current) is dict:
            for key, child in current.items():
                if type(key) is not str:
                    raise ValueError(f"{label} object keys must be strings")
                try:
                    key.encode("utf-8", errors="strict")
                except UnicodeError as exc:
                    raise ValueError(f"{label} contains invalid Unicode") from exc
                stack.append(child)
        elif type(current) is list:
            stack.extend(current)
        elif type(current) is float:
            if not math.isfinite(current):
                raise ValueError(f"{label} numbers must be finite")
        elif type(current) is str:
            try:
                current.encode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise ValueError(f"{label} contains invalid Unicode") from exc
        elif current is None or type(current) in {bool, int}:
            continue
        else:
            raise ValueError(f"{label} contains a non-JSON value")


@dataclass(slots=True)
class IndexEntry:
    """Metadata for a single built index."""

    index_type: str  # "bm25", "vector", "symbol_graph"
    path: str  # path to the index directory
    built_at: str  # ISO 8601 timestamp
    built_at_epoch: float  # epoch seconds (for age calculations)
    status: str  # "fresh" | "stale" | "failed"
    config: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    commit: str = ""
    source_fingerprint: str = ""
    source_selection_digest: str = ""

    def to_dict(self, *, manifest_version: str = MANIFEST_VERSION) -> Dict[str, Any]:
        if manifest_version not in SUPPORTED_MANIFEST_VERSIONS:
            raise ValueError(f"unsupported manifest version: {manifest_version!r}")
        payload = {
            "index_type": self.index_type,
            "path": self.path,
            "built_at": self.built_at,
            "built_at_epoch": self.built_at_epoch,
            "status": self.status,
            "config": dict(self.config),
            "metadata": dict(self.metadata),
            "commit": self.commit,
            "source_fingerprint": self.source_fingerprint,
        }
        if manifest_version == MANIFEST_VERSION:
            payload["source_selection_digest"] = self.source_selection_digest
        elif self.source_selection_digest:
            raise ValueError(
                "manifest v1.1 index entries cannot carry source-selection identity"
            )
        return payload

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        manifest_version: str = MANIFEST_VERSION,
    ) -> IndexEntry:
        if manifest_version not in SUPPORTED_MANIFEST_VERSIONS:
            raise ValueError(f"unsupported manifest version: {manifest_version!r}")
        expected = (
            _INDEX_V12_FIELDS
            if manifest_version == MANIFEST_VERSION
            else _INDEX_V11_FIELDS
        )
        payload = _require_exact_object(
            data,
            expected,
            label=f"manifest {manifest_version} index entry",
        )
        status = _require_string(payload["status"], label="index status")
        if status not in {"fresh", "stale", "failed"}:
            raise ValueError("index status is unsupported")
        selection_digest = ""
        if manifest_version == MANIFEST_VERSION:
            selection_digest = _require_selection_digest(
                payload["source_selection_digest"],
                label="index source_selection_digest",
                allow_empty=True,
            )
        return cls(
            index_type=_require_string(payload["index_type"], label="index index_type"),
            path=_require_string(payload["path"], label="index path"),
            built_at=_require_string(payload["built_at"], label="index built_at"),
            built_at_epoch=_require_epoch(
                payload["built_at_epoch"], label="index built_at_epoch"
            ),
            status=status,
            config=_require_json_object(payload["config"], label="index config"),
            metadata=_require_json_object(payload["metadata"], label="index metadata"),
            commit=_require_string(payload["commit"], label="index commit"),
            source_fingerprint=_require_string(
                payload["source_fingerprint"], label="index source_fingerprint"
            ),
            source_selection_digest=selection_digest,
        )


@dataclass(slots=True)
class RepoManifest:
    """
    Top-level manifest describing a compiled repository.

    Written by ``IndexCompiler`` after Phase 1.  Read by
    ``ManifestIndexStateStore`` at query time (Phase 2).
    """

    version: str = MANIFEST_VERSION
    repo_path: str = ""
    commit: str = ""
    last_indexed_commit: str = ""
    source_fingerprint: str = ""
    last_indexed_source_fingerprint: str = ""
    source_selection: RepositorySourceSelection | None = field(
        default_factory=lambda: DEFAULT_REPOSITORY_SOURCE_SELECTION
    )
    last_indexed_source_selection_digest: str = ""
    languages: List[str] = field(default_factory=list)
    file_count: int = 0
    indexes: Dict[str, IndexEntry] = field(default_factory=dict)
    capabilities: Dict[str, bool] = field(default_factory=dict)
    compiled_at: str = ""
    compiled_at_epoch: float = 0.0

    def __post_init__(self) -> None:
        if type(self.source_selection) is RepositorySourceSelection:
            self.source_selection = RepositorySourceSelection(
                self.source_selection.exclude_subtrees
            )
        # Keep the source-unbound programmatic constructor convenient for
        # existing empty-selection consumers. Strict persisted v1.2 input is
        # validated before construction, and non-empty policies are never
        # inferred on behalf of a builder.
        if (
            self.version == MANIFEST_VERSION
            and self.source_selection == DEFAULT_REPOSITORY_SOURCE_SELECTION
        ):
            digest = self.source_selection.digest
            if (
                self.last_indexed_source_fingerprint
                and not self.last_indexed_source_selection_digest
            ):
                self.last_indexed_source_selection_digest = digest
            for entry in self.indexes.values():
                if entry.status == "fresh":
                    if not entry.commit:
                        entry.commit = self.commit
                    if not entry.source_selection_digest:
                        entry.source_selection_digest = digest

    @property
    def source_selection_digest(self) -> str | None:
        """Canonical current selection identity, or the v1.1 legacy sentinel."""

        if self.source_selection is None:
            return None
        return self.source_selection.digest

    def _validate_v12_source_closure(self) -> None:
        """Require every current v1.2 view to bind the exact source identity."""

        if self.version != MANIFEST_VERSION:
            return
        if self.source_fingerprint and not is_secure_source_fingerprint_v2(
            self.source_fingerprint
        ):
            raise ValueError(
                "manifest v1.2 source_fingerprint must be a secure v2 identity"
            )
        if self.last_indexed_source_fingerprint and not (
            is_secure_source_fingerprint_v2(self.last_indexed_source_fingerprint)
        ):
            raise ValueError(
                "manifest v1.2 last_indexed_source_fingerprint must be a "
                "secure v2 identity"
            )
        selection_digest = self.source_selection_digest
        if (
            self.source_fingerprint
            and self.last_indexed_source_fingerprint == self.source_fingerprint
            and self.last_indexed_source_selection_digest != selection_digest
        ):
            raise ValueError(
                "the current and last-indexed source identities must use the "
                "same repository source selection"
            )
        for entry in self.indexes.values():
            if entry.status != "fresh":
                continue
            if not is_secure_source_fingerprint_v2(self.source_fingerprint):
                raise ValueError(
                    f"fresh index {entry.index_type!r} requires a secure v2 "
                    "repository source fingerprint"
                )
            if entry.commit != self.commit:
                raise ValueError(
                    f"fresh index {entry.index_type!r} commit does not match "
                    "the manifest repository commit"
                )
            if entry.source_fingerprint != self.source_fingerprint:
                raise ValueError(
                    f"fresh index {entry.index_type!r} source fingerprint does "
                    "not match the manifest repository source fingerprint"
                )
            if entry.source_selection_digest != selection_digest:
                raise ValueError(
                    f"fresh index {entry.index_type!r} does not match the "
                    "manifest source selection"
                )

    def to_dict(self) -> Dict[str, Any]:
        if self.version not in SUPPORTED_MANIFEST_VERSIONS:
            raise ValueError(f"unsupported manifest version: {self.version!r}")
        repo = {
            "path": self.repo_path,
            "commit": self.commit,
            "last_indexed_commit": self.last_indexed_commit,
            "source_fingerprint": self.source_fingerprint,
            "last_indexed_source_fingerprint": (self.last_indexed_source_fingerprint),
            "languages": list(self.languages),
            "file_count": self.file_count,
        }
        if self.version == MANIFEST_VERSION:
            if type(self.source_selection) is not RepositorySourceSelection:
                raise ValueError(
                    "manifest v1.2 requires a canonical repository source selection"
                )
            digest = self.source_selection.digest
            _require_selection_digest(
                self.last_indexed_source_selection_digest,
                label="last_indexed_source_selection_digest",
                allow_empty=True,
            )
            if bool(self.last_indexed_source_fingerprint) != bool(
                self.last_indexed_source_selection_digest
            ):
                raise ValueError(
                    "last indexed source fingerprint and selection digest "
                    "must be present together"
                )
            for entry in self.indexes.values():
                if (
                    entry.status == "fresh"
                    and self.source_selection == DEFAULT_REPOSITORY_SOURCE_SELECTION
                ):
                    if not entry.commit:
                        entry.commit = self.commit
                    if not entry.source_selection_digest:
                        entry.source_selection_digest = digest
                _require_selection_digest(
                    entry.source_selection_digest,
                    label=f"index {entry.index_type!r} source_selection_digest",
                    allow_empty=entry.status != "fresh",
                )
                if entry.status == "fresh" and entry.source_selection_digest != digest:
                    raise ValueError(
                        f"fresh index {entry.index_type!r} does not match the "
                        "manifest source selection"
                    )
            self._validate_v12_source_closure()
            repo["source_selection"] = self.source_selection.to_dict()
            repo["last_indexed_source_selection_digest"] = (
                self.last_indexed_source_selection_digest
            )
        else:
            if self.source_selection is not None:
                raise ValueError(
                    "manifest v1.1 must retain the legacy source-selection sentinel"
                )
            if self.last_indexed_source_selection_digest or any(
                entry.source_selection_digest for entry in self.indexes.values()
            ):
                raise ValueError("manifest v1.1 cannot carry source-selection identity")

        return {
            "version": self.version,
            "repo": repo,
            "indexes": {
                k: v.to_dict(manifest_version=self.version)
                for k, v in self.indexes.items()
            },
            "capabilities": dict(self.capabilities),
            "compiled_at": self.compiled_at,
            "compiled_at_epoch": self.compiled_at_epoch,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RepoManifest:
        _validate_json_tree(data, label="repository manifest")
        payload = _require_exact_object(
            data,
            _TOP_LEVEL_FIELDS,
            label="repository manifest",
        )
        version = _require_string(payload["version"], label="manifest version")
        if version not in SUPPORTED_MANIFEST_VERSIONS:
            raise ValueError(f"unsupported manifest version: {version!r}")
        repo = _require_exact_object(
            payload["repo"],
            _REPO_V12_FIELDS if version == MANIFEST_VERSION else _REPO_V11_FIELDS,
            label=f"manifest {version} repo",
        )
        raw_indexes = _require_json_object(payload["indexes"], label="manifest indexes")
        indexes: Dict[str, IndexEntry] = {}
        for k, v in raw_indexes.items():
            entry = IndexEntry.from_dict(v, manifest_version=version)
            if entry.index_type != k:
                raise ValueError(
                    f"manifest index key {k!r} does not match entry index_type"
                )
            indexes[k] = entry
        source_selection: RepositorySourceSelection | None = None
        last_selection_digest = ""
        repo_commit = _require_string(repo["commit"], label="manifest repo commit")
        repo_source = _require_string(
            repo["source_fingerprint"], label="source_fingerprint"
        )
        if version == MANIFEST_VERSION:
            try:
                source_selection = RepositorySourceSelection.from_dict(
                    repo["source_selection"]
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("manifest v1.2 source_selection is invalid") from exc
            last_selection_digest = _require_selection_digest(
                repo["last_indexed_source_selection_digest"],
                label="last_indexed_source_selection_digest",
                allow_empty=True,
            )
            last_source = _require_string(
                repo["last_indexed_source_fingerprint"],
                label="last_indexed_source_fingerprint",
            )
            if bool(last_source) != bool(last_selection_digest):
                raise ValueError(
                    "last indexed source fingerprint and selection digest "
                    "must be present together"
                )
            if repo_source and not is_secure_source_fingerprint_v2(repo_source):
                raise ValueError(
                    "manifest v1.2 source_fingerprint must be a secure v2 identity"
                )
            if last_source and not is_secure_source_fingerprint_v2(last_source):
                raise ValueError(
                    "manifest v1.2 last_indexed_source_fingerprint must be a "
                    "secure v2 identity"
                )
            current_digest = source_selection.digest
            for entry in indexes.values():
                if entry.status == "fresh" and (
                    not entry.source_selection_digest
                    or entry.source_selection_digest != current_digest
                ):
                    raise ValueError(
                        f"fresh index {entry.index_type!r} does not match the "
                        "manifest source selection"
                    )
                if entry.status == "fresh":
                    if not is_secure_source_fingerprint_v2(repo_source):
                        raise ValueError(
                            f"fresh index {entry.index_type!r} requires a secure "
                            "v2 repository source fingerprint"
                        )
                    if entry.commit != repo_commit:
                        raise ValueError(
                            f"fresh index {entry.index_type!r} commit does not "
                            "match the manifest repository commit"
                        )
                    if entry.source_fingerprint != repo_source:
                        raise ValueError(
                            f"fresh index {entry.index_type!r} source fingerprint "
                            "does not match the manifest repository source "
                            "fingerprint"
                        )
        else:
            last_source = _require_string(
                repo["last_indexed_source_fingerprint"],
                label="last_indexed_source_fingerprint",
            )

        file_count = repo["file_count"]
        if type(file_count) is not int or file_count < 0:
            raise ValueError("manifest repo file_count must be a non-negative integer")
        capabilities = _require_json_object(
            payload["capabilities"], label="manifest capabilities"
        )
        if any(type(value) is not bool for value in capabilities.values()):
            raise ValueError("manifest capabilities values must be booleans")

        manifest = cls(
            version=version,
            repo_path=_require_string(repo["path"], label="manifest repo path"),
            commit=repo_commit,
            last_indexed_commit=_require_string(
                repo["last_indexed_commit"], label="last_indexed_commit"
            ),
            source_fingerprint=repo_source,
            last_indexed_source_fingerprint=last_source,
            source_selection=source_selection,
            last_indexed_source_selection_digest=last_selection_digest,
            languages=_require_string_list(
                repo["languages"], label="manifest repo languages"
            ),
            file_count=file_count,
            indexes=indexes,
            capabilities=capabilities,
            compiled_at=_require_string(
                payload["compiled_at"], label="manifest compiled_at"
            ),
            compiled_at_epoch=_require_epoch(
                payload["compiled_at_epoch"], label="manifest compiled_at_epoch"
            ),
        )
        manifest._validate_v12_source_closure()
        return manifest

    def save(self, path: str | Path) -> None:
        """Atomically replace the manifest after flushing the JSON payload."""

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=str(p.parent),
            prefix=f".{p.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary)
        try:
            try:
                mode = p.stat().st_mode & 0o777
            except FileNotFoundError:
                mode = None
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fchmod = getattr(os, "fchmod", None)
                if fchmod is not None and mode is not None:
                    fchmod(handle.fileno(), mode)
                json.dump(self.to_dict(), handle, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, p)
        finally:
            temporary_path.unlink(missing_ok=True)

    @classmethod
    def load(cls, path: str | Path) -> RepoManifest:
        """Load a manifest from a JSON file."""
        with open(path, "rb") as source:
            payload = source.read(_MAX_MANIFEST_BYTES + 1)
        if len(payload) > _MAX_MANIFEST_BYTES:
            raise ValueError(f"repository manifest exceeds {_MAX_MANIFEST_BYTES} bytes")
        if payload.startswith(
            (
                b"\xef\xbb\xbf",
                b"\xff\xfe",
                b"\xfe\xff",
                b"\x00\x00\xfe\xff",
                b"\xff\xfe\x00\x00",
            )
        ):
            raise ValueError("repository manifest must use BOM-free UTF-8")
        validate_bounded_json_stream(
            io.BytesIO(payload),
            label="repository manifest",
            max_bytes=_MAX_MANIFEST_BYTES,
        )
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("repository manifest must use UTF-8") from exc
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
        return cls.from_dict(data)

    def derive_capabilities(self) -> None:
        """Compute capabilities from the set of available indexes."""
        has_bm25 = self.index_is_current("bm25")
        has_vector = self.index_is_current("vector")
        has_graph = self.index_is_current("symbol_graph")

        self.capabilities = {
            "sparse_search": has_bm25,
            "dense_search": has_vector,
            "hybrid_search": has_bm25 and has_vector,
            "symbol_navigation": has_graph,
        }

    def index_is_current(self, index_type: str) -> bool:
        """Whether a view was built for the manifest's current source state."""

        entry = self.indexes.get(index_type)
        if entry is None or entry.status != "fresh":
            return False
        if self.version == MANIFEST_VERSION:
            if not is_secure_source_fingerprint_v2(self.source_fingerprint):
                return False
            if entry.commit != self.commit and not (
                not entry.commit
                and not self.source_fingerprint
                and self.source_selection == DEFAULT_REPOSITORY_SOURCE_SELECTION
            ):
                return False
            if entry.source_fingerprint != self.source_fingerprint:
                return False
        elif self.commit and entry.commit and entry.commit != self.commit:
            return False
        if (
            self.version != MANIFEST_VERSION
            and self.source_fingerprint
            and (entry.source_fingerprint != self.source_fingerprint)
        ):
            return False
        if self.version == MANIFEST_VERSION:
            selection_digest = self.source_selection_digest
            if selection_digest is None or (
                entry.source_selection_digest != selection_digest
                and not (
                    not entry.source_selection_digest
                    and not self.source_fingerprint
                    and self.source_selection == DEFAULT_REPOSITORY_SOURCE_SELECTION
                )
            ):
                return False
        return True


# ---------------------------------------------------------------------------
# ManifestIndexStateStore — bridges manifest → ResourceResolver
# ---------------------------------------------------------------------------


class ManifestIndexStateStore:
    """
    Read-only ``IndexStateStore`` backed by a ``RepoManifest``.

    Maps manifest ``IndexEntry`` objects to ``IndexStatus`` objects that
    ``ResourceResolver`` can consume.  Does not support ``set_status`` —
    the manifest is written only by ``IndexCompiler``.
    """

    def __init__(self, manifest: RepoManifest) -> None:
        self._manifest = manifest

    def get_status(self, index_type: str, scope: str) -> Optional[IndexStatus]:
        entry = self._manifest.indexes.get(index_type)
        if entry is None:
            return None

        if not self._manifest.index_is_current(index_type):
            return None  # treat failed builds as missing

        age = time.time() - entry.built_at_epoch
        return IndexStatus(
            index_type=entry.index_type,
            state=IndexState.FRESH,  # ResourceResolver._decide() re-evaluates
            last_built=entry.built_at_epoch,
            age_seconds=age,
            scope=scope,
            path=entry.path,
            metadata=entry.metadata,
        )

    def set_status(self, status: IndexStatus) -> None:
        raise NotImplementedError(
            "ManifestIndexStateStore is read-only. "
            "Use IndexCompiler to rebuild indexes and update the manifest."
        )
