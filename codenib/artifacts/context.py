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

from .._atomic_directory import (
    capture_directory_ownership,
    discard_owned_directory,
    lexical_directory_path,
    publish_staged_directory,
)
from .._version import package_version
from ..compiler.artifact_fingerprints import require_bm25_manifest_artifact
from ..compiler.checkout_identity import validate_checkout_identity
from ..compiler.manifest import MANIFEST_FILENAME, RepoManifest
from ..compiler.snapshot_store import normalize_repo
from ..index.embedding.artifact_integrity import (
    require_complete_vector_view,
    validate_vector_config_artifact,
)
from ..provider_routes import resolve_embedding_artifact_route
from .portable_views import normalize_owned_query_view
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


def _validated_output(
    repo_path: Path,
    manifest_root: Path,
    output_dir: Path,
) -> tuple[Path, object | None]:
    output = lexical_directory_path(output_dir)
    if output.is_symlink():
        raise ValueError(
            f"context artifact output must not be a symbolic link: {output}"
        )
    for source, label in (
        (repo_path.resolve(), "repository"),
        (manifest_root.resolve(), "index root"),
    ):
        if output == source or source in output.parents or output in source.parents:
            raise ValueError(f"context artifact output overlaps the {label}: {output}")
    if output.exists() and not output.is_dir():
        raise ValueError(f"context artifact output is not a directory: {output}")
    try:
        expected_ownership = (
            capture_directory_ownership(
                output,
                required_root_file=CONTEXT_ARTIFACT_MANIFEST,
                allow_empty_root=True,
            )
            if output.exists()
            else None
        )
    except RuntimeError as exc:
        raise ValueError(
            "refusing to replace a non-empty directory that is not a CodeNib "
            f"context artifact: {output}"
        ) from exc
    return output, expected_ownership


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
        selected = sorted(available & PORTABLE_CONTEXT_VIEWS)
    else:
        selected = list(dict.fromkeys(str(name).strip() for name in requested))
        missing = sorted(name for name in selected if name not in available)
        if missing:
            raise ValueError(
                "context artifact requires current views: " + ", ".join(missing)
            )
    if not selected:
        raise ValueError(
            "context artifact requires at least one current view that is portable "
            "(bm25 or vector)"
        )
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
    output_dir, expected_output_ownership = _validated_output(
        repo_path,
        manifest_root,
        output_dir,
    )
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
    stage = lexical_directory_path(
        Path(
            tempfile.mkdtemp(
                prefix=f".{output_dir.name}.tmp-",
                dir=str(output_dir.parent),
            )
        )
    )
    stage_root_ownership = capture_directory_ownership(stage)
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
                require_complete_vector_view(source)
                expected_config = entry.config.get("persistence_config_fingerprint")
                if expected_config is not None:
                    validate_vector_config_artifact(
                        source,
                        route.model.replace("/", "__"),
                        expected_config,
                    )
            elif view == "bm25":
                # Validate the committed source generation before copying and
                # rewriting its machine-local metadata. Otherwise staging
                # could bless a torn or tampered source with fresh hashes.
                require_bm25_manifest_artifact(entry)
            relative = _copy_view(source, stage, view)
            adjustments = normalize_owned_query_view(
                stage.joinpath(*PurePosixPath(relative).parts),
                repo_path=repo_path,
                view_type=view,
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

        expected_metadata_bytes = _json_bytes(metadata)

        def validate_artifact(candidate: Path) -> None:
            assert_publishable_tree(
                candidate,
                forbidden_paths=(repo_path, manifest_root),
                environ=environment,
                label="context artifact",
            )
            candidate_metadata = candidate / CONTEXT_ARTIFACT_MANIFEST
            actual_files = [
                record
                for record in _inventory(candidate)
                if record["path"] != CONTEXT_ARTIFACT_MANIFEST
            ]
            if (
                not candidate_metadata.is_file()
                or candidate_metadata.read_bytes() != expected_metadata_bytes
                or actual_files != files
            ):
                raise ValueError(
                    "published context artifact differs from its staged identity"
                )

        publish_staged_directory(
            stage,
            output_dir,
            expected_stage_root_ownership=stage_root_ownership,
            expected_destination_ownership=expected_output_ownership,
            validate_staged_directory=validate_artifact,
            validate_published_destination=validate_artifact,
        )
    except BaseException:
        discard_owned_directory(stage, stage_root_ownership)
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
