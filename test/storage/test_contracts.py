# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib

import pytest

import codenib.storage as storage_module
from codenib.storage import (
    RETAINED_IMPORT_CATALOG_CONTRACT,
    RETAINED_IMPORT_RESPONSE_MAX_DEPTH,
    RETAINED_IMPORT_RESPONSE_MAX_KEY_CHARS,
    RETAINED_IMPORT_RESPONSE_MAX_NODES,
    RETAINED_IMPORT_RESPONSE_MAX_TEXT_CHARS,
    ReceiptRetainingObjectStore,
    RetainedImportCatalog,
    RetainedImportObjectStore,
    StreamingObjectStore,
)
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
    JobCycleWorkerCatalog,
    JobExecutionCatalog,
    JobPublicationCatalog,
    JobWorkerCatalog,
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
        assert isinstance(object_store, ReceiptRetainingObjectStore)
        assert isinstance(object_store, StreamingObjectStore)
        assert isinstance(object_store, RetainedImportObjectStore)
        assert isinstance(catalog, IndexCatalog)
        assert isinstance(catalog, RetainedImportCatalog)
        assert catalog.retained_import_contract() == RETAINED_IMPORT_CATALOG_CONTRACT
        assert isinstance(catalog, JobCatalog)
        assert isinstance(catalog, JobExecutionCatalog)
        assert isinstance(catalog, JobWorkerCatalog)
        assert isinstance(catalog, JobCycleWorkerCatalog)
    finally:
        catalog.close()


def test_execution_contract_models_are_public_storage_exports() -> None:
    names = {
        "INDEX_JOB_EVENT_PAYLOAD_MAX_DEPTH",
        "INDEX_JOB_EVENT_PAYLOAD_MAX_KEY_CHARS",
        "INDEX_JOB_EVENT_PAYLOAD_MAX_NODES",
        "INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS",
        "MAX_INDEX_JOB_EVENTS_PER_ATTEMPT",
        "IndexJobAttemptCompletionRecord",
        "IndexJobAttemptHeartbeat",
        "IndexJobAttemptRecord",
        "IndexJobCatalogSessionFactory",
        "IndexJobEffectiveMode",
        "IndexJobExecutionContext",
        "IndexJobExecutionControl",
        "IndexJobExecutionResult",
        "IndexJobExecutor",
        "IndexJobExecutorResolver",
        "IndexJobObjectStoreBoundResolver",
        "IndexJobEventKind",
        "IndexJobEventRecord",
        "IndexJobRunnableCycle",
        "IndexJobRunnableCursor",
        "IndexJobRunnablePage",
        "IndexJobStopReason",
        "IndexJobStopToken",
        "IndexJobViewExecutionResult",
        "IndexJobViewOutcome",
        "IndexJobWorker",
        "IndexJobWorkerDisposition",
        "IndexJobWorkerRunResult",
        "JobCycleWorkerCatalog",
        "JobExecutionCatalog",
        "JobWorkerCatalog",
    }

    assert names <= set(storage_module.__all__)
    assert all(hasattr(storage_module, name) for name in names)


def test_execution_catalog_is_additive_to_publication_only_adapters() -> None:
    class PublicationOnlyCatalog:
        def create_job(self, *args, **kwargs):
            raise NotImplementedError

        def get_job(self, *args, **kwargs):
            raise NotImplementedError

        def get_job_views(self, *args, **kwargs):
            raise NotImplementedError

        def acquire_job_lease(self, *args, **kwargs):
            raise NotImplementedError

        def renew_job_lease(self, *args, **kwargs):
            raise NotImplementedError

        def request_job_cancel(self, *args, **kwargs):
            raise NotImplementedError

        def finish_job_attempt(self, *args, **kwargs):
            raise NotImplementedError

        def publish_job_outputs(self, *args, **kwargs):
            raise NotImplementedError

    adapter = PublicationOnlyCatalog()

    assert isinstance(adapter, JobCatalog)
    assert isinstance(adapter, JobPublicationCatalog)
    assert not isinstance(adapter, JobExecutionCatalog)
    assert not isinstance(adapter, JobWorkerCatalog)
    assert not isinstance(adapter, JobCycleWorkerCatalog)


def test_retained_import_response_budgets_are_public_capability_contracts() -> None:
    assert RETAINED_IMPORT_RESPONSE_MAX_DEPTH == 64
    assert RETAINED_IMPORT_RESPONSE_MAX_KEY_CHARS == 4_096
    assert RETAINED_IMPORT_RESPONSE_MAX_NODES == 250_000
    assert RETAINED_IMPORT_RESPONSE_MAX_TEXT_CHARS == 64 * 1024 * 1024


