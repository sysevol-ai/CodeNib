# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for receipt-retained index-job publication coordination."""

from __future__ import annotations

import dis
import hashlib
import sys
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
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.completed = completed
        self.store = store
        self.events = events
        self.failure = failure
        self.calls = 0
        self.outputs = None

    def publish_job_outputs(self, job_id: str, **kwargs: Any) -> IndexJobRecord:
        self.calls += 1
        if self.store is not None:
            assert self.store.active
        self.events.append("catalog-enter")
        self.outputs = kwargs["outputs"]
        assert job_id == self.completed.job_id
        if self.failure is not None:
            raise self.failure
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
    callback_failure: BaseException | None = None

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
            except BaseException as exc:  # noqa: B036 - hostile store fixture
                self.callback_failure = exc
                return object()
            raise AssertionError("the publication callback was expected to fail")
        finally:
            self.active = False


class _CleanupFailingStore(_ExceptionSwallowingStore):
    def __init__(
        self,
        events: list[object],
        cleanup_failure: BaseException,
        *,
        invoke_callback: bool = True,
    ) -> None:
        super().__init__(events)
        self.cleanup_failure = cleanup_failure
        self.invoke_callback = invoke_callback

    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], Any],
    ) -> object:
        self.expected = expected
        self.active = True
        try:
            if self.invoke_callback:
                try:
                    callback()
                except BaseException as exc:  # noqa: B036 - hostile store fixture
                    self.callback_failure = exc
            if self.callback_failure is not None:
                raise self.cleanup_failure from self.callback_failure
            raise self.cleanup_failure
        finally:
            self.active = False


class _DoubleInvokingExceptionSwallowingStore(_RetainingOnlyStore):
    second_failure: BaseException | None = None

    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], Any],
    ) -> object:
        self.expected = expected
        self.active = True
        try:
            first_result = callback()
            try:
                callback()
            except BaseException as exc:  # noqa: B036 - hostile store fixture
                self.second_failure = exc
            else:
                raise AssertionError("the second callback invocation should fail")
            return first_result
        finally:
            self.active = False


class _CallbackFailureThenSecondInvokingStore(_ExceptionSwallowingStore):
    second_failure: BaseException | None = None

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
            except BaseException as exc:  # noqa: B036 - hostile store fixture
                self.callback_failure = exc
            else:
                raise AssertionError("the first callback invocation should fail")
            try:
                callback()
            except BaseException as exc:  # noqa: B036 - hostile store fixture
                self.second_failure = exc
            else:
                raise AssertionError("the second callback invocation should fail")
            return object()
        finally:
            self.active = False


class _DeferredCallbackStore(_RetainingOnlyStore):
    callback: Callable[[], Any] | None = None

    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], Any],
    ) -> object:
        self.expected = expected
        self.callback = callback
        return object()


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

    def before_catalog_publish() -> None:
        assert store.active
        events.append("pre-catalog-hook")

    result = publish_job_artifacts(
        completed.job_id,
        catalog=catalog,  # type: ignore[arg-type]
        object_store=store,
        owner_id="worker-1",
        fencing_token=7,
        outputs=outputs,
        _before_catalog_publish=before_catalog_publish,
    )

    assert result == completed
    assert events == [
        "retain-enter",
        "pre-catalog-hook",
        "catalog-enter",
        "catalog-return",
        "callback-returned",
        "retain-exit",
    ]
    assert catalog.calls == 1
    assert tuple(output.view_type for output in catalog.outputs) == ("bm25",)
    assert isinstance(store, ReceiptRetainingObjectStore)
    assert not isinstance(store, StreamingObjectStore)


def test_prepublication_hook_failure_is_exact_and_skips_the_catalog() -> None:
    outputs = (_artifact("bm25", "1", b"primary"),)
    completed = _completed_job(outputs)
    events: list[object] = []
    store = _RetainingOnlyStore(events)
    catalog = _CatalogSpy(completed, store, events)
    failure = RuntimeError("pre-catalog hook failed")

    def fail_before_catalog_publish() -> None:
        assert store.active
        events.append("pre-catalog-hook")
        raise failure

    with pytest.raises(RuntimeError) as caught:
        publish_job_artifacts(
            completed.job_id,
            catalog=catalog,  # type: ignore[arg-type]
            object_store=store,
            owner_id="worker-1",
            fencing_token=7,
            outputs=outputs,
            _before_catalog_publish=fail_before_catalog_publish,
        )

    assert caught.value is failure
    assert events == ["retain-enter", "pre-catalog-hook", "retain-exit"]
    assert catalog.calls == 0


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


