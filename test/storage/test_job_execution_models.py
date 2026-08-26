# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for backend-neutral index-job execution records."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import replace

import pytest

from codenib.storage.models import (
    INDEX_JOB_EVENT_PAYLOAD_MAX_DEPTH,
    INDEX_JOB_EVENT_PAYLOAD_MAX_KEY_CHARS,
    INDEX_JOB_EVENT_PAYLOAD_MAX_NODES,
    INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS,
    MAX_INDEX_JOB_EVENTS_PER_ATTEMPT,
    IndexJobAttemptCompletionRecord,
    IndexJobAttemptHeartbeat,
    IndexJobAttemptRecord,
    IndexJobCompletion,
    IndexJobEffectiveMode,
    IndexJobEventKind,
    IndexJobEventRecord,
    IndexJobRecord,
    IndexJobRequest,
    IndexJobRunnableCursor,
    IndexJobRunnableCycle,
    IndexJobRunnablePage,
    IndexJobStatus,
    IndexJobViewOutcome,
    RefJobLease,
    StorageValidationError,
    snapshot_index_job_event_payload,
)


def _request() -> IndexJobRequest:
    return IndexJobRequest.create(
        "repo_" + "a" * 64,
        "src_" + "b" * 64,
        "request",
        {
            "contract": "codenib.index-job-request.v1",
            "views": {
                "bm25": {
                    "profile_id": "profile_" + "c" * 64,
                    "requested_mode": "full",
                    "required": True,
                }
            },
        },
    )


def _job(*, created_at_ms: int = 10) -> IndexJobRecord:
    request = _request()
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
        status=IndexJobStatus.QUEUED,
        cancel_requested=False,
        attempt_count=0,
        result_snapshot_id=None,
        error_code=None,
        error_message=None,
        created_at_ms=created_at_ms,
        updated_at_ms=created_at_ms,
        started_at_ms=None,
        finished_at_ms=None,
    )


def test_execution_contract_bounds_are_explicit() -> None:
    assert INDEX_JOB_EVENT_PAYLOAD_MAX_DEPTH == 16
    assert INDEX_JOB_EVENT_PAYLOAD_MAX_NODES == 1_024
    assert INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS == 16 * 1_024
    assert INDEX_JOB_EVENT_PAYLOAD_MAX_KEY_CHARS == 128
    assert MAX_INDEX_JOB_EVENTS_PER_ATTEMPT == 256


def test_attempt_completion_and_heartbeat_close_exact_authority() -> None:
    request = _request()
    attempt = IndexJobAttemptRecord(
        job_id=request.job_id,
        attempt_count=1,
        repository_id=request.repository_id,
        ref_name=request.ref_name,
        request_digest=request.request_digest,
        owner_id="worker-1",
        fencing_token=3,
        started_at_ms=100,
    )
    completion = IndexJobAttemptCompletionRecord(
        job_id=attempt.job_id,
        attempt_count=attempt.attempt_count,
        owner_id=attempt.owner_id,
        fencing_token=attempt.fencing_token,
        outcome=IndexJobCompletion.REQUEUE,
        error_code="transient_failure",
        error_message=None,
        completed_at_ms=110,
    )
    lease = RefJobLease(
        repository_id=attempt.repository_id,
        ref_name=attempt.ref_name,
        job_id=attempt.job_id,
        owner_id=attempt.owner_id,
        fencing_token=attempt.fencing_token,
        acquired_at_ms=100,
        heartbeat_at_ms=105,
        lease_expires_at_ms=205,
    )
    heartbeat = IndexJobAttemptHeartbeat(
        job_id=attempt.job_id,
        attempt_count=attempt.attempt_count,
        cancel_requested=True,
        lease=lease,
    )

    assert completion.outcome is IndexJobCompletion.REQUEUE
    assert heartbeat.lease == lease
    with pytest.raises(StorageValidationError, match="another job"):
        IndexJobAttemptHeartbeat(
            job_id="job_" + "f" * 64,
            attempt_count=1,
            cancel_requested=False,
            lease=lease,
        )


