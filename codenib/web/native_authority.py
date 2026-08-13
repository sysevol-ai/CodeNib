# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Trusted local boundary for native vector views served by the Web runtime."""

from __future__ import annotations

import os
from pathlib import Path

from ..artifacts.runtime import (
    SourceBindingCleanupOwner,
    _raise_source_cleanup_failure,
    _source_cleanup_owner_is_pending,
)
from ..compiler.cache_lock import COMPILER_CACHE_LOCK_FILENAME, compiler_cache_lock
from ..compiler.manifest import IndexEntry, RepoManifest
from ..index.embedding.artifact_integrity import capture_authenticated_vector_view
from ..native_index_authorization import (
    NativeIndexAuthorization,
    _mint_trusted_local_admin_authorization,
)
from ..source_fingerprint import (
    capture_repository_source,
    is_secure_source_fingerprint_v2,
    lexical_repository_path,
)
from .config import RepoEntry


def _absolute_manifest_path(value: str) -> Path:
    lexical = Path(os.path.abspath(os.fspath(Path(value).expanduser())))
    try:
        resolved = lexical.resolve(strict=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"manifest not found: {lexical}") from exc
    if not resolved.is_file():
        raise FileNotFoundError(f"manifest not found: {lexical}")
    return resolved


def _require_same_vector_manifest(
    expected: RepoManifest,
    observed: RepoManifest,
    expected_vector: IndexEntry,
) -> None:
    """Reject a manifest or vector entry that changed around authorization."""

    observed_vector = observed.indexes.get("vector")
    if (
        observed.repo_path != expected.repo_path
        or observed.commit != expected.commit
        or observed.source_fingerprint != expected.source_fingerprint
        or observed.file_count != expected.file_count
        or observed_vector != expected_vector
        or not observed.index_is_current("vector")
    ):
        raise ValueError("vector manifest changed before native authorization")


def _manifest_source_exclusions(
    repo_path: Path,
    manifest_path: Path,
) -> tuple[Path, ...]:
    """Exclude a nested cache directory, or only a root-level manifest file."""

    manifest_parent = manifest_path.parent
    try:
        manifest_parent.relative_to(repo_path)
    except ValueError:
        return ()
    if manifest_parent == repo_path:
        return (
            manifest_path,
            manifest_parent / COMPILER_CACHE_LOCK_FILENAME,
        )
    return (manifest_parent,)


def _raise_after_source_cleanup(
    owner: SourceBindingCleanupOwner,
    primary: BaseException,
) -> None:
    """Close retained source authority without hiding the first failure."""

    cleanup_failure: BaseException | None = None
    try:
        owner.close()
    except BaseException as exc:  # noqa: B036 - shared priority decides
        cleanup_failure = exc
    pending_owner = owner if _source_cleanup_owner_is_pending(owner) else None
    nested_owner = getattr(primary, "source_cleanup_owner", None)
    if pending_owner is None and _source_cleanup_owner_is_pending(nested_owner):
        pending_owner = nested_owner
    _raise_source_cleanup_failure(primary, cleanup_failure, pending_owner)


def authorize_local_manifest_vector(
    repo_entry: RepoEntry,
    manifest: RepoManifest,
    vector_entry: IndexEntry,
) -> NativeIndexAuthorization:
    """Authorize one exact local vector tree after source-bound admin checks.

    The Web registry is only a consumer and cannot mint from an artifact path.
    Its production entry points inject this resolver as the trusted boundary.
    The resolver serializes against the compiler, verifies source fingerprint
    v2 through retained authority, captures the exact vector tree, and binds
    the capability to the unmodified manifest config. The later vector loader
    independently recaptures the tree and checks the same capability subject.
    """

    recorded_vector = manifest.indexes.get("vector")
    if recorded_vector is not vector_entry or not manifest.index_is_current("vector"):
        raise ValueError("resolver did not receive the current manifest vector entry")
    if not is_secure_source_fingerprint_v2(manifest.source_fingerprint):
        raise ValueError("native vector authorization requires source fingerprint v2")

    repo_path = lexical_repository_path(repo_entry.repo_dir)
    manifest_repo_path = lexical_repository_path(manifest.repo_path)
    if repo_path != manifest_repo_path:
        raise ValueError("registry repository path does not match the vector manifest")
    if (
        repo_entry.base_commit
        and manifest.commit
        and repo_entry.base_commit != manifest.commit
    ):
        raise ValueError("registry commit does not match the vector manifest")

    manifest_path = _absolute_manifest_path(repo_entry.manifest_path)

    cleanup_owner: SourceBindingCleanupOwner | None = None
    authorization: NativeIndexAuthorization | None = None
    with compiler_cache_lock(manifest_path.parent):
        observed_manifest = RepoManifest.load(str(manifest_path))
        _require_same_vector_manifest(manifest, observed_manifest, vector_entry)

        cleanup_owner = SourceBindingCleanupOwner()
        try:
            source_binding = capture_repository_source(
                repo_path,
                exclude_roots=_manifest_source_exclusions(repo_path, manifest_path),
                _source_owner=cleanup_owner.retain,
            )
            if (
                source_binding.fingerprint != manifest.source_fingerprint
                or source_binding.file_count != manifest.file_count
            ):
                raise ValueError(
                    "repository source content does not match the vector manifest"
                )

            with capture_authenticated_vector_view(vector_entry.path) as vector_view:
                authorization = _mint_trusted_local_admin_authorization(
                    vector_view.ownership,
                    view_type="vector",
                    semantic_contract=dict(vector_entry.config or {}),
                    evidence=(
                        "web-local-admin-manifest-intent",
                        "source-content-fingerprint-v2-bound",
                        "captured-vector-tree-subject",
                    ),
                )
                vector_view.verify_final()

            source_binding.verify_snapshot()
            final_manifest = RepoManifest.load(str(manifest_path))
            _require_same_vector_manifest(manifest, final_manifest, vector_entry)
        except BaseException as primary:  # noqa: B036 - preserve verification fault
            _raise_after_source_cleanup(cleanup_owner, primary)

        cleanup_failure: BaseException | None = None
        try:
            cleanup_owner.close()
        except BaseException as exc:  # noqa: B036 - retryable owner
            cleanup_failure = exc
        pending_owner = (
            cleanup_owner if _source_cleanup_owner_is_pending(cleanup_owner) else None
        )
        if cleanup_failure is not None:
            _raise_source_cleanup_failure(
                cleanup_failure,
                None,
                pending_owner,
            )
        if pending_owner is not None:
            _raise_source_cleanup_failure(
                RuntimeError("repository source cleanup did not finish"),
                None,
                pending_owner,
            )

    if authorization is None:  # pragma: no cover - successful path always mints
        raise AssertionError("native vector authorization was not minted")
    return authorization


__all__ = ["authorize_local_manifest_vector"]
