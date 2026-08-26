# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pure unit tests for the backend-neutral durable index-job worker."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

import codenib.storage.job_worker as job_worker_module
from codenib.storage.cas import BlobInfo
from codenib.storage.job_worker import (
    IndexJobExecutionContext,
    IndexJobExecutionResult,
    IndexJobExecutor,
    IndexJobStopReason,
    IndexJobViewExecutionResult,
    IndexJobWorker,
    IndexJobWorkerDisposition,
)
from codenib.storage.models import (
    INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS,
    INDEX_JOB_REQUEST_CONTRACT,
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
    IndexJobRunnablePage,
    IndexJobStatus,
    IndexJobViewOutcome,
    IndexJobViewOutput,
    IndexJobViewRecord,
    PublishConflict,
    RefJobLease,
    StorageIntegrityError,
    StorageNotFound,
    StorageValidationError,
    content_id,
)
from codenib.storage.publication import IndexJobViewArtifact

_REPOSITORY_ID = "repo_" + "a" * 64
_SOURCE_REVISION_ID = "src_" + "b" * 64


def _profile_id(view_type: str) -> str:
    marker = hashlib.sha256(view_type.encode("utf-8")).hexdigest()
    return "profile_" + marker


def _receipt(payload: bytes) -> BlobInfo:
    digest = hashlib.sha256(payload).hexdigest()
    return BlobInfo(
        digest=digest,
        byte_size=len(payload),
        storage_key=f"sha256/{digest[:2]}/{digest[2:]}",
    )


def _artifact(view: IndexJobViewRecord) -> IndexJobViewArtifact:
    return IndexJobViewArtifact.create(
        view.view_type,
        view.profile_id,
        _receipt(f"artifact:{view.view_type}".encode("utf-8")),
        schema_version="test.worker.v1",
        media_type="application/x-test-worker-view",
        metadata={"fixture": "job-worker"},
    )


def _request(
    idempotency_key: str,
    view_requirements: Mapping[str, bool],
    *,
    max_attempts: int = 3,
) -> IndexJobRequest:
    return IndexJobRequest.create(
        _REPOSITORY_ID,
        _SOURCE_REVISION_ID,
        idempotency_key,
        {
            "contract": INDEX_JOB_REQUEST_CONTRACT,
            "views": {
                view_type: {
                    "profile_id": _profile_id(view_type),
                    "requested_mode": "full",
                    "required": required,
                }
                for view_type, required in view_requirements.items()
            },
        },
        max_attempts=max_attempts,
    )


def _queued_job(request: IndexJobRequest, created_at_ms: int) -> IndexJobRecord:
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


def _published_snapshot_id(
    job: IndexJobRecord,
    outputs: tuple[IndexJobViewOutput, ...],
) -> str:
    members: list[list[str]] = []
    for output in outputs:
        generation_id = content_id(
            "view",
            {
                "repository_id": job.repository_id,
                "source_revision_id": job.source_revision_id,
                "profile_id": output.profile_id,
                "view_type": output.view_type,
                "object_digest": output.object_record.digest,
                "schema_version": output.schema_version,
                "metadata": output.generation_metadata,
            },
        )
        members.append([output.view_type, generation_id])
    return content_id(
        "snapshot",
        {
            "repository_id": job.repository_id,
            "source_revision_id": job.source_revision_id,
            "views": members,
        },
    )


@dataclass
class _Backend:
    jobs: dict[str, IndexJobRecord] = field(default_factory=dict)
    views: dict[str, tuple[IndexJobViewRecord, ...]] = field(default_factory=dict)
    attempts: dict[tuple[str, int], IndexJobAttemptRecord] = field(default_factory=dict)
    leases: dict[str, RefJobLease] = field(default_factory=dict)
    completions: dict[tuple[str, int], IndexJobAttemptCompletionRecord] = field(
        default_factory=dict
    )
    scan_job_ids: list[str] = field(default_factory=list)
    claim_conflicts: set[str] = field(default_factory=set)
    claim_not_found: set[str] = field(default_factory=set)
    acquire_response_loss: BaseException | None = None
    acquire_response_loss_raised: bool = False
    acquire_retry_failure: BaseException | None = None
    takeover_after_acquire: bool = False
    takeover_tail_state: str | None = None
    close_running_tail_during_history_read: bool = False
    skip_takeover_attempt_number: bool = False
    hide_latest_attempt_from_history: bool = False
    oversized_attempt_history: bool = False
    takeover_during_history_read: bool = False
    takeover_during_history_read_done: bool = False
    takeovers_during_completion_history: int = 0
    takeover_after_completion_miss: bool = False
    completion_miss_seen: bool = False
    takeover_after_completion_miss_done: bool = False
    forge_success_during_reconcile: bool = False
    close_claim_after_acquire: str | None = None
    completion_foreign_job_id: bool = False
    attempt_history_exceeds_max: bool = False
    acquire_calls: list[tuple[int, str, str]] = field(default_factory=list)
    heartbeat_calls: list[tuple[int, int, str]] = field(default_factory=list)
    progress_calls: list[dict[str, Any]] = field(default_factory=list)
    view_result_calls: list[dict[str, Any]] = field(default_factory=list)
    completion_calls: list[dict[str, Any]] = field(default_factory=list)
    publication_calls: list[tuple[IndexJobViewOutput, ...]] = field(
        default_factory=list
    )
    sessions: list[int] = field(default_factory=list)
    main_session_id: int | None = None
    next_session_id: int = 1
    next_fencing_token: int = 1
    next_event_sequence: int = 1
    bind_corruption: str | None = None
    background_heartbeat_error: BaseException | None = None
    heartbeat_corrupt_acquired_at: bool = False
    heartbeat_response_mode: str | None = None
    remove_job_on_background_not_found: bool = False
    background_cancel_on_call: int | None = None
    background_authority_loss_on_call: int | None = None
    background_heartbeat_count: int = 0
    cancel_on_final_heartbeat: bool = False
    authority_lost: bool = False
    completion_response_loss: BaseException | None = None
    completion_return_none: bool = False
    completion_return_damaged: bool = False
    completion_hide: bool = False
    completion_foreign_authority: bool = False
    completion_substitute_outcome: IndexJobCompletion | None = None
    completion_substitute_error_code: str | None = None
    completion_response_corruption: str | None = None
    completion_completed_at_ms_override: int | None = None
    completion_cancel_race: bool = False
    takeover_on_completion_call: bool = False
    forge_success_on_completion_conflict: bool = False
    takeover_successor_updated_before_completion: bool = False
    takeover_adjacency_corruption: str | None = None
    historical_completion_before_start: bool = False
    publication_mode: str = "success"
    publication_failure: BaseException | None = None
    publication_completed_at_ms_override: int | None = None
    progress_failure: BaseException | None = None
    progress_commit_response_loss: BaseException | None = None
    progress_retry_failure: BaseException | None = None
    progress_response_loss_raised: bool = False
    corrupt_progress_response: bool = False
    progress_replay_corruption: str | None = None
    progress_new_event_corruption: str | None = None
    corrupt_view_result_response: bool = False
    view_result_time_regression: bool = False
    view_result_failure: Exception | None = None
    view_result_mutate_then_raise: BaseException | None = None
    view_result_mutation_raised: bool = False
    view_result_commit_response_loss: BaseException | None = None
    view_result_retry_failure: BaseException | None = None
    view_result_response_loss_raised: bool = False
    mutate_progress_payload: bool = False
    ignore_scan_limit: bool = False
    reverse_scan_page_after_create: bool = False
    damage_scan_candidate_after_create: bool = False
    suppress_session_exceptions: bool = False
    heartbeat_entered: threading.Event = field(default_factory=threading.Event)
    second_heartbeat_entered: threading.Event = field(default_factory=threading.Event)
    heartbeat_release: threading.Event = field(default_factory=threading.Event)
    cancellation_observed: threading.Event = field(default_factory=threading.Event)
    authority_loss_observed: threading.Event = field(default_factory=threading.Event)
    events_by_key: dict[tuple[str, int, str], IndexJobEventRecord] = field(
        default_factory=dict
    )
    closed_sessions: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.heartbeat_release.set()

    def add_job(
        self,
        idempotency_key: str,
        view_requirements: Mapping[str, bool] | None = None,
        *,
        max_attempts: int = 3,
    ) -> IndexJobRecord:
        requirements = (
            {"bm25": True} if view_requirements is None else view_requirements
        )
        request = _request(
            idempotency_key,
            requirements,
            max_attempts=max_attempts,
        )
        job = _queued_job(request, 10 + len(self.jobs))
        self.jobs[job.job_id] = job
        self.views[job.job_id] = request.view_requests
        self.scan_job_ids.append(job.job_id)
        return job