def test_progress_and_view_result_events_are_structured_and_canonical() -> None:
    request = _request()
    progress = IndexJobEventRecord.create(
        sequence=1,
        job_id=request.job_id,
        attempt_count=1,
        event_key="compile-started",
        kind=IndexJobEventKind.PROGRESS,
        owner_id="worker",
        fencing_token=1,
        view_type="bm25",
        payload={"completed": 0, "phase": "capture"},
        created_at_ms=100,
    )
    result = IndexJobEventRecord.create(
        sequence=2,
        job_id=request.job_id,
        attempt_count=1,
        event_key="bm25-result",
        kind=IndexJobEventKind.VIEW_RESULT,
        owner_id="worker",
        fencing_token=1,
        view_type="bm25",
        effective_mode=IndexJobEffectiveMode.REBUILD_FALLBACK,
        outcome=IndexJobViewOutcome.SUCCEEDED,
        payload={"documents": 8},
        created_at_ms=101,
    )

    assert progress.payload_json == '{"completed":0,"phase":"capture"}'
    assert result.effective_mode is IndexJobEffectiveMode.REBUILD_FALLBACK
    assert result.outcome is IndexJobViewOutcome.SUCCEEDED
    with pytest.raises(StorageValidationError, match="require view"):
        IndexJobEventRecord.create(
            sequence=3,
            job_id=request.job_id,
            attempt_count=1,
            event_key="invalid-result",
            kind=IndexJobEventKind.VIEW_RESULT,
            owner_id="worker",
            fencing_token=1,
            payload={},
            created_at_ms=102,
        )
    with pytest.raises(StorageValidationError, match="cannot carry"):
        IndexJobEventRecord.create(
            sequence=3,
            job_id=request.job_id,
            attempt_count=1,
            event_key="invalid-progress",
            kind=IndexJobEventKind.PROGRESS,
            owner_id="worker",
            fencing_token=1,
            effective_mode=IndexJobEffectiveMode.FULL,
            payload={},
            created_at_ms=102,
        )


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"api_key": "do-not-store"}, "secret field"),
        ({"x": object()}, "non-exact JSON"),
        ({"x" * 129: 1}, "object key"),
        ({"x": "y" * (16 * 1_024)}, "invalid text"),
        ({"x": 2**63}, "invalid integer"),
    ],
)
def test_event_payload_rejects_secrets_and_unbounded_or_nonexact_values(
    payload: dict[str, object], message: str
) -> None:
    with pytest.raises(StorageValidationError, match=message):
        snapshot_index_job_event_payload(payload)


def test_event_payload_enforces_depth_nodes_and_exact_container_types() -> None:
    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(INDEX_JOB_EVENT_PAYLOAD_MAX_DEPTH):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    with pytest.raises(StorageValidationError, match="depth"):
        snapshot_index_job_event_payload(nested)
    with pytest.raises(StorageValidationError, match="node"):
        snapshot_index_job_event_payload(
            {"items": [None] * INDEX_JOB_EVENT_PAYLOAD_MAX_NODES}
        )
    with pytest.raises(StorageValidationError, match="exact object"):
        snapshot_index_job_event_payload([])


def test_event_record_detaches_payload_and_rejects_invalid_persisted_json() -> None:
    request = _request()
    payload = {"phase": ["capture"]}
    event = IndexJobEventRecord.create(
        sequence=1,
        job_id=request.job_id,
        attempt_count=1,
        event_key="phase",
        kind=IndexJobEventKind.PROGRESS,
        owner_id="worker",
        fencing_token=1,
        payload=payload,
        created_at_ms=100,
    )
    payload["phase"].append("mutated")  # type: ignore[union-attr]
    assert event.payload == {"phase": ["capture"]}

    values = {
        field: getattr(event, field)
        for field in event.__dataclass_fields__
        if field != "payload_json"
    }
    with pytest.raises(StorageValidationError, match="valid JSON"):
        IndexJobEventRecord(**values, payload_json="{")
    with pytest.raises(StorageValidationError, match="secret field"):
        IndexJobEventRecord(
            **values,
            payload_json=json.dumps({"authorization": "hidden"}),
        )


