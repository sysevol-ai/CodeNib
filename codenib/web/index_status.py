# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pure status projection for the three Web indexing surfaces."""

from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Callable, Mapping

from ..compiler.checkout_identity import checkout_commit
from .schemas import IndexSurfaceStatus, IndexUpdateMetrics, RepoIndexStatus

PRIMARY_INDEX_TYPES = ("bm25", "vector", "symbol_graph")


def _bounded_text(value: object, *, max_length: int = 128) -> str | None:
    if type(value) is not str:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or "\x00" in normalized:
        return None
    return normalized


def _nonnegative_integer(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _rate(value: object) -> float | None:
    if type(value) not in {int, float}:
        return None
    normalized = float(value)
    return (
        normalized if math.isfinite(normalized) and 0.0 <= normalized <= 1.0 else None
    )


def _same_commit(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    normalized_left = left.lower()
    normalized_right = right.lower()
    hex_characters = frozenset("0123456789abcdef")
    if (
        min(len(normalized_left), len(normalized_right)) < 7
        or set(normalized_left) - hex_characters
        or set(normalized_right) - hex_characters
    ):
        return normalized_left == normalized_right
    return normalized_left.startswith(normalized_right) or normalized_right.startswith(
        normalized_left
    )


def _metrics(entry: object) -> IndexUpdateMetrics | None:
    metadata = getattr(entry, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    values = {
        "changed_files": _nonnegative_integer(metadata.get("changed_files")),
        "chunks_reembedded": _nonnegative_integer(metadata.get("chunks_reembedded")),
        "chunks_from_cache": _nonnegative_integer(metadata.get("chunks_from_cache")),
        "cache_hit_rate": _rate(metadata.get("cache_hit_rate")),
        "new_commit": _bounded_text(metadata.get("new_commit")),
    }
    if all(value is None for value in values.values()):
        return None
    return IndexUpdateMetrics(**values)


def _current_head(
    repo_dir: object,
    resolver: Callable[[Path], str | None],
) -> str | None:
    if type(repo_dir) is not str or not repo_dir:
        return None
    try:
        return _bounded_text(resolver(Path(repo_dir)))
    except (OSError, subprocess.SubprocessError):
        return None


def _surface_status(
    manifest: object,
    index_type: str,
    *,
    current_head: str | None,
) -> IndexSurfaceStatus:
    indexes = getattr(manifest, "indexes", None)
    entry = indexes.get(index_type) if isinstance(indexes, Mapping) else None
    if entry is None:
        state = "missing"
        indexed_commit = None
        built_at = None
        metrics = None
    else:
        indexed_commit = _bounded_text(getattr(entry, "commit", None)) or _bounded_text(
            getattr(manifest, "last_indexed_commit", None)
        )
        built_at = _bounded_text(getattr(entry, "built_at", None))
        metrics = _metrics(entry)
        entry_status = getattr(entry, "status", None)
        if entry_status == "failed":
            state = "failed"
        else:
            is_current = False
            checker = getattr(manifest, "index_is_current", None)
            if callable(checker):
                is_current = bool(checker(index_type))
            commit_changed = bool(
                current_head
                and indexed_commit
                and not _same_commit(current_head, indexed_commit)
            )
            state = "built" if is_current and not commit_changed else "stale"
    return IndexSurfaceStatus(
        index_type=index_type,
        state=state,
        stale=state == "stale",
        indexed_commit=indexed_commit,
        built_at=built_at,
        metrics=metrics,
    )


def build_repo_index_status(
    bundle: object,
    *,
    current_head_resolver: Callable[[Path], str | None] = checkout_commit,
) -> RepoIndexStatus:
    """Project one pinned bundle into a detached reader-facing status value."""

    entry = getattr(bundle, "entry", None)
    manifest = getattr(bundle, "manifest", None)
    repo_id = _bounded_text(getattr(entry, "instance_id", None), max_length=512)
    if repo_id is None:
        raise ValueError("index status requires a repository instance id")
    current_head = _current_head(
        getattr(entry, "repo_dir", None),
        current_head_resolver,
    )
    indexes = [
        _surface_status(
            manifest,
            index_type,
            current_head=current_head,
        )
        for index_type in PRIMARY_INDEX_TYPES
    ]
    last_indexed_commit = _bounded_text(
        getattr(manifest, "last_indexed_commit", None)
    ) or _bounded_text(getattr(manifest, "commit", None))
    repo_stale = any(index.state == "stale" for index in indexes) or bool(
        current_head
        and last_indexed_commit
        and not _same_commit(current_head, last_indexed_commit)
    )
    return RepoIndexStatus(
        repo_id=repo_id,
        last_indexed_commit=last_indexed_commit,
        current_head=current_head,
        stale=repo_stale,
        indexes=indexes,
    )


__all__ = [
    "PRIMARY_INDEX_TYPES",
    "build_repo_index_status",
]
