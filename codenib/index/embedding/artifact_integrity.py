# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free integrity records for persisted vector levels."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

VECTOR_PERSISTENCE_SCHEMA = 1
_DIGEST_BYTES = 1024 * 1024


def _file_record(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"invalid vector artifact file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(_DIGEST_BYTES):
            digest.update(block)
            size += len(block)
    return {"file": path.name, "size": size, "sha256": digest.hexdigest()}


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


__all__ = [
    "VECTOR_PERSISTENCE_SCHEMA",
    "validate_vector_level_artifacts",
    "vector_level_artifact_records",
]
