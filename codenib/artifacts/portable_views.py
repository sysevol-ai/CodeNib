# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Normalize owned query views into portable, query-only artifacts."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping

from .. import compat_pickle
from ..compiler.artifact_fingerprints import bm25_artifact_file_fingerprints
from ..index.embedding.artifact_integrity import (
    VECTOR_PERSISTENCE_SCHEMA,
    VECTOR_VIEW_UPDATE_MARKER,
    require_complete_vector_view,
    validate_vector_config_artifact,
    validate_vector_generation_artifacts,
    vector_config_artifact_record,
    vector_level_artifact_records,
)
from ..index.embedding.model_policy import (
    resolve_embedding_load_policy_from_options,
)
from ..provider_routes import normalize_provider, resolve_embedding_artifact_route

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
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_PICKLE_SUFFIXES = frozenset({".pkl", ".pickle"})
SourceTrust = Literal["portable-inert", "trusted-local"]


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
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ValueError(
            f"{source} is not a portable repository-relative path: {raw!r}"
        )

    path = Path(raw).expanduser()
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(repo_path)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"{source} points outside the repository: {raw}") from exc
        raw = path.as_posix()
    else:
        native = path.as_posix()
        if native != raw:
            raw_parts = raw.replace("\\", "/").split("/")
            if any(part in {"", ".", ".."} for part in raw_parts):
                raise ValueError(f"{source} is not repository-relative: {raw}")
            raw = native
        elif "\\" in raw or raw.startswith("//") or _WINDOWS_DRIVE_RE.match(raw):
            raise ValueError(
                f"{source} is not a portable repository-relative path: {raw!r}"
            )

    raw_parts = raw.split("/")
    normalized = PurePosixPath(raw)
    if (
        normalized.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or raw != normalized.as_posix()
    ):
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


def _pickle_paths(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() in _PICKLE_SUFFIXES
    )


def _reject_inert_pickles(root: Path) -> None:
    pickles = _pickle_paths(root)
    if pickles:
        raise ValueError(
            "portable-inert query view must not contain pickle: "
            f"{pickles[0].relative_to(root)}"
        )


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


def _validate_vector_model_policy(
    root: Path,
    *,
    model_suffix: str,
    source_trust: SourceTrust,
) -> None:
    """Reject ambiguous multi-model trees and untrusted serialized objects."""

    allowed_model_artifacts = {
        root / f"config_{model_suffix}.json",
    }
    for level in _VECTOR_LEVELS:
        allowed_model_artifacts.update(
            {
                root / level / f"config_{model_suffix}.json",
                root / level / f"documents_{model_suffix}.json",
                root / level / f"documents_{model_suffix}.pkl",
                root / level / f"index_{model_suffix}.faiss",
                root / level / f"index_{model_suffix}.pkl",
            }
        )
    unexpected_model_artifacts = sorted(
        path
        for path in root.rglob("*")
        if path.name.casefold().startswith(("config_", "documents_", "index_"))
        and path not in allowed_model_artifacts
    )
    if unexpected_model_artifacts:
        raise ValueError(
            "portable vector view contains an unknown or other-model artifact: "
            f"{unexpected_model_artifacts[0].relative_to(root)}"
        )

    pickles = _pickle_paths(root)
    if source_trust == "portable-inert":
        _reject_inert_pickles(root)
        return

    allowed_pickles = {
        root / level / f"documents_{model_suffix}.pkl" for level in _VECTOR_LEVELS
    }
    allowed_pickles.update(
        root / level / f"index_{model_suffix}.pkl" for level in _VECTOR_LEVELS
    )
    allowed_pickles.update(
        root / name for name in _REMOVABLE_MUTABLE_VECTOR_FILES if name.endswith(".pkl")
    )
    unexpected = [path for path in pickles if path not in allowed_pickles]
    if unexpected:
        raise ValueError(
            "trusted-local vector view contains an unexpected pickle: "
            f"{unexpected[0].relative_to(root)}"
        )


