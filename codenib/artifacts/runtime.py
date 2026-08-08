# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Verify and bind portable context artifacts for query-only runtimes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .._contained_source import validate_repository_file
from ..compiler.checkout_identity import checkout_commit
from ..compiler.manifest import MANIFEST_FILENAME, MANIFEST_VERSION, RepoManifest
from ..compiler.snapshot_store import normalize_repo
from ..source_fingerprint import fingerprint_repository
from .context import (
    CONTEXT_ARTIFACT_MANIFEST,
    CONTEXT_ARTIFACT_SCHEMA,
    PORTABLE_CONTEXT_VIEWS,
)
from .security import file_sha256

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPOSITORY_RE = re.compile(r"^[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*$")
_DEFAULT_MAX_FILES = 100_000
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024 * 1024
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_DOCUMENTS_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class VerifiedContextArtifact:
    """Integrity-checked, still-unbound portable context artifact."""

    root: Path
    metadata_path: Path
    manifest_path: Path
    metadata: Mapping[str, Any]
    manifest: RepoManifest
    repository: str
    commit: str
    source_fingerprint: str
    views: tuple[str, ...]
    source_paths: tuple[str, ...]
    file_count: int
    byte_count: int


@dataclass(frozen=True, slots=True)
class ContextArtifactBinding:
    """Verified artifact rebound in memory to one exact source checkout."""

    artifact: VerifiedContextArtifact
    repo_path: Path
    manifest: RepoManifest


def _load_json_object(path: Path, *, label: str, max_bytes: int) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"{label} is not readable: {path}") from exc
    if size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _relative_path(value: object, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError(f"{label} must be a normalized relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{label} must be a normalized relative POSIX path")
    return path


def _artifact_path(root: Path, value: object, *, label: str) -> Path:
    relative = _relative_path(value, label=label)
    path = root.joinpath(*relative.parts)
    resolved = path.resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError(f"{label} escapes the context artifact")
    return resolved


def _actual_files(root: Path, *, max_files: int) -> set[str]:
    files: set[str] = set()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"context artifact contains a symbolic link: {relative}")
        if path.is_file():
            files.add(relative)
            # The metadata file is intentionally outside its own inventory.
            if len(files) > max_files + 1:
                raise ValueError(
                    f"context artifact contains more than {max_files} inventoried files"
                )
        elif not path.is_dir():
            raise ValueError(f"context artifact contains a special file: {relative}")
    return files


def _inventory(
    metadata: Mapping[str, Any],
    *,
    max_files: int,
    max_bytes: int,
) -> tuple[dict[str, tuple[int, str]], int]:
    raw_files = metadata.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("context artifact inventory must be a non-empty list")
    if len(raw_files) > max_files:
        raise ValueError(f"context artifact inventory exceeds {max_files} files")

    result: dict[str, tuple[int, str]] = {}
    total_bytes = 0
    for index, record in enumerate(raw_files):
        if not isinstance(record, dict):
            raise ValueError(f"context artifact file record {index} must be an object")
        relative = _relative_path(
            record.get("path"),
            label=f"context artifact file record {index} path",
        ).as_posix()
        if relative == CONTEXT_ARTIFACT_MANIFEST:
            raise ValueError("context artifact metadata cannot inventory itself")
        if relative in result:
            raise ValueError(f"duplicate context artifact inventory path: {relative}")
        size = record.get("bytes")
        digest = record.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError(f"invalid byte size for context artifact file: {relative}")
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise ValueError(f"invalid SHA-256 for context artifact file: {relative}")
        if PurePosixPath(relative).suffix.lower() in {".pickle", ".pkl"}:
            raise ValueError(
                f"portable context artifacts must not contain pickle: {relative}"
            )
        total_bytes += size
        if total_bytes > max_bytes:
            raise ValueError(f"context artifact exceeds {max_bytes} inventoried bytes")
        result[relative] = (size, digest)
    return result, total_bytes


def _repository_identity(metadata: Mapping[str, Any]) -> tuple[str, str, str]:
    repository = metadata.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("context artifact repository identity must be an object")
    slug = repository.get("slug")
    commit = repository.get("commit")
    source_fingerprint = repository.get("source_fingerprint")
    if not isinstance(slug, str):
        raise ValueError("context artifact repository slug must be a string")
    try:
        normalized_slug = normalize_repo(slug)
    except ValueError as exc:
        raise ValueError("context artifact repository slug is invalid") from exc
    if slug != normalized_slug or not _REPOSITORY_RE.fullmatch(slug):
        raise ValueError("context artifact repository slug is not canonical")
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise ValueError("context artifact commit must be a full lowercase Git SHA")
    if not isinstance(source_fingerprint, str) or not _SOURCE_FINGERPRINT_RE.fullmatch(
        source_fingerprint
    ):
        raise ValueError("context artifact source fingerprint is invalid")
    return slug, commit, source_fingerprint


