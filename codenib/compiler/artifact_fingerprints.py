# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free fingerprints for persisted compiler artifacts."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any, Mapping

_BM25_ARTIFACT_PATHS = (Path("documents.json"), Path("bm25_metadata.json"))
_READ_BYTES = 1024 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
        getattr(metadata, "st_file_attributes", 0),
    )


def _file_fingerprint(path: Path) -> dict[str, Any]:
    descriptor = -1
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or bool(
                getattr(before, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
            )
        ):
            raise ValueError(f"invalid BM25 artifact file: {path}")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(
            before
        ):
            raise ValueError(f"BM25 artifact file changed while opening: {path}")
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            block = os.read(descriptor, min(remaining, _READ_BYTES))
            if not block:
                raise ValueError(f"BM25 artifact file was truncated: {path}")
            digest.update(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise ValueError(f"BM25 artifact file grew while hashing: {path}")
        after_open = os.fstat(descriptor)
        after_path = path.lstat()
        if _file_identity(after_open) != _file_identity(opened) or _file_identity(
            after_path
        ) != _file_identity(opened):
            raise ValueError(f"BM25 artifact file changed while hashing: {path}")
        return {"size": opened.st_size, "sha256": digest.hexdigest()}
    except OSError as exc:
        raise ValueError(f"invalid BM25 artifact file: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def bm25_artifact_file_fingerprints(
    root: str | Path,
) -> dict[str, dict[str, Any]]:
    """Fingerprint the complete persisted BM25 artifact."""

    artifact_root = Path(root)
    fingerprints: dict[str, dict[str, Any]] = {}
    for relative in _BM25_ARTIFACT_PATHS:
        path = artifact_root / relative
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