def _validate_vector_semantics(
    config: Mapping[str, Any],
    view_config: Mapping[str, Any],
) -> tuple[str, int, str | None, str, str]:
    """Close the manifest-route to persisted-config compatibility contract."""

    route = resolve_embedding_artifact_route(view_config)
    expected_revision = (
        resolve_embedding_load_policy_from_options(
            route.model,
            route.compatibility_options,
        ).revision
        if route.provider == "huggingface"
        else None
    )
    required_checks: tuple[tuple[str, object], ...] = (
        ("embedding_model", route.model),
        ("dimension", route.dimension),
        ("embedding_revision", expected_revision),
    )
    for key, expected in required_checks:
        if config.get(key) != expected:
            raise ValueError(
                f"portable vector persistence {key} does not match its view route"
            )
    if "embedding_dimension" in config and config["embedding_dimension"] != (
        route.dimension
    ):
        raise ValueError(
            "portable vector persistence embedding_dimension does not match its "
            "view route"
        )
    try:
        provider = normalize_provider(str(config.get("embedding_provider", "")))
    except ValueError as exc:
        raise ValueError(
            "portable vector persistence has an invalid embedding provider"
        ) from exc
    if provider != route.provider:
        raise ValueError(
            "portable vector persistence embedding provider does not match "
            "its view route"
        )

    expected_metric = view_config.get("index_metric", "ip")
    if expected_metric not in {"ip", "l2"}:
        raise ValueError(
            f"portable vector view has unsupported index metric: {expected_metric!r}"
        )
    persisted_metric = config.get("index_metric")
    if persisted_metric != expected_metric:
        raise ValueError(
            "portable vector persistence index metric does not match its view config"
        )

    expected_index_type = view_config.get("index_type", "flat")
    if expected_index_type not in {"flat", "ivf"}:
        raise ValueError(
            "portable vector view has unsupported index type: "
            f"{expected_index_type!r}"
        )
    if config.get("index_type") != expected_index_type:
        raise ValueError(
            "portable vector persistence index type does not match its view config"
        )

    persisted_identity = config.get("artifact")
    builder_schema = view_config.get("builder_schema")
    identity_required = (
        view_config.get("embedding_fingerprint") is not None
        or (
            isinstance(builder_schema, int)
            and not isinstance(builder_schema, bool)
            and builder_schema >= 3
        )
        or bool(route.compatibility_options)
    )
    if persisted_identity is not None:
        if not isinstance(persisted_identity, Mapping):
            raise ValueError("portable vector persistence artifact identity is invalid")
        persisted_route = resolve_embedding_artifact_route(persisted_identity)
        if persisted_route.public_identity() != route.public_identity():
            raise ValueError(
                "portable vector persistence route identity does not match its view "
                "route"
            )
        expected_fingerprint = view_config.get("embedding_fingerprint")
        if (
            expected_fingerprint is not None
            and persisted_identity.get("embedding_fingerprint") != expected_fingerprint
        ):
            raise ValueError(
                "portable vector persistence embedding fingerprint does not match "
                "its view config"
            )
        persisted_revision = (
            resolve_embedding_load_policy_from_options(
                persisted_route.model,
                persisted_route.compatibility_options,
            ).revision
            if persisted_route.provider == "huggingface"
            else None
        )
        if persisted_revision != expected_revision:
            raise ValueError(
                "portable vector persistence artifact revision does not match its "
                "view config"
            )
        persisted_identity_metric = persisted_identity.get("index_metric")
        if persisted_identity_metric != expected_metric:
            raise ValueError(
                "portable vector persistence identity metric does not match its "
                "view config"
            )
    elif identity_required:
        raise ValueError(
            "portable vector persistence is missing its embedding artifact identity"
        )
    elif "embedding_kwargs" in config:
        # Legacy configs sometimes expose the semantic options directly. When
        # present, absence is not treated as a wildcard.
        persisted_route = resolve_embedding_artifact_route(config)
        if persisted_route.public_identity() != route.public_identity():
            raise ValueError(
                "portable vector persistence options do not match its view route"
            )

    if route.dimension is None:
        raise ValueError("portable vector route is missing its embedding dimension")
    return (
        route.model.replace("/", "__"),
        route.dimension,
        expected_revision,
        expected_metric,
        expected_index_type,
    )