def _artifact_views(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    raw_views = metadata.get("views")
    if not isinstance(raw_views, list) or not raw_views:
        raise ValueError("context artifact views must be a non-empty list")
    if not all(isinstance(view, str) for view in raw_views):
        raise ValueError("context artifact view names must be strings")
    views = tuple(raw_views)
    if len(set(views)) != len(views):
        raise ValueError("context artifact view names must be unique")
    unsupported = sorted(set(views) - PORTABLE_CONTEXT_VIEWS)
    if unsupported:
        raise ValueError(
            "context artifact contains unsupported portable views: "
            + ", ".join(unsupported)
        )
    return views


def _source_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError(f"{label} must be a repository-relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{label} must be a repository-relative POSIX path")
    return path.as_posix()


def _document_source_paths(
    path: Path,
    *,
    label: str,
    max_bytes: int,
) -> set[str]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"{label} is not readable: {path}") from exc
    if size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes: {path}")
    try:
        documents = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(documents, list):
        raise ValueError(f"{label} must be a JSON list: {path}")

    result: set[str] = set()
    for index, document in enumerate(documents):
        if not isinstance(document, dict):
            raise ValueError(f"{label} document {index} must be an object")
        page_content = document.get("page_content")
        metadata = document.get("metadata")
        if not isinstance(page_content, str) or not isinstance(metadata, dict):
            raise ValueError(
                f"{label} document {index} has invalid content or metadata"
            )
        raw_file = metadata.get("file")
        if raw_file is not None and raw_file != "":
            result.add(
                _source_path(
                    raw_file,
                    label=f"{label} document {index} file",
                )
            )
        for field in ("start_line", "end_line"):
            value = metadata.get(field)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{label} document {index} has invalid {field}")
    return result


def _validate_view_payloads(
    root: Path,
    inventory: Mapping[str, tuple[int, str]],
    *,
    views: tuple[str, ...],
) -> tuple[str, ...]:
    source_paths: set[str] = set()
    inventory_paths = set(inventory)

    if "bm25" in views:
        metadata_relative = "views/bm25/bm25_metadata.json"
        documents_relative = "views/bm25/documents.json"
        required = {metadata_relative, documents_relative}
        if not required <= inventory_paths:
            raise ValueError("portable BM25 view is missing its serving files")
        metadata = _load_json_object(
            root / metadata_relative,
            label="portable BM25 metadata",
            max_bytes=_MAX_METADATA_BYTES,
        )
        if metadata.get("project_root") != "source":
            raise ValueError("portable BM25 project root must be 'source'")
        source_paths.update(
            _document_source_paths(
                root / documents_relative,
                label="portable BM25 documents",
                max_bytes=_MAX_DOCUMENTS_BYTES,
            )
        )

    if "vector" in views:
        document_paths = sorted(
            relative
            for relative in inventory_paths
            if PurePosixPath(relative).parent.name in {"l0", "l2"}
            and PurePosixPath(relative).parent.parent.as_posix() == "views/vector"
            and PurePosixPath(relative).name.startswith("documents_")
            and PurePosixPath(relative).suffix == ".json"
        )
        if not document_paths:
            raise ValueError("portable vector view has no JSON document store")
        for relative in document_paths:
            path = PurePosixPath(relative)
            suffix = path.name.removeprefix("documents_").removesuffix(".json")
            index_relative = (path.parent / f"index_{suffix}.faiss").as_posix()
            if not suffix or index_relative not in inventory_paths:
                raise ValueError(
                    f"portable vector documents have no matching FAISS index: {relative}"
                )
            source_paths.update(
                _document_source_paths(
                    root.joinpath(*path.parts),
                    label=f"portable vector documents {relative}",
                    max_bytes=_MAX_DOCUMENTS_BYTES,
                )
            )
    return tuple(sorted(source_paths))


