# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Validate that a repository checkout matches a compiled manifest."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..source_fingerprint import (
    fingerprint_repository,
    is_secure_source_fingerprint_v2,
    lexical_repository_path,
)
from .manifest import RepoManifest


def checkout_commit(repo_path: Path) -> str | None:
    """Return the checkout's current commit when the directory is in Git."""

    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def validate_checkout_identity(
    repo_path: Path,
    manifest: RepoManifest,
    *,
    artifact_root: Path,
) -> None:
    """Reject source or commit drift between a checkout and its manifest."""

    repo_path = lexical_repository_path(repo_path)
    artifact_root = lexical_repository_path(artifact_root)
    excluded_roots = (
        (artifact_root,)
        if artifact_root != repo_path and repo_path in artifact_root.parents
        else ()
    )
    expected_source = (manifest.source_fingerprint or "").strip()
    if not is_secure_source_fingerprint_v2(expected_source):
        raise ValueError(
            "repository manifest uses a legacy or unsupported source fingerprint "
            "(including a missing value). Rebuild the index before serving or "
            "publishing context."
        )
    actual_source = fingerprint_repository(
        repo_path,
        exclude_roots=excluded_roots,
    ).value
    if actual_source != expected_source:
        raise ValueError(
            "repository source files do not match the indexed content. "
            "Rebuild the index for the current working tree before serving "
            "or publishing context."
        )

    expected = (manifest.commit or "").strip()
    actual = checkout_commit(repo_path)
    if not expected or not actual:
        return
    if actual.startswith(expected) or expected.startswith(actual):
        return
    raise ValueError(
        "repository checkout does not match the indexed snapshot: "
        f"HEAD is {actual[:12]}, manifest is {expected[:12]}. "
        "Rebuild the index or check out the manifest commit before serving "
        "or publishing context."
    )


__all__ = ["checkout_commit", "validate_checkout_identity"]