class _FakeCatalog:
    def __init__(self, backend: _Backend, session_id: int) -> None:
        self.backend = backend
        self.session_id = session_id

    def __enter__(self) -> _FakeCatalog:
        return self

    def __exit__(self, *_args: object) -> bool | None:
        self.backend.closed_sessions.append(self.session_id)
        return True if self.backend.suppress_session_exceptions else None

    def create_job(self, *_args: object, **_kwargs: object) -> IndexJobRecord:
        raise NotImplementedError

    def _commit_takeover(self, job_id: str, *, lease_duration_ms: int) -> None:
        backend = self.backend
        running = backend.jobs[job_id]
        lease = backend.leases[job_id]
        attempt = backend.attempts[(job_id, running.attempt_count)]
        event_high_water = max(
            (
                event.created_at_ms
                for event in backend.events_by_key.values()
                if event.job_id == job_id
                and event.attempt_count == attempt.attempt_count
            ),
            default=0,
        )
        completed_at_ms = (
            max(
                running.updated_at_ms,
                lease.heartbeat_at_ms,
                event_high_water,
            )
            + 1
        )
        if backend.historical_completion_before_start and attempt.attempt_count == 2:
            completed_at_ms = attempt.started_at_ms - 1
        backend.completions[(job_id, attempt.attempt_count)] = (
            IndexJobAttemptCompletionRecord(
                job_id=job_id,
                attempt_count=attempt.attempt_count,
                owner_id=attempt.owner_id,
                fencing_token=attempt.fencing_token,
                outcome=IndexJobCompletion.REQUEUE,
                error_code="lease_expired",
                error_message=None,
                completed_at_ms=completed_at_ms,
            )
        )
        next_attempt_count = attempt.attempt_count + (
            2 if backend.skip_takeover_attempt_number else 1
        )
        next_started_at_ms = completed_at_ms + 1
        next_token = backend.next_fencing_token
        backend.next_fencing_token += 1
        if next_attempt_count == 2:
            if backend.takeover_adjacency_corruption == "start":
                next_started_at_ms = completed_at_ms - 1
            elif backend.takeover_adjacency_corruption == "fencing_token":
                next_token = attempt.fencing_token
        next_owner = f"takeover-owner-{next_attempt_count}"
        backend.jobs[job_id] = replace(
            running,
            status=IndexJobStatus.RUNNING,
            attempt_count=next_attempt_count,
            error_code="lease_expired",
            updated_at_ms=completed_at_ms + 1,
        )
        backend.attempts[(job_id, next_attempt_count)] = IndexJobAttemptRecord(
            job_id=running.job_id,
            attempt_count=next_attempt_count,
            repository_id=running.repository_id,
            ref_name=running.ref_name,
            request_digest=running.request_digest,
            owner_id=next_owner,
            fencing_token=next_token,
            started_at_ms=next_started_at_ms,
        )
        backend.leases[job_id] = RefJobLease(
            repository_id=running.repository_id,
            ref_name=running.ref_name,
            job_id=running.job_id,
            owner_id=next_owner,
            fencing_token=next_token,
            acquired_at_ms=next_started_at_ms,
            heartbeat_at_ms=next_started_at_ms,
            lease_expires_at_ms=next_started_at_ms + lease_duration_ms,
        )
        if backend.takeover_successor_updated_before_completion:
            backend.jobs[job_id] = replace(
                backend.jobs[job_id],
                updated_at_ms=completed_at_ms - 1,
            )

    def get_job(self, job_id: str) -> IndexJobRecord:
        if (
            self.backend.takeover_after_completion_miss
            and self.backend.completion_miss_seen
            and not self.backend.takeover_after_completion_miss_done
        ):
            self.backend.takeover_after_completion_miss_done = True
            self._commit_takeover(job_id, lease_duration_ms=30)
        if (
            self.backend.bind_corruption == "job_missing"
            and self.backend.jobs.get(job_id) is not None
            and self.backend.jobs[job_id].status is IndexJobStatus.RUNNING
        ):
            raise StorageNotFound("claimed job is absent")
        try:
            return self.backend.jobs[job_id]
        except KeyError as exc:
            raise StorageNotFound("job is absent") from exc

    def get_job_views(self, job_id: str) -> tuple[IndexJobViewRecord, ...]:
        if self.backend.bind_corruption == "views_missing":
            raise StorageNotFound("claimed views are absent")
        views = self.backend.views[job_id]
        if self.backend.bind_corruption == "view_mode_raw":
            object.__setattr__(views[0], "requested_mode", "full")
        if self.backend.bind_corruption == "views_oversized":
            return (views[0],) * 65
        if self.backend.bind_corruption == "views_reversed":
            return tuple(reversed(views))
        return views

    def acquire_job_lease(
        self,
        job_id: str,
        *,
        owner_id: str,
        lease_duration_ms: int,
    ) -> RefJobLease:
        backend = self.backend
        backend.acquire_calls.append((self.session_id, job_id, owner_id))
        if job_id in backend.claim_not_found:
            raise StorageNotFound("scanned job disappeared")
        if job_id in backend.claim_conflicts:
            raise PublishConflict("claim lost")
        existing = backend.leases.get(job_id)
        if existing is not None and existing.owner_id == owner_id:
            if backend.acquire_retry_failure is not None:
                raise backend.acquire_retry_failure
            return existing
        queued = backend.jobs[job_id]
        attempt_count = queued.attempt_count + 1
        started_at_ms = 100 + attempt_count
        token = backend.next_fencing_token
        backend.next_fencing_token += 1
        lease = RefJobLease(
            repository_id=queued.repository_id,
            ref_name=queued.ref_name,
            job_id=queued.job_id,
            owner_id=owner_id,
            fencing_token=token,
            acquired_at_ms=started_at_ms,
            heartbeat_at_ms=started_at_ms,
            lease_expires_at_ms=started_at_ms + lease_duration_ms,
        )
        running = replace(
            queued,
            status=IndexJobStatus.RUNNING,
            attempt_count=attempt_count,
            updated_at_ms=started_at_ms,
            started_at_ms=queued.started_at_ms or started_at_ms,
        )
        if backend.bind_corruption == "job_updated_subclass":
            object.__setattr__(
                running,
                "updated_at_ms",
                _IntSubclass(running.updated_at_ms),
            )
        elif backend.bind_corruption == "job_updated_overflow":
            object.__setattr__(running, "updated_at_ms", 2**63)
        attempt_owner = (
            "corrupt-owner" if backend.bind_corruption == "attempt_owner" else owner_id
        )
        attempt = IndexJobAttemptRecord(
            job_id=running.job_id,
            attempt_count=attempt_count,
            repository_id=running.repository_id,
            ref_name=running.ref_name,
            request_digest=running.request_digest,
            owner_id=attempt_owner,
            fencing_token=token,
            started_at_ms=started_at_ms,
        )
        if backend.bind_corruption == "running_updated_before_attempt":
            forged_start = started_at_ms + 1
            attempt = replace(attempt, started_at_ms=forged_start)
            lease = replace(
                lease,
                acquired_at_ms=forged_start,
                heartbeat_at_ms=forged_start,
                lease_expires_at_ms=forged_start + lease_duration_ms,
            )
        elif backend.bind_corruption in {
            "attempt_before_job_creation",
            "attempt_before_job_start",
        }:
            forged_start = (
                running.created_at_ms - 1
                if backend.bind_corruption == "attempt_before_job_creation"
                else running.started_at_ms - 1
            )
            attempt = replace(attempt, started_at_ms=forged_start)
            lease = replace(
                lease,
                acquired_at_ms=forged_start,
                heartbeat_at_ms=forged_start,
                lease_expires_at_ms=forged_start + lease_duration_ms,
            )
        elif backend.bind_corruption == "attempt_fence_below_count":
            forged_token = attempt_count - 1
            attempt = replace(attempt, fencing_token=forged_token)
            lease = replace(lease, fencing_token=forged_token)
        elif backend.bind_corruption == "initial_lease_heartbeat":
            lease = replace(
                lease,
                heartbeat_at_ms=lease.heartbeat_at_ms + 1,
            )
        elif backend.bind_corruption == "initial_lease_expiry":
            lease = replace(
                lease,
                lease_expires_at_ms=lease.lease_expires_at_ms + 1,
            )
        elif backend.bind_corruption == "initial_lease_time_subclass":
            object.__setattr__(
                lease,
                "heartbeat_at_ms",
                _IntSubclass(lease.heartbeat_at_ms),
            )
        elif backend.bind_corruption == "initial_lease_time_overflow":
            object.__setattr__(lease, "heartbeat_at_ms", 2**63)
            object.__setattr__(lease, "lease_expires_at_ms", 2**63 + 1)
        backend.jobs[job_id] = running
        backend.attempts[(job_id, attempt_count)] = attempt
        backend.leases[job_id] = lease
        if backend.takeover_after_acquire:
            backend.takeover_after_acquire = False
            self._commit_takeover(job_id, lease_duration_ms=lease_duration_ms)
            tail_job = backend.jobs[job_id]
            tail_attempt = backend.attempts[(job_id, tail_job.attempt_count)]
            if backend.takeover_tail_state == "queued_missing_completion":
                backend.jobs[job_id] = replace(
                    tail_job,
                    status=IndexJobStatus.QUEUED,
                    error_code="tail_requeue",
                    updated_at_ms=tail_attempt.started_at_ms + 1,
                )
            elif backend.takeover_tail_state == "succeeded_with_completion":
                completed_at_ms = tail_attempt.started_at_ms + 1
                backend.completions[(job_id, tail_attempt.attempt_count)] = (
                    IndexJobAttemptCompletionRecord(
                        job_id=job_id,
                        attempt_count=tail_attempt.attempt_count,
                        owner_id=tail_attempt.owner_id,
                        fencing_token=tail_attempt.fencing_token,
                        outcome=IndexJobCompletion.REQUEUE,
                        error_code="bogus_success_completion",
                        error_message=None,
                        completed_at_ms=completed_at_ms,
                    )
                )
                backend.jobs[job_id] = replace(
                    tail_job,
                    status=IndexJobStatus.SUCCEEDED,
                    result_snapshot_id="snapshot_" + ("d" * 64),
                    error_code=None,
                    error_message=None,
                    updated_at_ms=completed_at_ms,
                    finished_at_ms=completed_at_ms,
                )
        if backend.close_claim_after_acquire == "failed_foreign_completion":
            completed_at_ms = started_at_ms + 1
            backend.completions[(job_id, attempt_count)] = (
                IndexJobAttemptCompletionRecord(
                    job_id=job_id,
                    attempt_count=attempt_count,
                    owner_id=owner_id,
                    fencing_token=token,
                    outcome=IndexJobCompletion.FAILED,
                    error_code="claim_closed",
                    error_message=None,
                    completed_at_ms=completed_at_ms,
                )
            )
            backend.jobs[job_id] = replace(
                running,
                status=IndexJobStatus.FAILED,
                error_code="claim_closed",
                updated_at_ms=completed_at_ms,
                finished_at_ms=completed_at_ms,
            )
            backend.completion_foreign_job_id = True
        elif backend.close_claim_after_acquire == "cancelled_success":
            completed_at_ms = started_at_ms + 1
            backend.jobs[job_id] = replace(
                running,
                status=IndexJobStatus.SUCCEEDED,
                cancel_requested=True,
                result_snapshot_id="snapshot_" + ("a" * 64),
                updated_at_ms=completed_at_ms,
                finished_at_ms=completed_at_ms,
            )
        if backend.bind_corruption == "attempt_damaged":
            object.__delattr__(attempt, "request_digest")
        if backend.bind_corruption == "lease_damaged":
            object.__delattr__(lease, "heartbeat_at_ms")
        if backend.bind_corruption == "lease_type":
            return object()  # type: ignore[return-value]
        if backend.bind_corruption == "lease_owner":
            return replace(lease, owner_id="corrupt-owner")
        if (
            backend.acquire_response_loss is not None
            and not backend.acquire_response_loss_raised
        ):
            backend.acquire_response_loss_raised = True
            raise backend.acquire_response_loss
        return lease

    def renew_job_lease(self, *_args: object, **_kwargs: object) -> RefJobLease:
        raise NotImplementedError

    def request_job_cancel(self, job_id: str) -> IndexJobRecord:
        job = self.backend.jobs[job_id]
        requested = replace(
            job, cancel_requested=True, updated_at_ms=job.updated_at_ms + 1
        )
        self.backend.jobs[job_id] = requested
        return requested

    def finish_job_attempt(self, *_args: object, **_kwargs: object) -> IndexJobRecord:
        raise NotImplementedError

    def scan_runnable_jobs(
        self,
        *,
        cursor: object = None,
        limit: int = 64,
    ) -> IndexJobRunnablePage:
        del cursor
        scan_limit = (
            len(self.backend.scan_job_ids) if self.backend.ignore_scan_limit else limit
        )
        jobs = tuple(
            self.backend.jobs[job_id]
            for job_id in self.backend.scan_job_ids[:scan_limit]
            if self.backend.jobs[job_id].status is IndexJobStatus.QUEUED
        )
        page = IndexJobRunnablePage(jobs=jobs, next_cursor=None)
        if self.backend.bind_corruption == "job_status_raw" and page.jobs:
            object.__setattr__(page.jobs[0], "status", "queued")
        if self.backend.damage_scan_candidate_after_create and page.jobs:
            object.__delattr__(page.jobs[0], "error_message")
        if self.backend.reverse_scan_page_after_create:
            object.__setattr__(page, "jobs", tuple(reversed(page.jobs)))
        return page

    def get_job_attempt(
        self,
        job_id: str,
        attempt_count: int,
    ) -> IndexJobAttemptRecord:
        if self.backend.bind_corruption == "attempt_missing":
            raise StorageNotFound("claimed attempt is absent")
        return self.backend.attempts[(job_id, attempt_count)]

    def list_job_attempts(self, job_id: str) -> tuple[IndexJobAttemptRecord, ...]:
        if (
            self.backend.takeover_during_history_read
            and not self.backend.takeover_during_history_read_done
        ):
            self.backend.takeover_during_history_read_done = True
            self._commit_takeover(job_id, lease_duration_ms=30)
        attempts = tuple(
            value
            for (candidate, _attempt), value in sorted(self.backend.attempts.items())
            if candidate == job_id
        )
        if self.backend.hide_latest_attempt_from_history and attempts:
            return attempts[:-1]
        if self.backend.oversized_attempt_history and attempts:
            return attempts + ((attempts[-1],) * (1_001 - len(attempts)))
        if self.backend.attempt_history_exceeds_max and attempts:
            job = self.backend.jobs[job_id]
            extra = tuple(
                replace(
                    attempts[-1],
                    attempt_count=count,
                    owner_id=f"future-owner-{count}",
                    fencing_token=count,
                    started_at_ms=attempts[-1].started_at_ms + count,
                )
                for count in range(attempts[-1].attempt_count + 1, job.max_attempts + 2)
            )
            return attempts + extra
        return attempts

    def get_job_attempt_completion(
        self,
        job_id: str,
        attempt_count: int,
    ) -> IndexJobAttemptCompletionRecord:
        if self.backend.forge_success_during_reconcile:
            self.backend.forge_success_during_reconcile = False
            job = self.backend.jobs[job_id]
            self.backend.jobs[job_id] = replace(
                job,
                status=IndexJobStatus.SUCCEEDED,
                result_snapshot_id="snapshot_" + ("c" * 64),
                error_code=None,
                error_message=None,
                updated_at_ms=500,
                finished_at_ms=500,
            )
            raise StorageNotFound("forged success has no completion")
        if (
            self.backend.takeover_after_completion_miss
            and not self.backend.completion_miss_seen
            and self.session_id == self.backend.main_session_id
        ):
            self.backend.completion_miss_seen = True
            raise StorageNotFound("completion was absent before takeover")
        if self.backend.completion_return_none:
            return None  # type: ignore[return-value]
        if self.backend.completion_hide:
            raise StorageNotFound("completion is hidden")
        try:
            completion = self.backend.completions[(job_id, attempt_count)]
        except KeyError as exc:
            raise StorageNotFound("completion is absent") from exc
        if self.backend.completion_return_damaged:
            object.__delattr__(completion, "error_message")
        if self.backend.completion_foreign_authority:
            return replace(completion, owner_id="foreign-completion-owner")
        if self.backend.completion_foreign_job_id:
            return replace(completion, job_id="foreign-job")
        return completion

    def list_job_attempt_completions(
        self,
        job_id: str,
    ) -> tuple[IndexJobAttemptCompletionRecord, ...]:
        if self.backend.completion_hide:
            return ()
        while self.backend.takeovers_during_completion_history > 0:
            self.backend.takeovers_during_completion_history -= 1
            self._commit_takeover(job_id, lease_duration_ms=30)
        if self.backend.close_running_tail_during_history_read:
            job = self.backend.jobs[job_id]
            if job.status is IndexJobStatus.RUNNING:
                attempt = self.backend.attempts[(job_id, job.attempt_count)]
                completed_at_ms = (
                    max(
                        job.updated_at_ms,
                        self.backend.leases[job_id].heartbeat_at_ms,
                    )
                    + 1
                )
                self.backend.completions[(job_id, job.attempt_count)] = (
                    IndexJobAttemptCompletionRecord(
                        job_id=job_id,
                        attempt_count=job.attempt_count,
                        owner_id=attempt.owner_id,
                        fencing_token=attempt.fencing_token,
                        outcome=IndexJobCompletion.REQUEUE,
                        error_code="history_read_requeue",
                        error_message=None,
                        completed_at_ms=completed_at_ms,
                    )
                )
                self.backend.jobs[job_id] = replace(
                    job,
                    status=IndexJobStatus.QUEUED,
                    error_code="history_read_requeue",
                    updated_at_ms=completed_at_ms,
                )
            self.backend.close_running_tail_during_history_read = False
        values = tuple(
            value
            for (candidate, _attempt), value in sorted(self.backend.completions.items())
            if candidate == job_id
        )
        if self.backend.completion_return_damaged and values:
            object.__delattr__(values[-1], "error_message")
        if self.backend.completion_foreign_authority and values:
            values = values[:-1] + (
                replace(values[-1], owner_id="foreign-completion-owner"),
            )
        if self.backend.completion_foreign_job_id and values:
            values = values[:-1] + (replace(values[-1], job_id="foreign-job"),)
        return values

    def heartbeat_job_attempt(
        self,
        job_id: str,
        *,
        attempt_count: int,
        owner_id: str,
        fencing_token: int,
        lease_duration_ms: int,
    ) -> IndexJobAttemptHeartbeat:
        backend = self.backend
        is_background = self.session_id != backend.main_session_id
        backend.heartbeat_calls.append(
            (
                self.session_id,
                threading.get_ident(),
                "background" if is_background else "main",
            )
        )
        if is_background:
            backend.background_heartbeat_count += 1
            backend.heartbeat_entered.set()
            if backend.background_heartbeat_count >= 2:
                backend.second_heartbeat_entered.set()
            if not backend.heartbeat_release.wait(5):
                raise AssertionError("test did not release heartbeat")
            if backend.background_heartbeat_error is not None:
                if backend.remove_job_on_background_not_found and isinstance(
                    backend.background_heartbeat_error, StorageNotFound
                ):
                    backend.jobs.pop(job_id, None)
                raise backend.background_heartbeat_error
            if (
                backend.background_cancel_on_call is not None
                and backend.background_heartbeat_count
                >= backend.background_cancel_on_call
            ):
                self.request_job_cancel(job_id)
                backend.cancellation_observed.set()
            if (
                backend.background_authority_loss_on_call is not None
                and backend.background_heartbeat_count
                >= backend.background_authority_loss_on_call
            ):
                backend.authority_lost = True
                backend.authority_loss_observed.set()
        elif backend.cancel_on_final_heartbeat:
            self.request_job_cancel(job_id)
        if backend.authority_lost:
            raise PublishConflict("authority lost")
        lease = backend.leases[job_id]
        if (
            lease.owner_id != owner_id
            or lease.fencing_token != fencing_token
            or backend.jobs[job_id].attempt_count != attempt_count
        ):
            raise PublishConflict("stale heartbeat authority")
        heartbeat_at_ms = lease.heartbeat_at_ms + 1
        lease_expires_at_ms = heartbeat_at_ms + lease_duration_ms
        if backend.heartbeat_response_mode == "same_heartbeat":
            heartbeat_at_ms = lease.heartbeat_at_ms
            lease_expires_at_ms = lease.lease_expires_at_ms + 1
        elif backend.heartbeat_response_mode == "expiry_not_advanced":
            lease_expires_at_ms = lease.lease_expires_at_ms
        elif (
            backend.heartbeat_response_mode == "heartbeat_regression"
            and backend.background_heartbeat_count >= 2
        ):
            heartbeat_at_ms = lease.heartbeat_at_ms - 1
            lease_expires_at_ms = lease.lease_expires_at_ms + 1
        elif backend.heartbeat_response_mode == "heartbeat_ahead" or (
            backend.heartbeat_response_mode == "final_heartbeat_ahead"
            and not is_background
        ):
            heartbeat_at_ms = max(600, heartbeat_at_ms)
            lease_expires_at_ms = heartbeat_at_ms + lease_duration_ms
        renewed = replace(
            lease,
            acquired_at_ms=(
                lease.acquired_at_ms + 1
                if backend.heartbeat_corrupt_acquired_at
                else lease.acquired_at_ms
            ),
            heartbeat_at_ms=heartbeat_at_ms,
            lease_expires_at_ms=lease_expires_at_ms,
        )
        if backend.heartbeat_response_mode == "time_subclass":
            object.__setattr__(
                renewed,
                "heartbeat_at_ms",
                _IntSubclass(renewed.heartbeat_at_ms),
            )
        backend.leases[job_id] = renewed
        return IndexJobAttemptHeartbeat(
            job_id=job_id,
            attempt_count=attempt_count,
            cancel_requested=backend.jobs[job_id].cancel_requested,
            lease=renewed,
        )

    def _event(
        self,
        *,
        job_id: str,
        attempt_count: int,
        owner_id: str,
        fencing_token: int,
        event_key: str,
        kind: IndexJobEventKind,
        payload: Mapping[str, Any] | None,
        view_type: str | None,
        effective_mode: IndexJobEffectiveMode | None,
        outcome: IndexJobViewOutcome | None,
    ) -> IndexJobEventRecord:
        backend = self.backend
        identity = (job_id, attempt_count, event_key)
        existing = backend.events_by_key.get(identity)
        if existing is not None:
            expected = IndexJobEventRecord.create(
                sequence=existing.sequence,
                job_id=job_id,
                attempt_count=attempt_count,
                event_key=event_key,
                kind=kind,
                owner_id=owner_id,
                fencing_token=fencing_token,
                payload=payload,
                view_type=view_type,
                effective_mode=effective_mode,
                outcome=outcome,
                created_at_ms=existing.created_at_ms,
            )
            if expected != existing:
                raise PublishConflict("event replay mismatch")
            if kind is IndexJobEventKind.PROGRESS:
                if backend.progress_replay_corruption == "sequence":
                    return replace(existing, sequence=existing.sequence + 1)
                if backend.progress_replay_corruption == "time":
                    return replace(
                        existing,
                        created_at_ms=existing.created_at_ms + 1,
                    )
            return existing
        prior_progress = tuple(
            event
            for event in backend.events_by_key.values()
            if event.job_id == job_id
            and event.attempt_count == attempt_count
            and event.kind is IndexJobEventKind.PROGRESS
        )
        sequence = backend.next_event_sequence
        backend.next_event_sequence += 1
        event = IndexJobEventRecord.create(
            sequence=sequence,
            job_id=job_id,
            attempt_count=attempt_count,
            event_key=event_key,
            kind=kind,
            owner_id=owner_id,
            fencing_token=fencing_token,
            payload=payload,
            view_type=view_type,
            effective_mode=effective_mode,
            outcome=outcome,
            created_at_ms=200 + sequence,
        )
        backend.events_by_key[identity] = event
        if kind is IndexJobEventKind.PROGRESS:
            if backend.progress_new_event_corruption == "before_start":
                attempt = backend.attempts[(job_id, attempt_count)]
                return replace(event, created_at_ms=attempt.started_at_ms - 1)
            if prior_progress and backend.progress_new_event_corruption == "sequence":
                return replace(event, sequence=prior_progress[-1].sequence)
            if prior_progress and backend.progress_new_event_corruption == "time":
                return replace(
                    event,
                    created_at_ms=prior_progress[-1].created_at_ms - 1,
                )
        return event

    def append_job_event(
        self,
        job_id: str,
        *,
        attempt_count: int,
        owner_id: str,
        fencing_token: int,
        event_key: str,
        payload: Mapping[str, Any] | None = None,
        view_type: str | None = None,
    ) -> IndexJobEventRecord:
        self.backend.progress_calls.append(
            {
                "session_id": self.session_id,
                "event_key": event_key,
                "payload": payload,
                "view_type": view_type,
            }
        )
        if (
            self.backend.progress_response_loss_raised
            and self.backend.progress_retry_failure is not None
        ):
            raise self.backend.progress_retry_failure
        if self.backend.progress_failure is not None:
            raise self.backend.progress_failure
        if self.backend.mutate_progress_payload:
            assert type(payload) is dict
            payload["phase"] = "tampered"
        event = self._event(
            job_id=job_id,
            attempt_count=attempt_count,
            owner_id=owner_id,
            fencing_token=fencing_token,
            event_key=event_key,
            kind=IndexJobEventKind.PROGRESS,
            payload=payload,
            view_type=view_type,
            effective_mode=None,
            outcome=None,
        )
        if (
            self.backend.progress_commit_response_loss is not None
            and not self.backend.progress_response_loss_raised
        ):
            self.backend.progress_response_loss_raised = True
            raise self.backend.progress_commit_response_loss
        if self.backend.corrupt_progress_response:
            return "not-an-event"  # type: ignore[return-value]
        return event

    def record_job_view_result(
        self,
        job_id: str,
        *,
        attempt_count: int,
        owner_id: str,
        fencing_token: int,
        event_key: str,
        view_type: str,
        effective_mode: IndexJobEffectiveMode,
        outcome: IndexJobViewOutcome,
        payload: Mapping[str, Any] | None = None,
    ) -> IndexJobEventRecord:
        self.backend.view_result_calls.append(
            {
                "session_id": self.session_id,
                "event_key": event_key,
                "view_type": view_type,
                "effective_mode": effective_mode,
                "outcome": outcome,
                "payload": payload,
            }
        )
        if (
            self.backend.view_result_response_loss_raised
            and self.backend.view_result_retry_failure is not None
        ):
            raise self.backend.view_result_retry_failure
        if (
            self.backend.view_result_mutate_then_raise is not None
            and not self.backend.view_result_mutation_raised
        ):
            assert type(payload) is dict
            payload["documents"] = "tampered"
            self.backend.view_result_mutation_raised = True
            raise self.backend.view_result_mutate_then_raise
        if self.backend.view_result_failure is not None:
            raise self.backend.view_result_failure
        event = self._event(
            job_id=job_id,
            attempt_count=attempt_count,
            owner_id=owner_id,
            fencing_token=fencing_token,
            event_key=event_key,
            kind=IndexJobEventKind.VIEW_RESULT,
            payload=payload,
            view_type=view_type,
            effective_mode=effective_mode,
            outcome=outcome,
        )
        if (
            self.backend.view_result_commit_response_loss is not None
            and not self.backend.view_result_response_loss_raised
        ):
            self.backend.view_result_response_loss_raised = True
            raise self.backend.view_result_commit_response_loss
        if self.backend.view_result_time_regression:
            prior_progress = tuple(
                value
                for value in self.backend.events_by_key.values()
                if value.job_id == job_id
                and value.attempt_count == attempt_count
                and value.kind is IndexJobEventKind.PROGRESS
            )
            assert prior_progress
            return replace(
                event,
                created_at_ms=prior_progress[-1].created_at_ms - 1,
            )
        if self.backend.corrupt_view_result_response:
            return "not-an-event"  # type: ignore[return-value]
        return event

    def list_job_events(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 128,
    ) -> tuple[IndexJobEventRecord, ...]:
        return tuple(
            event
            for event in sorted(
                self.backend.events_by_key.values(), key=lambda item: item.sequence
            )
            if event.job_id == job_id and event.sequence > after_sequence
        )[:limit]

    def complete_job_attempt(
        self,
        job_id: str,
        *,
        attempt_count: int,
        owner_id: str,
        fencing_token: int,
        outcome: IndexJobCompletion,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> IndexJobRecord:
        backend = self.backend
        backend.completion_calls.append(
            {
                "session_id": self.session_id,
                "job_id": job_id,
                "attempt_count": attempt_count,
                "owner_id": owner_id,
                "fencing_token": fencing_token,
                "outcome": outcome,
                "error_code": error_code,
                "error_message": error_message,
            }
        )
        if backend.forge_success_on_completion_conflict:
            backend.forge_success_on_completion_conflict = False
            job = backend.jobs[job_id]
            backend.jobs[job_id] = replace(
                job,
                status=IndexJobStatus.SUCCEEDED,
                result_snapshot_id="snapshot_" + ("b" * 64),
                error_code=None,
                error_message=None,
                updated_at_ms=500,
                finished_at_ms=500,
            )
            raise PublishConflict("forged success won the completion race")
        if backend.takeover_on_completion_call:
            backend.takeover_on_completion_call = False
            self._commit_takeover(job_id, lease_duration_ms=30)
            raise PublishConflict("takeover won the completion race")
        if (
            backend.completion_cancel_race
            and outcome is not IndexJobCompletion.CANCELLED
        ):
            backend.completion_cancel_race = False
            self.request_job_cancel(job_id)
            raise PublishConflict("cancellation won")
        if backend.authority_lost:
            raise PublishConflict("authority lost")
        job = backend.jobs[job_id]
        persisted_outcome = backend.completion_substitute_outcome or outcome
        code = (
            "cancelled"
            if persisted_outcome is IndexJobCompletion.CANCELLED
            else backend.completion_substitute_error_code or error_code
        )
        assert code is not None
        completed_at_ms = (
            400
            if backend.completion_completed_at_ms_override is None
            else backend.completion_completed_at_ms_override
        )
        completion = IndexJobAttemptCompletionRecord(
            job_id=job_id,
            attempt_count=attempt_count,
            owner_id=owner_id,
            fencing_token=fencing_token,
            outcome=persisted_outcome,
            error_code=code,
            error_message=error_message,
            completed_at_ms=completed_at_ms,
        )
        backend.completions[(job_id, attempt_count)] = completion
        if persisted_outcome is IndexJobCompletion.REQUEUE:
            updated = replace(
                job,
                status=IndexJobStatus.QUEUED,
                updated_at_ms=completed_at_ms,
                result_snapshot_id=None,
                error_code=code,
                error_message=error_message,
                finished_at_ms=None,
            )
        elif persisted_outcome is IndexJobCompletion.FAILED:
            updated = replace(
                job,
                status=IndexJobStatus.FAILED,
                updated_at_ms=completed_at_ms,
                error_code=code,
                error_message=error_message,
                finished_at_ms=completed_at_ms,
            )
        else:
            updated = replace(
                job,
                status=IndexJobStatus.CANCELLED,
                cancel_requested=True,
                updated_at_ms=completed_at_ms,
                error_code=code,
                error_message=error_message,
                finished_at_ms=completed_at_ms,
            )
        backend.jobs[job_id] = updated
        if backend.completion_response_corruption == "running_after_requeue":
            backend.jobs[job_id] = replace(
                updated,
                status=IndexJobStatus.RUNNING,
                cancel_requested=False,
                error_code=None,
                error_message=None,
                finished_at_ms=None,
            )
        elif backend.completion_response_corruption == "failed_cancel_requested":
            backend.jobs[job_id] = replace(updated, cancel_requested=True)
        elif backend.completion_response_corruption == "shifted_terminal_time":
            backend.jobs[job_id] = replace(
                updated,
                updated_at_ms=401,
                finished_at_ms=401,
            )
        elif backend.completion_response_corruption == "later_attempt_after_failed":
            backend.jobs[job_id] = replace(
                updated,
                status=IndexJobStatus.RUNNING,
                cancel_requested=False,
                attempt_count=updated.attempt_count + 1,
                error_code=None,
                error_message=None,
                updated_at_ms=401,
                finished_at_ms=None,
            )
        elif backend.completion_response_corruption == "success_without_completion":
            backend.completions.pop((job_id, attempt_count), None)
            backend.jobs[job_id] = replace(
                updated,
                status=IndexJobStatus.SUCCEEDED,
                cancel_requested=False,
                result_snapshot_id="snapshot_" + ("e" * 64),
                error_code=None,
                error_message=None,
                updated_at_ms=401,
                finished_at_ms=401,
            )
        if backend.completion_response_loss is not None:
            raise backend.completion_response_loss
        return updated

    def publish_job_outputs(
        self,
        job_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        outputs: tuple[IndexJobViewOutput, ...],
    ) -> IndexJobRecord:
        backend = self.backend
        backend.publication_calls.append(outputs)
        if backend.publication_mode == "conflict_takeover":
            self._commit_takeover(job_id, lease_duration_ms=30)
            raise PublishConflict("takeover won the publication race")
        if backend.publication_mode == "conflict_cancel":
            self.request_job_cancel(job_id)
            raise PublishConflict("publication observed cancellation")
        if backend.publication_mode == "conflict_lost":
            backend.authority_lost = True
            raise PublishConflict("publication lost authority")
        if backend.publication_mode == "conflict_permanent":
            raise PublishConflict("publication precondition failed")
        job = backend.jobs[job_id]
        if backend.publication_mode == "failure_then_raise":
            self.complete_job_attempt(
                job_id,
                attempt_count=job.attempt_count,
                owner_id=owner_id,
                fencing_token=fencing_token,
                outcome=IndexJobCompletion.FAILED,
                error_code="substituted_failure",
                error_message=None,
            )
            assert backend.publication_failure is not None
            raise backend.publication_failure
        if backend.publication_mode == "success_wrong_identity":
            foreign_request = IndexJobRequest(
                repository_id=job.repository_id,
                source_revision_id="foreign-source-revision",
                ref_name="different-ref",
                idempotency_key=job.idempotency_key,
                expected_ref_generation=job.expected_ref_generation,
                max_attempts=job.max_attempts,
                request_json=job.request_json,
            )
            job = replace(
                job,
                source_revision_id=foreign_request.source_revision_id,
                ref_name=foreign_request.ref_name,
                request_digest=foreign_request.request_digest,
            )
        snapshot_id = _published_snapshot_id(job, outputs)
        completed_at_ms = (
            500
            if backend.publication_completed_at_ms_override is None
            else backend.publication_completed_at_ms_override
        )
        succeeded = replace(
            job,
            status=IndexJobStatus.SUCCEEDED,
            result_snapshot_id=snapshot_id,
            error_code=None,
            error_message=None,
            updated_at_ms=completed_at_ms,
            finished_at_ms=completed_at_ms,
        )
        if backend.publication_mode.startswith("success_cancel_requested"):
            succeeded = replace(succeeded, cancel_requested=True)
        if backend.publication_mode.startswith("success_shifted_updated"):
            succeeded = replace(
                succeeded,
                updated_at_ms=completed_at_ms + 1,
            )
        if backend.publication_mode in {
            "success_wrong_snapshot_then_raise",
            "success_wrong_snapshot_then_conflict",
        }:
            succeeded = replace(
                succeeded,
                result_snapshot_id="snapshot_" + ("f" * 64),
            )
        backend.jobs[job_id] = succeeded
        if backend.publication_mode in {
            "success_then_raise",
            "success_cancel_requested_then_raise",
            "success_wrong_snapshot_then_raise",
            "success_shifted_updated_then_raise",
        }:
            assert backend.publication_failure is not None
            raise backend.publication_failure
        if backend.publication_mode == "success_wrong_snapshot_then_conflict":
            raise PublishConflict("different publication closure already committed")
        return succeeded


class _SessionFactory:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend

    def __call__(self) -> _FakeCatalog:
        session_id = self.backend.next_session_id
        self.backend.next_session_id += 1
        self.backend.sessions.append(session_id)
        if self.backend.main_session_id is None:
            self.backend.main_session_id = session_id
        return _FakeCatalog(self.backend, session_id)


class _RetainingStore:
    def __init__(self) -> None:
        self.retained: list[tuple[BlobInfo, ...]] = []

    def put_bytes(self, data: bytes) -> BlobInfo:
        return _receipt(data)

    def put_file(self, source: str | Path) -> BlobInfo:
        return _receipt(Path(source).read_bytes())

    def has(self, digest: str) -> bool:
        return bool(digest)

    def open(self, digest: str):
        raise NotImplementedError(digest)

    def read_bytes(self, digest: str) -> bytes:
        raise NotImplementedError(digest)

    def verify(self, digest: str) -> BlobInfo:
        raise NotImplementedError(digest)

    def materialize(self, digest: str, destination: str | Path) -> Path:
        raise NotImplementedError(digest, destination)

    def verify_receipt(self, expected: BlobInfo) -> BlobInfo:
        return expected

    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], Any],
    ) -> Any:
        self.retained.append(expected)
        return callback()