def test_event_payload_distinguishes_none_from_a_falsey_mapping() -> None:
    class FalseyPayload(dict[str, object]):
        def __bool__(self) -> bool:
            return False

    request = _request()
    event = IndexJobEventRecord.create(
        sequence=1,
        job_id=request.job_id,
        attempt_count=1,
        event_key="falsey",
        kind=IndexJobEventKind.PROGRESS,
        owner_id="worker",
        fencing_token=1,
        payload=FalseyPayload(preserved=0),
        created_at_ms=100,
    )

    assert event.payload == {"preserved": 0}


def test_event_payload_mapping_detach_is_bounded_and_rejects_lying_iterators() -> None:
    class ProbeMapping(Mapping[str, object]):
        def __init__(
            self,
            *,
            item_count: int,
            reported_lengths: tuple[int, ...],
            duplicate: bool = False,
        ) -> None:
            self.item_count = item_count
            self.reported_lengths = iter(reported_lengths)
            self.duplicate = duplicate
            self.pulls = 0
            self.lookups = 0

        def __len__(self) -> int:
            return next(self.reported_lengths)

        def __iter__(self) -> Iterator[str]:
            for index in range(self.item_count):
                self.pulls += 1
                yield "key" if self.duplicate else f"key-{index}"

        def __getitem__(self, key: str) -> object:
            self.lookups += 1
            return 0

    oversized = ProbeMapping(item_count=50_000, reported_lengths=(50_000,))
    with pytest.raises(StorageValidationError, match="node limit"):
        snapshot_index_job_event_payload(oversized)
    assert oversized.pulls == 0
    assert oversized.lookups == 0

    lying = ProbeMapping(item_count=50_000, reported_lengths=(1, 1))
    with pytest.raises(StorageValidationError, match="node limit"):
        snapshot_index_job_event_payload(lying)
    assert lying.pulls == INDEX_JOB_EVENT_PAYLOAD_MAX_NODES
    assert lying.lookups == INDEX_JOB_EVENT_PAYLOAD_MAX_NODES - 1

    duplicate = ProbeMapping(
        item_count=2,
        reported_lengths=(2, 2),
        duplicate=True,
    )
    with pytest.raises(StorageValidationError, match="duplicate object key"):
        snapshot_index_job_event_payload(duplicate)
    assert duplicate.pulls == 2
    assert duplicate.lookups == 1

    changed = ProbeMapping(item_count=1, reported_lengths=(1, 2))
    with pytest.raises(StorageValidationError, match="changed"):
        snapshot_index_job_event_payload(changed)


def test_event_payload_mapping_detach_preserves_base_exceptions() -> None:
    class InterruptedMapping(Mapping[str, object]):
        def __len__(self) -> int:
            return 1

        def __iter__(self) -> Iterator[str]:
            raise KeyboardInterrupt

        def __getitem__(self, key: str) -> object:
            raise AssertionError(key)

    with pytest.raises(KeyboardInterrupt):
        snapshot_index_job_event_payload(InterruptedMapping())


def test_execution_cursors_and_fencing_tokens_are_exact_int64_values() -> None:
    too_large = 2**63
    job = _job()
    with pytest.raises(StorageValidationError, match="cursor time"):
        IndexJobRunnableCursor(too_large, job.job_id)
    with pytest.raises(StorageValidationError, match="cycle job sequence"):
        IndexJobRunnableCycle(too_large)
    with pytest.raises(StorageValidationError, match="fencing token"):
        RefJobLease(
            repository_id=job.repository_id,
            ref_name=job.ref_name,
            job_id=job.job_id,
            owner_id="worker",
            fencing_token=too_large,
            acquired_at_ms=1,
            heartbeat_at_ms=1,
            lease_expires_at_ms=2,
        )


