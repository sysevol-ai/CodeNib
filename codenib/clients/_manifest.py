# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared manifest resolution for external localization-policy clients."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from codenib.compiler.manifest import MANIFEST_FILENAME, RepoManifest
from codenib.paths import legacy_repo_index_dir, repo_index_dir

ManifestResolver = Callable[[Path], str | Path]


def resolve_checkout_manifest(repo_path: str | Path) -> Path:
    """Resolve the canonical or legacy CodeNib manifest for one checkout."""

    repo = Path(repo_path).expanduser().resolve()
    candidates = (
        repo_index_dir(repo) / MANIFEST_FILENAME,
        legacy_repo_index_dir(repo) / MANIFEST_FILENAME,
    )
    manifest_path = next(
        (candidate for candidate in candidates if candidate.is_file()),
        candidates[0],
    )
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"CodeNib manifest not found for {repo}: {manifest_path}. "
            "Run `codenib index <repo> --preset graph` first."
        )
    return manifest_path.resolve()


def select_checkout_manifest(
    repo_path: str | Path,
    *,
    integration_name: str,
    manifest_path: str | Path | None = None,
    manifest_resolver: ManifestResolver | None = None,
) -> Path:
    """Select and validate the manifest bound to an external-policy request."""

    repo = Path(repo_path).expanduser().resolve()
    if manifest_path is not None and manifest_resolver is not None:
        raise ValueError("Specify either manifest_path or manifest_resolver, not both")
    if manifest_path is not None:
        selected = Path(manifest_path).expanduser().resolve()
    elif manifest_resolver is not None:
        selected = Path(manifest_resolver(repo)).expanduser().resolve()
    else:
        selected = resolve_checkout_manifest(repo)

    if not selected.is_file():
        raise FileNotFoundError(f"CodeNib manifest not found: {selected}")
    manifest = RepoManifest.load(selected)
    if manifest.repo_path:
        manifest_repo = Path(manifest.repo_path).expanduser().resolve()
        if manifest_repo != repo:
            raise ValueError(
                f"{integration_name} repo_path does not match the CodeNib manifest: "
                f"{repo} != {manifest_repo}"
            )
    return selected


__all__ = [
    "ManifestResolver",
    "resolve_checkout_manifest",
    "select_checkout_manifest",
]
