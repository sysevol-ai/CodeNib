# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib

import pytest

from codenib.storage import StreamingObjectStore
from codenib.storage.cas import BlobInfo, LocalCAS
from codenib.storage.models import (
    ObjectRecord,
    PublishedSnapshot,
    RepositoryIdentity,
    SnapshotView,
    SourceRevision,
    StorageIntegrityError,
    StorageValidationError,
    ViewGeneration,
    ViewProfile,
)
from codenib.storage.protocols import (
    IndexCatalog,
    JobCatalog,
    ObjectStore,
    ReceiptVerifyingObjectStore,
)
from codenib.storage.sqlite_catalog import DEFAULT_NAMESPACE_ID, SQLiteCatalog


def test_embedded_backends_implement_storage_protocols(tmp_path) -> None:
    object_store = LocalCAS(tmp_path / "objects")
    catalog = SQLiteCatalog(tmp_path / "catalog.sqlite3")
    try:
        assert isinstance(object_store, ObjectStore)
        assert isinstance(object_store, ReceiptVerifyingObjectStore)
        assert isinstance(object_store, StreamingObjectStore)
        assert isinstance(catalog, IndexCatalog)
        assert isinstance(catalog, JobCatalog)
    finally:
        catalog.close()


def test_receipt_verification_is_an_additive_object_store_capability() -> None:
    class LegacyObjectStore:
        def put_bytes(self, data):
            raise NotImplementedError

        def put_file(self, source):
            raise NotImplementedError

        def has(self, digest):
            raise NotImplementedError

        def open(self, digest):
            raise NotImplementedError

        def read_bytes(self, digest):
            raise NotImplementedError

        def verify(self, digest):
            raise NotImplementedError

        def materialize(self, digest, destination):
            raise NotImplementedError

    legacy = LegacyObjectStore()

    assert isinstance(legacy, ObjectStore)
    assert not isinstance(legacy, ReceiptVerifyingObjectStore)
    assert not isinstance(legacy, StreamingObjectStore)


def test_streaming_is_additive_to_receipt_verifying_object_store() -> None:
    class ReceiptOnlyObjectStore:
        def put_bytes(self, data):
            raise NotImplementedError

        def put_file(self, source):
            raise NotImplementedError

        def has(self, digest):
            raise NotImplementedError

        def open(self, digest):
            raise NotImplementedError

        def read_bytes(self, digest):
            raise NotImplementedError

        def verify(self, digest):
            raise NotImplementedError

        def materialize(self, digest, destination):
            raise NotImplementedError

        def verify_receipt(self, expected):
            raise NotImplementedError

    receipt_only = ReceiptOnlyObjectStore()

    assert isinstance(receipt_only, ObjectStore)
    assert isinstance(receipt_only, ReceiptVerifyingObjectStore)
    assert not isinstance(receipt_only, StreamingObjectStore)


def test_streaming_object_store_protocol_executes_expected_identity_put(
    tmp_path,
) -> None:
    object_store: StreamingObjectStore = LocalCAS(tmp_path / "objects")
    payload = b"protocol streamed object"
    digest = hashlib.sha256(payload).hexdigest()

    receipt = object_store.put_chunks(
        iter((payload[:8], payload[8:])),
        digest,
        len(payload),
    )

    assert receipt.digest == digest
    assert object_store.read_bytes(digest) == payload


def test_object_receipt_revalidation_is_executable_and_does_not_pin_lifetime(
    tmp_path,
    monkeypatch,
) -> None:
    object_store = LocalCAS(tmp_path / "objects")
    receipt = object_store.put_bytes(b"durable at the verified read boundary")
    verified: list[str] = []
    real_verify = object_store.verify

    def count_verify(digest: str) -> BlobInfo:
        verified.append(digest)
        return real_verify(digest)

    monkeypatch.setattr(object_store, "verify", count_verify)

    assert object_store.verify_receipt(receipt) == receipt
    assert verified == [receipt.digest]
    with pytest.raises(
        StorageIntegrityError,
        match="receipt does not match",
    ):
        object_store.verify_receipt(
            BlobInfo(
                digest=receipt.digest,
                byte_size=receipt.byte_size + 1,
                storage_key=receipt.storage_key,
            )
        )
    assert verified == [receipt.digest, receipt.digest]
    with pytest.raises(StorageValidationError, match="not canonical"):
        object_store.verify_receipt(
            BlobInfo(
                digest=receipt.digest,
                byte_size=receipt.byte_size,
                storage_key=f"objects/{receipt.digest}",
            )
        )
    assert verified == [receipt.digest, receipt.digest]

    # A BlobInfo is an immutable identity receipt, not a retention lease.  A
    # future GC may remove unpinned bytes, so each read boundary must execute
    # the exact-receipt gate again instead of inferring a pin from API shape.
    (object_store.root / receipt.storage_key).unlink()
    with pytest.raises(FileNotFoundError):
        object_store.verify_receipt(receipt)
    assert verified == [receipt.digest, receipt.digest, receipt.digest]


