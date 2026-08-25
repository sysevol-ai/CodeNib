# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for receipt-retained index-job publication coordination."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from codenib.storage.cas import BlobInfo, LocalCAS
from codenib.storage.models import (
    INDEX_JOB_REQUEST_CONTRACT,
    IndexJobRecord,
    IndexJobRequest,
    IndexJobStatus,
    StorageIntegrityError,
    StorageValidationError,
    content_id,
)
from codenib.storage.protocols import ReceiptRetainingObjectStore, StreamingObjectStore
from codenib.storage.publication import (
    IndexJobObjectArtifact,
    IndexJobViewArtifact,
    publish_job_artifacts,
)


def _receipt(payload: bytes) -> BlobInfo:
    digest = hashlib.sha256(payload).hexdigest()
    return BlobInfo(
        digest=digest,
        byte_size=len(payload),
        storage_key=f"sha256/{digest[:2]}/{digest[2:]}",
    )


def _artifact(
    view_type: str,
    profile_suffix: str,
    payload: bytes,
    *,
    members: tuple[IndexJobObjectArtifact, ...] = (),
    media_type: str = "application/x-test-index",
) -> IndexJobViewArtifact:
    return IndexJobViewArtifact.create(
        view_type,
        "profile_" + profile_suffix * 64,
        _receipt(payload),
        schema_version="test.v1",
        media_type=media_type,
        metadata={"builder": "deterministic"},
        member_artifacts=members,
    )


def _completed_job(
    outputs: tuple[IndexJobViewArtifact, ...],
    *,
    idempotency_key: str = "request",
) -> IndexJobRecord:
    repository_id = "repo_" + "a" * 64
    source_revision_id = "src_" + "b" * 64
    request = IndexJobRequest.create(
        repository_id,
        source_revision_id,
        idempotency_key,
        {
            "contract": INDEX_JOB_REQUEST_CONTRACT,
            "views": {
                output.view_type: {
                    "profile_id": output.profile_id,
                    "requested_mode": "auto",
                    "required": True,
                }
                for output in outputs
            },
        },
    )
    snapshot_members: list[list[str]] = []
    for artifact in sorted(outputs, key=lambda item: item.view_type):
        output = artifact._output()
        generation_id = content_id(
            "view",
            {
                "repository_id": repository_id,
                "source_revision_id": source_revision_id,
                "profile_id": output.profile_id,
                "view_type": output.view_type,
                "object_digest": output.object_record.digest,
                "schema_version": output.schema_version,
                "metadata": output.generation_metadata,
            },
        )
        snapshot_members.append([output.view_type, generation_id])
    snapshot_id = content_id(
        "snapshot",
        {
            "repository_id": repository_id,
            "source_revision_id": source_revision_id,
            "views": snapshot_members,
        },
    )
    return IndexJobRecord(
        job_id=request.job_id,
        repository_id=request.repository_id,
        source_revision_id=request.source_revision_id,
        ref_name=request.ref_name,
        idempotency_key=request.idempotency_key,
        expected_ref_generation=request.expected_ref_generation,
        max_attempts=request.max_attempts,
        request_json=request.request_json,
        request_digest=request.request_digest,
        status=IndexJobStatus.SUCCEEDED,
        cancel_requested=False,
        attempt_count=1,
        result_snapshot_id=snapshot_id,
        error_code=None,
        error_message=None,
        created_at_ms=1,
        updated_at_ms=3,
        started_at_ms=2,
        finished_at_ms=3,
    )


class _RetainingOnlyStore:
    """Structural receipt-retaining store deliberately lacking put_chunks."""

    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.active = False
        self.expected: tuple[BlobInfo, ...] | None = None

    def put_bytes(self, data: bytes) -> BlobInfo:
        raise NotImplementedError

    def put_file(self, source: str | Path) -> BlobInfo:
        raise NotImplementedError

    def has(self, digest: str) -> bool:
        raise NotImplementedError

    def open(self, digest: str):
        raise NotImplementedError

    def read_bytes(self, digest: str) -> bytes:
        raise NotImplementedError

    def verify(self, digest: str) -> BlobInfo:
        raise NotImplementedError

    def materialize(self, digest: str, destination: str | Path) -> Path:
        raise NotImplementedError

    def verify_receipt(self, expected: BlobInfo) -> BlobInfo:
        raise NotImplementedError

    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], Any],
    ) -> Any:
        self.expected = expected
        self.events.append("retain-enter")
        self.active = True
        try:
            result = callback()
            self.events.append("callback-returned")
            return result
        finally:
            assert self.active
            self.active = False
            self.events.append("retain-exit")


class _CatalogSpy:
    def __init__(
        self,
        completed: IndexJobRecord,
        store: _RetainingOnlyStore | None,
        events: list[object],
    ) -> None:
        self.completed = completed
        self.store = store
        self.events = events
        self.calls = 0
        self.outputs = None

    def publish_job_outputs(self, job_id: str, **kwargs: Any) -> IndexJobRecord:
        self.calls += 1
        if self.store is not None:
            assert self.store.active
        self.events.append("catalog-enter")
        self.outputs = kwargs["outputs"]
        assert job_id == self.completed.job_id
        self.events.append("catalog-return")
        return self.completed


