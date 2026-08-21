# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared manifest resolution for external localization-policy clients."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from codenib.compiler.manifest import MANIFEST_FILENAME, RepoManifest
from codenib.compiler.manifest_source import fingerprint_repository_for_manifest
from codenib.paths import legacy_repo_index_dir, repo_index_dir
from codenib.source_fingerprint import (
    is_secure_source_fingerprint_v2,
    lexical_repository_path,
)

ManifestResolver = Callable[[Path], str | Path]


def _git_head(repo: Path) -> str | None:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo}",
                "-C",
                str(repo),
                "rev-parse",
                "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip().lower() or None


def _validate_source_identity(
    repo: Path,
    manifest: RepoManifest,
    *,
    integration_name: str,
) -> None:
    """Reject a manifest that cannot be tied to the current checkout state."""

    expected_commit = manifest.commit.strip().lower()
    head = _git_head(repo)
    if expected_commit and head is not None and head != expected_commit:
        raise ValueError(
            f"{integration_name} checkout revision does not match the CodeNib "
            f"manifest: {head} != {expected_commit}"
        )

    if not is_secure_source_fingerprint_v2(manifest.source_fingerprint):
        raise ValueError(
            f"{integration_name} CodeNib manifest has no secure v2 source "
            "fingerprint; rebuild the index"
        )

    excluded = (repo_index_dir(repo), legacy_repo_index_dir(repo))
    observed = fingerprint_repository_for_manifest(
        repo,
        manifest,
        exclude_roots=excluded,
    ).value
    if observed != manifest.source_fingerprint:
        raise ValueError(
            f"{integration_name} checkout source fingerprint does not match the "
            "CodeNib manifest"
        )


def resolve_checkout_manifest(repo_path: str | Path) -> Path:
    """Resolve the canonical or legacy CodeNib manifest for one checkout."""

    repo = lexical_repository_path(repo_path)
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
    return manifest_path


def select_checkout_manifest(
    repo_path: str | Path,
    *,
    integration_name: str,
    manifest_path: str | Path | None = None,
    manifest_resolver: ManifestResolver | None = None,
) -> Path:
    """Select and validate the manifest bound to an external-policy request."""

    repo = lexical_repository_path(repo_path)
    if manifest_path is not None and manifest_resolver is not None:
        raise ValueError("Specify either manifest_path or manifest_resolver, not both")
    if manifest_path is not None:
        selected = Path(os.path.abspath(os.fspath(Path(manifest_path).expanduser())))
    elif manifest_resolver is not None:
        selected = Path(
            os.path.abspath(os.fspath(Path(manifest_resolver(repo)).expanduser()))
        )
    else:
        selected = resolve_checkout_manifest(repo)

    if not selected.is_file():
        raise FileNotFoundError(f"CodeNib manifest not found: {selected}")
    manifest = RepoManifest.load(selected)
    if manifest.repo_path:
        manifest_repo = lexical_repository_path(manifest.repo_path)
        if manifest_repo != repo:
            raise ValueError(
                f"{integration_name} repo_path does not match the CodeNib manifest: "
                f"{repo} != {manifest_repo}"
            )
    _validate_source_identity(repo, manifest, integration_name=integration_name)
    return selected


__all__ = [
    "ManifestResolver",
    "resolve_checkout_manifest",
    "select_checkout_manifest",
]
