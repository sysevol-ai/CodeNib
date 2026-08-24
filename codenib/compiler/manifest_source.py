# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bind live repository reads to a manifest's exact source-selection era."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from .. import source_fingerprint as source_fingerprint_module
from ..repository_filters import REPOSITORY_FILTER_POLICY_VERSION
from ..repository_source_selection import RepositorySourceSelection
from ..source_fingerprint import (
    RepositorySourceBinding,
    RepositorySourceIdentitySnapshot,
    RepositorySourceRootAuthority,
    SourceFingerprint,
)
from .manifest import (
    LEGACY_MANIFEST_VERSION,
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    RepoManifest,
)

_LEGACY_MANIFEST_REPOSITORY_FILTER_POLICY_VERSION = 3


def repository_source_selection_for_manifest(
    manifest: RepoManifest,
) -> RepositorySourceSelection | None:
    selection = manifest.source_selection
    if manifest.version == LEGACY_MANIFEST_VERSION:
        if selection is not None:
            raise ValueError(
                "manifest v1.1 must use its legacy source-selection identity"
            )
        return None
    if manifest.version != MANIFEST_VERSION:
        raise ValueError(f"unsupported manifest version: {manifest.version!r}")
    if type(selection) is not RepositorySourceSelection:
        raise ValueError("manifest v1.2 has no canonical repository source selection")
    return RepositorySourceSelection(selection.exclude_subtrees)


def repository_filter_policy_for_manifest(manifest: RepoManifest) -> int:
    """Return the repository-filter policy embedded by one manifest era."""

    selection = repository_source_selection_for_manifest(manifest)
    if selection is None:
        return _LEGACY_MANIFEST_REPOSITORY_FILTER_POLICY_VERSION
    return REPOSITORY_FILTER_POLICY_VERSION


def resolve_compiler_source_selection(
    cache_dir: str | Path,
    explicit: RepositorySourceSelection | None = None,
) -> RepositorySourceSelection:
    """Resolve an explicit policy or reuse the current persisted manifest policy.

    Public compiler wrappers use ``None`` to mean "reuse". Passing an exact
    empty selection remains the explicit way to clear custom exclusions.
    Legacy manifests migrate to the current empty policy instead of being
    relabeled as if policy v3 and v4 had identical identities.
    """

    if explicit is not None:
        if type(explicit) is not RepositorySourceSelection:
            raise TypeError("source_selection must be a RepositorySourceSelection")
        return RepositorySourceSelection(explicit.exclude_subtrees)
    manifest_path = Path(cache_dir) / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return RepositorySourceSelection()
    manifest = RepoManifest.load(manifest_path)
    selected = repository_source_selection_for_manifest(manifest)
    return (
        RepositorySourceSelection()
        if selected is None
        else RepositorySourceSelection(selected.exclude_subtrees)
    )


def fingerprint_repository_for_manifest(
    root: str | Path,
    manifest: RepoManifest,
    *,
    exclude_roots: Iterable[str | Path] = (),
) -> SourceFingerprint:
    """Recompute the source identity using the manifest's persisted policy."""

    selection = repository_source_selection_for_manifest(manifest)
    if selection is None:
        return source_fingerprint_module._fingerprint_repository_legacy_manifest_v11(
            root,
            exclude_roots=exclude_roots,
        )
    return source_fingerprint_module.fingerprint_repository(
        root,
        exclude_roots=exclude_roots,
        selection=selection,
    )


def capture_repository_source_for_manifest(
    root: str | Path,
    manifest: RepoManifest,
    *,
    exclude_roots: Iterable[str | Path] = (),
    _source_owner: Callable[[object], None] | None = None,
    expected_root_authority: RepositorySourceRootAuthority | None = None,
) -> RepositorySourceBinding:
    """Capture retained reads using the manifest's persisted policy."""

    selection = repository_source_selection_for_manifest(manifest)
    if selection is None:
        return source_fingerprint_module._capture_repository_source_legacy_manifest_v11(
            root,
            exclude_roots=exclude_roots,
            _source_owner=_source_owner,
            expected_root_authority=expected_root_authority,
        )
    return source_fingerprint_module.capture_repository_source(
        root,
        exclude_roots=exclude_roots,
        selection=selection,
        _source_owner=_source_owner,
        expected_root_authority=expected_root_authority,
    )


def require_manifest_source_identity(
    identity: RepositorySourceIdentitySnapshot,
    manifest: RepoManifest,
    *,
    label: str,
    mismatch_message: str | None = None,
) -> None:
    """Require one retained identity to match every manifest source axis."""

    expected_selection = repository_source_selection_for_manifest(manifest)

    def reject(axis: str) -> None:
        if mismatch_message is not None:
            raise ValueError(mismatch_message)
        raise ValueError(f"{label} {axis} does not match the manifest")

    if identity.fingerprint != manifest.source_fingerprint:
        reject("source fingerprint")
    if identity.file_count != manifest.file_count:
        reject("source file count")
    if identity.source_selection != expected_selection:
        reject("source selection")


__all__ = [
    "capture_repository_source_for_manifest",
    "fingerprint_repository_for_manifest",
    "repository_filter_policy_for_manifest",
    "repository_source_selection_for_manifest",
    "resolve_compiler_source_selection",
    "require_manifest_source_identity",
]