class _ReturnSubstitutingStore(_RetainingOnlyStore):
    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], Any],
    ) -> object:
        self.expected = expected
        self.active = True
        try:
            callback()
            return object()
        finally:
            self.active = False


class _ExceptionSwallowingStore(_RetainingOnlyStore):
    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], Any],
    ) -> object:
        self.expected = expected
        self.active = True
        try:
            try:
                callback()
            except BaseException:  # noqa: B036 - deliberately models a hostile store
                return object()
            raise AssertionError("the publication callback was expected to fail")
        finally:
            self.active = False


def test_retention_encloses_catalog_transaction_and_return_attestation() -> None:
    member = IndexJobObjectArtifact(
        _receipt(b"member"),
        "application/x-test-member",
    )
    outputs = (_artifact("bm25", "1", b"primary", members=(member,)),)
    completed = _completed_job(outputs)
    events: list[object] = []
    store = _RetainingOnlyStore(events)
    catalog = _CatalogSpy(completed, store, events)

    result = publish_job_artifacts(
        completed.job_id,
        catalog=catalog,  # type: ignore[arg-type]
        object_store=store,
        owner_id="worker-1",
        fencing_token=7,
        outputs=outputs,
    )

    assert result == completed
    assert events == [
        "retain-enter",
        "catalog-enter",
        "catalog-return",
        "callback-returned",
        "retain-exit",
    ]
    assert catalog.calls == 1
    assert tuple(output.view_type for output in catalog.outputs) == ("bm25",)
    assert isinstance(store, ReceiptRetainingObjectStore)
    assert not isinstance(store, StreamingObjectStore)


def test_receipts_are_frozen_sorted_and_deduplicated_before_retention() -> None:
    shared = _receipt(b"shared immutable bytes")
    outputs = (
        IndexJobViewArtifact.create(
            "vector",
            "profile_" + "2" * 64,
            shared,
            schema_version="1",
            media_type="application/x-shared-index",
        ),
        IndexJobViewArtifact.create(
            "bm25",
            "profile_" + "1" * 64,
            shared,
            schema_version="1",
            media_type="application/x-shared-index",
        ),
    )
    completed = _completed_job(outputs)
    events: list[object] = []
    store = _RetainingOnlyStore(events)
    catalog = _CatalogSpy(completed, store, events)

    publish_job_artifacts(
        completed.job_id,
        catalog=catalog,  # type: ignore[arg-type]
        object_store=store,
        owner_id="worker",
        fencing_token=1,
        outputs=outputs,
    )

    assert store.expected == (shared,)
    assert tuple(output.view_type for output in catalog.outputs) == ("bm25", "vector")


def test_conflicting_metadata_for_one_digest_fails_before_retention_or_catalog() -> (
    None
):
    shared = _receipt(b"same digest")
    outputs = (
        IndexJobViewArtifact.create(
            "bm25",
            "profile_" + "1" * 64,
            shared,
            schema_version="1",
            media_type="application/x-bm25",
        ),
        IndexJobViewArtifact.create(
            "vector",
            "profile_" + "2" * 64,
            shared,
            schema_version="1",
            media_type="application/x-vector",
        ),
    )
    completed = _completed_job(outputs)
    events: list[object] = []
    store = _RetainingOnlyStore(events)
    catalog = _CatalogSpy(completed, store, events)

    with pytest.raises(StorageValidationError, match="conflicting publication"):
        publish_job_artifacts(
            completed.job_id,
            catalog=catalog,  # type: ignore[arg-type]
            object_store=store,
            owner_id="worker",
            fencing_token=1,
            outputs=outputs,
        )

    assert events == []
    assert catalog.calls == 0


@pytest.mark.parametrize("mismatch", ("digest", "byte_size", "storage_key"))
def test_exact_receipt_mismatch_fails_before_the_catalog_call(
    tmp_path,
    mismatch: str,
) -> None:
    object_store = LocalCAS(tmp_path / "objects")
    actual = object_store.put_bytes(b"verified publication bytes")
    other = object_store.put_bytes(b"different publication bytes")
    values = {
        "digest": actual.digest,
        "byte_size": actual.byte_size,
        "storage_key": actual.storage_key,
    }
    if mismatch == "digest":
        values["digest"] = other.digest
    elif mismatch == "byte_size":
        values["byte_size"] = actual.byte_size + 1
    else:
        values["storage_key"] = other.storage_key
    forged = BlobInfo(**values)  # type: ignore[arg-type]
    outputs = (
        IndexJobViewArtifact.create(
            "bm25",
            "profile_" + "1" * 64,
            forged,
            schema_version="1",
        ),
    )
    completed = _completed_job(outputs)
    catalog = _CatalogSpy(completed, None, [])

    with pytest.raises(
        (StorageIntegrityError, StorageValidationError, FileNotFoundError)
    ):
        publish_job_artifacts(
            completed.job_id,
            catalog=catalog,  # type: ignore[arg-type]
            object_store=object_store,
            owner_id="worker",
            fencing_token=1,
            outputs=outputs,
        )

    assert catalog.calls == 0


