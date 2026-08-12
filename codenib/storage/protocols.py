# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Minimal replaceable interfaces for catalog and object-store backends."""

from __future__ import annotations

from pathlib import Path
from typing import Any, BinaryIO, Mapping, Protocol, Sequence, runtime_checkable

from .cas import BlobInfo
from .models import IndexJobCompletion, IndexJobRecord, IndexJobViewRecord, RefJobLease


@runtime_checkable
class ObjectStore(Protocol):
    """Immutable byte-object operations required by artifact publication.

    ``BlobInfo`` is an immutable identity receipt, not a retention pin.  A
    receipt says which bytes were durably observed at one completed operation;
    it does not promise that a future GC cannot remove an unleased object.
    Consumers that require exact receipt revalidation can additionally require
    :class:`ReceiptVerifyingObjectStore` without breaking existing object-store
    implementations.  A separate ``open`` after verification is not pinned by
    that earlier result.  This protocol gains a longer lifetime only with an
    explicit pin or lease.
    """

    def put_bytes(self, data: bytes) -> BlobInfo: ...

    def put_file(self, source: str | Path) -> BlobInfo: ...

    def has(self, digest: str) -> bool: ...

    def open(self, digest: str) -> BinaryIO:
        """Open an unverified stream whose bytes the caller must authenticate."""

        ...

    def read_bytes(self, digest: str) -> bytes:
        """Return bytes authenticated against the digest during this read."""

        ...

    def verify(self, digest: str) -> BlobInfo: ...

    def materialize(self, digest: str, destination: str | Path) -> Path:
        """Materialize bytes authenticated during the copy; return a locator."""

        ...


@runtime_checkable
class ReceiptVerifyingObjectStore(ObjectStore, Protocol):
    """Additive capability for exact point-in-time receipt revalidation."""

    def verify_receipt(self, expected: BlobInfo) -> BlobInfo:
        """Revalidate one exact digest/size/storage-key receipt."""

        ...


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
        member_object_digests: Sequence[str] = (),
    ) -> str: ...

    def publish_snapshot(
        self,
        repository_id: str,
        source_revision_id: str,
        view_generation_ids: Sequence[str],
        *,
        ref_name: str = "main",
        expected_generation: int = 0,
    ) -> dict[str, Any]:
        """Publish a desired snapshot or return its unchanged current ref.

        Implementations return ``changed=False`` without advancing the ref when
        it already targets the fully validated desired snapshot.  Otherwise,
        ``expected_generation`` is a compare-and-swap precondition and a
        successful advance returns ``changed=True``.
        """

        ...

    def resolve_ref(
        self,
        repository_id: str,
        ref_name: str = "main",
    ) -> dict[str, Any]: ...

    def get_manifest_summary(self, snapshot_id: str) -> dict[str, Any]:
        """Return a ready snapshot with namespace/repository identity closure."""

        ...


@runtime_checkable
class JobCatalog(Protocol):
    """Durable index-job coordination, separate from snapshot publication."""

    def create_job(
        self,
        repository_id: str,
        source_revision_id: str,
        idempotency_key: str,
        request: Mapping[str, Any],
        *,
        ref_name: str = "main",
        expected_ref_generation: int = 0,
        max_attempts: int = 3,
    ) -> IndexJobRecord: ...

    def get_job(self, job_id: str) -> IndexJobRecord: ...

    def get_job_views(self, job_id: str) -> tuple[IndexJobViewRecord, ...]: ...

    def acquire_job_lease(
        self,
        job_id: str,
        *,
        owner_id: str,
        lease_duration_ms: int,
    ) -> RefJobLease: ...

    def renew_job_lease(
        self,
        job_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        lease_duration_ms: int,
    ) -> RefJobLease: ...

    def request_job_cancel(self, job_id: str) -> IndexJobRecord: ...

    def finish_job_attempt(
        self,
        job_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        outcome: IndexJobCompletion,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> IndexJobRecord: ...


__all__ = [
    "IndexCatalog",
    "JobCatalog",
    "ObjectStore",
    "ReceiptVerifyingObjectStore",
]