class _ReplacingRetainingStore(_RetainingStore):
    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], Any],
    ) -> Any:
        super().retain_receipts(expected, callback)
        return object()


class _Executor:
    def __init__(
        self,
        operation: Callable[[IndexJobExecutionContext], IndexJobExecutionResult],
    ) -> None:
        self.operation = operation
        self.calls: list[IndexJobExecutionContext] = []

    def execute(self, context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        self.calls.append(context)
        return self.operation(context)


class _Resolver:
    def __init__(
        self,
        executor: IndexJobExecutor,
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.executor = executor
        self.failure = failure
        self.calls: list[tuple[IndexJobRecord, tuple[IndexJobViewRecord, ...]]] = []

    def resolve(
        self,
        job: IndexJobRecord,
        views: tuple[IndexJobViewRecord, ...],
    ) -> IndexJobExecutor:
        self.calls.append((job, views))
        if self.failure is not None:
            raise self.failure
        return self.executor


def _succeeded_result(
    context: IndexJobExecutionContext,
    *,
    skip_optional: bool = True,
) -> IndexJobExecutionResult:
    results = []
    for view in context.views:
        if skip_optional and not view.required:
            results.append(
                IndexJobViewExecutionResult.create(
                    view,
                    effective_mode=IndexJobEffectiveMode.UNAVAILABLE,
                    outcome=IndexJobViewOutcome.SKIPPED,
                    payload={"reason": "not-selected"},
                )
            )
        else:
            results.append(
                IndexJobViewExecutionResult.create(
                    view,
                    effective_mode=IndexJobEffectiveMode.FULL,
                    outcome=IndexJobViewOutcome.SUCCEEDED,
                    artifact=_artifact(view),
                    payload={"documents": 3},
                )
            )
    return IndexJobExecutionResult(tuple(results), retryable=False)


def _failed_result(
    context: IndexJobExecutionContext,
    *,
    retryable: bool,
    error_code: str = "controlled_failure",
) -> IndexJobExecutionResult:
    return IndexJobExecutionResult(
        tuple(
            IndexJobViewExecutionResult.create(
                view,
                effective_mode=IndexJobEffectiveMode.FULL,
                outcome=IndexJobViewOutcome.FAILED,
                payload={"phase": "build"},
            )
            for view in context.views
        ),
        retryable=retryable,
        error_code=error_code,
    )


def _worker(
    backend: _Backend,
    resolver: _Resolver,
    *,
    owner_id_factory: Callable[[], str] = lambda: "worker-default",
    lease_duration_ms: int = 300,
    heartbeat_interval_ms: int = 50,
    scan_limit: int = 64,
) -> tuple[IndexJobWorker, _RetainingStore, _SessionFactory]:
    store = _RetainingStore()
    factory = _SessionFactory(backend)
    worker = IndexJobWorker(
        catalog_factory=factory,
        object_store=store,
        resolver=resolver,
        lease_duration_ms=lease_duration_ms,
        heartbeat_interval_ms=heartbeat_interval_ms,
        scan_limit=scan_limit,
        owner_id_factory=owner_id_factory,
        monotonic=lambda: 0.0,
    )
    return worker, store, factory


def _default_resolver() -> _Resolver:
    return _Resolver(_Executor(_succeeded_result))


class _IntSubclass(int):
    pass


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("lease_duration_ms", True),
        ("lease_duration_ms", 0),
        ("lease_duration_ms", 2_147_483_648),
        ("lease_duration_ms", _IntSubclass(300)),
        ("heartbeat_interval_ms", False),
        ("heartbeat_interval_ms", 0),
        ("heartbeat_interval_ms", _IntSubclass(50)),
        ("scan_limit", True),
        ("scan_limit", 0),
        ("scan_limit", 257),
        ("scan_limit", _IntSubclass(64)),
    ),
)
def test_worker_constructor_rejects_nonexact_or_out_of_range_integers(
    field: str,
    value: object,
) -> None:
    backend = _Backend()
    arguments: dict[str, object] = {
        "catalog_factory": _SessionFactory(backend),
        "object_store": _RetainingStore(),
        "resolver": _default_resolver(),
        "lease_duration_ms": 300,
        "heartbeat_interval_ms": 50,
        "scan_limit": 64,
    }
    arguments[field] = value

    with pytest.raises(StorageValidationError):
        IndexJobWorker(**arguments)  # type: ignore[arg-type]