def _faiss_contract(path: Path) -> tuple[int, int, str, str]:
    """Read the persisted index contract, importing FAISS on demand."""

    try:
        faiss = importlib.import_module("faiss")
        index = faiss.read_index(str(path))
        dimension = int(index.d)
        total = int(index.ntotal)
        metric_type = int(index.metric_type)
        if metric_type == int(faiss.METRIC_INNER_PRODUCT):
            metric = "ip"
        elif metric_type == int(faiss.METRIC_L2):
            metric = "l2"
        else:
            raise ValueError(
                f"portable vector FAISS index has unsupported metric: {metric_type}"
            )
        if isinstance(index, faiss.IndexIVF):
            index_type = "ivf"
        elif isinstance(index, faiss.IndexFlat):
            index_type = "flat"
        else:
            raise ValueError(
                f"portable vector FAISS index has unsupported type: {type(index).__name__}"
            )
    except Exception as exc:
        raise ValueError(f"portable vector FAISS index is unreadable: {path}") from exc
    return dimension, total, metric, index_type


def _validate_level_semantics(
    path: Path,
    *,
    level: str,
    model: str,
    provider: str,
    revision: str | None,
    dimension: int,
    metric: object,
    index_type: str,
    count: int,
) -> None:
    if not path.exists():
        return
    config = _load_json_object(path, label=f"portable vector {level} config")
    checks: tuple[tuple[str, object], ...] = (
        ("embedding_model", model),
        ("embedding_revision", revision),
        ("dimension", dimension),
        ("index_metric", metric),
        ("index_type", index_type),
        ("level", level),
        ("num_documents", count),
    )
    for key, expected in checks:
        if config.get(key) != expected:
            raise ValueError(
                f"portable vector {level} persistence {key} does not match"
            )
    try:
        persisted_provider = normalize_provider(
            str(config.get("embedding_provider", ""))
        )
    except ValueError as exc:
        raise ValueError(
            f"portable vector {level} persistence provider is invalid"
        ) from exc
    if persisted_provider != provider:
        raise ValueError(f"portable vector {level} persistence provider does not match")


