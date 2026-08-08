# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Normalize owned query views into portable, query-only artifacts."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .. import compat_pickle
from ..compiler.artifact_fingerprints import bm25_artifact_file_fingerprints
from ..index.embedding.artifact_integrity import (
    VECTOR_PERSISTENCE_SCHEMA,
    VECTOR_VIEW_UPDATE_MARKER,
    require_complete_vector_view,
    validate_vector_generation_artifacts,
    vector_config_artifact_record,
    vector_level_artifact_records,
)
from ..provider_routes import resolve_embedding_artifact_route

_VECTOR_LEVELS = ("l0", "l2")
_REMOVABLE_MUTABLE_VECTOR_FILES = frozenset(
    {
        "chunk_store.json",
        "chunk_store.pkl",
        "embeddings_cache.json",
        "embeddings_cache.npz",
        "embeddings_cache.pkl",
        "incremental_state.json",
    }
)
_MUTABLE_VECTOR_PREFIXES = (
    "chunk_store.",
    "embeddings_cache.",
    "incremental_state.",
)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def _load_json(path: Path, *, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is not a regular file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid JSON: {path}") from exc


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    value = _load_json(path, label=label)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _owned_view_root(root: Path, repo_path: Path) -> tuple[Path, Path]:
    candidate = root.expanduser()
    if candidate.is_symlink():
        raise ValueError(f"portable query view root must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise ValueError(f"portable query view must be a directory: {resolved}")

    repository = repo_path.expanduser().resolve()
    if not repository.is_dir():
        raise ValueError(f"repository directory does not exist: {repository}")
    if (
        resolved == repository
        or resolved in repository.parents
        or repository in resolved.parents
    ):
        raise ValueError(
            "portable query view must not overlap the source repository: " f"{resolved}"
        )

    for path in sorted(resolved.rglob("*")):
        relative = path.relative_to(resolved)
        if path.is_symlink():
            raise ValueError(f"portable query view contains a symlink: {relative}")
        if not path.is_dir() and not path.is_file():
            raise ValueError(
                f"portable query view contains a non-regular entry: {relative}"
            )
    return resolved, repository


def _portable_source_path(value: object, repo_path: Path, *, source: str) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(repo_path)
        except ValueError as exc:
            raise ValueError(f"{source} points outside the repository: {raw}") from exc
    normalized = PurePosixPath(path.as_posix())
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError(f"{source} is not repository-relative: {raw}")
    return normalized.as_posix()


def _normalize_pickle_documents(
    path: Path,
    repo_path: Path,
) -> list[dict[str, Any]]:
    """Load documents from an explicitly trusted, machine-local pickle."""

    try:
        with path.open("rb") as handle:
            documents = compat_pickle.load(handle)
    except Exception as exc:
        raise ValueError(
            f"trusted-local vector documents pickle is invalid: {path.name}"
        ) from exc
    if not isinstance(documents, list):
        raise ValueError(f"portable vector documents must be a list: {path.name}")

    payload: list[dict[str, Any]] = []
    for index, document in enumerate(documents):
        page_content = getattr(document, "page_content", None)
        if not isinstance(page_content, str):
            raise ValueError(
                f"portable vector document {index} has invalid content: {path.name}"
            )
        metadata = getattr(document, "metadata", None)
        if not isinstance(metadata, dict):
            raise ValueError(
                f"portable vector document {index} has invalid metadata: {path.name}"
            )
        normalized_metadata = dict(metadata)
        normalized_metadata["file"] = _portable_source_path(
            metadata.get("file"),
            repo_path,
            source=f"vector document {index} file",
        )
        payload.append(
            {
                "page_content": page_content,
                "metadata": normalized_metadata,
            }
        )
    # Validate serializability before any file in the owned view is changed.
    _json_bytes(payload)
    return payload


def _normalize_json_documents(
    path: Path,
    repo_path: Path,
) -> list[dict[str, Any]]:
    documents = _load_json(path, label="portable vector documents")
    if not isinstance(documents, list):
        raise ValueError(f"portable vector documents must be a JSON list: {path}")

    payload: list[dict[str, Any]] = []
    for index, document in enumerate(documents):
        if not isinstance(document, dict) or set(document) != {
            "page_content",
            "metadata",
        }:
            raise ValueError(
                f"portable vector document {index} has invalid shape: {path.name}"
            )
        page_content = document["page_content"]
        metadata = document["metadata"]
        if not isinstance(page_content, str) or not isinstance(metadata, dict):
            raise ValueError(
                "portable vector document "
                f"{index} has invalid content or metadata: {path.name}"
            )
        raw_file = metadata.get("file")
        if raw_file is not None and not isinstance(raw_file, str):
            raise ValueError(
                f"portable vector document {index} has invalid file: {path.name}"
            )
        normalized_metadata = dict(metadata)
        normalized_metadata["file"] = _portable_source_path(
            raw_file,
            repo_path,
            source=f"vector document {index} file",
        )
        payload.append(
            {
                "page_content": page_content,
                "metadata": normalized_metadata,
            }
        )
    return payload


def _is_mutable_vector_state(path: Path) -> bool:
    name = path.name.lower()
    return (
        name == VECTOR_VIEW_UPDATE_MARKER
        or name.endswith(".save-in-progress")
        or name.startswith(_MUTABLE_VECTOR_PREFIXES)
    )


def _validate_vector_layout(
    root: Path,
    repo_path: Path,
    *,
    model_suffix: str,
    config: Mapping[str, Any],
) -> tuple[str, dict[Path, list[dict[str, Any]]]]:
    expected_documents: set[Path] = set()
    selected: dict[Path, list[dict[str, Any]]] = {}
    formats: set[str] = set()

    for level in _VECTOR_LEVELS:
        count = config.get(f"{level}_documents")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(
                f"portable vector config has invalid {level} count: {count!r}"
            )
        level_path = root / level
        pickle_path = level_path / f"documents_{model_suffix}.pkl"
        json_path = level_path / f"documents_{model_suffix}.json"
        present = [path for path in (pickle_path, json_path) if path.is_file()]
        expected_documents.update(present)

        if count == 0:
            if present:
                raise ValueError(
                    f"portable vector view contains documents for empty {level} level"
                )
            continue
        if len(present) > 1:
            raise ValueError(
                f"portable vector view mixes pickle and JSON documents in {level}"
            )
        if not present:
            raise ValueError(
                f"portable vector view is missing documents for non-empty {level}"
            )
        index_path = level_path / f"index_{model_suffix}.faiss"
        if index_path.is_symlink() or not index_path.is_file():
            raise ValueError(f"portable vector view is missing its index: {index_path}")

        path = present[0]
        document_format = path.suffix.removeprefix(".")
        formats.add(document_format)
        if document_format == "pkl":
            payload = _normalize_pickle_documents(path, repo_path)
        else:
            payload = _normalize_json_documents(path, repo_path)
        if len(payload) != count:
            raise ValueError(
                f"portable vector {level} count differs from {path.name}: "
                f"expected {count}, found {len(payload)}"
            )
        selected[path] = payload

    if not selected:
        raise ValueError("portable vector view has no non-empty document store")
    if len(formats) != 1:
        raise ValueError("portable vector view mixes pickle and JSON documents")

    candidates = {
        path
        for path in root.rglob("documents_*")
        if path.suffix.lower() in {".json", ".pkl"}
    }
    unexpected_documents = sorted(candidates - expected_documents)
    if unexpected_documents:
        raise ValueError(
            "portable vector view contains an unexpected document store: "
            f"{unexpected_documents[0].relative_to(root)}"
        )

    allowed_pickles = {path for path in selected if path.suffix.lower() == ".pkl"}
    allowed_pickles.update(
        path for path in root.glob("l[02]/index_*.pkl") if path.is_file()
    )
    allowed_pickles.update(
        root / name
        for name in _REMOVABLE_MUTABLE_VECTOR_FILES
        if name.endswith(".pkl") and (root / name).is_file()
    )
    residual_pickles = sorted(
        path
        for path in root.rglob("*")
        if path.suffix.lower() == ".pkl" and path not in allowed_pickles
    )
    if residual_pickles:
        raise ValueError(
            "portable vector view contains an unexpected pickle: "
            f"{residual_pickles[0].relative_to(root)}"
        )

    removable_mutable = {root / name for name in _REMOVABLE_MUTABLE_VECTOR_FILES}
    residual_mutable = sorted(
        path
        for path in root.rglob("*")
        if _is_mutable_vector_state(path)
        and (path not in removable_mutable or not path.is_file())
    )
    if residual_mutable:
        raise ValueError(
            "portable vector view contains unexpected mutable state: "
            f"{residual_mutable[0].relative_to(root)}"
        )

    return next(iter(formats)), selected


def _refresh_vector_persistence_records(
    root: Path,
    *,
    model_suffix: str,
) -> None:
    config_path = root / f"config_{model_suffix}.json"
    config = _load_json_object(config_path, label="portable vector config")
    level_artifacts: dict[str, dict[str, dict[str, Any]]] = {}
    for level in _VECTOR_LEVELS:
        count = config.get(f"{level}_documents")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(
                f"portable vector config has invalid {level} count: {count!r}"
            )
        if count == 0:
            continue
        level_path = root / level
        documents_path = level_path / f"documents_{model_suffix}.json"
        level_artifacts[level] = vector_level_artifact_records(
            level_path,
            model_suffix,
            documents_file=documents_path.name,
        )
    config["persistence_schema"] = VECTOR_PERSISTENCE_SCHEMA
    config["level_artifacts"] = level_artifacts
    config_path.write_bytes(_json_bytes(config))


def _assert_normalized_vector_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() == ".pkl":
            raise ValueError(
                "portable vector normalization left a pickle behind: "
                f"{path.relative_to(root)}"
            )
        if _is_mutable_vector_state(path):
            raise ValueError(
                "portable vector normalization left mutable state behind: "
                f"{path.relative_to(root)}"
            )


def _normalize_vector_view(
    root: Path,
    repo_path: Path,
    *,
    view_config: Mapping[str, Any],
) -> dict[str, Any]:
    require_complete_vector_view(root)
    interrupted = sorted(root.glob(".config_*.json.save-in-progress"))
    if interrupted:
        raise ValueError(
            "portable vector view contains an interrupted save marker: "
            f"{interrupted[0].name}"
        )

    route = resolve_embedding_artifact_route(view_config)
    model_suffix = route.model.replace("/", "__")
    # Validate the committed source generation before replacing any trusted-local
    # pickle or refreshing records. Otherwise normalization could bless torn files.
    validate_vector_generation_artifacts(root, model_suffix)
    config_path = root / f"config_{model_suffix}.json"
    config = _load_json_object(config_path, label="portable vector config")
    if config.get("embedding_model") != route.model:
        raise ValueError("portable vector config embedding model does not match")

    document_format, documents = _validate_vector_layout(
        root,
        repo_path,
        model_suffix=model_suffix,
        config=config,
    )

    for path, payload in documents.items():
        output = path.with_suffix(".json")
        output.write_bytes(_json_bytes(payload))
        if document_format == "pkl":
            path.unlink()

    # Query serving does not need build-time incremental state. These exact
    # machine-local files are safe to discard from the owned copy; unfamiliar
    # mutable state was rejected before mutation.
    for name in _REMOVABLE_MUTABLE_VECTOR_FILES:
        (root / name).unlink(missing_ok=True)
    for legacy in root.glob("l[02]/index_*.pkl"):
        legacy.unlink()

    _refresh_vector_persistence_records(root, model_suffix=model_suffix)
    _assert_normalized_vector_tree(root)
    validate_vector_generation_artifacts(root, model_suffix)
    return {
        "artifact_scope": "query-serving",
        "portable_document_format": "codenib.vector-documents.v1",
        "persistence_config_fingerprint": vector_config_artifact_record(
            root,
            model_suffix,
        ),
    }


def normalize_owned_query_view(
    root: Path,
    *,
    repo_path: Path,
    view_type: str,
    view_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize one copied view and return its portable identity adjustments.

    ``root`` must be a caller-owned copy that can be safely rewritten. Local
    vector pickles are deserialized only on that explicit trust boundary;
    already-portable JSON document stores are parsed as inert data.
    """

    normalized_root, repository = _owned_view_root(root, repo_path)
    if view_type == "vector":
        return _normalize_vector_view(
            normalized_root,
            repository,
            view_config=view_config,
        )
    if view_type != "bm25":
        raise ValueError(
            f"view {view_type!r} is not yet supported by portable query views; "
            "select bm25 and/or vector"
        )

    metadata_path = normalized_root / "bm25_metadata.json"
    metadata = _load_json_object(metadata_path, label="portable BM25 metadata")
    metadata["project_root"] = "source"
    metadata_path.write_bytes(_json_bytes(metadata))
    return {
        "artifact_file_fingerprints": bm25_artifact_file_fingerprints(normalized_root)
    }


__all__ = ["normalize_owned_query_view"]