def test_worker_constructor_requires_three_intervals_strictly_below_lease() -> None:
    backend = _Backend()
    common = {
        "catalog_factory": _SessionFactory(backend),
        "object_store": _RetainingStore(),
        "resolver": _default_resolver(),
        "heartbeat_interval_ms": 100,
    }
    with pytest.raises(StorageValidationError, match="less than one third"):
        IndexJobWorker(lease_duration_ms=300, **common)

    IndexJobWorker(lease_duration_ms=301, **common)


def test_worker_constructor_rejects_a_different_bound_resolver_store() -> None:
    backend = _Backend()
    worker_store = _RetainingStore()
    resolver_store = _RetainingStore()
    resolver = _default_resolver()
    resolver.object_store = resolver_store  # type: ignore[attr-defined]

    with pytest.raises(StorageValidationError, match="same object store"):
        IndexJobWorker(
            catalog_factory=_SessionFactory(backend),
            object_store=worker_store,
            resolver=resolver,
            lease_duration_ms=301,
            heartbeat_interval_ms=100,
        )


def test_empty_scan_returns_idle_without_creating_an_owner() -> None:
    backend = _Backend()
    owners: list[str] = []
    resolver = _default_resolver()
    worker, store, _factory = _worker(
        backend,
        resolver,
        owner_id_factory=lambda: owners.append("unexpected") or "unexpected",
    )

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.IDLE
    assert result.job_id is None
    assert owners == []
    assert resolver.calls == []
    assert store.retained == []


def test_scan_page_cannot_exceed_the_requested_bound() -> None:
    backend = _Backend(ignore_scan_limit=True)
    backend.add_job("first")
    backend.add_job("second")
    worker, store, _factory = _worker(
        backend,
        _default_resolver(),
        scan_limit=1,
    )

    with pytest.raises(StorageIntegrityError, match="requested page limit"):
        worker.run_once()

    assert backend.acquire_calls == []
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_scan_page_order_is_reattested_before_any_claim() -> None:
    backend = _Backend(reverse_scan_page_after_create=True)
    backend.add_job("ordered-first")
    backend.add_job("ordered-second")
    worker, store, _factory = _worker(backend, _default_resolver())

    with pytest.raises(StorageIntegrityError, match="invalid runnable page"):
        worker.run_once()

    assert backend.acquire_calls == []
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_structurally_damaged_scan_candidate_fails_closed() -> None:
    backend = _Backend(damage_scan_candidate_after_create=True)
    backend.add_job("damaged-scan-candidate")
    worker, store, _factory = _worker(backend, _default_resolver())

    with pytest.raises(StorageIntegrityError, match="invalid runnable page"):
        worker.run_once()

    assert backend.acquire_calls == []
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_claim_conflict_continues_with_a_fresh_owner_for_next_candidate() -> None:
    backend = _Backend()
    first = backend.add_job("first")
    second = backend.add_job("second")
    backend.claim_conflicts.add(first.job_id)
    owners = iter(("owner-first", "owner-second"))
    resolver = _Resolver(
        _Executor(lambda context: _failed_result(context, retryable=False))
    )
    worker, _store, _factory = _worker(
        backend,
        resolver,
        owner_id_factory=lambda: next(owners),
    )

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.FAILED
    assert result.job_id == second.job_id
    assert [call[2] for call in backend.acquire_calls] == [
        "owner-first",
        "owner-second",
    ]
    assert resolver.calls[0][0].job_id == second.job_id


def test_stale_scan_not_found_continues_to_the_next_candidate() -> None:
    backend = _Backend()
    first = backend.add_job("stale-first")
    second = backend.add_job("available-second")
    backend.claim_not_found.add(first.job_id)
    owners = iter(("owner-stale", "owner-available"))
    worker, _store, _factory = _worker(
        backend,
        _Resolver(_Executor(lambda context: _failed_result(context, retryable=False))),
        owner_id_factory=lambda: next(owners),
    )

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.FAILED
    assert result.job_id == second.job_id
    assert [call[2] for call in backend.acquire_calls] == [
        "owner-stale",
        "owner-available",
    ]


def test_claim_response_loss_retries_with_the_same_owner_and_attempt() -> None:
    response_loss = RuntimeError("claim response lost")
    backend = _Backend(acquire_response_loss=response_loss)
    job = backend.add_job("claim-response-loss")
    worker, store, _factory = _worker(backend, _default_resolver())

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.SUCCEEDED
    assert result.job_id == job.job_id
    assert result.attempt_count == 1
    assert len(backend.acquire_calls) == 2
    assert backend.acquire_calls[0][2] == backend.acquire_calls[1][2]
    assert len(backend.attempts) == 1
    assert len(backend.publication_calls) == 1
    assert len(store.retained) == 1


def test_takeover_before_initial_heartbeat_reports_lost_authority() -> None:
    backend = _Backend(takeover_after_acquire=True)
    job = backend.add_job("takeover-before-bind")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.LOST_AUTHORITY
    assert result.job_id == job.job_id
    assert result.attempt_count == 1
    assert backend.jobs[job.job_id].status is IndexJobStatus.RUNNING
    assert backend.jobs[job.job_id].attempt_count == 2
    assert resolver.calls == []
    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize(
    "corruption",
    (
        "missing_closure",
        "missing_current_attempt",
        "missing_middle_attempt",
        "oversized_attempt_history",
        "attempt_history_exceeds_max",
    ),
)
def test_takeover_before_bind_requires_exact_durable_history(corruption: str) -> None:
    backend = _Backend(
        takeover_after_acquire=True,
        completion_hide=corruption == "missing_closure",
        hide_latest_attempt_from_history=corruption == "missing_current_attempt",
        skip_takeover_attempt_number=corruption == "missing_middle_attempt",
        oversized_attempt_history=corruption == "oversized_attempt_history",
        attempt_history_exceeds_max=corruption == "attempt_history_exceeds_max",
    )
    backend.add_job(f"takeover-history-{corruption}")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(StorageIntegrityError):
        worker.run_once()

    assert resolver.calls == []
    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize("corruption", ("start", "fencing_token"))
def test_takeover_history_rejects_invalid_attempt_adjacency(
    corruption: str,
) -> None:
    backend = _Backend(
        takeover_after_acquire=True,
        takeover_adjacency_corruption=corruption,
    )
    backend.add_job(f"takeover-adjacency-{corruption}")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(StorageIntegrityError):
        worker.run_once()

    assert resolver.calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_takeover_history_rejects_a_completion_before_its_own_start() -> None:
    backend = _Backend(
        takeover_after_acquire=True,
        takeover_during_history_read=True,
        historical_completion_before_start=True,
    )
    backend.add_job("takeover-completion-before-start")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(StorageIntegrityError):
        worker.run_once()

    assert resolver.calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_takeover_history_may_advance_during_claim_attestation() -> None:
    backend = _Backend(
        takeover_after_acquire=True,
        takeover_during_history_read=True,
    )
    job = backend.add_job("takeover-during-history-read")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.LOST_AUTHORITY
    assert result.job_id == job.job_id
    assert result.attempt_count == 1
    assert backend.jobs[job.job_id].status is IndexJobStatus.RUNNING
    assert backend.jobs[job.job_id].attempt_count == 3
    assert tuple(sorted(attempt for _, attempt in backend.attempts)) == (1, 2, 3)
    assert tuple(sorted(attempt for _, attempt in backend.completions)) == (1, 2)
    assert resolver.calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_claim_history_converges_across_two_between_read_takeovers() -> None:
    backend = _Backend(takeovers_during_completion_history=2)
    job = backend.add_job("takeover-between-history-lists")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.LOST_AUTHORITY
    assert result.job_id == job.job_id
    assert result.attempt_count == 1
    assert tuple(sorted(attempt for _, attempt in backend.attempts)) == (1, 2, 3)
    assert tuple(sorted(attempt for _, attempt in backend.completions)) == (1, 2)
    assert resolver.calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_superseded_claim_accepts_a_contiguous_post_legacy_history() -> None:
    backend = _Backend(takeover_after_acquire=True)
    backend.next_fencing_token = 2
    job = backend.add_job("takeover-after-legacy-prefix")
    backend.jobs[job.job_id] = replace(
        job,
        attempt_count=1,
        updated_at_ms=50,
        started_at_ms=50,
        error_code="legacy_requeue",
    )
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.LOST_AUTHORITY
    assert result.job_id == job.job_id
    assert result.attempt_count == 2
    assert tuple(sorted(attempt for _, attempt in backend.attempts)) == (2, 3)
    assert tuple(sorted(attempt for _, attempt in backend.completions)) == (2,)
    assert resolver.calls == []
    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize(
    "tail_state",
    ("queued_missing_completion", "succeeded_with_completion"),
)
def test_superseded_claim_requires_an_exact_tail_closure_xor(
    tail_state: str,
) -> None:
    backend = _Backend(
        takeover_after_acquire=True,
        takeover_tail_state=tail_state,
    )
    backend.add_job(f"takeover-tail-{tail_state}")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(StorageIntegrityError):
        worker.run_once()

    assert resolver.calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_running_claim_may_close_while_completion_history_is_read() -> None:
    backend = _Backend(close_running_tail_during_history_read=True)
    job = backend.add_job("running-tail-closes-during-history")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.LOST_AUTHORITY
    assert result.job_id == job.job_id
    assert result.attempt_count == 1
    assert resolver.calls == []
    assert backend.completions[(job.job_id, 1)].outcome is (IndexJobCompletion.REQUEUE)
    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize(
    "post_claim_state",
    ("failed_foreign_completion", "cancelled_success"),
)
def test_closed_claim_requires_an_exact_uncancelled_terminal_state(
    post_claim_state: str,
) -> None:
    backend = _Backend(close_claim_after_acquire=post_claim_state)
    backend.add_job(f"closed-claim-{post_claim_state}")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(StorageIntegrityError):
        worker.run_once()

    assert resolver.calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_claim_retry_surfaces_a_second_integrity_failure() -> None:
    first = RuntimeError("claim response lost")
    second = StorageIntegrityError("claim replay integrity alarm")
    backend = _Backend(
        acquire_response_loss=first,
        acquire_retry_failure=second,
    )
    backend.add_job("claim-retry-integrity")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(StorageIntegrityError) as caught:
        worker.run_once()

    assert caught.value is second
    assert len(backend.acquire_calls) == 2
    assert len(backend.attempts) == 1
    assert resolver.calls == []
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_every_candidate_on_every_run_gets_a_new_owner_identity() -> None:
    backend = _Backend()
    first = backend.add_job("first")
    second = backend.add_job("second")
    backend.claim_conflicts.update((first.job_id, second.job_id))
    owner_values = iter(f"owner-{index}" for index in range(4))
    worker, _store, _factory = _worker(
        backend,
        _default_resolver(),
        owner_id_factory=lambda: next(owner_values),
    )

    assert worker.run_once().disposition is IndexJobWorkerDisposition.IDLE
    assert worker.run_once().disposition is IndexJobWorkerDisposition.IDLE
    assert [call[2] for call in backend.acquire_calls] == [
        "owner-0",
        "owner-1",
        "owner-2",
        "owner-3",
    ]


def test_worker_rejects_reentrant_run_without_waiting() -> None:
    backend = _Backend()
    backend.add_job("blocking")
    entered = threading.Event()
    release = threading.Event()

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        entered.set()
        assert release.wait(5)
        return _failed_result(context, retryable=False)

    worker, _store, _factory = _worker(backend, _Resolver(_Executor(execute)))
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(worker.run_once())
        except Exception as exc:  # pragma: no cover - assertion transport
            outcome.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(5)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            worker.run_once()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(outcome) == 1
    assert not isinstance(outcome[0], BaseException)