def _validate_manifest(
    root: Path,
    metadata: Mapping[str, Any],
    *,
    commit: str,
    source_fingerprint: str,
    views: tuple[str, ...],
    inventoried_paths: set[str],
) -> tuple[Path, RepoManifest]:
    manifest_record = metadata.get("manifest")
    if not isinstance(manifest_record, dict):
        raise ValueError("context artifact manifest descriptor must be an object")
    if manifest_record.get("repository_path") != "source":
        raise ValueError("context artifact manifest repository path must be 'source'")
    if manifest_record.get("paths") != "artifact-relative-posix":
        raise ValueError("context artifact manifest path contract is unsupported")
    if manifest_record.get("path") != MANIFEST_FILENAME:
        raise ValueError("context artifact manifest filename is unsupported")
    manifest_path = _artifact_path(
        root,
        manifest_record.get("path"),
        label="context artifact manifest path",
    )
    manifest_relative = manifest_path.relative_to(root).as_posix()
    if manifest_relative not in inventoried_paths:
        raise ValueError("context artifact manifest is absent from its inventory")
    try:
        manifest = RepoManifest.load(manifest_path)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ValueError("context artifact repository manifest is invalid") from exc
    if manifest.version != MANIFEST_VERSION:
        raise ValueError(
            "context artifact repository manifest version is incompatible: "
            f"expected {MANIFEST_VERSION}, found {manifest.version}"
        )
    if manifest.repo_path != "source":
        raise ValueError("portable repository manifest path must be 'source'")
    if (
        not isinstance(manifest.file_count, int)
        or isinstance(manifest.file_count, bool)
        or manifest.file_count < 0
    ):
        raise ValueError("context artifact repository file count is invalid")
    if manifest.commit != commit or manifest.source_fingerprint != source_fingerprint:
        raise ValueError("context artifact and repository manifest identities differ")
    if set(manifest.indexes) != set(views):
        raise ValueError(
            "context artifact view list differs from its repository manifest"
        )

    metadata_capabilities = metadata.get("capabilities")
    if (
        not isinstance(metadata_capabilities, dict)
        or not all(isinstance(value, bool) for value in metadata_capabilities.values())
        or metadata_capabilities != manifest.capabilities
    ):
        raise ValueError("context artifact capability records differ")
    metadata_languages = metadata.get("repository", {}).get("languages")
    if (
        not isinstance(metadata_languages, list)
        or not all(isinstance(language, str) for language in metadata_languages)
        or metadata_languages != manifest.languages
    ):
        raise ValueError("context artifact language records differ")
    source_locations = metadata.get("source_locations")
    if not isinstance(source_locations, dict) or source_locations != {
        "path": "repository-relative-posix",
        "line_base": 1,
        "end_line": "inclusive",
        "commit": commit,
    }:
        raise ValueError("context artifact source-location contract is unsupported")
    builder = metadata.get("builder")
    if (
        not isinstance(builder, dict)
        or builder.get("manifest_version") != MANIFEST_VERSION
        or not isinstance(builder.get("codenib_version"), str)
        or not isinstance(builder.get("compiled_at"), str)
    ):
        raise ValueError("context artifact builder identity is invalid")

    for view in views:
        entry = manifest.indexes[view]
        if not isinstance(entry.config, dict) or not isinstance(entry.metadata, dict):
            raise ValueError(f"context artifact view metadata is invalid: {view}")
        if not manifest.index_is_current(view):
            raise ValueError(f"context artifact view is not current: {view}")
        if entry.index_type != view:
            raise ValueError(f"context artifact view type differs from its key: {view}")
        expected_view_path = f"views/{view}"
        if entry.path != expected_view_path:
            raise ValueError(
                f"context artifact {view} view path must be {expected_view_path!r}"
            )
        view_path = _artifact_path(
            root,
            entry.path,
            label=f"context artifact {view} view path",
        )
        if not view_path.is_dir():
            raise ValueError(f"context artifact view is missing: {view}")
        view_relative = view_path.relative_to(root).as_posix()
        if not any(
            relative == view_relative or relative.startswith(f"{view_relative}/")
            for relative in inventoried_paths
        ):
            raise ValueError(f"context artifact view has no inventoried files: {view}")
        if view == "vector" and entry.config.get("portable_document_format") != (
            "codenib.vector-documents.v1"
        ):
            raise ValueError("portable vector document format is unsupported")
    return manifest_path, manifest


