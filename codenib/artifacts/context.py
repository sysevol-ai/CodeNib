# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Stage a portable, commit-addressed repository-context artifact."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .. import compat_pickle
from .._version import package_version
from ..compiler.artifact_fingerprints import bm25_artifact_file_fingerprints
from ..compiler.checkout_identity import validate_checkout_identity
from ..compiler.manifest import MANIFEST_FILENAME, RepoManifest
from ..compiler.snapshot_store import normalize_repo
from ..index.embedding.artifact_integrity import (
    VECTOR_PERSISTENCE_SCHEMA,
    validate_vector_config_artifact,
    vector_config_artifact_record,
    vector_level_artifact_records,
)
from ..provider_routes import resolve_embedding_artifact_route
from .security import assert_no_credential_fields, assert_publishable_tree, file_sha256

CONTEXT_ARTIFACT_SCHEMA = "codenib.context-artifact.v1"
CONTEXT_ARTIFACT_MANIFEST = "codenib-context.json"
PORTABLE_CONTEXT_VIEWS = frozenset({"bm25", "vector"})
_VIEW_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class ContextArtifactResult:
    """Paths and identity of one staged context artifact."""

    output_dir: Path
    metadata_path: Path
    manifest_path: Path
    repository: str
    commit: str
    views: tuple[str, ...]
    file_count: int
    byte_count: int


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def _write_json(root: Path, relative: str, value: Any) -> Path:
    target = root.joinpath(*PurePosixPath(relative).parts).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"artifact path escapes the output directory: {relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_json_bytes(value))
    return target


def _repository_slug(repo_path: Path, explicit: str | None) -> str:
    if explicit:
        return normalize_repo(explicit)
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "config", "--get", "remote.origin.url"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        origin = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        origin = ""
    return normalize_repo(origin or repo_path.name)


def _validated_output(repo_path: Path, manifest_root: Path, output_dir: Path) -> Path:
    output = output_dir.expanduser().resolve()
    for source, label in (
        (repo_path.resolve(), "repository"),
        (manifest_root.resolve(), "index root"),
    ):
        if output == source or source in output.parents or output in source.parents:
            raise ValueError(f"context artifact output overlaps the {label}: {output}")
    if output.exists() and not output.is_dir():
        raise ValueError(f"context artifact output is not a directory: {output}")
    if output.exists() and any(output.iterdir()):
        if not (output / CONTEXT_ARTIFACT_MANIFEST).is_file():
            raise ValueError(
                "refusing to replace a non-empty directory that is not a CodeNib "
                f"context artifact: {output}"
            )
    return output


def _view_source(entry_path: str, manifest_root: Path, *, view: str) -> Path:
    source = Path(entry_path).expanduser()
    if not source.is_absolute():
        source = manifest_root / source
    source = source.resolve()
    if source != manifest_root and manifest_root not in source.parents:
        raise ValueError(f"view {view!r} is outside the manifest index root: {source}")
    if not source.exists():
        raise ValueError(f"view {view!r} is missing: {source}")
    return source


def _copy_view(source: Path, stage: Path, view: str) -> str:
    if not _VIEW_NAME_RE.fullmatch(view):
        raise ValueError(f"invalid context artifact view name: {view!r}")
    for candidate in (source, *source.rglob("*")) if source.is_dir() else (source,):
        if candidate.is_symlink():
            raise ValueError(f"view {view!r} contains a symbolic link: {candidate}")

    destination = stage / "views" / view
    if source.is_dir():
        shutil.copytree(source, destination)
        return destination.relative_to(stage).as_posix()
    destination.mkdir(parents=True)
    target = destination / source.name
    shutil.copy2(source, target)
    return target.relative_to(stage).as_posix()