def test_retention_cleanup_failure_after_callback_success_is_integrity() -> None:
    outputs = (_artifact("bm25", "1", b"primary"),)
    completed = _completed_job(outputs)
    cleanup_failure = RuntimeError("retention cleanup failed")
    store = _CleanupFailingStore([], cleanup_failure)
    catalog = _CatalogSpy(completed, store, [])

    with pytest.raises(StorageIntegrityError, match="retention failed") as raised:
        publish_job_artifacts(
            completed.job_id,
            catalog=catalog,  # type: ignore[arg-type]
            object_store=store,
            owner_id="worker",
            fencing_token=1,
            outputs=outputs,
            _retention_cleanup_as_integrity=True,
        )

    assert raised.value.__cause__ is cleanup_failure
    assert store.callback_failure is None
    assert catalog.calls == 1


@pytest.mark.parametrize("failure_type", (KeyboardInterrupt, SystemExit))
def test_retention_cleanup_base_exception_after_success_remains_exact(
    failure_type: type[BaseException],
) -> None:
    outputs = (_artifact("bm25", "1", b"primary"),)
    completed = _completed_job(outputs)
    cleanup_failure = failure_type("retention cleanup interrupted")
    store = _CleanupFailingStore([], cleanup_failure)
    catalog = _CatalogSpy(completed, store, [])

    with pytest.raises(failure_type) as raised:
        publish_job_artifacts(
            completed.job_id,
            catalog=catalog,  # type: ignore[arg-type]
            object_store=store,
            owner_id="worker",
            fencing_token=1,
            outputs=outputs,
            _retention_cleanup_as_integrity=True,
        )

    assert raised.value is cleanup_failure
    assert store.callback_failure is None
    assert catalog.calls == 1


def test_retention_cleanup_integrity_alarm_after_success_remains_exact() -> None:
    outputs = (_artifact("bm25", "1", b"primary"),)
    completed = _completed_job(outputs)
    cleanup_failure = StorageIntegrityError("retention integrity alarm")
    store = _CleanupFailingStore([], cleanup_failure)
    catalog = _CatalogSpy(completed, store, [])

    with pytest.raises(StorageIntegrityError) as raised:
        publish_job_artifacts(
            completed.job_id,
            catalog=catalog,  # type: ignore[arg-type]
            object_store=store,
            owner_id="worker",
            fencing_token=1,
            outputs=outputs,
            _retention_cleanup_as_integrity=True,
        )

    assert raised.value is cleanup_failure
    assert store.callback_failure is None
    assert catalog.calls == 1


def test_retaining_store_cannot_swallow_a_callback_failure() -> None:
    outputs = (_artifact("bm25", "1", b"primary"),)
    completed = _completed_job(outputs)
    wrong = replace(completed, result_snapshot_id="snapshot_" + "f" * 64)
    store = _ExceptionSwallowingStore([])
    catalog = _CatalogSpy(wrong, store, [])

    with pytest.raises(StorageIntegrityError) as raised:
        publish_job_artifacts(
            completed.job_id,
            catalog=catalog,  # type: ignore[arg-type]
            object_store=store,
            owner_id="worker",
            fencing_token=1,
            outputs=outputs,
        )

    assert catalog.calls == 1
    assert raised.value is store.callback_failure


def test_retaining_store_cannot_swallow_a_second_callback_invocation() -> None:
    outputs = (_artifact("bm25", "1", b"primary"),)
    completed = _completed_job(outputs)
    store = _DoubleInvokingExceptionSwallowingStore([])
    catalog = _CatalogSpy(completed, store, [])

    with pytest.raises(StorageIntegrityError, match="more than once") as raised:
        publish_job_artifacts(
            completed.job_id,
            catalog=catalog,  # type: ignore[arg-type]
            object_store=store,
            owner_id="worker",
            fencing_token=1,
            outputs=outputs,
        )

    assert catalog.calls == 1
    assert raised.value is store.second_failure


def test_retaining_store_cannot_defer_the_callback_past_retention() -> None:
    outputs = (_artifact("bm25", "1", b"primary"),)
    completed = _completed_job(outputs)
    store = _DeferredCallbackStore([])
    catalog = _CatalogSpy(completed, store, [])

    with pytest.raises(StorageIntegrityError, match="did not invoke"):
        publish_job_artifacts(
            completed.job_id,
            catalog=catalog,  # type: ignore[arg-type]
            object_store=store,
            owner_id="worker",
            fencing_token=1,
            outputs=outputs,
        )

    assert store.callback is not None
    with pytest.raises(StorageIntegrityError, match="outside its retention scope"):
        store.callback()
    assert catalog.calls == 0