def _validate_vector_layout(
    root: Path,
    repo_path: Path,
    *,
    model_suffix: str,
    config: Mapping[str, Any],
    expected_model: str,
    expected_provider: str,
    expected_revision: str | None,
    expected_dimension: int,
    expected_metric: str,
    expected_index_type: str,
    source_trust: SourceTrust,
) -> tuple[
    str,
    dict[Path, list[dict[str, Any]]],
    dict[str, int],
    set[Path],
]:
    expected_documents: set[Path] = set()
    selected: dict[Path, list[dict[str, Any]]] = {}
    formats: set[str] = set()
    counts_present = [f"{level}_documents" in config for level in _VECTOR_LEVELS]
    if any(counts_present) and not all(counts_present):
        raise ValueError("portable vector config has partial level counts")
    legacy_counts = not any(counts_present)
    if legacy_counts and (
        config.get("persistence_schema") is not None
        or config.get("level_artifacts") is not None
    ):
        raise ValueError(
            "portable vector config with persistence records requires level counts"
        )

    derived_counts: dict[str, int] = {}
    stale_paths: set[Path] = set()

    for level in _VECTOR_LEVELS:
        count = config.get(f"{level}_documents") if not legacy_counts else None
        if count is not None and (
            not isinstance(count, int) or isinstance(count, bool) or count < 0
        ):
            raise ValueError(
                f"portable vector config has invalid {level} count: {count!r}"
            )
        level_path = root / level
        pickle_path = level_path / f"documents_{model_suffix}.pkl"
        json_path = level_path / f"documents_{model_suffix}.json"
        present = [path for path in (pickle_path, json_path) if path.is_file()]
        expected_documents.update(present)
        index_path = level_path / f"index_{model_suffix}.faiss"

        if count == 0:
            # A zero count is authoritative. Older writers left uncommitted
            # current-model level files behind; this caller-owned copy may prune
            # those exact names, but never unknown or other-model artifacts.
            stale_paths.update(
                path
                for path in (
                    pickle_path,
                    json_path,
                    index_path,
                    level_path / f"index_{model_suffix}.pkl",
                    level_path / f"config_{model_suffix}.json",
                )
                if path.is_file()
            )
            derived_counts[level] = 0
            continue
        if len(present) > 1:
            raise ValueError(
                f"portable vector view mixes pickle and JSON documents in {level}"
            )
        if not present and (count is not None and count > 0):
            raise ValueError(
                f"portable vector view is missing documents for non-empty {level}"
            )
        if not present and count is None:
            if (
                index_path.exists()
                or (level_path / f"config_{model_suffix}.json").exists()
            ):
                raise ValueError(f"portable legacy vector {level} level is incomplete")
            derived_counts[level] = 0
            continue
        if index_path.is_symlink() or not index_path.is_file():
            raise ValueError(f"portable vector view is missing its index: {index_path}")

        path = present[0]
        document_format = path.suffix.removeprefix(".")
        if document_format == "pkl":
            if source_trust != "trusted-local":
                raise ValueError(
                    "portable-inert vector view must not deserialize documents"
                )
            payload = _normalize_pickle_documents(path, repo_path)
        else:
            payload = _normalize_json_documents(path, repo_path)
        if count is not None and len(payload) != count:
            raise ValueError(
                f"portable vector {level} count differs from {path.name}: "
                f"expected {count}, found {len(payload)}"
            )
        count = len(payload)
        dimension, total, metric, index_type = _faiss_contract(index_path)
        if dimension != expected_dimension:
            raise ValueError(
                f"portable vector FAISS dimension mismatch in {level}: "
                f"expected {expected_dimension}, found {dimension}"
            )
        if total != count:
            raise ValueError(
                f"portable vector FAISS count mismatch in {level}: "
                f"expected {count}, found {total}"
            )
        if metric != expected_metric:
            raise ValueError(
                f"portable vector FAISS metric mismatch in {level}: "
                f"expected {expected_metric}, found {metric}"
            )
        if index_type != expected_index_type:
            raise ValueError(
                f"portable vector FAISS index type mismatch in {level}: "
                f"expected {expected_index_type}, found {index_type}"
            )
        derived_counts[level] = count
        if legacy_counts and count == 0:
            stale_paths.update(
                candidate
                for candidate in (
                    path,
                    index_path,
                    level_path / f"index_{model_suffix}.pkl",
                    level_path / f"config_{model_suffix}.json",
                )
                if candidate.is_file()
            )
            continue
        _validate_level_semantics(
            level_path / f"config_{model_suffix}.json",
            level=level,
            model=expected_model,
            provider=expected_provider,
            revision=expected_revision,
            dimension=expected_dimension,
            metric=expected_metric,
            index_type=expected_index_type,
            count=count,
        )
        formats.add(document_format)
        selected[path] = payload

    if not selected:
        raise ValueError("portable vector view has no non-empty document store")
    if len(formats) != 1:
        raise ValueError("portable vector view mixes pickle and JSON documents")

    candidates = {
        path
        for path in root.rglob("documents_*")
        if path.suffix.casefold() in {".json", ".pkl", ".pickle"}
    }
    unexpected_documents = sorted(candidates - expected_documents)
    if unexpected_documents:
        raise ValueError(
            "portable vector view contains an unexpected document store: "
            f"{unexpected_documents[0].relative_to(root)}"
        )

    allowed_pickles = {path for path in selected if path.suffix.lower() == ".pkl"}
    allowed_pickles.update(
        path for path in stale_paths if path.suffix.casefold() in _PICKLE_SUFFIXES
    )
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
        if path.suffix.casefold() in _PICKLE_SUFFIXES and path not in allowed_pickles
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

    return next(iter(formats)), selected, derived_counts, stale_paths


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


