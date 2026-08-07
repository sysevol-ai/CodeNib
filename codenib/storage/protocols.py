# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Minimal replaceable interfaces for catalog and object-store backends."""

from __future__ import annotations

from pathlib import Path
from typing import Any, BinaryIO, Mapping, Protocol, Sequence, runtime_checkable

from .cas import BlobInfo


@runtime_checkable
class ObjectStore(Protocol):
    """Immutable byte-object operations required by artifact publication."""

    def put_bytes(self, data: bytes) -> BlobInfo: ...

    def put_file(self, source: str | Path) -> BlobInfo: ...

    def has(self, digest: str) -> bool: ...

    def open(self, digest: str) -> BinaryIO: ...

    def read_bytes(self, digest: str) -> bytes: ...

    def verify(self, digest: str) -> BlobInfo: ...

    def materialize(self, digest: str, destination: str | Path) -> Path: ...


@runtime_checkable
class IndexCatalog(Protocol):
    """Transactional identities and publication, excluding query engines.

    Object registration is a metadata operation, not a byte upload.  A
    coordinator must first obtain and verify the corresponding ``ObjectStore``
    receipt and must compare digest, size, and storage key before publication.
    """

    def create_namespace(self, name: str) -> str: ...

    def create_repository(
        self,
        repository_key: str,
        *,
        namespace_id: str,
    ) -> str: ...

    def create_source_revision(
        self,
        repository_id: str,
        *,
        commit_sha: str | None = None,
        tree_sha: str | None = None,
        dirty: bool = False,
        source_fingerprint: str | None = None,
    ) -> str: ...

    def create_view_profile(
        self,
        view_type: str,
        config: Mapping[str, Any] | None = None,
        *,
        name: str = "default",
    ) -> str: ...

    def register_object(
        self,
        digest: str,
        *,
        storage_key: str,
        byte_size: int,
        media_type: str = "application/octet-stream",
    ) -> str: ...

    def stage_view_generation(
        self,
        repository_id: str,
        source_revision_id: str,
        profile_id: str,
        view_type: str,
        object_digest: str,
        *,
        schema_version: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> str: ...

    def publish_snapshot(
        self,
        repository_id: str,
        source_revision_id: str,
        view_generation_ids: Sequence[str],
        *,
        ref_name: str = "main",
        expected_generation: int = 0,
    ) -> dict[str, Any]: ...

    def resolve_ref(
        self,
        repository_id: str,
        ref_name: str = "main",
    ) -> dict[str, Any]: ...

    def get_manifest_summary(self, snapshot_id: str) -> dict[str, Any]: ...


__all__ = ["IndexCatalog", "ObjectStore"]