def test_execution_models_reject_int_subclasses_and_non_int_scalars() -> None:
    class IntSubclass(int):
        pass

    job = _job()
    attempt = IndexJobAttemptRecord(
        job_id=job.job_id,
        attempt_count=1,
        repository_id=job.repository_id,
        ref_name=job.ref_name,
        request_digest=job.request_digest,
        owner_id="worker",
        fencing_token=1,
        started_at_ms=10,
    )
    event = IndexJobEventRecord.create(
        sequence=1,
        job_id=job.job_id,
        attempt_count=1,
        event_key="event",
        kind=IndexJobEventKind.PROGRESS,
        owner_id="worker",
        fencing_token=1,
        created_at_ms=10,
    )
    completion = IndexJobAttemptCompletionRecord(
        job_id=job.job_id,
        attempt_count=1,
        owner_id="worker",
        fencing_token=1,
        outcome=IndexJobCompletion.REQUEUE,
        error_code="retry",
        error_message=None,
        completed_at_ms=11,
    )
    lease = RefJobLease(
        repository_id=job.repository_id,
        ref_name=job.ref_name,
        job_id=job.job_id,
        owner_id="worker",
        fencing_token=1,
        acquired_at_ms=10,
        heartbeat_at_ms=10,
        lease_expires_at_ms=20,
    )
    for value in (True, 1.0, IntSubclass(1), -(2**63), 2**63):
        with pytest.raises(StorageValidationError, match="cursor time"):
            IndexJobRunnableCursor(value, job.job_id)  # type: ignore[arg-type]
        with pytest.raises(StorageValidationError, match="cycle job sequence"):
            IndexJobRunnableCycle(value)  # type: ignore[arg-type]
        with pytest.raises(StorageValidationError, match="fencing token"):
            replace(attempt, fencing_token=value)
        with pytest.raises(StorageValidationError, match="attempt count"):
            replace(attempt, attempt_count=value)
        with pytest.raises(StorageValidationError, match="event sequence"):
            replace(event, sequence=value)
        with pytest.raises(StorageValidationError, match="job attempt"):
            replace(job, attempt_count=value)
        with pytest.raises(StorageValidationError, match="start time"):
            replace(attempt, started_at_ms=value)
        with pytest.raises(StorageValidationError, match="completion time"):
            replace(completion, completed_at_ms=value)
        with pytest.raises(StorageValidationError, match="job event time"):
            replace(event, created_at_ms=value)
        with pytest.raises(StorageValidationError, match="lease fencing token"):
            replace(lease, fencing_token=value)


def test_job_lifecycle_rejects_stranded_queued_and_failed_zero_states() -> None:
    job = _job()
    with pytest.raises(StorageValidationError, match="uncancelled and retryable"):
        replace(job, cancel_requested=True)
    with pytest.raises(StorageValidationError, match="uncancelled and retryable"):
        replace(job, attempt_count=job.max_attempts)
    with pytest.raises(StorageValidationError, match="at least one attempt"):
        replace(
            job,
            status=IndexJobStatus.FAILED,
            error_code="failed",
            finished_at_ms=job.updated_at_ms,
        )


def test_runnable_page_requires_deterministic_exact_records() -> None:
    job = _job(created_at_ms=10)
    cursor = IndexJobRunnableCursor(job.created_at_ms, job.job_id)
    page = IndexJobRunnablePage((job,), cursor)

    assert page.next_cursor == cursor
    with pytest.raises(StorageValidationError, match="exact tuple"):
        IndexJobRunnablePage([job], None)  # type: ignore[arg-type]
    later = _job(created_at_ms=11)
    with pytest.raises(StorageValidationError, match="ordering"):
        IndexJobRunnablePage((later, job), None)
