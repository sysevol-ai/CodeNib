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

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .resources import IndexState, IndexStatus

MANIFEST_FILENAME = "repo_manifest.json"
MANIFEST_VERSION = "1.1"


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

    def to_dict(self) -> Dict[str, Any]:
        return {
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

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> IndexEntry:
        return cls(
            index_type=data["index_type"],
            path=data["path"],
            built_at=data["built_at"],
            built_at_epoch=data["built_at_epoch"],
            status=data["status"],
            config=data.get("config", {}),
            metadata=data.get("metadata", {}),
            commit=data.get("commit", ""),
            source_fingerprint=data.get("source_fingerprint", ""),
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
    languages: List[str] = field(default_factory=list)
    file_count: int = 0
    indexes: Dict[str, IndexEntry] = field(default_factory=dict)
    capabilities: Dict[str, bool] = field(default_factory=dict)
    compiled_at: str = ""
    compiled_at_epoch: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "repo": {
                "path": self.repo_path,
                "commit": self.commit,
                "last_indexed_commit": self.last_indexed_commit,
                "source_fingerprint": self.source_fingerprint,
                "last_indexed_source_fingerprint": (
                    self.last_indexed_source_fingerprint
                ),
                "languages": list(self.languages),
                "file_count": self.file_count,
            },
            "indexes": {k: v.to_dict() for k, v in self.indexes.items()},
            "capabilities": dict(self.capabilities),
            "compiled_at": self.compiled_at,
            "compiled_at_epoch": self.compiled_at_epoch,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RepoManifest:
        repo = data.get("repo", {})
        indexes: Dict[str, IndexEntry] = {}
        for k, v in data.get("indexes", {}).items():
            entry = IndexEntry.from_dict(v)
            if not entry.commit and entry.status == "fresh":
                entry.commit = repo.get("commit", "")
            indexes[k] = entry
        return cls(
            version=data.get("version", MANIFEST_VERSION),
            repo_path=repo.get("path", ""),
            commit=repo.get("commit", ""),
            last_indexed_commit=repo.get("last_indexed_commit", repo.get("commit", "")),
            source_fingerprint=repo.get("source_fingerprint", ""),
            last_indexed_source_fingerprint=repo.get(
                "last_indexed_source_fingerprint",
                repo.get("source_fingerprint", ""),
            ),
            languages=repo.get("languages", []),
            file_count=repo.get("file_count", 0),
            indexes=indexes,
            capabilities=data.get("capabilities", {}),
            compiled_at=data.get("compiled_at", ""),
            compiled_at_epoch=data.get("compiled_at_epoch", 0.0),
        )

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
                json.dump(self.to_dict(), handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, p)
        finally:
            temporary_path.unlink(missing_ok=True)

    @classmethod
    def load(cls, path: str | Path) -> RepoManifest:
        """Load a manifest from a JSON file."""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
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
        if self.commit and entry.commit and entry.commit != self.commit:
            return False
        if self.source_fingerprint:
            return entry.source_fingerprint == self.source_fingerprint
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