@pytest.mark.parametrize("phase", ("before", "after"))
def test_interrupted_heartbeat_start_preserves_failure_and_closes_sessions(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    backend = _Backend()
    backend.add_job("start-interrupt")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)
    failure = KeyboardInterrupt("interrupted after heartbeat readiness")
    original_start = job_worker_module._HeartbeatPump.start

    def start_then_interrupt(pump: object) -> None:
        if phase == "after":
            original_start(pump)  # type: ignore[arg-type]
        raise failure

    monkeypatch.setattr(
        job_worker_module._HeartbeatPump,
        "start",
        start_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        worker.run_once()

    assert caught.value is failure
    assert resolver.calls == []
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []
    assert set(backend.sessions) == set(backend.closed_sessions)
    assert not any(
        thread.name.startswith("codenib-job-heartbeat-")
        for thread in threading.enumerate()
    )


def test_heartbeat_start_note_failure_cannot_replace_the_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HostileInterrupt(KeyboardInterrupt):
        def add_note(self, _note: str) -> None:
            raise SystemExit(23)

    primary = HostileInterrupt("primary heartbeat startup interruption")
    secondary = RuntimeError("heartbeat cleanup failed")
    authority = job_worker_module._AttemptAuthority(
        job_id="job-1",
        repository_id="repo-1",
        source_revision_id="source-1",
        ref_name="main",
        request_digest="request-1",
        attempt_count=1,
        started_at_ms=1,
        owner_id="owner-1",
        fencing_token=1,
    )
    lease = RefJobLease(
        repository_id=authority.repository_id,
        ref_name=authority.ref_name,
        job_id=authority.job_id,
        owner_id=authority.owner_id,
        fencing_token=authority.fencing_token,
        acquired_at_ms=authority.started_at_ms,
        heartbeat_at_ms=authority.started_at_ms,
        lease_expires_at_ms=authority.started_at_ms + 10_000,
    )
    pump = job_worker_module._HeartbeatPump(
        catalog_factory=lambda: None,
        main_catalog=object(),
        authority=authority,
        stop_state=job_worker_module._StopState(),
        causal_evidence=job_worker_module._AttemptCausalEvidence(
            authority,
            job_updated_at_ms=authority.started_at_ms,
            lease=lease,
            lease_duration_ms=10_000,
        ),
        lease_duration_ms=10_000,
        interval_ms=1_000,
        monotonic=lambda: 0.0,
    )

    monkeypatch.setattr(pump._thread, "start", lambda: None)

    def interrupt_wait() -> None:
        raise primary

    def fail_cleanup() -> None:
        raise secondary

    monkeypatch.setattr(pump._ready, "wait", interrupt_wait)
    monkeypatch.setattr(pump, "stop_and_join", fail_cleanup)

    with pytest.raises(HostileInterrupt) as caught:
        pump.start()

    assert caught.value is primary


def test_catalog_session_preserves_rollback_and_exception_precedence() -> None:
    class TransactionalSession:
        def __init__(
            self,
            *,
            suppress: bool = False,
            cleanup_failure: BaseException | None = None,
        ) -> None:
            self.suppress = suppress
            self.cleanup_failure = cleanup_failure
            self.exit_type: type[BaseException] | None = None
            self.committed = False
            self.rolled_back = False

        def __enter__(self) -> TransactionalSession:
            return self

        def __exit__(
            self,
            exception_type: type[BaseException] | None,
            _exception: BaseException | None,
            _traceback: object,
        ) -> bool:
            self.exit_type = exception_type
            self.committed = exception_type is None
            self.rolled_back = exception_type is not None
            if self.cleanup_failure is not None:
                raise self.cleanup_failure
            return self.suppress

    primary = RuntimeError("operation failed")

    for suppress in (False, True):
        session = TransactionalSession(suppress=suppress)

        def fail_operation(_catalog: object) -> object:
            raise primary

        with pytest.raises(RuntimeError) as caught:
            job_worker_module._run_catalog_session(
                lambda current=session: current,
                fail_operation,
            )
        assert caught.value is primary
        assert session.exit_type is RuntimeError
        assert session.rolled_back
        assert not session.committed

    secondary = OSError("session cleanup failed")
    session = TransactionalSession(cleanup_failure=secondary)
    with pytest.raises(RuntimeError) as caught:
        job_worker_module._run_catalog_session(lambda: session, fail_operation)
    assert caught.value is primary
    assert session.exit_type is RuntimeError
    assert session.rolled_back

    session = TransactionalSession(cleanup_failure=secondary)
    with pytest.raises(OSError) as caught:
        job_worker_module._run_catalog_session(lambda: session, lambda _catalog: 1)
    assert caught.value is secondary
    assert session.exit_type is None
    assert session.committed


@pytest.mark.parametrize("phase", ("before", "after"))
def test_interrupted_control_seal_still_stops_the_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    backend = _Backend()
    backend.add_job("seal-interrupt")
    executor = _Executor(_succeeded_result)
    worker, store, _factory = _worker(backend, _Resolver(executor))
    failure = KeyboardInterrupt("interrupted while sealing progress")
    original_seal = job_worker_module._AttemptExecutionControl.seal
    armed = True

    def seal_then_interrupt(control: object) -> None:
        nonlocal armed
        if not armed:
            original_seal(control)  # type: ignore[arg-type]
            return
        armed = False
        if phase == "after":
            original_seal(control)  # type: ignore[arg-type]
        raise failure

    monkeypatch.setattr(
        job_worker_module._AttemptExecutionControl,
        "seal",
        seal_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        worker.run_once()

    assert caught.value is failure
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []
    assert len(executor.calls) == 1
    with pytest.raises(RuntimeError, match="sealed"):
        executor.calls[0].control.append_progress("executor.after-worker")
    assert set(backend.sessions) == set(backend.closed_sessions)
    assert not any(
        thread.name.startswith("codenib-job-heartbeat-")
        for thread in threading.enumerate()
    )


@pytest.mark.parametrize("operation", ("shutdown", "join"))
def test_interrupted_heartbeat_settle_finishes_cleanup_before_rethrow(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    backend = _Backend()
    backend.add_job("settle-interrupt")
    worker, store, _factory = _worker(backend, _default_resolver())
    failure = KeyboardInterrupt(f"interrupted during heartbeat {operation}")
    original_start = job_worker_module._HeartbeatPump.start

    def start_then_arm_interrupt(pump: object) -> None:
        original_start(pump)  # type: ignore[arg-type]
        target = (
            pump._shutdown  # type: ignore[attr-defined]
            if operation == "shutdown"
            else pump._thread  # type: ignore[attr-defined]
        )
        method_name = "set" if operation == "shutdown" else "join"
        original = getattr(target, method_name)
        armed = True

        def interrupt_once(*args: object, **kwargs: object) -> object:
            nonlocal armed
            if armed:
                armed = False
                raise failure
            return original(*args, **kwargs)

        setattr(target, method_name, interrupt_once)

    monkeypatch.setattr(
        job_worker_module._HeartbeatPump,
        "start",
        start_then_arm_interrupt,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        worker.run_once()

    assert caught.value is failure
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    # Publication now keeps renewing through receipt verification and settles
    # the pump only inside the retained callback, before the catalog mutation.
    assert len(store.retained) == 1
    assert set(backend.sessions) == set(backend.closed_sessions)
    assert not any(
        thread.name.startswith("codenib-job-heartbeat-")
        for thread in threading.enumerate()
    )


@pytest.mark.parametrize("failure_phase", ("settle", "final_heartbeat"))
def test_prepublication_failure_is_not_reclassified_as_response_loss(
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    backend = _Backend()
    job = backend.add_job(f"prepublication-{failure_phase}")
    worker, store, _factory = _worker(backend, _default_resolver())
    failure = RuntimeError(f"{failure_phase} failed before catalog publication")

    def commit_takeover() -> None:
        session_id = backend.main_session_id
        assert session_id is not None
        _FakeCatalog(backend, session_id)._commit_takeover(
            job.job_id,
            lease_duration_ms=300,
        )

    if failure_phase == "settle":
        original_stop = job_worker_module._HeartbeatPump.stop_and_join
        armed = True

        def stop_then_fail(pump: object) -> None:
            nonlocal armed
            original_stop(pump)  # type: ignore[arg-type]
            if armed:
                armed = False
                commit_takeover()
                raise failure

        monkeypatch.setattr(
            job_worker_module._HeartbeatPump,
            "stop_and_join",
            stop_then_fail,
        )
    else:

        def heartbeat_then_fail(*_args: object) -> None:
            commit_takeover()
            raise failure

        monkeypatch.setattr(worker, "_final_heartbeat", heartbeat_then_fail)

    with pytest.raises(RuntimeError) as caught:
        worker.run_once()

    assert caught.value is failure
    assert len(store.retained) == 1
    assert backend.publication_calls == []
    assert backend.completions[(job.job_id, 1)].outcome is IndexJobCompletion.REQUEUE
    assert backend.jobs[job.job_id].attempt_count == 2
    assert set(backend.sessions) == set(backend.closed_sessions)
    assert not any(
        thread.name.startswith("codenib-job-heartbeat-")
        for thread in threading.enumerate()
    )


def test_prepublication_terminal_does_not_hide_a_pending_heartbeat_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _Backend()
    job = backend.add_job("prepublication-terminal-heartbeat-fault")
    worker, store, _factory = _worker(backend, _default_resolver())
    failure = RuntimeError("heartbeat failed while terminal state was reconciled")
    original_reconcile = worker._reconcile_attempt
    original_stop = job_worker_module._HeartbeatPump.stop_and_join
    takeover_armed = True
    fault_armed = True

    def reconcile_after_takeover(*args: object, **kwargs: object):
        nonlocal takeover_armed
        if takeover_armed:
            takeover_armed = False
            session_id = backend.main_session_id
            assert session_id is not None
            _FakeCatalog(backend, session_id)._commit_takeover(
                job.job_id,
                lease_duration_ms=300,
            )
        return original_reconcile(*args, **kwargs)

    def stop_then_fault(pump: object) -> None:
        nonlocal fault_armed
        original_stop(pump)  # type: ignore[arg-type]
        if fault_armed:
            fault_armed = False
            pump.fault = failure  # type: ignore[attr-defined]

    monkeypatch.setattr(worker, "_reconcile_attempt", reconcile_after_takeover)
    monkeypatch.setattr(
        job_worker_module._HeartbeatPump,
        "stop_and_join",
        stop_then_fault,
    )

    with pytest.raises(RuntimeError) as caught:
        worker.run_once()

    assert caught.value is failure
    assert store.retained == []
    assert backend.publication_calls == []
    assert backend.completions[(job.job_id, 1)].outcome is IndexJobCompletion.REQUEUE
    assert backend.jobs[job.job_id].attempt_count == 2
    assert set(backend.sessions) == set(backend.closed_sessions)
    assert not any(
        thread.name.startswith("codenib-job-heartbeat-")
        for thread in threading.enumerate()
    )


@pytest.mark.parametrize(
    "corruption",
    (
        "lease_type",
        "lease_owner",
        "lease_damaged",
        "attempt_owner",
        "attempt_damaged",
        "views_reversed",
        "view_mode_raw",
        "job_status_raw",
        "job_missing",
        "views_missing",
        "attempt_missing",
        "views_oversized",
        "running_updated_before_attempt",
        "attempt_before_job_creation",
        "attempt_before_job_start",
        "initial_lease_heartbeat",
        "initial_lease_expiry",
        "initial_lease_time_subclass",
        "initial_lease_time_overflow",
        "job_updated_subclass",
        "job_updated_overflow",
    ),
)
def test_claim_binding_corruption_fails_closed_before_resolution(
    corruption: str,
) -> None:
    backend = _Backend(bind_corruption=corruption)
    requirements = {"bm25": True, "vector": False}
    backend.add_job("corrupt", requirements)
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(StorageIntegrityError):
        worker.run_once()

    assert resolver.calls == []
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize("corruption", ("completion_time", "fencing_order"))
def test_normal_claim_attests_its_visible_retry_prefix(corruption: str) -> None:
    backend = _Backend()
    job = backend.add_job(f"normal-history-{corruption}")
    completed_at_ms = 400 if corruption == "completion_time" else 102
    prior_token = 1 if corruption == "completion_time" else 5
    backend.jobs[job.job_id] = replace(
        job,
        attempt_count=1,
        started_at_ms=101,
        updated_at_ms=completed_at_ms,
        error_code="prior_requeue",
    )
    backend.attempts[(job.job_id, 1)] = IndexJobAttemptRecord(
        job_id=job.job_id,
        attempt_count=1,
        repository_id=job.repository_id,
        ref_name=job.ref_name,
        request_digest=job.request_digest,
        owner_id="prior-owner",
        fencing_token=prior_token,
        started_at_ms=101,
    )
    backend.completions[(job.job_id, 1)] = IndexJobAttemptCompletionRecord(
        job_id=job.job_id,
        attempt_count=1,
        owner_id="prior-owner",
        fencing_token=prior_token,
        outcome=IndexJobCompletion.REQUEUE,
        error_code="prior_requeue",
        error_message=None,
        completed_at_ms=completed_at_ms,
    )
    backend.next_fencing_token = 2
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(StorageIntegrityError):
        worker.run_once()

    assert resolver.calls == []
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_current_attempt_fencing_token_cannot_be_below_its_count() -> None:
    backend = _Backend(bind_corruption="attempt_fence_below_count")
    job = backend.add_job("current-attempt-fence-floor")
    backend.jobs[job.job_id] = replace(
        job,
        attempt_count=1,
        started_at_ms=50,
        updated_at_ms=50,
        error_code="legacy_requeue",
    )
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(StorageIntegrityError):
        worker.run_once()

    assert resolver.calls == []
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_worker_redetaches_an_exact_but_mutated_executor_result() -> None:
    backend = _Backend()
    backend.add_job("mutated-result", {"bm25": False})

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        result = _succeeded_result(context, skip_optional=False)
        object.__setattr__(
            result.views[0],
            "outcome",
            IndexJobViewOutcome.FAILED,
        )
        return result

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    run_result = worker.run_once()

    assert run_result.disposition is IndexJobWorkerDisposition.FAILED
    assert backend.view_result_calls == []
    assert backend.publication_calls == []
    assert backend.completion_calls[-1]["error_code"] == "worker_executor_incomplete"
    assert store.retained == []


def test_worker_rejects_cross_view_object_metadata_conflicts_before_events() -> None:
    backend = _Backend()
    backend.add_job("conflicting-objects", {"bm25": True, "vector": True})

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        result = _succeeded_result(context, skip_optional=False)
        first_artifact = result.views[0].artifact
        assert first_artifact is not None
        conflicting = IndexJobViewArtifact.create(
            result.views[1].request.view_type,
            result.views[1].request.profile_id,
            first_artifact.object_artifact.receipt,
            schema_version="test.worker.v1",
            media_type="application/x-conflicting-worker-view",
        )
        object.__setattr__(result.views[1], "artifact", conflicting)
        return result

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    run_result = worker.run_once()

    assert run_result.disposition is IndexJobWorkerDisposition.FAILED
    assert backend.view_result_calls == []
    assert backend.publication_calls == []
    assert backend.completion_calls[-1]["error_code"] == "worker_executor_incomplete"
    assert store.retained == []


def test_worker_preflights_retained_closure_bounds_before_events() -> None:
    backend = _Backend()
    backend.add_job("deep-publication-closure")

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        result = _succeeded_result(context)
        metadata: dict[str, Any] = {}
        for _ in range(70):
            metadata = {"nested": metadata}
        deep_artifact = IndexJobViewArtifact.create(
            result.views[0].request.view_type,
            result.views[0].request.profile_id,
            _receipt(b"deep-publication-object"),
            schema_version="test.worker.v1",
            media_type="application/x-test-worker-view",
            metadata=metadata,
        )
        object.__setattr__(result.views[0], "artifact", deep_artifact)
        return result

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    run_result = worker.run_once()

    assert run_result.disposition is IndexJobWorkerDisposition.FAILED
    assert backend.view_result_calls == []
    assert backend.publication_calls == []
    assert backend.completion_calls[-1]["error_code"] == "worker_executor_incomplete"
    assert store.retained == []


def test_worker_classifies_a_damaged_exact_result_as_incomplete() -> None:
    backend = _Backend()
    backend.add_job("damaged-result")

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        result = _succeeded_result(context)
        object.__delattr__(result, "views")
        return result

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    run_result = worker.run_once()

    assert run_result.disposition is IndexJobWorkerDisposition.FAILED
    assert backend.view_result_calls == []
    assert backend.publication_calls == []
    assert backend.completion_calls[-1]["error_code"] == "worker_executor_incomplete"
    assert store.retained == []


def test_executor_cannot_mutate_the_worker_retry_policy_snapshot() -> None:
    backend = _Backend()
    backend.add_job("mutated-context", max_attempts=3)

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        object.__setattr__(context.job, "max_attempts", 1)
        return _failed_result(context, retryable=True)

    worker, _store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    run_result = worker.run_once()

    assert run_result.disposition is IndexJobWorkerDisposition.REQUEUED
    assert backend.completion_calls[-1]["outcome"] is IndexJobCompletion.REQUEUE


def test_reconciliation_rejects_a_returned_null_completion() -> None:
    backend = _Backend(completion_return_none=True)
    backend.add_job("null-completion")
    worker, store, _factory = _worker(
        backend,
        _Resolver(_Executor(lambda context: _failed_result(context, retryable=False))),
    )

    with pytest.raises(StorageIntegrityError, match="non-exact completion"):
        worker.run_once()

    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_reconciliation_rejects_a_damaged_exact_completion() -> None:
    backend = _Backend(completion_return_damaged=True)
    backend.add_job("damaged-completion")
    worker, store, _factory = _worker(
        backend,
        _Resolver(_Executor(lambda context: _failed_result(context, retryable=False))),
    )

    with pytest.raises(StorageIntegrityError, match="exact attempt closure"):
        worker.run_once()

    assert len(backend.completion_calls) == 1
    assert backend.publication_calls == []
    assert store.retained == []


def test_whole_canonical_view_set_is_resolved_and_executed_once() -> None:
    backend = _Backend()
    backend.add_job("whole", {"vector": False, "bm25": True})
    executor = _Executor(_succeeded_result)
    resolver = _Resolver(executor)
    worker, _store, _factory = _worker(backend, resolver)

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.SUCCEEDED
    assert len(resolver.calls) == 1
    assert len(executor.calls) == 1
    resolved_job, resolved_views = resolver.calls[0]
    assert (
        resolved_views
        == IndexJobRequest(
            repository_id=resolved_job.repository_id,
            source_revision_id=resolved_job.source_revision_id,
            ref_name=resolved_job.ref_name,
            idempotency_key=resolved_job.idempotency_key,
            expected_ref_generation=resolved_job.expected_ref_generation,
            max_attempts=resolved_job.max_attempts,
            request_json=resolved_job.request_json,
        ).view_requests
    )
    assert executor.calls[0].views == resolved_views


def test_execution_context_rejects_an_oversized_view_tuple_before_detach() -> None:
    backend = _Backend()
    queued = backend.add_job("oversized-context")
    catalog = _FakeCatalog(backend, session_id=1)
    lease = catalog.acquire_job_lease(
        queued.job_id,
        owner_id="context-owner",
        lease_duration_ms=300,
    )
    job = backend.jobs[queued.job_id]
    attempt = backend.attempts[(queued.job_id, 1)]

    with pytest.raises(StorageValidationError, match="bounded exact tuple"):
        IndexJobExecutionContext(
            job=job,
            views=(backend.views[queued.job_id][0],) * 65,
            attempt=attempt,
            lease=lease,
            control=job_worker_module._ValidationControl(),
        )


def test_result_models_reject_invalid_combinations_and_nonexact_values() -> None:
    request = _request("models", {"bm25": True, "vector": False})
    bm25, vector = request.view_requests
    bm25_success = IndexJobViewExecutionResult.create(
        bm25,
        effective_mode=IndexJobEffectiveMode.FULL,
        outcome=IndexJobViewOutcome.SUCCEEDED,
        artifact=_artifact(bm25),
    )
    vector_skipped = IndexJobViewExecutionResult.create(
        vector,
        effective_mode=IndexJobEffectiveMode.UNAVAILABLE,
        outcome=IndexJobViewOutcome.SKIPPED,
    )
    valid = IndexJobExecutionResult(
        (bm25_success, vector_skipped),
        retryable=False,
    )
    assert valid.publishable
    assert valid.artifacts == (bm25_success.artifact,)

    shared_receipt = _receipt(b"shared-result-object")
    conflicting_bm25 = IndexJobViewExecutionResult.create(
        bm25,
        effective_mode=IndexJobEffectiveMode.FULL,
        outcome=IndexJobViewOutcome.SUCCEEDED,
        artifact=IndexJobViewArtifact.create(
            bm25.view_type,
            bm25.profile_id,
            shared_receipt,
            schema_version="test.worker.v1",
            media_type="application/x-first",
        ),
    )
    conflicting_vector = IndexJobViewExecutionResult.create(
        vector,
        effective_mode=IndexJobEffectiveMode.FULL,
        outcome=IndexJobViewOutcome.SUCCEEDED,
        artifact=IndexJobViewArtifact.create(
            vector.view_type,
            vector.profile_id,
            shared_receipt,
            schema_version="test.worker.v1",
            media_type="application/x-second",
        ),
    )
    with pytest.raises(StorageValidationError, match="conflicting publication"):
        IndexJobExecutionResult((conflicting_bm25, conflicting_vector))

    with pytest.raises(StorageValidationError, match="non-empty exact tuple"):
        IndexJobExecutionResult([], retryable=False)  # type: ignore[arg-type]
    with pytest.raises(StorageValidationError, match="exact boolean"):
        IndexJobExecutionResult((bm25_success,), retryable=1)  # type: ignore[arg-type]
    with pytest.raises(StorageValidationError, match="not canonical"):
        IndexJobExecutionResult((vector_skipped, bm25_success), retryable=False)
    with pytest.raises(StorageValidationError, match="cannot be retryable"):
        IndexJobExecutionResult((bm25_success,), retryable=True)
    with pytest.raises(StorageValidationError, match="required exactly"):
        IndexJobViewExecutionResult.create(
            bm25,
            effective_mode=IndexJobEffectiveMode.FULL,
            outcome=IndexJobViewOutcome.SUCCEEDED,
        )
    with pytest.raises(StorageValidationError, match="exactly the unavailable"):
        IndexJobViewExecutionResult.create(
            vector,
            effective_mode=IndexJobEffectiveMode.FULL,
            outcome=IndexJobViewOutcome.SKIPPED,
        )
    oversized_payload = (
        "{" + '"key":"' + ("x" * INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS) + '"}'
    )
    with pytest.raises(StorageValidationError, match="payload JSON is out of bounds"):
        IndexJobViewExecutionResult(
            request=bm25,
            effective_mode=IndexJobEffectiveMode.FULL,
            outcome=IndexJobViewOutcome.SUCCEEDED,
            artifact=_artifact(bm25),
            payload_json=oversized_payload,
        )


def test_progress_control_is_thread_affine_reserved_and_sealed() -> None:
    backend = _Backend()
    backend.add_job("control")
    captured: list[IndexJobExecutionContext] = []
    crossed: list[BaseException] = []

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        captured.append(context)
        token = context.control.stop_token
        assert not hasattr(token, "set")
        assert not token.is_set()
        assert token.reason is None
        assert token.wait(0) is False
        with pytest.raises(StorageValidationError, match="reserved"):
            context.control.append_progress("worker.private")

        def cross_thread() -> None:
            try:
                context.control.append_progress("executor.cross-thread")
            except Exception as exc:
                crossed.append(exc)

        thread = threading.Thread(target=cross_thread)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        context.control.append_progress(
            "executor.prepare",
            {"phase": "prepare"},
            "bm25",
        )
        return _failed_result(context, retryable=False)

    worker, _store, _factory = _worker(backend, _Resolver(_Executor(execute)))
    assert worker.run_once().disposition is IndexJobWorkerDisposition.FAILED
    assert len(crossed) == 1
    assert isinstance(crossed[0], RuntimeError)
    assert "thread boundary" in str(crossed[0])
    assert backend.progress_calls[0]["event_key"] == "executor.prepare"
    with pytest.raises(RuntimeError, match="sealed"):
        captured[0].control.append_progress("executor.after-return")


def test_progress_conflict_is_reconciled_as_lost_authority() -> None:
    backend = _Backend(progress_failure=PublishConflict("event conflict"))
    backend.add_job("progress-conflict")

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        context.control.append_progress("executor.progress")
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.LOST_AUTHORITY
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_unknown_progress_catalog_failure_is_rethrown_without_closure() -> None:
    failure = RuntimeError("catalog transport failed")
    backend = _Backend(progress_failure=failure)
    backend.add_job("progress-fault")

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        try:
            context.control.append_progress("executor.progress")
        except RuntimeError as caught:
            assert caught is failure
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    with pytest.raises(RuntimeError) as caught:
        worker.run_once()

    assert caught.value is failure
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_progress_catalog_validation_is_a_sticky_integrity_fault() -> None:
    failure = StorageValidationError("catalog rejected progress")
    backend = _Backend(progress_failure=failure)
    backend.add_job("progress-catalog-validation")
    observed: list[StorageIntegrityError] = []

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        try:
            context.control.append_progress("executor.progress")
        except StorageIntegrityError as exc:
            observed.append(exc)
            assert context.control.stop_token.reason is IndexJobStopReason.CONTROL_FAULT
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    with pytest.raises(StorageIntegrityError, match="prevalidated event") as caught:
        worker.run_once()

    assert observed == [caught.value]
    assert caught.value.__cause__ is failure
    assert len(backend.progress_calls) == 1
    assert backend.completion_calls == []
    assert backend.view_result_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_progress_response_is_attested_before_executor_can_continue() -> None:
    backend = _Backend(corrupt_progress_response=True)
    backend.add_job("progress-response")

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        context.control.append_progress("executor.progress")
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    with pytest.raises(StorageIntegrityError, match="non-exact record"):
        worker.run_once()

    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize("committed", (False, True))
def test_executor_cannot_suppress_progress_base_exception(
    committed: bool,
) -> None:
    failure = KeyboardInterrupt("progress interrupted")
    backend = _Backend(
        progress_failure=None if committed else failure,
        progress_commit_response_loss=failure if committed else None,
    )
    backend.add_job(f"progress-base-exception-{committed}")
    observed: list[BaseException] = []

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        try:
            context.control.append_progress("executor.progress", {"phase": "build"})
        except BaseException as exc:  # noqa: B036 - executor suppresses deliberately
            observed.append(exc)
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    with pytest.raises(KeyboardInterrupt) as caught:
        worker.run_once()

    assert caught.value is failure
    assert observed == [failure]
    assert len(backend.events_by_key) == (1 if committed else 0)
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_progress_commit_response_loss_replays_the_same_event() -> None:
    backend = _Backend(
        progress_commit_response_loss=RuntimeError("progress response lost")
    )
    backend.add_job("progress-response-loss")

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        event = context.control.append_progress(
            "executor.progress",
            {"phase": "prepare"},
        )
        assert event.event_key == "executor.progress"
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.SUCCEEDED
    assert len(backend.progress_calls) == 2
    assert len(backend.events_by_key) == 2
    assert len(backend.publication_calls) == 1
    assert len(store.retained) == 1


@pytest.mark.parametrize("corruption", ("sequence", "time"))
def test_progress_replay_must_match_its_first_observed_record(
    corruption: str,
) -> None:
    backend = _Backend(progress_replay_corruption=corruption)
    backend.add_job(f"progress-replay-{corruption}")
    observed: list[StorageIntegrityError] = []

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        context.control.append_progress("executor.progress", {"step": 1})
        try:
            context.control.append_progress("executor.progress", {"step": 1})
        except StorageIntegrityError as exc:
            observed.append(exc)
            assert context.control.stop_token.reason is IndexJobStopReason.CONTROL_FAULT
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    with pytest.raises(StorageIntegrityError, match="first observed record") as caught:
        worker.run_once()

    assert observed == [caught.value]
    assert len(backend.progress_calls) == 2
    assert len(backend.events_by_key) == 1
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_mutating_returned_progress_does_not_change_private_replay_evidence() -> None:
    backend = _Backend()
    backend.add_job("progress-return-mutation")

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        first = context.control.append_progress("executor.progress", {"step": 1})
        original_sequence = first.sequence
        object.__setattr__(first, "sequence", original_sequence + 1_000)
        replay = context.control.append_progress("executor.progress", {"step": 1})
        assert replay.sequence == original_sequence
        assert replay is not first
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.SUCCEEDED
    assert len(backend.progress_calls) == 2
    assert len(backend.events_by_key) == 2
    assert len(backend.publication_calls) == 1
    assert len(store.retained) == 1


def test_exact_progress_replay_does_not_consume_event_budget_twice() -> None:
    backend = _Backend()
    backend.add_job("progress-replay-budget")

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        progress_limit = MAX_INDEX_JOB_EVENTS_PER_ATTEMPT - len(context.views)
        context.control.append_progress("executor.progress.000")
        context.control.append_progress("executor.progress.000")
        for ordinal in range(1, progress_limit):
            context.control.append_progress(f"executor.progress.{ordinal:03d}")
        with pytest.raises(StorageValidationError, match="reserved event budget"):
            context.control.append_progress("executor.progress.overflow")
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.SUCCEEDED
    assert len(backend.progress_calls) == MAX_INDEX_JOB_EVENTS_PER_ATTEMPT
    assert len(backend.events_by_key) == MAX_INDEX_JOB_EVENTS_PER_ATTEMPT
    assert len(backend.publication_calls) == 1
    assert len(store.retained) == 1


@pytest.mark.parametrize(
    ("event_key", "view_type"),
    (
        ("", None),
        (" padded", None),
        ("x" * 129, None),
        ("executor.progress", "unknown"),
        ("executor.progress", " padded"),
    ),
)
def test_progress_identifiers_are_validated_before_the_backend(
    event_key: str,
    view_type: str | None,
) -> None:
    backend = _Backend()
    backend.add_job(f"progress-input-{len(event_key)}-{view_type}")

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        with pytest.raises(StorageValidationError):
            context.control.append_progress(event_key, view_type=view_type)
        return _succeeded_result(context)

    worker, _store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    assert worker.run_once().disposition is IndexJobWorkerDisposition.SUCCEEDED
    assert backend.progress_calls == []


@pytest.mark.parametrize("corruption", ("before_start", "sequence", "time"))
def test_new_progress_events_follow_the_observed_causal_frontier(
    corruption: str,
) -> None:
    backend = _Backend(progress_new_event_corruption=corruption)
    backend.add_job(f"progress-frontier-{corruption}")
    observed: list[StorageIntegrityError] = []

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        if corruption != "before_start":
            context.control.append_progress("executor.progress.1")
        try:
            context.control.append_progress("executor.progress.2")
        except StorageIntegrityError as exc:
            observed.append(exc)
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    with pytest.raises(StorageIntegrityError) as caught:
        worker.run_once()

    assert observed == [caught.value]
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_view_result_event_cannot_move_behind_observed_progress() -> None:
    backend = _Backend(view_result_time_regression=True)
    backend.add_job("view-result-time-regression")

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        context.control.append_progress("executor.progress")
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    with pytest.raises(StorageIntegrityError, match="causal frontier"):
        worker.run_once()

    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_progress_unknown_outcome_then_validation_is_a_sticky_fault() -> None:
    backend = _Backend(
        progress_commit_response_loss=RuntimeError("progress response lost"),
        progress_retry_failure=StorageValidationError("replay validation failed"),
    )
    backend.add_job("progress-replay-validation")
    observed: list[BaseException] = []

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        try:
            context.control.append_progress("executor.progress", {"phase": "build"})
        except BaseException as exc:  # noqa: B036 - executor suppresses deliberately
            observed.append(exc)
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    with pytest.raises(StorageIntegrityError, match="unknown write outcome") as caught:
        worker.run_once()

    assert observed == [caught.value]
    assert len(backend.events_by_key) == 1
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_progress_payload_mutation_cannot_shift_expected_attestation() -> None:
    backend = _Backend(mutate_progress_payload=True)
    backend.add_job("progress-payload-mutation")
    observed: list[StorageIntegrityError] = []

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        try:
            context.control.append_progress(
                "executor.progress",
                {"phase": "original"},
            )
        except StorageIntegrityError as exc:
            observed.append(exc)
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    with pytest.raises(StorageIntegrityError, match="different closure") as caught:
        worker.run_once()

    assert observed == [caught.value]
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_progress_payload_callback_cannot_reenter_control() -> None:
    backend = _Backend()
    backend.add_job("progress-reentry")

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        class ReentrantPayload(Mapping[str, Any]):
            def __len__(self) -> int:
                context.control.append_progress("executor.nested")
                return 0

            def __iter__(self):
                return iter(())

            def __getitem__(self, key: str) -> Any:
                raise KeyError(key)

        with pytest.raises(StorageValidationError, match="could not be snapshotted"):
            context.control.append_progress("executor.outer", ReentrantPayload())
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    with pytest.raises(RuntimeError, match="not reentrant"):
        worker.run_once()

    assert backend.progress_calls == []
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_suppressed_payload_reentry_cannot_persist_the_outer_event() -> None:
    backend = _Backend()
    backend.add_job("suppressed-progress-reentry")

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        class ReentrantPayload(Mapping[str, Any]):
            def __len__(self) -> int:
                with pytest.raises(RuntimeError, match="not reentrant"):
                    context.control.append_progress("executor.nested")
                return 0

            def __iter__(self):
                return iter(())

            def __getitem__(self, key: str) -> Any:
                raise KeyError(key)

        with pytest.raises(RuntimeError, match="not reentrant"):
            context.control.append_progress("executor.outer", ReentrantPayload())
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    with pytest.raises(RuntimeError, match="not reentrant"):
        worker.run_once()

    assert backend.progress_calls == []
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_stop_token_observes_heartbeat_cancellation_without_sleeping() -> None:
    backend = _Backend(background_cancel_on_call=2)
    backend.add_job("cooperative-cancel")
    entered = threading.Event()
    observed: list[IndexJobStopReason | None] = []

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        entered.set()
        assert backend.cancellation_observed.wait(5)
        assert context.control.stop_token.is_set()
        observed.append(context.control.stop_token.reason)
        return _succeeded_result(context)

    worker, _store, _factory = _worker(
        backend,
        _Resolver(_Executor(execute)),
        lease_duration_ms=100,
        heartbeat_interval_ms=10,
    )

    result = worker.run_once()

    assert entered.is_set()
    assert observed == [IndexJobStopReason.CANCEL_REQUESTED]
    assert result.disposition is IndexJobWorkerDisposition.CANCELLED
    assert backend.publication_calls == []


@pytest.mark.parametrize("phase", ("resolver", "executor"))
@pytest.mark.parametrize(
    ("max_attempts", "expected"),
    (
        (3, IndexJobWorkerDisposition.REQUEUED),
        (1, IndexJobWorkerDisposition.FAILED),
    ),
)
def test_phase_exception_uses_static_code_without_exception_text(
    phase: str,
    max_attempts: int,
    expected: IndexJobWorkerDisposition,
) -> None:
    backend = _Backend()
    backend.add_job("phase-exception", max_attempts=max_attempts)
    failure = RuntimeError("sensitive dynamic exception text")
    if phase == "resolver":
        resolver = _Resolver(_Executor(_succeeded_result), failure=failure)
    else:

        def raise_failure(
            _context: IndexJobExecutionContext,
        ) -> IndexJobExecutionResult:
            raise failure

        resolver = _Resolver(_Executor(raise_failure))
    worker, _store, _factory = _worker(backend, resolver)

    result = worker.run_once()

    assert result.disposition is expected
    completion = backend.completion_calls[-1]
    assert completion["error_code"] == f"worker_{phase}_failed"
    assert completion["error_message"] is None
    assert "sensitive" not in repr(completion)


@pytest.mark.parametrize("phase", ("resolver", "executor"))
@pytest.mark.parametrize("failure", (KeyboardInterrupt(), SystemExit(19)))
def test_control_flow_base_exception_is_rethrown_exactly_without_closure(
    phase: str,
    failure: BaseException,
) -> None:
    backend = _Backend()
    backend.add_job(f"base-{phase}-{type(failure).__name__}")
    if phase == "resolver":
        resolver = _Resolver(_Executor(_succeeded_result), failure=failure)
    else:

        def raise_failure(
            _context: IndexJobExecutionContext,
        ) -> IndexJobExecutionResult:
            raise failure

        resolver = _Resolver(_Executor(raise_failure))
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(type(failure)) as caught:
        worker.run_once()

    assert caught.value is failure
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize("phase", ("resolver", "executor"))
def test_storage_integrity_alarm_from_execution_phase_is_not_downgraded(
    phase: str,
) -> None:
    failure = StorageIntegrityError(f"{phase} integrity alarm")
    backend = _Backend()
    backend.add_job(f"integrity-{phase}")
    if phase == "resolver":
        resolver = _Resolver(_Executor(_succeeded_result), failure=failure)
    else:

        def raise_failure(
            _context: IndexJobExecutionContext,
        ) -> IndexJobExecutionResult:
            raise failure

        resolver = _Resolver(_Executor(raise_failure))
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(StorageIntegrityError) as caught:
        worker.run_once()

    assert caught.value is failure
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize(
    ("race", "expected"),
    (
        ("cancel", IndexJobWorkerDisposition.CANCELLED),
        ("authority", IndexJobWorkerDisposition.LOST_AUTHORITY),
    ),
)
def test_stop_during_resolver_never_enters_executor(
    race: str,
    expected: IndexJobWorkerDisposition,
) -> None:
    backend = _Backend(
        background_cancel_on_call=2 if race == "cancel" else None,
        background_authority_loss_on_call=2 if race == "authority" else None,
    )
    backend.add_job(f"resolver-{race}")
    executor = _Executor(_succeeded_result)

    class BlockingResolver:
        def resolve(
            self,
            job: IndexJobRecord,
            views: tuple[IndexJobViewRecord, ...],
        ) -> IndexJobExecutor:
            del job, views
            observed = (
                backend.cancellation_observed
                if race == "cancel"
                else backend.authority_loss_observed
            )
            assert observed.wait(5)
            return executor

    worker, store, _factory = _worker(
        backend,
        BlockingResolver(),  # type: ignore[arg-type]
    )

    result = worker.run_once()

    assert result.disposition is expected
    assert executor.calls == []
    assert backend.publication_calls == []
    assert store.retained == []
    if race == "cancel":
        assert backend.completion_calls[-1]["outcome"] is IndexJobCompletion.CANCELLED
    else:
        assert backend.completion_calls == []


@pytest.mark.parametrize(
    ("retryable", "max_attempts", "expected", "outcome"),
    (
        (True, 3, IndexJobWorkerDisposition.REQUEUED, IndexJobCompletion.REQUEUE),
        (True, 1, IndexJobWorkerDisposition.FAILED, IndexJobCompletion.FAILED),
        (False, 3, IndexJobWorkerDisposition.FAILED, IndexJobCompletion.FAILED),
    ),
)
def test_controlled_failure_maps_retry_and_final_attempt_exactly(
    retryable: bool,
    max_attempts: int,
    expected: IndexJobWorkerDisposition,
    outcome: IndexJobCompletion,
) -> None:
    backend = _Backend()
    backend.add_job("controlled", max_attempts=max_attempts)
    resolver = _Resolver(
        _Executor(lambda context: _failed_result(context, retryable=retryable))
    )
    worker, _store, _factory = _worker(backend, resolver)

    result = worker.run_once()

    assert result.disposition is expected
    assert backend.completion_calls[-1]["outcome"] is outcome
    assert backend.completion_calls[-1]["error_code"] == "controlled_failure"


def test_view_results_use_deterministic_keys_and_optional_skip_still_publishes() -> (
    None
):
    backend = _Backend()
    backend.add_job("publish", {"vector": False, "bm25": True})
    contexts: list[IndexJobExecutionContext] = []

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        contexts.append(context)
        context.control.append_progress("executor.start", {"phase": "start"})
        return _succeeded_result(context)

    resolver = _Resolver(_Executor(execute))
    worker, store, _factory = _worker(backend, resolver)

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.SUCCEEDED
    assert len(resolver.calls) == 1
    resolved_job, resolved_views = resolver.calls[0]
    assert (
        resolved_views
        == IndexJobRequest(
            repository_id=resolved_job.repository_id,
            source_revision_id=resolved_job.source_revision_id,
            ref_name=resolved_job.ref_name,
            idempotency_key=resolved_job.idempotency_key,
            expected_ref_generation=resolved_job.expected_ref_generation,
            max_attempts=resolved_job.max_attempts,
            request_json=resolved_job.request_json,
        ).view_requests
    )
    assert contexts[0].views == resolved_views
    assert [call["event_key"] for call in backend.view_result_calls] == [
        "worker.view-result.00",
        "worker.view-result.01",
    ]
    assert [call["view_type"] for call in backend.view_result_calls] == [
        "bm25",
        "vector",
    ]
    assert [call["outcome"] for call in backend.view_result_calls] == [
        IndexJobViewOutcome.SUCCEEDED,
        IndexJobViewOutcome.SKIPPED,
    ]
    assert len(backend.publication_calls) == 1
    assert tuple(output.view_type for output in backend.publication_calls[0]) == (
        "bm25",
    )
    assert len(store.retained) == 1
    background_sessions = {
        session_id
        for session_id, _thread_id, lane in backend.heartbeat_calls
        if lane == "background"
    }
    assert background_sessions
    assert backend.main_session_id not in background_sessions
    assert (
        len({thread_id for _session, thread_id, _lane in backend.heartbeat_calls}) >= 2
    )


def test_view_result_response_is_attested_before_publication() -> None:
    backend = _Backend(corrupt_view_result_response=True)
    backend.add_job("view-result-response")
    worker, store, _factory = _worker(backend, _default_resolver())

    with pytest.raises(StorageIntegrityError, match="non-exact record"):
        worker.run_once()

    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_view_result_not_found_is_reconciled_as_a_phase_conflict() -> None:
    backend = _Backend(view_result_failure=StorageNotFound("attempt disappeared"))
    backend.add_job("view-result-not-found")
    worker, store, _factory = _worker(backend, _default_resolver())

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.LOST_AUTHORITY
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_view_result_retry_uses_a_fresh_detached_payload() -> None:
    backend = _Backend(
        view_result_mutate_then_raise=RuntimeError("view result response lost")
    )
    backend.add_job("view-result-fresh-retry")
    worker, store, _factory = _worker(backend, _default_resolver())

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.SUCCEEDED
    assert len(backend.view_result_calls) == 2
    assert backend.view_result_calls[0]["payload"] == {"documents": "tampered"}
    assert backend.view_result_calls[1]["payload"] == {"documents": 3}
    assert len(backend.publication_calls) == 1
    assert len(store.retained) == 1


def test_view_result_replay_validation_is_an_integrity_failure() -> None:
    first_failure = RuntimeError("view result response lost")
    replay_failure = StorageValidationError("view result replay validation failed")
    backend = _Backend(
        view_result_commit_response_loss=first_failure,
        view_result_retry_failure=replay_failure,
    )
    backend.add_job("view-result-replay-validation")
    worker, store, _factory = _worker(backend, _default_resolver())

    with pytest.raises(
        StorageIntegrityError,
        match="replay failed validation after an unknown write outcome",
    ) as caught:
        worker.run_once()

    assert caught.value is not first_failure
    assert caught.value.__cause__ is replay_failure
    assert len(backend.view_result_calls) == 2
    assert len(backend.events_by_key) == 1
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_progress_budget_reserves_one_result_event_per_requested_view() -> None:
    backend = _Backend()
    backend.add_job("event-budget")

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        progress_limit = MAX_INDEX_JOB_EVENTS_PER_ATTEMPT - len(context.views)
        for ordinal in range(progress_limit):
            context.control.append_progress(f"executor.progress.{ordinal:03d}")
        with pytest.raises(StorageValidationError, match="reserved event budget"):
            context.control.append_progress("executor.progress.overflow")
        return _succeeded_result(context)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.SUCCEEDED
    assert len(backend.progress_calls) == MAX_INDEX_JOB_EVENTS_PER_ATTEMPT - 1
    assert len(backend.view_result_calls) == 1
    assert len(backend.events_by_key) == MAX_INDEX_JOB_EVENTS_PER_ATTEMPT
    assert len(backend.publication_calls) == 1
    assert len(store.retained) == 1


@pytest.mark.parametrize("case", ("required_failed", "all_optional_skipped"))
def test_no_publishable_outputs_never_calls_publication(case: str) -> None:
    backend = _Backend()
    requirements = {"bm25": case == "required_failed"}
    backend.add_job(case, requirements)

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        if case == "required_failed":
            return _failed_result(context, retryable=False, error_code="build_failed")
        return IndexJobExecutionResult(
            tuple(
                IndexJobViewExecutionResult.create(
                    view,
                    effective_mode=IndexJobEffectiveMode.UNAVAILABLE,
                    outcome=IndexJobViewOutcome.SKIPPED,
                )
                for view in context.views
            ),
            retryable=False,
        )

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.FAILED
    assert backend.publication_calls == []
    assert store.retained == []
    expected_code = (
        "build_failed" if case == "required_failed" else "worker_executor_incomplete"
    )
    assert backend.completion_calls[-1]["error_code"] == expected_code


def test_final_heartbeat_cancellation_wins_over_publishable_result() -> None:
    backend = _Backend(cancel_on_final_heartbeat=True)
    backend.add_job("final-cancel")
    worker, store, _factory = _worker(backend, _default_resolver())

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.CANCELLED
    assert backend.completion_calls[-1]["outcome"] is IndexJobCompletion.CANCELLED
    assert backend.publication_calls == []
    assert len(store.retained) == 1


def test_heartbeat_authority_conflict_stops_without_stale_closure() -> None:
    backend = _Backend(background_heartbeat_error=PublishConflict("lost"))
    backend.add_job("heartbeat-lost")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.LOST_AUTHORITY
    assert resolver.calls == []
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_unknown_heartbeat_fault_is_rethrown_without_stale_closure() -> None:
    failure = RuntimeError("heartbeat transport failed")
    backend = _Backend(background_heartbeat_error=failure)
    backend.add_job("heartbeat-fault")
    worker, store, _factory = _worker(backend, _default_resolver())

    with pytest.raises(RuntimeError) as caught:
        worker.run_once()

    assert caught.value is failure
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_heartbeat_attests_the_immutable_attempt_start() -> None:
    backend = _Backend(heartbeat_corrupt_acquired_at=True)
    backend.add_job("heartbeat-acquired-corruption")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(StorageIntegrityError, match="different authority"):
        worker.run_once()

    assert resolver.calls == []
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize(
    "mode",
    ("expiry_not_advanced", "time_subclass"),
)
def test_heartbeat_response_requires_exact_monotonic_lease_evidence(
    mode: str,
) -> None:
    backend = _Backend(heartbeat_response_mode=mode)
    backend.add_job(f"heartbeat-evidence-{mode}")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(StorageIntegrityError):
        worker.run_once()

    assert resolver.calls == []
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_later_heartbeat_cannot_regress() -> None:
    backend = _Backend(heartbeat_response_mode="heartbeat_regression")
    backend.add_job("heartbeat-regression")

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        assert backend.second_heartbeat_entered.wait(5)
        return _succeeded_result(context)

    worker, store, _factory = _worker(
        backend,
        _Resolver(_Executor(execute)),
        lease_duration_ms=30,
        heartbeat_interval_ms=5,
    )

    with pytest.raises(StorageIntegrityError, match="lease progression"):
        worker.run_once()

    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_same_millisecond_heartbeat_with_advancing_expiry_is_valid() -> None:
    backend = _Backend(heartbeat_response_mode="same_heartbeat")
    backend.add_job("same-heartbeat")
    worker, store, _factory = _worker(backend, _default_resolver())

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.SUCCEEDED
    assert len(backend.publication_calls) == 1
    assert len(store.retained) == 1


@pytest.mark.parametrize("terminal", ("completion", "publication"))
def test_final_heartbeat_raises_the_fresh_content_causal_floor(
    terminal: str,
) -> None:
    backend = _Backend(heartbeat_response_mode="final_heartbeat_ahead")
    backend.add_job(f"final-heartbeat-floor-{terminal}")
    resolver = (
        _Resolver(
            _Executor(_succeeded_result),
            failure=RuntimeError("resolver failed"),
        )
        if terminal == "completion"
        else _default_resolver()
    )
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(StorageIntegrityError):
        worker.run_once()

    if terminal == "completion":
        assert len(backend.completions) == 1
        assert backend.publication_calls == []
        assert store.retained == []
    else:
        assert len(backend.publication_calls) == 1
        assert len(store.retained) == 1


def test_heartbeat_ahead_of_content_rejects_the_next_event() -> None:
    backend = _Backend(heartbeat_response_mode="heartbeat_ahead")
    backend.add_job("heartbeat-ahead-event")
    worker, store, _factory = _worker(backend, _default_resolver())

    with pytest.raises(StorageIntegrityError, match="causal frontier"):
        worker.run_once()

    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_suppressing_main_session_cannot_swallow_executor_base_exception() -> None:
    failure = KeyboardInterrupt("executor interrupted")
    backend = _Backend(suppress_session_exceptions=True)
    backend.add_job("suppressing-main-session")

    def interrupt(_context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        raise failure

    worker, store, _factory = _worker(
        backend,
        _Resolver(_Executor(interrupt)),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        worker.run_once()

    assert caught.value is failure
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_suppressing_heartbeat_session_cannot_hide_renewal_failure() -> None:
    failure = RuntimeError("heartbeat transport failed")
    backend = _Backend(
        background_heartbeat_error=failure,
        suppress_session_exceptions=True,
    )
    backend.add_job("suppressing-heartbeat-session")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(RuntimeError) as caught:
        worker.run_once()

    assert caught.value is failure
    assert resolver.calls == []
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_heartbeat_not_found_with_missing_job_reports_lost_authority() -> None:
    backend = _Backend(
        background_heartbeat_error=StorageNotFound("job disappeared"),
        remove_job_on_background_not_found=True,
    )
    job = backend.add_job("heartbeat-not-found")
    resolver = _default_resolver()
    worker, store, _factory = _worker(backend, resolver)

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.LOST_AUTHORITY
    assert result.job_id == job.job_id
    assert resolver.calls == []
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_completion_response_loss_reconciles_the_durable_closure() -> None:
    response_loss = RuntimeError("completion response lost")
    backend = _Backend(completion_response_loss=response_loss)
    backend.add_job("completion-loss")
    worker, _store, _factory = _worker(
        backend,
        _Resolver(_Executor(lambda context: _failed_result(context, retryable=True))),
    )

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.REQUEUED
    assert len(backend.completions) == 1
    assert backend.completion_calls[-1]["outcome"] is IndexJobCompletion.REQUEUE


def test_prepublication_reconciliation_rejects_an_unattested_success() -> None:
    backend = _Backend(forge_success_during_reconcile=True)
    backend.add_job("forged-prepublication-success")
    worker, store, _factory = _worker(backend, _default_resolver())

    with pytest.raises(StorageIntegrityError, match="non-publication"):
        worker.run_once()

    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


def test_completion_conflict_cannot_reconcile_as_an_unattested_success() -> None:
    backend = _Backend(forge_success_on_completion_conflict=True)
    backend.add_job("forged-completion-conflict-success")
    resolver = _Resolver(
        _Executor(_succeeded_result),
        failure=RuntimeError("resolver failed"),
    )
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(StorageIntegrityError, match="non-publication"):
        worker.run_once()

    assert len(backend.completion_calls) == 1
    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize("response_loss", (False, True))
def test_completion_cannot_precede_worker_observed_events(
    response_loss: bool,
) -> None:
    backend = _Backend(
        completion_completed_at_ms_override=150,
        completion_response_loss=(
            RuntimeError("completion response lost") if response_loss else None
        ),
    )
    backend.add_job(f"completion-event-floor-{response_loss}")

    def execute(context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        context.control.append_progress("executor.progress")
        return _failed_result(context, retryable=False)

    worker, store, _factory = _worker(backend, _Resolver(_Executor(execute)))

    with pytest.raises(StorageIntegrityError):
        worker.run_once()

    assert len(backend.completions) == 1
    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize("response_loss", (False, True))
def test_modeled_retry_completion_cannot_precede_its_attempt_start(
    response_loss: bool,
) -> None:
    backend = _Backend(
        completion_completed_at_ms_override=101,
        completion_response_loss=(
            RuntimeError("completion response lost") if response_loss else None
        ),
    )
    job = backend.add_job(f"attempt-two-completion-time-{response_loss}")
    backend.jobs[job.job_id] = replace(
        job,
        attempt_count=1,
        started_at_ms=50,
        updated_at_ms=50,
        error_code="legacy_requeue",
    )
    backend.next_fencing_token = 2
    resolver = _Resolver(
        _Executor(_succeeded_result),
        failure=RuntimeError("resolver failed"),
    )
    worker, store, _factory = _worker(backend, resolver)

    with pytest.raises(StorageIntegrityError):
        worker.run_once()

    assert backend.attempts[(job.job_id, 2)].started_at_ms == 102
    assert backend.completions[(job.job_id, 2)].completed_at_ms == 101
    assert backend.publication_calls == []
    assert store.retained == []


def test_completion_conflict_reconciles_a_concurrent_takeover() -> None:
    backend = _Backend(takeover_on_completion_call=True)
    job = backend.add_job("completion-takeover-race")
    worker, store, _factory = _worker(
        backend,
        _Resolver(_Executor(lambda context: _failed_result(context, retryable=False))),
    )

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.REQUEUED
    assert result.job_id == job.job_id
    assert result.attempt_count == 1
    assert backend.jobs[job.job_id].status is IndexJobStatus.RUNNING
    assert backend.jobs[job.job_id].attempt_count == 2
    assert backend.completions[(job.job_id, 1)].outcome is IndexJobCompletion.REQUEUE
    assert backend.completions[(job.job_id, 1)].error_code == "lease_expired"
    assert len(backend.completion_calls) == 1
    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize("race", ("conflict", "between_reads"))
def test_reconciliation_rejects_a_successor_before_the_requeue_closure(
    race: str,
) -> None:
    backend = _Backend(
        takeover_on_completion_call=race == "conflict",
        takeover_after_completion_miss=race == "between_reads",
        takeover_successor_updated_before_completion=True,
    )
    backend.add_job(f"successor-before-closure-{race}")
    worker, store, _factory = _worker(
        backend,
        _Resolver(_Executor(lambda context: _failed_result(context, retryable=False))),
    )

    with pytest.raises(StorageIntegrityError, match="impossible successor"):
        worker.run_once()

    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize(
    ("race", "corruption"),
    (
        ("conflict", "start"),
        ("between_reads", "fencing_token"),
    ),
)
def test_reconciliation_attests_the_visible_successor_history(
    race: str,
    corruption: str,
) -> None:
    backend = _Backend(
        takeover_on_completion_call=race == "conflict",
        takeover_after_completion_miss=race == "between_reads",
        takeover_adjacency_corruption=corruption,
    )
    backend.add_job(f"reconcile-history-{race}-{corruption}")
    worker, store, _factory = _worker(
        backend,
        _Resolver(_Executor(lambda context: _failed_result(context, retryable=False))),
    )

    with pytest.raises(StorageIntegrityError):
        worker.run_once()

    assert backend.jobs[next(iter(backend.jobs))].attempt_count == 2
    assert backend.publication_calls == []
    assert store.retained == []


def test_reconciliation_observes_a_takeover_committed_between_reads() -> None:
    backend = _Backend(takeover_after_completion_miss=True)
    job = backend.add_job("reconcile-takeover-between-reads")
    worker, store, _factory = _worker(
        backend,
        _Resolver(_Executor(lambda context: _failed_result(context, retryable=False))),
    )

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.REQUEUED
    assert result.job_id == job.job_id
    assert result.attempt_count == 1
    assert backend.jobs[job.job_id].status is IndexJobStatus.RUNNING
    assert backend.jobs[job.job_id].attempt_count == 2
    assert backend.completions[(job.job_id, 1)].outcome is IndexJobCompletion.REQUEUE
    assert backend.completion_calls == []
    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize(
    ("corruption", "message"),
    (
        ("missing", "missing attempt closure"),
        ("foreign", "different completion authority"),
    ),
)
def test_completion_response_loss_requires_the_exact_authority_closure(
    corruption: str,
    message: str,
) -> None:
    backend = _Backend(
        completion_response_loss=RuntimeError("completion response lost"),
        completion_hide=corruption == "missing",
        completion_foreign_authority=corruption == "foreign",
    )
    backend.add_job(f"completion-authority-{corruption}")
    worker, store, _factory = _worker(
        backend,
        _Resolver(_Executor(lambda context: _failed_result(context, retryable=True))),
    )

    with pytest.raises(StorageIntegrityError, match=message):
        worker.run_once()

    assert len(backend.completions) == 1
    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize(
    ("corruption", "retryable"),
    (
        ("running_after_requeue", True),
        ("failed_cancel_requested", False),
        ("shifted_terminal_time", False),
        ("later_attempt_after_failed", False),
    ),
)
def test_completion_response_loss_attests_the_same_attempt_job_state(
    corruption: str,
    retryable: bool,
) -> None:
    backend = _Backend(
        completion_response_loss=RuntimeError("completion response lost"),
        completion_response_corruption=corruption,
    )
    backend.add_job(f"completion-response-{corruption}")
    worker, store, _factory = _worker(
        backend,
        _Resolver(
            _Executor(lambda context: _failed_result(context, retryable=retryable))
        ),
    )

    with pytest.raises(
        StorageIntegrityError,
        match="inconsistent with its closure|impossible successor",
    ):
        worker.run_once()

    assert len(backend.completions) == 1
    assert backend.publication_calls == []
    assert store.retained == []


def test_completion_integrity_alarm_surfaces_after_a_durable_closure() -> None:
    failure = StorageIntegrityError("completion integrity alarm")
    backend = _Backend(completion_response_loss=failure)
    backend.add_job("completion-integrity")
    worker, _store, _factory = _worker(
        backend,
        _Resolver(_Executor(lambda context: _failed_result(context, retryable=True))),
    )

    with pytest.raises(StorageIntegrityError) as caught:
        worker.run_once()

    assert caught.value is failure
    assert len(backend.completions) == 1
    assert backend.completion_calls[-1]["outcome"] is IndexJobCompletion.REQUEUE


@pytest.mark.parametrize("substitution", ("outcome", "error_code"))
def test_completion_response_loss_must_match_the_requested_closure(
    substitution: str,
) -> None:
    backend = _Backend(
        completion_response_loss=RuntimeError("completion response lost"),
        completion_substitute_outcome=(
            IndexJobCompletion.REQUEUE if substitution == "outcome" else None
        ),
        completion_substitute_error_code=(
            "substituted_closure" if substitution == "error_code" else None
        ),
    )
    backend.add_job(f"completion-loss-substitution-{substitution}")
    worker, store, _factory = _worker(
        backend,
        _Resolver(_Executor(lambda context: _failed_result(context, retryable=False))),
    )

    with pytest.raises(StorageIntegrityError, match="persisted a different closure"):
        worker.run_once()

    assert len(backend.completions) == 1
    assert backend.publication_calls == []
    assert store.retained == []


def test_completion_response_loss_cannot_be_reconciled_as_success() -> None:
    backend = _Backend(
        completion_response_loss=RuntimeError("completion response lost"),
        completion_response_corruption="success_without_completion",
    )
    backend.add_job("completion-loss-substituted-success")
    worker, store, _factory = _worker(
        backend,
        _Resolver(_Executor(lambda context: _failed_result(context, retryable=False))),
    )

    with pytest.raises(StorageIntegrityError, match="persisted a successful closure"):
        worker.run_once()

    assert backend.completions == {}
    assert backend.publication_calls == []
    assert store.retained == []


@pytest.mark.parametrize("substitution", ("outcome", "error_code"))
def test_normal_completion_response_attests_the_requested_closure(
    substitution: str,
) -> None:
    backend = _Backend(
        completion_substitute_outcome=(
            IndexJobCompletion.REQUEUE if substitution == "outcome" else None
        ),
        completion_substitute_error_code=(
            "substituted_closure" if substitution == "error_code" else None
        ),
    )
    backend.add_job(f"completion-substitution-{substitution}")
    worker, _store, _factory = _worker(
        backend,
        _Resolver(_Executor(lambda context: _failed_result(context, retryable=False))),
    )

    with pytest.raises(StorageIntegrityError, match="different .* closure"):
        worker.run_once()

    assert len(backend.completions) == 1
    assert backend.publication_calls == []


@pytest.mark.parametrize(
    ("mode", "expected", "expected_code"),
    (
        ("conflict_cancel", IndexJobWorkerDisposition.CANCELLED, None),
        ("conflict_lost", IndexJobWorkerDisposition.LOST_AUTHORITY, None),
        (
            "conflict_permanent",
            IndexJobWorkerDisposition.FAILED,
            "worker_publication_conflict",
        ),
    ),
)
def test_publication_conflict_is_reconciled_as_cancel_lost_or_permanent(
    mode: str,
    expected: IndexJobWorkerDisposition,
    expected_code: str | None,
) -> None:
    backend = _Backend(publication_mode=mode)
    backend.add_job(f"publication-{mode}")
    worker, _store, _factory = _worker(backend, _default_resolver())

    result = worker.run_once()

    assert result.disposition is expected
    if expected_code is None and expected is IndexJobWorkerDisposition.LOST_AUTHORITY:
        assert backend.completion_calls == []
    elif expected_code is None:
        assert backend.completion_calls[-1]["outcome"] is IndexJobCompletion.CANCELLED
    else:
        assert backend.completion_calls[-1]["outcome"] is IndexJobCompletion.FAILED
        assert backend.completion_calls[-1]["error_code"] == expected_code


def test_publication_conflict_reconciles_a_concurrent_takeover() -> None:
    backend = _Backend(publication_mode="conflict_takeover")
    job = backend.add_job("publication-takeover-race")
    worker, store, _factory = _worker(backend, _default_resolver())

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.REQUEUED
    assert result.job_id == job.job_id
    assert result.attempt_count == 1
    assert backend.jobs[job.job_id].status is IndexJobStatus.RUNNING
    assert backend.jobs[job.job_id].attempt_count == 2
    assert backend.completions[(job.job_id, 1)].outcome is IndexJobCompletion.REQUEUE
    assert backend.completions[(job.job_id, 1)].error_code == "lease_expired"
    assert len(backend.publication_calls) == 1
    assert backend.completion_calls == []
    assert len(store.retained) == 1


def test_publication_exception_after_durable_success_reconciles_success() -> None:
    response_loss = RuntimeError("publication response lost")
    backend = _Backend(
        publication_mode="success_then_raise",
        publication_failure=response_loss,
    )
    backend.add_job("publication-response-loss")
    worker, store, _factory = _worker(backend, _default_resolver())

    result = worker.run_once()

    assert result.disposition is IndexJobWorkerDisposition.SUCCEEDED
    assert len(backend.publication_calls) == 1
    assert len(store.retained) == 1
    assert backend.completion_calls == []


@pytest.mark.parametrize("response_loss", (False, True))
def test_successful_publication_cannot_precede_its_observed_causal_floor(
    response_loss: bool,
) -> None:
    backend = _Backend(
        publication_mode="success_then_raise" if response_loss else "success",
        publication_failure=(
            RuntimeError("publication response lost") if response_loss else None
        ),
        publication_completed_at_ms_override=101,
    )
    job = backend.add_job(f"publication-causal-floor-{response_loss}")
    backend.jobs[job.job_id] = replace(
        job,
        attempt_count=1,
        started_at_ms=50,
        updated_at_ms=50,
        error_code="legacy_requeue",
    )
    backend.next_fencing_token = 2
    worker, store, _factory = _worker(backend, _default_resolver())

    with pytest.raises(StorageIntegrityError, match="causal completion time"):
        worker.run_once()

    assert backend.attempts[(job.job_id, 2)].started_at_ms == 102
    assert backend.jobs[job.job_id].finished_at_ms == 101
    assert len(backend.publication_calls) == 1
    assert backend.completion_calls == []
    assert len(store.retained) == 1


def test_successful_publication_time_must_be_an_exact_catalog_int64() -> None:
    backend = _Backend(publication_completed_at_ms_override=2**63)
    backend.add_job("publication-time-overflow")
    worker, store, _factory = _worker(backend, _default_resolver())

    with pytest.raises(StorageIntegrityError, match="invalid job"):
        worker.run_once()

    assert len(backend.publication_calls) == 1
    assert backend.completion_calls == []
    assert len(store.retained) == 1


@pytest.mark.parametrize("response_loss", (False, True))
def test_successful_publication_requires_one_exact_terminal_time(
    response_loss: bool,
) -> None:
    backend = _Backend(
        publication_mode=(
            "success_shifted_updated_then_raise"
            if response_loss
            else "success_shifted_updated"
        ),
        publication_failure=(
            RuntimeError("publication response lost") if response_loss else None
        ),
    )
    backend.add_job(f"publication-exact-time-{response_loss}")
    worker, store, _factory = _worker(backend, _default_resolver())

    with pytest.raises(StorageIntegrityError, match="causal completion time"):
        worker.run_once()

    assert len(backend.publication_calls) == 1
    assert backend.completion_calls == []
    assert len(store.retained) == 1


@pytest.mark.parametrize("failure_kind", ("response_loss", "conflict"))
def test_publication_failure_attests_the_expected_snapshot(
    failure_kind: str,
) -> None:
    backend = _Backend(
        publication_mode=(
            "success_wrong_snapshot_then_raise"
            if failure_kind == "response_loss"
            else "success_wrong_snapshot_then_conflict"
        ),
        publication_failure=(
            RuntimeError("publication response lost")
            if failure_kind == "response_loss"
            else None
        ),
    )
    backend.add_job(f"publication-wrong-snapshot-{failure_kind}")
    worker, store, _factory = _worker(backend, _default_resolver())

    with pytest.raises(StorageIntegrityError, match="different publication snapshot"):
        worker.run_once()

    assert len(backend.publication_calls) == 1
    assert backend.completion_calls == []
    assert len(store.retained) == 1


def test_publication_response_loss_cannot_be_reconciled_as_failure() -> None:
    backend = _Backend(
        publication_mode="failure_then_raise",
        publication_failure=RuntimeError("publication response lost"),
    )
    backend.add_job("publication-substituted-failure")
    worker, store, _factory = _worker(backend, _default_resolver())

    with pytest.raises(StorageIntegrityError, match="non-success closure"):
        worker.run_once()

    assert len(backend.completions) == 1
    assert backend.completion_calls[-1]["error_code"] == "substituted_failure"
    assert len(store.retained) == 1


@pytest.mark.parametrize(
    "mode",
    ("success_cancel_requested", "success_cancel_requested_then_raise"),
)
def test_publication_success_cannot_hide_a_cancellation_request(mode: str) -> None:
    backend = _Backend(
        publication_mode=mode,
        publication_failure=(
            RuntimeError("publication response lost")
            if mode.endswith("then_raise")
            else None
        ),
    )
    backend.add_job(f"publication-{mode}")
    worker, store, _factory = _worker(backend, _default_resolver())

    with pytest.raises(StorageIntegrityError, match="successful job"):
        worker.run_once()

    assert len(backend.publication_calls) == 1
    assert backend.completion_calls == []
    assert len(store.retained) == 1


def test_publication_integrity_failure_is_not_hidden_by_durable_success() -> None:
    backend = _Backend()
    job = backend.add_job("publication-integrity")
    store = _ReplacingRetainingStore()
    worker = IndexJobWorker(
        catalog_factory=_SessionFactory(backend),
        object_store=store,
        resolver=_default_resolver(),
        lease_duration_ms=300,
        heartbeat_interval_ms=50,
        owner_id_factory=lambda: "worker-publication-integrity",
        monotonic=lambda: 0.0,
    )

    with pytest.raises(StorageIntegrityError, match="replaced the retained"):
        worker.run_once()

    assert backend.jobs[job.job_id].status is IndexJobStatus.SUCCEEDED
    assert len(backend.publication_calls) == 1
    assert backend.completion_calls == []


def test_publication_success_must_match_the_claimed_immutable_job() -> None:
    backend = _Backend(publication_mode="success_wrong_identity")
    backend.add_job("publication-wrong-identity")
    worker, store, _factory = _worker(backend, _default_resolver())

    with pytest.raises(StorageIntegrityError, match="different job identity"):
        worker.run_once()

    assert len(backend.publication_calls) == 1
    assert backend.completion_calls == []
    assert len(store.retained) == 1