@pytest.mark.parametrize("failure_type", (KeyboardInterrupt, SystemExit))
@pytest.mark.parametrize(
    "retention_outcome",
    ("return", "cleanup_failure", "second_attempt"),
)
def test_callback_base_exception_remains_the_exact_primary_failure(
    failure_type: type[BaseException],
    retention_outcome: str,
) -> None:
    outputs = (_artifact("bm25", "1", b"primary"),)
    completed = _completed_job(outputs)
    failure = failure_type("catalog callback failure")
    if retention_outcome == "return":
        store: _ExceptionSwallowingStore = _ExceptionSwallowingStore([])
    elif retention_outcome == "cleanup_failure":
        store = _CleanupFailingStore(
            [],
            RuntimeError("retention cleanup failure"),
        )
    else:
        store = _CallbackFailureThenSecondInvokingStore([])
    catalog = _CatalogSpy(completed, store, [], failure=failure)

    with pytest.raises(failure_type) as raised:
        publish_job_artifacts(
            completed.job_id,
            catalog=catalog,  # type: ignore[arg-type]
            object_store=store,
            owner_id="worker",
            fencing_token=1,
            outputs=outputs,
        )

    assert raised.value is failure
    assert store.callback_failure is failure
    assert catalog.calls == 1
    if retention_outcome == "second_attempt":
        assert isinstance(
            getattr(store, "second_failure", None),
            StorageIntegrityError,
        )


@pytest.mark.parametrize(
    ("trace_point", "expected_catalog_calls"),
    (
        ("first_opcode", 0),
        ("attested_assignment", 1),
        ("return_opcode", 1),
        ("return_event", 1),
    ),
)
def test_trace_injected_callback_failure_remains_exact(
    trace_point: str,
    expected_catalog_calls: int,
) -> None:
    outputs = (_artifact("bm25", "1", b"primary"),)
    completed = _completed_job(outputs)
    inner_code = next(
        constant
        for constant in publish_job_artifacts.__code__.co_consts
        if getattr(constant, "co_name", None) == "publish_retained_inner"
    )
    instructions = {
        instruction.offset: instruction
        for instruction in dis.get_instructions(inner_code)
    }

    def warm_inner_opcode_tracing(frame, event: str, arg):
        if event == "call" and frame.f_code is inner_code:
            frame.f_trace_opcodes = True
        return warm_inner_opcode_tracing

    warm_store = _RetainingOnlyStore([])
    warm_catalog = _CatalogSpy(completed, warm_store, [])
    previous_trace = sys.gettrace()
    sys.settrace(warm_inner_opcode_tracing)
    try:
        warm_result = publish_job_artifacts(
            completed.job_id,
            catalog=warm_catalog,  # type: ignore[arg-type]
            object_store=warm_store,
            owner_id="warm-worker",
            fencing_token=1,
            outputs=outputs,
        )
    finally:
        sys.settrace(previous_trace)
    assert warm_result is completed
    assert warm_catalog.calls == 1

    store = _ExceptionSwallowingStore([])
    catalog = _CatalogSpy(completed, store, [])
    failure = KeyboardInterrupt(f"trace injection at {trace_point}")
    injected = False

    def inject_at_inner_boundary(frame, event: str, arg):
        nonlocal injected
        if frame.f_code is not inner_code:
            return None
        frame.f_trace_opcodes = True
        should_inject = trace_point == "return_event" and event == "return"
        if event == "opcode":
            instruction = instructions[frame.f_lasti]
            should_inject = (
                trace_point == "first_opcode"
                or (
                    trace_point == "attested_assignment"
                    and instruction.opname == "STORE_DEREF"
                    and instruction.argval == "attested_result"
                )
                or (
                    trace_point == "return_opcode"
                    and instruction.opname == "RETURN_VALUE"
                )
            )
        if should_inject and not injected:
            injected = True
            raise failure
        return inject_at_inner_boundary

    previous_trace = sys.gettrace()
    try:
        sys.settrace(inject_at_inner_boundary)
        with pytest.raises(KeyboardInterrupt) as raised:
            publish_job_artifacts(
                completed.job_id,
                catalog=catalog,  # type: ignore[arg-type]
                object_store=store,
                owner_id="worker",
                fencing_token=1,
                outputs=outputs,
            )
    finally:
        sys.settrace(previous_trace)

    assert injected
    assert raised.value is failure
    assert store.callback_failure is failure
    assert catalog.calls == expected_catalog_calls


@pytest.mark.parametrize("invoke_callback", (False, True))
def test_retention_failure_keeps_exact_identity_without_callback_failure(
    invoke_callback: bool,
) -> None:
    outputs = (_artifact("bm25", "1", b"primary"),)
    completed = _completed_job(outputs)
    failure = RuntimeError("retention failure")
    store = _CleanupFailingStore(
        [],
        failure,
        invoke_callback=invoke_callback,
    )
    catalog = _CatalogSpy(completed, store, [])

    with pytest.raises(RuntimeError) as raised:
        publish_job_artifacts(
            completed.job_id,
            catalog=catalog,  # type: ignore[arg-type]
            object_store=store,
            owner_id="worker",
            fencing_token=1,
            outputs=outputs,
        )

    assert raised.value is failure
    assert catalog.calls == int(invoke_callback)


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