def _normalize_copied_view(
    stage: Path,
    *,
    repo_path: Path,
    view: str,
    relative: str,
    view_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Rewrite view-local machine paths and return identity adjustments."""

    target = stage.joinpath(*PurePosixPath(relative).parts)
    if view == "vector":
        route = resolve_embedding_artifact_route(view_config)
        return _normalize_vector_view(
            target,
            repo_path,
            embedding_model=route.model,
        )
    if view != "bm25":
        raise ValueError(
            f"view {view!r} is not yet supported by portable context artifacts; "
            "select bm25 and/or vector"
        )
    root = target if target.is_dir() else target.parent
    metadata_path = root / "bm25_metadata.json"
    if not metadata_path.is_file():
        raise ValueError("portable BM25 view is missing bm25_metadata.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("portable BM25 metadata must be a JSON object")
    metadata["project_root"] = "source"
    metadata_path.write_bytes(_json_bytes(metadata))
    return {"artifact_file_fingerprints": bm25_artifact_file_fingerprints(root)}


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


def _convert_vector_documents(path: Path, repo_path: Path) -> Path:
    """Convert a trusted local document pickle to portable, inert JSON."""

    with path.open("rb") as handle:
        documents = compat_pickle.load(handle)
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

    output = path.with_suffix(".json")
    output.write_bytes(_json_bytes(payload))
    path.unlink()
    return output


def _refresh_vector_persistence_records(target: Path) -> None:
    """Commit the portable JSON/index pairs in copied vector configs."""

    for config_path in sorted(target.glob("config_*.json")):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError(f"portable vector config must be an object: {config_path}")
        embedding_model = config.get("embedding_model")
        if not isinstance(embedding_model, str) or not embedding_model:
            continue
        model_suffix = embedding_model.replace("/", "__")
        level_artifacts: dict[str, dict[str, dict[str, Any]]] = {}
        has_level_counts = False
        for level in ("l0", "l2"):
            count = config.get(f"{level}_documents")
            if count is None:
                continue
            has_level_counts = True
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError(
                    f"portable vector config has invalid {level} count: {count!r}"
                )
            if count == 0:
                continue
            level_path = target / level
            documents_path = level_path / f"documents_{model_suffix}.json"
            level_artifacts[level] = vector_level_artifact_records(
                level_path,
                model_suffix,
                documents_file=documents_path.name,
            )
        if has_level_counts:
            config["persistence_schema"] = VECTOR_PERSISTENCE_SCHEMA
            config["level_artifacts"] = level_artifacts
            config_path.write_bytes(_json_bytes(config))


def _normalize_vector_view(
    target: Path,
    repo_path: Path,
    *,
    embedding_model: str,
) -> dict[str, Any]:
    if not target.is_dir():
        raise ValueError("portable vector view must be a directory")
    interrupted = sorted(target.glob(".config_*.json.save-in-progress"))
    if interrupted:
        raise ValueError(
            "portable vector view contains an interrupted save marker: "
            f"{interrupted[0].name}"
        )

    # Query serving does not need the mutable state used to build the next
    # commit. Excluding it keeps the downloadable artifact smaller and avoids
    # publishing build-machine paths from incremental caches.
    for name in (
        "chunk_store.json",
        "chunk_store.pkl",
        "embeddings_cache.json",
        "embeddings_cache.npz",
        "embeddings_cache.pkl",
        "incremental_state.json",
    ):
        (target / name).unlink(missing_ok=True)

    document_files = sorted(target.glob("l[02]/documents_*.pkl"))
    if not document_files:
        raise ValueError(
            "portable vector view requires the current documents_*.pkl format; "
            "rebuild the vector view"
        )
    for path in document_files:
        _convert_vector_documents(path, repo_path)

    # The current document files supersede legacy LangChain docstore pickles.
    # Leaving both formats would retain duplicate absolute source paths.
    for legacy in target.glob("l[02]/index_*.pkl"):
        legacy.unlink()
    _refresh_vector_persistence_records(target)
    model_suffix = embedding_model.replace("/", "__")
    return {
        "artifact_scope": "query-serving",
        "portable_document_format": "codenib.vector-documents.v1",
        "persistence_config_fingerprint": vector_config_artifact_record(
            target,
            model_suffix,
        ),
    }


def _inventory(root: Path) -> list[dict[str, Any]]:
    files = []
    for path in sorted(
        candidate for candidate in root.rglob("*") if candidate.is_file()
    ):
        size, digest = file_sha256(path)
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": size,
                "sha256": digest,
            }
        )
    return files


def _selected_views(
    manifest: RepoManifest,
    requested: Sequence[str] | None,
) -> tuple[str, ...]:
    available = {name for name in manifest.indexes if manifest.index_is_current(name)}
    if requested is None:
        selected = sorted(available)
    else:
        selected = list(dict.fromkeys(str(name).strip() for name in requested))
        missing = sorted(name for name in selected if name not in available)
        if missing:
            raise ValueError(
                "context artifact requires current views: " + ", ".join(missing)
            )
    if not selected:
        raise ValueError("context artifact requires at least one current view")
    return tuple(selected)


def _portable_capabilities(
    capabilities: Mapping[str, bool],
    views: Iterable[str],
) -> dict[str, bool]:
    selected = set(views)
    result = dict(capabilities)
    result.update(
        {
            "sparse_search": "bm25" in selected,
            "dense_search": "vector" in selected,
            "hybrid_search": {"bm25", "vector"} <= selected,
            "symbol_navigation": "symbol_graph" in selected,
        }
    )
    return result


def stage_context_artifact(
    repo_path: Path,
    manifest_path: Path,
    output_dir: Path,
    *,
    repository: str | None = None,
    views: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    validate_checkout: bool = True,
) -> ContextArtifactResult:
    """Copy current views into an atomic, path-independent artifact directory."""

    repo_path = repo_path.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    if not repo_path.is_dir():
        raise ValueError(f"repository directory does not exist: {repo_path}")
    if not manifest_path.is_file():
        raise ValueError(f"repository manifest does not exist: {manifest_path}")
    manifest_root = manifest_path.parent
    output_dir = _validated_output(repo_path, manifest_root, output_dir)
    environment = os.environ if environ is None else environ
    manifest = RepoManifest.load(manifest_path)
    if validate_checkout:
        validate_checkout_identity(
            repo_path,
            manifest,
            artifact_root=manifest_root,
        )
    selected = _selected_views(manifest, views)
    unsupported = sorted(set(selected) - PORTABLE_CONTEXT_VIEWS)
    if unsupported:
        raise ValueError(
            "portable context artifacts do not yet support views: "
            + ", ".join(unsupported)
        )
    slug = _repository_slug(repo_path, repository)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.tmp-",
            dir=str(output_dir.parent),
        )
    ).resolve()
    try:
        portable = manifest.to_dict()
        portable["repo"]["path"] = "source"
        portable_indexes: dict[str, Any] = {}
        for view in selected:
            entry = manifest.indexes[view]
            assert_no_credential_fields(entry.config, source=f"view {view!r} config")
            assert_no_credential_fields(
                entry.metadata,
                source=f"view {view!r} metadata",
            )
            if view == "vector":
                route = resolve_embedding_artifact_route(entry.config)
            source = _view_source(entry.path, manifest_root, view=view)
            if view == "vector":
                expected_config = entry.config.get("persistence_config_fingerprint")
                if expected_config is not None:
                    validate_vector_config_artifact(
                        source,
                        route.model.replace("/", "__"),
                        expected_config,
                    )
            relative = _copy_view(source, stage, view)
            adjustments = _normalize_copied_view(
                stage,
                repo_path=repo_path,
                view=view,
                relative=relative,
                view_config=entry.config,
            )
            entry_data = entry.to_dict()
            entry_data["path"] = relative
            for section in ("config", "metadata"):
                entry_data[section].update(adjustments)
            portable_indexes[view] = entry_data
        portable["indexes"] = portable_indexes
        portable["capabilities"] = _portable_capabilities(
            manifest.capabilities,
            selected,
        )
        portable_manifest = _write_json(stage, MANIFEST_FILENAME, portable)

        assert_publishable_tree(
            stage,
            forbidden_paths=(repo_path, manifest_root),
            environ=environment,
            label="context artifact",
        )
        files = _inventory(stage)
        metadata = {
            "schema": CONTEXT_ARTIFACT_SCHEMA,
            "repository": {
                "slug": slug,
                "commit": manifest.commit,
                "source_fingerprint": manifest.source_fingerprint,
                "languages": list(manifest.languages),
            },
            "builder": {
                "codenib_version": package_version(),
                "manifest_version": manifest.version,
                "compiled_at": manifest.compiled_at,
            },
            "manifest": {
                "path": MANIFEST_FILENAME,
                "repository_path": "source",
                "paths": "artifact-relative-posix",
            },
            "source_locations": {
                "path": "repository-relative-posix",
                "line_base": 1,
                "end_line": "inclusive",
                "commit": manifest.commit,
            },
            "views": list(selected),
            "capabilities": portable["capabilities"],
            "files": files,
        }
        metadata_path = _write_json(stage, CONTEXT_ARTIFACT_MANIFEST, metadata)
        assert_publishable_tree(
            stage,
            forbidden_paths=(repo_path, manifest_root),
            environ=environment,
            label="context artifact",
        )

        if output_dir.exists():
            shutil.rmtree(output_dir)
        os.replace(stage, output_dir)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    byte_count = sum(int(item["bytes"]) for item in files)
    return ContextArtifactResult(
        output_dir=output_dir,
        metadata_path=output_dir / metadata_path.relative_to(stage),
        manifest_path=output_dir / portable_manifest.relative_to(stage),
        repository=slug,
        commit=manifest.commit,
        views=selected,
        file_count=len(files),
        byte_count=byte_count,
    )


__all__ = [
    "CONTEXT_ARTIFACT_MANIFEST",
    "CONTEXT_ARTIFACT_SCHEMA",
    "PORTABLE_CONTEXT_VIEWS",
    "ContextArtifactResult",
    "stage_context_artifact",
]