def test_retained_import_catalog_requires_explicit_contract_opt_in() -> None:
    class LegacyCatalog:
        def create_namespace(self, name):
            raise NotImplementedError

        def create_repository(self, repository_key, *, namespace_id):
            raise NotImplementedError

        def create_source_revision(self, repository_id, **kwargs):
            raise NotImplementedError

        def create_view_profile(self, view_type, config=None, *, name="default"):
            raise NotImplementedError

        def register_object(self, digest, **kwargs):
            raise NotImplementedError

        def stage_view_generation(self, *args, **kwargs):
            raise NotImplementedError

        def publish_snapshot(self, *args, **kwargs):
            raise NotImplementedError

        def resolve_ref(self, repository_id, ref_name="main"):
            raise NotImplementedError

        def get_manifest_summary(self, snapshot_id):
            raise NotImplementedError

    legacy = LegacyCatalog()

    assert isinstance(legacy, IndexCatalog)
    assert not isinstance(legacy, RetainedImportCatalog)


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


def test_receipt_retention_is_narrower_than_retained_import_capability() -> None:
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
    assert not isinstance(receipt_only, ReceiptRetainingObjectStore)
    assert not isinstance(receipt_only, StreamingObjectStore)
    assert not isinstance(receipt_only, RetainedImportObjectStore)

    class RetainingObjectStore(ReceiptOnlyObjectStore):
        def retain_receipts(self, expected, callback):
            raise NotImplementedError

    retaining = RetainingObjectStore()

    assert isinstance(retaining, ObjectStore)
    assert isinstance(retaining, ReceiptVerifyingObjectStore)
    assert isinstance(retaining, ReceiptRetainingObjectStore)
    assert not isinstance(retaining, StreamingObjectStore)
    assert not isinstance(retaining, RetainedImportObjectStore)

    class StreamingVerifyingObjectStore(ReceiptOnlyObjectStore):
        def put_chunks(self, chunks, expected_digest, expected_size):
            raise NotImplementedError

    streaming_verifying = StreamingVerifyingObjectStore()

    assert isinstance(streaming_verifying, StreamingObjectStore)
    assert isinstance(streaming_verifying, ReceiptVerifyingObjectStore)
    assert not isinstance(streaming_verifying, ReceiptRetainingObjectStore)
    assert not isinstance(streaming_verifying, RetainedImportObjectStore)

    class ImportObjectStore(RetainingObjectStore):
        def put_chunks(self, chunks, expected_digest, expected_size):
            raise NotImplementedError

    retained_import = ImportObjectStore()

    assert isinstance(retained_import, ReceiptRetainingObjectStore)
    assert isinstance(retained_import, StreamingObjectStore)
    assert isinstance(retained_import, RetainedImportObjectStore)


def test_retained_import_capability_requires_streaming_and_receipt_checks() -> None:
    class StreamingOnlyObjectStore:
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

        def put_chunks(self, chunks, expected_digest, expected_size):
            raise NotImplementedError

    streaming_only = StreamingOnlyObjectStore()

    assert isinstance(streaming_only, ObjectStore)
    assert isinstance(streaming_only, StreamingObjectStore)
    assert not isinstance(streaming_only, ReceiptVerifyingObjectStore)
    assert not isinstance(streaming_only, ReceiptRetainingObjectStore)
    assert not isinstance(streaming_only, RetainedImportObjectStore)


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
    # the exact-receipt gate again instead of inferring a pin from BlobInfo.
    (object_store.root / receipt.storage_key).unlink()
    with pytest.raises(FileNotFoundError):
        object_store.verify_receipt(receipt)
    assert verified == [receipt.digest, receipt.digest, receipt.digest]


def test_retained_receipt_scope_verifies_before_callback_and_is_bounded(
    tmp_path,
    monkeypatch,
) -> None:
    object_store = LocalCAS(tmp_path / "objects")
    receipt = object_store.put_bytes(b"retained publication payload")
    events: list[object] = []
    real_verify = object_store.verify_receipt

    def record_verify(expected: BlobInfo) -> BlobInfo:
        events.append(("verify", expected.digest))
        return real_verify(expected)

    monkeypatch.setattr(object_store, "verify_receipt", record_verify)
    callback_error = RuntimeError("publication failed")

    def fail_publication() -> None:
        events.append("callback")
        raise callback_error

    with pytest.raises(RuntimeError) as caught:
        object_store.retain_receipts((receipt,), fail_publication)
    assert caught.value is callback_error
    assert events == [("verify", receipt.digest), "callback"]

    events.clear()
    assert object_store.retain_receipts((receipt,), lambda: "published") == "published"
    assert events == [("verify", receipt.digest)]

    callback_calls = 0

    def forbidden_callback() -> None:
        nonlocal callback_calls
        callback_calls += 1

    with pytest.raises(TypeError, match="exact tuple"):
        object_store.retain_receipts([receipt], forbidden_callback)  # type: ignore[arg-type]
    with pytest.raises(StorageValidationError, match="must not be empty"):
        object_store.retain_receipts((), forbidden_callback)
    with pytest.raises(StorageValidationError, match="unique digests"):
        object_store.retain_receipts((receipt, receipt), forbidden_callback)
    with pytest.raises(TypeError, match="must be BlobInfo"):
        object_store.retain_receipts((object(),), forbidden_callback)  # type: ignore[arg-type]
    assert callback_calls == 0


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
