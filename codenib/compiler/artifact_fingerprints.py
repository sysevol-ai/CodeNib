# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free fingerprints for persisted compiler artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

_BM25_ARTIFACT_PATHS = (Path("documents.json"), Path("bm25_metadata.json"))


def _file_fingerprint(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    return {"size": size, "sha256": digest.hexdigest()}


def bm25_artifact_file_fingerprints(
    root: str | Path,
) -> dict[str, dict[str, Any]]:
    """Fingerprint the complete persisted BM25 artifact."""

    artifact_root = Path(root)
    fingerprints: dict[str, dict[str, Any]] = {}
    for relative in _BM25_ARTIFACT_PATHS:
        path = artifact_root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"invalid BM25 artifact file: {path}")
        fingerprints[relative.as_posix()] = _file_fingerprint(path)
    return fingerprints


def bm25_artifact_files_match(
    root: str | Path,
    *,
    expected_fingerprints: object,
) -> bool:
    """Return whether every persisted BM25 file matches its build record."""

    if not isinstance(expected_fingerprints, Mapping):
        return False
    if set(expected_fingerprints) != {path.as_posix() for path in _BM25_ARTIFACT_PATHS}:
        return False
    try:
        observed = bm25_artifact_file_fingerprints(root)
    except (OSError, ValueError):
        return False
    return observed == dict(expected_fingerprints)


def require_bm25_manifest_artifact(entry: object) -> bool:
    """Validate a manifest-backed BM25 artifact before loading it.

    Returns ``False`` for legacy entries that predate persisted fingerprints.
    Once an entry records fingerprints, malformed or mismatched records are a
    hard failure rather than permission to load potentially mixed generations.
    """

    missing = object()
    expected: object = missing
    for field_name in ("config", "metadata"):
        field = getattr(entry, field_name, None)
        if isinstance(field, Mapping) and "artifact_file_fingerprints" in field:
            expected = field["artifact_file_fingerprints"]
            break
    if expected is missing:
        return False

    root = getattr(entry, "path", None)
    if not isinstance(root, (str, Path)) or not bm25_artifact_files_match(
        root,
        expected_fingerprints=expected,
    ):
        raise ValueError(
            "BM25 artifact does not match its manifest fingerprints; "
            "rebuild or restore the view before loading it"
        )
    return True


__all__ = [
    "bm25_artifact_file_fingerprints",
    "bm25_artifact_files_match",
    "require_bm25_manifest_artifact",
]