def test_object_receipt_revalidation_rejects_equality_forged_subclasses(
    tmp_path,
    monkeypatch,
) -> None:
    class ForgedBlobInfo(BlobInfo):
        def __eq__(self, _other: object) -> bool:
            return True

        def __ne__(self, _other: object) -> bool:
            return False

    class ForgedString(str):
        def __eq__(self, _other: object) -> bool:
            return True

        def __ne__(self, _other: object) -> bool:
            return False

    class ForgedInteger(int):
        def __eq__(self, _other: object) -> bool:
            return True

        def __ne__(self, _other: object) -> bool:
            return False

    object_store = LocalCAS(tmp_path / "objects")
    receipt = object_store.put_bytes(b"exact receipt")
    verified = False

    def reject_verify(_digest: str) -> BlobInfo:
        nonlocal verified
        verified = True
        raise AssertionError("backend verification ran for a forged receipt type")

    monkeypatch.setattr(object_store, "verify", reject_verify)

    for forged in (
        ForgedBlobInfo(
            digest=receipt.digest,
            byte_size=receipt.byte_size + 99,
            storage_key="attacker/key",
        ),
        BlobInfo(
            digest=ForgedString(receipt.digest),
            byte_size=receipt.byte_size,
            storage_key=receipt.storage_key,
        ),
        BlobInfo(
            digest=receipt.digest,
            byte_size=ForgedInteger(receipt.byte_size + 99),
            storage_key=receipt.storage_key,
        ),
        BlobInfo(
            digest=receipt.digest,
            byte_size=receipt.byte_size,
            storage_key=ForgedString("attacker/key"),
        ),
    ):
        with pytest.raises(TypeError, match="must be BlobInfo"):
            object_store.verify_receipt(forged)
    assert verified is False


def test_domain_and_sqlite_content_identities_match(tmp_path) -> None:
    object_store = LocalCAS(tmp_path / "objects")
    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        repository = RepositoryIdentity(DEFAULT_NAMESPACE_ID, "owner/repo")
        repository_id = catalog.create_repository("owner/repo")
        assert repository_id == repository.repository_id

        source = SourceRevision.clean(
            repository_id,
            commit_sha="a" * 40,
            tree_sha="b" * 64,
        )
        source_revision_id = catalog.create_source_revision(
            repository_id,
            commit_sha="a" * 40,
            tree_sha="b" * 64,
        )
        assert source_revision_id == source.source_revision_id

        profile = ViewProfile.create("bm25", {"max_k": 128})
        profile_id = catalog.create_view_profile("bm25", {"max_k": 128})
        assert profile_id == profile.profile_id

        blob = object_store.put_bytes(b"immutable BM25 artifact")
        object_record = ObjectRecord(
            digest=blob.digest,
            byte_size=blob.byte_size,
            storage_key=blob.storage_key,
        )
        catalog.register_object(
            blob.digest,
            storage_key=blob.storage_key,
            byte_size=blob.byte_size,
        )

        generation = ViewGeneration.create(
            source,
            profile,
            object_record,
            schema_version="1",
            metadata={"document_count": 4},
        )
        generation_id = catalog.stage_view_generation(
            repository_id,
            source_revision_id,
            profile_id,
            "bm25",
            blob.digest,
            schema_version="1",
            metadata={"document_count": 4},
        )
        assert generation_id == generation.view_generation_id

        snapshot = PublishedSnapshot(
            repository_id,
            source_revision_id,
            (SnapshotView(generation),),
        )
        published = catalog.publish_snapshot(
            repository_id,
            source_revision_id,
            [generation_id],
            expected_generation=0,
        )
        assert published["snapshot_id"] == snapshot.snapshot_id
