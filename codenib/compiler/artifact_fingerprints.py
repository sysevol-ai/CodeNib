# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free fingerprints for persisted compiler artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

_BM25_ARTIFACT_PATHS = (Path("documents.json"), Path("bm25_metadata.json"))
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_READ_BYTES = 1024 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def regular_file_fingerprint(path: str | Path) -> dict[str, Any]:
    """Return the size and SHA-256 of one regular, non-symlink file."""

    path = Path(path)
    before_path = path.lstat()
    if not stat.S_ISREG(before_path.st_mode):
        raise ValueError(f"invalid artifact file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    size = 0
    try:
        before_file = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
                size += len(block)
        after_file = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = path.lstat()

    def identity(value: os.stat_result) -> tuple[int, int, int, int]:
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    if (
        not stat.S_ISREG(before_file.st_mode)
        or not stat.S_ISREG(after_path.st_mode)
        or identity(before_path) != identity(before_file)
        or identity(before_file) != identity(after_file)
        or identity(after_file) != identity(after_path)
        or size != after_file.st_size
    ):
        raise ValueError(f"artifact file changed while hashing: {path}")
    return {"size": size, "sha256": digest.hexdigest()}


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


def scip_decoded_artifact_fingerprints(
    root: str | Path,
    *,
    receipts: Mapping[str, object],
    languages: list[str],
    multi_language_layout: bool = False,
) -> dict[str, dict[str, Any]]:
    """Validate decoded SCIP inputs that graph decoders actually consumed.

    A non-SCIP graph route legitimately has no ``index.decoded``.  Such a
    build reports no receipt and records an empty mapping.  This function never
    discovers artifacts by scanning after a graph build: every persisted
    fingerprint must come from the decoder's before/after consumption bracket.
    """

    artifact_root = Path(root).resolve()
    if not isinstance(receipts, Mapping) or not all(
        type(language) is str for language in receipts
    ):
        raise ValueError("decoded SCIP receipts must be a language mapping")
    if not set(receipts).issubset(set(languages)):
        raise ValueError("decoded SCIP receipt has an unavailable language")
    fingerprints: dict[str, dict[str, Any]] = {}
    for language, raw_receipt in receipts.items():
        if not isinstance(raw_receipt, Mapping) or set(raw_receipt) != {
            "path",
            "size",
            "sha256",
        }:
            raise ValueError(f"malformed decoded SCIP receipt for {language}")
        relative = (
            Path("graphs") / language / "index.decoded"
            if multi_language_layout
            else Path("index.decoded")
        )
        path = artifact_root / relative
        expected_path = path.resolve(strict=True)
        receipt_path = raw_receipt["path"]
        if type(receipt_path) is not str or receipt_path != str(expected_path):
            raise ValueError(f"decoded SCIP receipt path mismatch for {language}")
        size = raw_receipt["size"]
        digest = raw_receipt["sha256"]
        if type(size) is not int or size < 0:
            raise ValueError(f"decoded SCIP receipt size is invalid for {language}")
        if type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
            raise ValueError(f"decoded SCIP receipt digest is invalid for {language}")
        observed = regular_file_fingerprint(path)
        if observed != {"size": size, "sha256": digest}:
            raise ValueError(f"decoded SCIP artifact changed after use for {language}")
        fingerprints[language] = {
            "relative_path": relative.as_posix(),
            "size": size,
            "sha256": digest,
        }
    return fingerprints


def graph_output_artifact_fingerprint(
    root: str | Path,
    *,
    receipt: object,
) -> dict[str, Any]:
    """Validate the graph generation receipt emitted by its responsible writer."""

    artifact_root = Path(root).resolve()
    path = artifact_root / "graph.pkl"
    resolved = path.resolve(strict=True)
    if not isinstance(receipt, Mapping) or set(receipt) != {
        "path",
        "size",
        "sha256",
        "query_surface_sha256",
    }:
        raise ValueError("malformed symbol graph writer receipt")
    if type(receipt["path"]) is not str or receipt["path"] != str(resolved):
        raise ValueError("symbol graph writer receipt path mismatch")
    size = receipt["size"]
    digest = receipt["sha256"]
    if type(size) is not int or size < 0:
        raise ValueError("symbol graph writer receipt size is invalid")
    if type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
        raise ValueError("symbol graph writer receipt digest is invalid")
    query_digest = receipt["query_surface_sha256"]
    if type(query_digest) is not str or _SHA256_RE.fullmatch(query_digest) is None:
        raise ValueError("symbol graph writer query digest is invalid")
    observed = regular_file_fingerprint(path)
    if observed != {"size": size, "sha256": digest}:
        raise ValueError("symbol graph artifact changed after its writer returned")
    return {
        "relative_path": "graph.pkl",
        "size": size,
        "sha256": digest,
        "query_surface_sha256": query_digest,
    }


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
    "graph_output_artifact_fingerprint",
    "regular_file_fingerprint",
    "require_bm25_manifest_artifact",
    "scip_decoded_artifact_fingerprints",
]