@pytest.mark.parametrize("case", ("empty", "duplicate"))
def test_output_shape_fails_before_receipt_retention_or_catalog(case: str) -> None:
    output = _artifact("bm25", "1", b"primary")
    outputs = () if case == "empty" else (output, output)
    completed = _completed_job((output,))
    events: list[object] = []
    store = _RetainingOnlyStore(events)
    catalog = _CatalogSpy(completed, store, events)

    with pytest.raises(StorageValidationError, match="output|duplicate"):
        publish_job_artifacts(
            completed.job_id,
            catalog=catalog,  # type: ignore[arg-type]
            object_store=store,
            owner_id="worker",
            fencing_token=1,
            outputs=outputs,
        )

    assert events == []
    assert catalog.calls == 0


def test_catalog_response_is_attested_before_receipts_are_released() -> None:
    outputs = (_artifact("bm25", "1", b"primary"),)
    completed = _completed_job(outputs)
    wrong = replace(completed, result_snapshot_id="snapshot_" + "f" * 64)
    events: list[object] = []
    store = _RetainingOnlyStore(events)
    catalog = _CatalogSpy(wrong, store, events)

    with pytest.raises(StorageIntegrityError, match="different publication snapshot"):
        publish_job_artifacts(
            completed.job_id,
            catalog=catalog,  # type: ignore[arg-type]
            object_store=store,
            owner_id="worker",
            fencing_token=1,
            outputs=outputs,
        )

    assert store.active is False
    assert events == [
        "retain-enter",
        "catalog-enter",
        "catalog-return",
        "retain-exit",
    ]


def test_retaining_store_cannot_substitute_the_callback_result() -> None:
    outputs = (_artifact("bm25", "1", b"primary"),)
    completed = _completed_job(outputs)
    store = _ReturnSubstitutingStore([])
    catalog = _CatalogSpy(completed, store, [])

    with pytest.raises(StorageIntegrityError, match="callback result"):
        publish_job_artifacts(
            completed.job_id,
            catalog=catalog,  # type: ignore[arg-type]
            object_store=store,
            owner_id="worker",
            fencing_token=1,
            outputs=outputs,
        )

    assert catalog.calls == 1


def test_retaining_store_cannot_swallow_a_callback_failure() -> None:
    outputs = (_artifact("bm25", "1", b"primary"),)
    completed = _completed_job(outputs)
    wrong = replace(completed, result_snapshot_id="snapshot_" + "f" * 64)
    store = _ExceptionSwallowingStore([])
    catalog = _CatalogSpy(wrong, store, [])

    with pytest.raises(StorageIntegrityError):
        publish_job_artifacts(
            completed.job_id,
            catalog=catalog,  # type: ignore[arg-type]
            object_store=store,
            owner_id="worker",
            fencing_token=1,
            outputs=outputs,
        )

    assert catalog.calls == 1


@pytest.mark.parametrize("changed", ("job_id", "owner_id"))
@pytest.mark.parametrize("nonexact_type", ("bytes", "str_subclass"))
def test_coordinator_rejects_nonexact_authority_text_before_retention(
    changed: str,
    nonexact_type: str,
) -> None:
    class ForgedString(str):
        pass

    outputs = (_artifact("bm25", "1", b"primary"),)
    completed = _completed_job(outputs)
    events: list[object] = []
    store = _RetainingOnlyStore(events)
    catalog = _CatalogSpy(completed, store, events)
    values: dict[str, object] = {
        "job_id": completed.job_id,
        "owner_id": "worker",
    }
    original = str(values[changed])
    values[changed] = (
        original.encode() if nonexact_type == "bytes" else ForgedString(original)
    )

    with pytest.raises(StorageValidationError, match="canonical text"):
        publish_job_artifacts(
            values["job_id"],  # type: ignore[arg-type]
            catalog=catalog,  # type: ignore[arg-type]
            object_store=store,
            owner_id=values["owner_id"],  # type: ignore[arg-type]
            fencing_token=1,
            outputs=outputs,
        )

    assert events == []
    assert catalog.calls == 0


@pytest.mark.parametrize("changed", ("receipt_digest", "media_type"))
def test_object_artifact_rejects_nonexact_receipt_or_media_text(changed: str) -> None:
    class ForgedString(str):
        pass

    receipt = _receipt(b"primary")
    if changed == "receipt_digest":
        receipt = BlobInfo(
            digest=ForgedString(receipt.digest),
            byte_size=receipt.byte_size,
            storage_key=receipt.storage_key,
        )
        media_type: object = "application/x-test"
    else:
        media_type = b"application/x-test"

    with pytest.raises(TypeError, match="exact str"):
        IndexJobObjectArtifact(
            receipt,
            media_type,  # type: ignore[arg-type]
        )
