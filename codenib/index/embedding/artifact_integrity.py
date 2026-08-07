# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free integrity records for persisted vector levels."""

from __future__ import annotations

import hashlib
import json
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


def vector_config_artifact_record(
    root: str | Path,
    model_suffix: str,
) -> dict[str, Any]:
    """Fingerprint the top-level record that commits all vector levels."""

    return _file_record(Path(root) / f"config_{model_suffix}.json")


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
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError(f"invalid vector generation config: {config_path}")
    with config_path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError(f"vector generation config must be an object: {config_path}")

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
    "validate_vector_config_artifact",
    "validate_vector_generation_artifacts",
    "validate_vector_level_artifacts",
    "vector_config_artifact_record",
    "vector_level_artifact_records",
]