def _validate_view_document_count(
    view_config: Mapping[str, Any],
    counts: Mapping[str, int],
) -> None:
    if "document_count" not in view_config:
        return
    raw = view_config["document_count"]
    if not isinstance(raw, Mapping):
        raise ValueError("portable vector document_count must be a mapping")
    expected = {
        level: count for level in _VECTOR_LEVELS if (count := counts[level]) > 0
    }
    if set(raw) != set(expected):
        raise ValueError(
            "portable vector document_count must contain exactly the non-empty "
            "levels"
        )
    for level, count in raw.items():
        if (
            not isinstance(level, str)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
            or count != expected[level]
        ):
            raise ValueError(
                "portable vector document_count does not match persisted levels"
            )


def _assert_normalized_vector_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.suffix.casefold() in _PICKLE_SUFFIXES:
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
    source_trust: SourceTrust,
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
    _validate_vector_model_policy(
        root,
        model_suffix=model_suffix,
        source_trust=source_trust,
    )
    expected_config = view_config.get("persistence_config_fingerprint")
    if expected_config is not None:
        validate_vector_config_artifact(
            root,
            model_suffix,
            expected_config,
        )
    # Validate the committed source generation before replacing any trusted-local
    # pickle or refreshing records. Otherwise normalization could bless torn files.
    validate_vector_generation_artifacts(root, model_suffix)
    config_path = root / f"config_{model_suffix}.json"
    config = _load_json_object(config_path, label="portable vector config")
    (
        semantic_suffix,
        expected_dimension,
        expected_revision,
        expected_metric,
        expected_index_type,
    ) = _validate_vector_semantics(config, view_config)
    if semantic_suffix != model_suffix:
        raise ValueError("portable vector config embedding model does not match")

    document_format, documents, counts, stale_paths = _validate_vector_layout(
        root,
        repo_path,
        model_suffix=model_suffix,
        config=config,
        expected_model=route.model,
        expected_provider=route.provider,
        expected_revision=expected_revision,
        expected_dimension=expected_dimension,
        expected_metric=expected_metric,
        expected_index_type=expected_index_type,
        source_trust=source_trust,
    )
    _validate_view_document_count(view_config, counts)

    for path, payload in documents.items():
        output = path.with_suffix(".json")
        output.write_bytes(_json_bytes(payload))
        if document_format == "pkl":
            path.unlink()

    for path in sorted(stale_paths):
        path.unlink(missing_ok=True)
    for level in _VECTOR_LEVELS:
        level_path = root / level
        try:
            level_path.rmdir()
        except OSError:
            pass

    config.update({f"{level}_documents": counts[level] for level in _VECTOR_LEVELS})
    config_path.write_bytes(_json_bytes(config))

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
    source_trust: SourceTrust = "portable-inert",
) -> dict[str, Any]:
    """Normalize one copied view and return its portable identity adjustments.

    ``root`` must be a caller-owned copy that can be safely rewritten.
    ``source_trust`` defaults to inert portable data; only ``trusted-local`` may
    deserialize the exact legacy vector pickle names owned by this program.
    """

    if not isinstance(source_trust, str) or source_trust not in {
        "portable-inert",
        "trusted-local",
    }:
        raise ValueError(f"invalid portable query view source trust: {source_trust!r}")
    normalized_root, repository = _owned_view_root(root, repo_path)
    if view_type == "vector":
        return _normalize_vector_view(
            normalized_root,
            repository,
            view_config=view_config,
            source_trust=source_trust,
        )
    if view_type != "bm25":
        raise ValueError(
            f"view {view_type!r} is not yet supported by portable query views; "
            "select bm25 and/or vector"
        )

    _reject_inert_pickles(normalized_root)
    metadata_path = normalized_root / "bm25_metadata.json"
    metadata = _load_json_object(metadata_path, label="portable BM25 metadata")
    metadata["project_root"] = "source"
    metadata_path.write_bytes(_json_bytes(metadata))
    return {
        "artifact_file_fingerprints": bm25_artifact_file_fingerprints(normalized_root)
    }


__all__ = ["SourceTrust", "normalize_owned_query_view"]
