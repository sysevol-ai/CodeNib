# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free integrity records for persisted vector levels."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping

VECTOR_PERSISTENCE_SCHEMA = 1
VECTOR_VIEW_UPDATE_MARKER = ".vector-view.update-in-progress"
_DIGEST_BYTES = 1024 * 1024
_MAX_CONFIG_BYTES = 16 * 1024 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_UPDATE_MARKER_PAYLOAD = b'{"schema":"codenib.vector-view-update.v1"}\n'


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


def _open_stable_regular(path: Path) -> tuple[int, os.stat_result]:
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
            raise ValueError(f"invalid vector artifact file: {path}")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(
            before
        ):
            raise ValueError(f"vector artifact changed while opening: {path}")
        return descriptor, opened
    except ValueError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise ValueError(f"invalid vector artifact file: {path}") from exc


def _read_exact(descriptor: int, size: int, path: Path) -> bytearray:
    payload = bytearray()
    remaining = size
    while remaining:
        block = os.read(descriptor, min(remaining, _DIGEST_BYTES))
        if not block:
            raise ValueError(f"vector artifact was truncated: {path}")
        payload.extend(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise ValueError(f"vector artifact grew while reading: {path}")
    return payload


def _verify_open_file(
    descriptor: int,
    opened: os.stat_result,
    path: Path,
) -> None:
    try:
        after_open = os.fstat(descriptor)
        after_path = path.lstat()
    except OSError as exc:
        raise ValueError(f"vector artifact changed while reading: {path}") from exc
    if _file_identity(after_open) != _file_identity(opened) or _file_identity(
        after_path
    ) != _file_identity(opened):
        raise ValueError(f"vector artifact changed while reading: {path}")


def _file_record(path: Path) -> dict[str, Any]:
    descriptor, opened = _open_stable_regular(path)
    try:
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            block = os.read(descriptor, min(remaining, _DIGEST_BYTES))
            if not block:
                raise ValueError(f"vector artifact was truncated: {path}")
            digest.update(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise ValueError(f"vector artifact grew while hashing: {path}")
        _verify_open_file(descriptor, opened, path)
        return {
            "file": path.name,
            "size": opened.st_size,
            "sha256": digest.hexdigest(),
        }
    except OSError as exc:
        raise ValueError(f"vector artifact could not be read: {path}") from exc
    finally:
        os.close(descriptor)


def _load_config(path: Path) -> dict[str, Any]:
    descriptor, opened = _open_stable_regular(path)
    try:
        if opened.st_size > _MAX_CONFIG_BYTES:
            raise ValueError(f"vector generation config exceeds its byte limit: {path}")
        payload = _read_exact(descriptor, opened.st_size, path)
        _verify_open_file(descriptor, opened, path)
        try:
            config = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"vector generation config is invalid JSON: {path}"
            ) from exc
    except OSError as exc:
        raise ValueError(f"vector generation config could not be read: {path}") from exc
    finally:
        os.close(descriptor)
    if not isinstance(config, dict):
        raise ValueError(f"vector generation config must be an object: {path}")
    return config


def vector_level_artifact_records(
    level_path: Path,
    model_suffix: str,
    *,
    documents_file: str,
) -> dict[str, dict[str, Any]]:
    """Fingerprint the index/document pair committed for one vector level."""

    allowed_documents = {
        f"documents_{model_suffix}.pkl",
        f"documents_{model_suffix}.json",
    }
    if documents_file not in allowed_documents:
        raise ValueError(
            f"unsupported vector document artifact for {level_path}: {documents_file}"
        )
    return {
        "index": _file_record(level_path / f"index_{model_suffix}.faiss"),
        "documents": _file_record(level_path / documents_file),
    }


def vector_config_artifact_record(
    root: str | Path,
    model_suffix: str,
) -> dict[str, Any]:
    """Fingerprint the top-level record that commits all vector levels."""

    return _file_record(Path(root) / f"config_{model_suffix}.json")


def require_complete_vector_view(root: str | Path) -> None:
    """Reject a vector view whose multi-artifact update did not finish."""

    marker = Path(root) / VECTOR_VIEW_UPDATE_MARKER
    if marker.exists() or marker.is_symlink():
        raise ValueError(f"vector view has an incomplete update marker: {marker}")


def begin_vector_view_update(root: str | Path) -> Path:
    """Publish a crash marker before replacing any vector-view component."""

    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / VECTOR_VIEW_UPDATE_MARKER
    if marker.is_symlink():
        raise ValueError(f"refusing symlinked vector update marker: {marker}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(marker, flags, 0o600)
    try:
        remaining = memoryview(_UPDATE_MARKER_PAYLOAD)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("could not persist vector view update marker")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return marker


def finish_vector_view_update(root: str | Path) -> None:
    """Remove the marker only after every vector-view component is written."""

    marker = Path(root) / VECTOR_VIEW_UPDATE_MARKER
    if not marker.exists() and not marker.is_symlink():
        raise ValueError(f"vector view update marker disappeared: {marker}")
    marker.unlink()


def validate_vector_config_artifact(
    root: str | Path,
    model_suffix: str,
    expected: object,
) -> Path:
    """Validate the vector generation selected by a manifest entry."""

    expected_name = f"config_{model_suffix}.json"
    if not isinstance(expected, Mapping) or set(expected) != {
        "file",
        "size",
        "sha256",
    }:
        raise ValueError("invalid vector config fingerprint in manifest")
    if expected.get("file") != expected_name:
        raise ValueError("invalid vector config filename in manifest")
    path = Path(root) / expected_name
    if _file_record(path) != dict(expected):
        raise ValueError("vector config does not match its manifest fingerprint")
    return path


def validate_vector_level_artifacts(
    level_path: Path,
    model_suffix: str,
    expected: object,
) -> tuple[Path, Path]:
    """Validate one committed pair and return its index/document paths."""

    if not isinstance(expected, Mapping) or set(expected) != {"index", "documents"}:
        raise ValueError(f"invalid committed vector artifacts for {level_path}")

    allowed_names = {
        "index": {f"index_{model_suffix}.faiss"},
        "documents": {
            f"documents_{model_suffix}.pkl",
            f"documents_{model_suffix}.json",
        },
    }
    paths: dict[str, Path] = {}
    for kind in ("index", "documents"):
        record = expected[kind]
        if not isinstance(record, Mapping):
            raise ValueError(f"invalid {kind} vector artifact record for {level_path}")
        filename = record.get("file")
        size = record.get("size")
        digest = record.get("sha256")
        if filename not in allowed_names[kind]:
            raise ValueError(
                f"invalid {kind} vector artifact filename for {level_path}: "
                f"{filename!r}"
            )
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError(f"invalid {kind} vector artifact size for {level_path}")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"invalid {kind} vector artifact digest for {level_path}")

        path = level_path / filename
        observed = _file_record(path)
        if observed != {"file": filename, "size": size, "sha256": digest}:
            raise ValueError(f"committed {kind} vector artifact does not match {path}")
        paths[kind] = path

    return paths["index"], paths["documents"]


def validate_vector_generation_artifacts(
    root: str | Path,
    model_suffix: str,
) -> dict[str, Any]:
    """Validate every file committed by one modern top-level vector config."""

    config_path = Path(root) / f"config_{model_suffix}.json"
    config = _load_config(config_path)

    persistence_schema = config.get("persistence_schema")
    committed_levels = config.get("level_artifacts")
    if persistence_schema is None and committed_levels is None:
        return config
    if persistence_schema != VECTOR_PERSISTENCE_SCHEMA:
        raise ValueError(
            "vector generation has unsupported persistence schema: "
            f"{persistence_schema!r}"
        )
    if not isinstance(committed_levels, Mapping) or not set(committed_levels) <= {
        "l0",
        "l2",
    }:
        raise ValueError("vector generation has invalid committed levels")

    for level in ("l0", "l2"):
        count = config.get(f"{level}_documents")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(
                f"vector generation has invalid {level} document count: {count!r}"
            )
        artifacts = committed_levels.get(level)
        if count == 0:
            if artifacts is not None:
                raise ValueError(
                    f"vector generation commits artifacts for empty {level} level"
                )
            continue
        if artifacts is None:
            raise ValueError(
                f"vector generation is missing committed artifacts for {level}"
            )
        validate_vector_level_artifacts(
            Path(root) / level,
            model_suffix,
            artifacts,
        )
    return config


__all__ = [
    "VECTOR_PERSISTENCE_SCHEMA",
    "VECTOR_VIEW_UPDATE_MARKER",
    "begin_vector_view_update",
    "finish_vector_view_update",
    "require_complete_vector_view",
    "validate_vector_config_artifact",
    "validate_vector_generation_artifacts",
    "validate_vector_level_artifacts",
    "vector_config_artifact_record",
    "vector_level_artifact_records",
]