def verify_context_artifact(
    root: str | Path,
    *,
    expected_repository: str | None = None,
    expected_commit: str | None = None,
    max_files: int = _DEFAULT_MAX_FILES,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> VerifiedContextArtifact:
    """Verify an artifact completely without opening any persisted index."""

    candidate = Path(root).expanduser()
    if candidate.is_symlink():
        raise ValueError(
            f"context artifact root must not be a symbolic link: {candidate}"
        )
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise ValueError(f"context artifact directory does not exist: {resolved}")
    metadata_path = resolved / CONTEXT_ARTIFACT_MANIFEST
    if metadata_path.is_symlink() or not metadata_path.is_file():
        raise ValueError(f"context artifact metadata is missing: {metadata_path}")
    metadata = _load_json_object(
        metadata_path,
        label="context artifact metadata",
        max_bytes=_MAX_METADATA_BYTES,
    )
    if metadata.get("schema") != CONTEXT_ARTIFACT_SCHEMA:
        raise ValueError(
            "context artifact schema is incompatible: "
            f"expected {CONTEXT_ARTIFACT_SCHEMA}, found {metadata.get('schema')!r}"
        )
    repository, commit, source_fingerprint = _repository_identity(metadata)
    views = _artifact_views(metadata)
    inventory, byte_count = _inventory(
        metadata,
        max_files=max_files,
        max_bytes=max_bytes,
    )

    expected_files = set(inventory) | {CONTEXT_ARTIFACT_MANIFEST}
    actual_files = _actual_files(resolved, max_files=max_files)
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise ValueError(
            "context artifact file set differs from its inventory: "
            f"missing={missing}, extra={extra}"
        )
    for relative, (expected_size, expected_digest) in inventory.items():
        path = _artifact_path(
            resolved,
            relative,
            label=f"context artifact inventory path {relative!r}",
        )
        size, digest = file_sha256(path)
        if size != expected_size or digest != expected_digest:
            raise ValueError(f"context artifact file digest mismatch: {relative}")

    manifest_path, manifest = _validate_manifest(
        resolved,
        metadata,
        commit=commit,
        source_fingerprint=source_fingerprint,
        views=views,
        inventoried_paths=set(inventory),
    )
    source_paths = _validate_view_payloads(
        resolved,
        inventory,
        views=views,
    )
    if expected_repository is not None:
        expected = normalize_repo(expected_repository)
        if repository != expected:
            raise ValueError(
                "context artifact repository mismatch: "
                f"expected {expected}, found {repository}"
            )
    if expected_commit is not None:
        expected = expected_commit.strip().lower()
        if not _COMMIT_RE.fullmatch(expected):
            raise ValueError("expected context artifact commit must be a full Git SHA")
        if commit != expected:
            raise ValueError(
                "context artifact commit mismatch: "
                f"expected {expected[:12]}, found {commit[:12]}"
            )

    return VerifiedContextArtifact(
        root=resolved,
        metadata_path=metadata_path,
        manifest_path=manifest_path,
        metadata=metadata,
        manifest=manifest,
        repository=repository,
        commit=commit,
        source_fingerprint=source_fingerprint,
        views=views,
        source_paths=source_paths,
        file_count=len(inventory),
        byte_count=byte_count,
    )


def bind_context_artifact(
    root: str | Path,
    repo_path: str | Path,
    *,
    expected_repository: str | None = None,
    expected_commit: str | None = None,
) -> ContextArtifactBinding:
    """Verify an artifact and bind it to an exact, unchanged checkout."""

    artifact = verify_context_artifact(
        root,
        expected_repository=expected_repository,
        expected_commit=expected_commit,
    )
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"repository checkout does not exist: {repo}")
    actual_commit = checkout_commit(repo)
    if actual_commit != artifact.commit:
        actual_label = actual_commit[:12] if actual_commit else "not-a-git-checkout"
        raise ValueError(
            "repository checkout commit does not match the context artifact: "
            f"checkout={actual_label}, artifact={artifact.commit[:12]}"
        )
    source = fingerprint_repository(
        repo,
        exclude_roots=(artifact.root,),
    )
    if source.value != artifact.source_fingerprint:
        raise ValueError(
            "repository source files do not match the context artifact fingerprint"
        )
    if source.file_count != artifact.manifest.file_count:
        raise ValueError(
            "repository file count does not match the context artifact manifest"
        )
    for relative in artifact.source_paths:
        try:
            validate_repository_file(repo, relative)
        except ValueError as exc:
            raise ValueError(
                "context artifact source path is not a stable file inside "
                f"the repository checkout: {relative}"
            ) from exc

    manifest_data = artifact.manifest.to_dict()
    manifest_data["repo"]["path"] = str(repo)
    for view, entry in manifest_data["indexes"].items():
        entry["path"] = str(
            _artifact_path(
                artifact.root,
                entry["path"],
                label=f"context artifact {view} view path",
            )
        )
    manifest = RepoManifest.from_dict(manifest_data)
    return ContextArtifactBinding(
        artifact=artifact,
        repo_path=repo,
        manifest=manifest,
    )


__all__ = [
    "ContextArtifactBinding",
    "VerifiedContextArtifact",
    "bind_context_artifact",
    "verify_context_artifact",
]
