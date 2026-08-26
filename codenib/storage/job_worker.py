# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Backend-neutral orchestration for durable whole-index jobs."""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .models import (
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
    IndexJobRequestedMode,
    IndexJobRunnableCursor,
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
    assert_no_secret_fields,
    canonical_json,
    snapshot_index_job_event_payload,
)
from .protocols import JobWorkerCatalog, ReceiptRetainingObjectStore
from .publication import (
    IndexJobViewArtifact,
    _attest_completed_publication,
    _preflight_job_artifacts,
    publish_job_artifacts,
)

_MAX_JOB_ID_CHARS = 80
_MAX_JOB_ERROR_CODE_CHARS = 128
_MAX_JOB_ERROR_MESSAGE_CHARS = 4_096
_MAX_JOB_ATTEMPTS = 1_000
_MAX_LEASE_DURATION_MS = 2_147_483_647
_MAX_SCAN_LIMIT = 256
_CATALOG_INT64_MAX = 2**63 - 1
_WORKER_EVENT_KEY_PREFIX = "worker."
_MISSING_SESSION_RESULT = object()


def _add_secondary_exception_note(
    primary: BaseException,
    secondary: BaseException,
    label: str,
) -> None:
    """Best-effort note cleanup failure without replacing the primary error."""

    try:
        add_note = getattr(primary, "add_note", None)
        if callable(add_note):
            add_note(f"{label}: {type(secondary).__name__}")
    except BaseException:  # noqa: B036 - notes must never replace the primary failure
        pass


def _run_catalog_session(
    factory: IndexJobCatalogSessionFactory,
    operation: Callable[[JobWorkerCatalog], object],
) -> object:
    """Run one operation without allowing ``__exit__`` to suppress failures."""

    result: object = _MISSING_SESSION_RESULT
    primary: BaseException | None = None
    try:
        with factory() as catalog:
            try:
                result = operation(catalog)
            except BaseException as exc:  # noqa: B036 - rethrown after session cleanup
                primary = exc
                raise
    except BaseException as cleanup_exc:  # noqa: B036 - preserve operation failure
        if primary is None:
            raise
        if cleanup_exc is not primary:
            _add_secondary_exception_note(
                primary,
                cleanup_exc,
                "catalog session cleanup also failed",
            )
    if primary is not None:
        raise primary
    if result is _MISSING_SESSION_RESULT:
        raise StorageIntegrityError("worker catalog session returned no result")
    return result


def _bounded_exact_text(value: object, field: str, maximum: int) -> str:
    if type(value) is not str or not value:
        raise StorageValidationError(f"{field} must be non-empty canonical text")
    if len(value) > maximum or "\x00" in value:
        raise StorageValidationError(f"{field} is out of bounds")
    if value != value.strip():
        raise StorageValidationError(f"{field} must be non-empty canonical text")
    return value


def _optional_safe_text(
    value: object,
    field: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    return _bounded_exact_text(value, field, maximum)


def _exact_attempt_count(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_JOB_ATTEMPTS:
        raise StorageValidationError(
            "worker attempt count must be an exact integer between 1 and 1000"
        )
    return value


def _canonical_event_payload_json(value: object) -> str:
    if type(value) is not str:
        raise StorageValidationError(
            "worker view-result payload JSON must be exact text"
        )
    if (
        not value
        or len(value) > INDEX_JOB_EVENT_PAYLOAD_MAX_TEXT_CHARS
        or "\x00" in value
    ):
        raise StorageValidationError("worker view-result payload JSON is out of bounds")
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError, RecursionError) as exc:
        raise StorageValidationError(
            "worker view-result payload must be valid JSON"
        ) from exc
    payload = snapshot_index_job_event_payload(parsed)
    canonical = canonical_json(payload)
    if canonical != value:
        raise StorageValidationError(
            "worker view-result payload JSON must be canonical"
        )
    return canonical


def _detach_job_view(value: object) -> IndexJobViewRecord:
    if type(value) is not IndexJobViewRecord:
        raise StorageValidationError(
            "worker view result requires an exact index job view request"
        )
    try:
        if type(value.requested_mode) is not IndexJobRequestedMode:
            raise StorageValidationError(
                "worker view request mode must use the exact enum"
            )
        detached = IndexJobViewRecord(
            job_id=value.job_id,
            view_type=value.view_type,
            profile_id=value.profile_id,
            requested_mode=value.requested_mode,
            required=value.required,
        )
    except StorageValidationError:
        raise
    except Exception as exc:
        raise StorageValidationError(
            "worker view request is structurally damaged"
        ) from exc
    if detached != value:
        raise StorageValidationError("worker view request is not canonical")
    return detached


def _detach_job_record(value: object) -> IndexJobRecord:
    if type(value) is not IndexJobRecord:
        raise StorageValidationError("worker requires an exact index job record")
    try:
        if type(value.status) is not IndexJobStatus:
            raise StorageValidationError("worker job status must use the exact enum")
        detached = replace(value)
    except StorageValidationError:
        raise
    except Exception as exc:
        raise StorageValidationError(
            "worker index job record is structurally damaged"
        ) from exc
    if (
        type(detached.max_attempts) is not int
        or not 1 <= detached.max_attempts <= _MAX_JOB_ATTEMPTS
        or type(detached.attempt_count) is not int
        or not 0 <= detached.attempt_count <= _MAX_JOB_ATTEMPTS
        or (
            detached.expected_ref_generation is not None
            and (
                type(detached.expected_ref_generation) is not int
                or not 0 <= detached.expected_ref_generation <= _CATALOG_INT64_MAX
            )
        )
        or any(
            timestamp is not None
            and (type(timestamp) is not int or not 0 <= timestamp <= _CATALOG_INT64_MAX)
            for timestamp in (
                detached.created_at_ms,
                detached.updated_at_ms,
                detached.started_at_ms,
                detached.finished_at_ms,
            )
        )
    ):
        raise StorageValidationError(
            "worker index job record contains a non-exact catalog integer"
        )
    if detached != value:
        raise StorageValidationError("worker index job record is not canonical")
    return detached


def _detach_job_attempt(value: object) -> IndexJobAttemptRecord:
    if type(value) is not IndexJobAttemptRecord:
        raise StorageValidationError("worker requires an exact job attempt record")
    try:
        detached = replace(value)
    except StorageValidationError:
        raise
    except Exception as exc:
        raise StorageValidationError(
            "worker job attempt record is structurally damaged"
        ) from exc
    if detached != value:
        raise StorageValidationError("worker job attempt record is not canonical")
    return detached


def _detach_job_lease(value: object) -> RefJobLease:
    if type(value) is not RefJobLease:
        raise StorageValidationError("worker requires an exact job lease record")
    try:
        detached = replace(value)
    except StorageValidationError:
        raise
    except Exception as exc:
        raise StorageValidationError(
            "worker job lease record is structurally damaged"
        ) from exc
    if detached != value:
        raise StorageValidationError("worker job lease record is not canonical")
    return detached


def _detach_attempt_completion(
    value: object,
) -> IndexJobAttemptCompletionRecord:
    if type(value) is not IndexJobAttemptCompletionRecord:
        raise StorageValidationError(
            "worker requires an exact attempt completion record"
        )
    try:
        detached = replace(value)
    except StorageValidationError:
        raise
    except Exception as exc:
        raise StorageValidationError(
            "worker attempt completion record is structurally damaged"
        ) from exc
    if detached != value or type(value.outcome) is not IndexJobCompletion:
        raise StorageValidationError(
            "worker attempt completion record is not canonical"
        )
    return detached


def _detach_view_artifact(value: object) -> IndexJobViewArtifact:
    if type(value) is not IndexJobViewArtifact:
        raise StorageValidationError(
            "successful worker view result requires an exact artifact"
        )
    try:
        detached = IndexJobViewArtifact(
            view_type=value.view_type,
            profile_id=value.profile_id,
            object_artifact=value.object_artifact,
            schema_version=value.schema_version,
            metadata_json=value.metadata_json,
            member_artifacts=value.member_artifacts,
        )
    except StorageValidationError:
        raise
    except Exception as exc:
        raise StorageValidationError(
            "worker view artifact is structurally damaged"
        ) from exc
    if detached != value:
        raise StorageValidationError("worker view artifact is not canonical")
    return detached


def _attest_runnable_page(
    value: object,
    *,
    limit: int,
) -> IndexJobRunnablePage:
    if type(value) is not IndexJobRunnablePage:
        raise StorageIntegrityError("worker scan returned a non-exact runnable page")
    try:
        raw_jobs = value.jobs
        raw_cursor = value.next_cursor
    except AttributeError as exc:
        raise StorageIntegrityError(
            "worker scan returned a structurally damaged page"
        ) from exc
    if type(raw_jobs) is not tuple:
        raise StorageIntegrityError("worker scan returned non-exact candidate jobs")
    if len(raw_jobs) > limit:
        raise StorageIntegrityError("worker scan exceeded its requested page limit")
    try:
        jobs = tuple(_detach_job_record(candidate) for candidate in raw_jobs)
        if raw_cursor is None:
            cursor = None
        elif type(raw_cursor) is IndexJobRunnableCursor:
            cursor = replace(raw_cursor)
            if cursor != raw_cursor:
                raise StorageValidationError("worker runnable cursor is not canonical")
        else:
            raise StorageValidationError(
                "worker runnable cursor must use the exact model"
            )
        detached = IndexJobRunnablePage(jobs=jobs, next_cursor=cursor)
    except (StorageValidationError, AttributeError) as exc:
        raise StorageIntegrityError(
            "worker scan returned an invalid runnable page"
        ) from exc
    if detached != value:
        raise StorageIntegrityError("worker scan returned a noncanonical page")
    return detached


def _canonical_job_views(job: IndexJobRecord) -> tuple[IndexJobViewRecord, ...]:
    request = IndexJobRequest(
        repository_id=job.repository_id,
        source_revision_id=job.source_revision_id,
        ref_name=job.ref_name,
        idempotency_key=job.idempotency_key,
        expected_ref_generation=job.expected_ref_generation,
        max_attempts=job.max_attempts,
        request_json=job.request_json,
    )
    if request.job_id != job.job_id or request.request_digest != job.request_digest:
        raise StorageValidationError("worker job request identity is inconsistent")
    return request.view_requests


class IndexJobStopReason(str, Enum):
    """Cooperative reasons for an executor to stop work promptly."""

    CANCEL_REQUESTED = "cancel_requested"
    AUTHORITY_LOST = "authority_lost"
    HEARTBEAT_FAULT = "heartbeat_fault"
    CONTROL_CONFLICT = "control_conflict"
    CONTROL_FAULT = "control_fault"


@runtime_checkable
class IndexJobStopToken(Protocol):
    """Read-only cooperative stop signal exposed to one executor."""

    @property
    def reason(self) -> IndexJobStopReason | None: ...

    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for a stop request without granting mutation authority."""

        ...


@runtime_checkable
class IndexJobExecutionControl(Protocol):
    """Thread-affine, attempt-scoped progress capability for an executor.

    The supported control API exposes neither the catalog nor raw publication
    methods. The worker seals the capability before it starts final reconciliation.
    One result-event slot is reserved for every requested view; exact replay
    of a progress key does not consume another slot.
    """

    @property
    def stop_token(self) -> IndexJobStopToken: ...

    def append_progress(
        self,
        event_key: str,
        payload: Mapping[str, Any] | None = None,
        view_type: str | None = None,
    ) -> IndexJobEventRecord: ...


@dataclass(frozen=True, slots=True)
class IndexJobExecutionContext:
    """Exact immutable inputs and bounded control for one claimed attempt."""

    job: IndexJobRecord
    views: tuple[IndexJobViewRecord, ...]
    attempt: IndexJobAttemptRecord
    lease: RefJobLease
    control: IndexJobExecutionControl

    def __post_init__(self) -> None:
        if type(self) is not IndexJobExecutionContext:
            raise StorageValidationError(
                "worker execution context must use the exact model"
            )
        job = _detach_job_record(self.job)
        if type(self.views) is not tuple or not 1 <= len(self.views) <= 64:
            raise StorageValidationError(
                "worker execution context views must be a bounded exact tuple"
            )
        views = tuple(_detach_job_view(view) for view in self.views)
        if not views or views != _canonical_job_views(self.job):
            raise StorageValidationError(
                "worker execution context views differ from the job request"
            )
        attempt = _detach_job_attempt(self.attempt)
        lease = _detach_job_lease(self.lease)
        if not isinstance(self.control, IndexJobExecutionControl):
            raise StorageValidationError(
                "worker execution context requires execution control"
            )
        token = self.control.stop_token
        if not isinstance(token, IndexJobStopToken):
            raise StorageValidationError(
                "worker execution control requires a stop token"
            )
        if job.status is not IndexJobStatus.RUNNING:
            raise StorageValidationError(
                "worker execution context requires a running job"
            )
        if (
            attempt.job_id != job.job_id
            or attempt.attempt_count != job.attempt_count
            or attempt.repository_id != job.repository_id
            or attempt.ref_name != job.ref_name
            or attempt.request_digest != job.request_digest
            or lease.job_id != job.job_id
            or lease.repository_id != job.repository_id
            or lease.ref_name != job.ref_name
            or lease.owner_id != attempt.owner_id
            or lease.fencing_token != attempt.fencing_token
            or lease.acquired_at_ms != attempt.started_at_ms
            or attempt.started_at_ms < job.created_at_ms
            or (
                job.started_at_ms is not None
                and attempt.started_at_ms < job.started_at_ms
            )
            or attempt.fencing_token < attempt.attempt_count
            or job.updated_at_ms < attempt.started_at_ms
        ):
            raise StorageValidationError(
                "worker execution context authority is inconsistent"
            )
        object.__setattr__(self, "job", job)
        object.__setattr__(self, "views", views)
        object.__setattr__(self, "attempt", attempt)
        object.__setattr__(self, "lease", lease)


@dataclass(frozen=True, slots=True)
class IndexJobViewExecutionResult:
    """One requested view's exact, secret-free executor result."""

    request: IndexJobViewRecord
    effective_mode: IndexJobEffectiveMode
    outcome: IndexJobViewOutcome
    artifact: IndexJobViewArtifact | None
    payload_json: str = "{}"

    def __post_init__(self) -> None:
        if type(self) is not IndexJobViewExecutionResult:
            raise StorageValidationError("worker view result must use the exact model")
        request = _detach_job_view(self.request)
        if type(self.effective_mode) is not IndexJobEffectiveMode:
            raise StorageValidationError(
                "worker view result effective mode must use the exact enum"
            )
        if type(self.outcome) is not IndexJobViewOutcome:
            raise StorageValidationError(
                "worker view result outcome must use the exact enum"
            )
        succeeded = self.outcome is IndexJobViewOutcome.SUCCEEDED
        if succeeded != (self.artifact is not None):
            raise StorageValidationError(
                "worker view artifacts are required exactly for successful views"
            )
        skipped = self.outcome is IndexJobViewOutcome.SKIPPED
        unavailable = self.effective_mode is IndexJobEffectiveMode.UNAVAILABLE
        if skipped != unavailable:
            raise StorageValidationError(
                "skipped worker views must be exactly the unavailable views"
            )
        artifact = (
            None if self.artifact is None else _detach_view_artifact(self.artifact)
        )
        if artifact is not None and (
            artifact.view_type != request.view_type
            or artifact.profile_id != request.profile_id
        ):
            raise StorageValidationError(
                "worker view artifact differs from its requested view or profile"
            )
        payload_json = _canonical_event_payload_json(self.payload_json)
        object.__setattr__(self, "request", request)
        object.__setattr__(self, "artifact", artifact)
        object.__setattr__(self, "payload_json", payload_json)

    @classmethod
    def create(
        cls,
        request: IndexJobViewRecord,
        *,
        effective_mode: IndexJobEffectiveMode,
        outcome: IndexJobViewOutcome,
        artifact: IndexJobViewArtifact | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> IndexJobViewExecutionResult:
        frozen = snapshot_index_job_event_payload({} if payload is None else payload)
        return cls(
            request=request,
            effective_mode=effective_mode,
            outcome=outcome,
            artifact=artifact,
            payload_json=canonical_json(frozen),
        )

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)


@dataclass(frozen=True, slots=True)
class IndexJobExecutionResult:
    """Exact whole-job result returned by one resolved executor."""

    views: tuple[IndexJobViewExecutionResult, ...]
    retryable: bool = False
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if type(self) is not IndexJobExecutionResult:
            raise StorageValidationError(
                "worker execution result must use the exact model"
            )
        if type(self.views) is not tuple or not self.views:
            raise StorageValidationError(
                "worker execution result views must be a non-empty exact tuple"
            )
        if len(self.views) > 64:
            raise StorageValidationError(
                "worker execution result cannot exceed 64 views"
            )
        if any(type(view) is not IndexJobViewExecutionResult for view in self.views):
            raise StorageValidationError(
                "worker execution result views must use exact result models"
            )
        views = tuple(
            IndexJobViewExecutionResult(
                request=view.request,
                effective_mode=view.effective_mode,
                outcome=view.outcome,
                artifact=view.artifact,
                payload_json=view.payload_json,
            )
            for view in self.views
        )
        ordering = tuple(view.request.view_type for view in views)
        if ordering != tuple(sorted(ordering)) or len(ordering) != len(set(ordering)):
            raise StorageValidationError(
                "worker execution result views are not canonical"
            )
        job_ids = {view.request.job_id for view in views}
        if len(job_ids) != 1:
            raise StorageValidationError(
                "worker execution result views belong to different jobs"
            )
        artifacts = tuple(view.artifact for view in views if view.artifact is not None)
        if artifacts:
            try:
                _preflight_job_artifacts(artifacts)
            except StorageValidationError:
                raise
            except (StorageIntegrityError, TypeError) as exc:
                raise StorageValidationError(
                    "worker execution result publication closure is invalid"
                ) from exc
        if type(self.retryable) is not bool:
            raise StorageValidationError(
                "worker execution retryable must be an exact boolean"
            )
        code = _optional_safe_text(
            self.error_code,
            "worker execution error code",
            _MAX_JOB_ERROR_CODE_CHARS,
        )
        message = _optional_safe_text(
            self.error_message,
            "worker execution error message",
            _MAX_JOB_ERROR_MESSAGE_CHARS,
        )
        if message is not None and code is None:
            raise StorageValidationError(
                "worker execution error message requires an error code"
            )
        assert_no_secret_fields(
            {"code": code, "message": message},
            source="worker execution error",
        )
        object.__setattr__(self, "views", views)
        object.__setattr__(self, "error_code", code)
        object.__setattr__(self, "error_message", message)
        if self.publishable and self.retryable:
            raise StorageValidationError(
                "publishable worker execution result cannot be retryable"
            )

    @property
    def publishable(self) -> bool:
        return (
            self.error_code is None
            and self.error_message is None
            and any(view.artifact is not None for view in self.views)
            and all(
                not view.request.required
                or view.outcome is IndexJobViewOutcome.SUCCEEDED
                for view in self.views
            )
        )

    @property
    def artifacts(self) -> tuple[IndexJobViewArtifact, ...]:
        return tuple(view.artifact for view in self.views if view.artifact is not None)


@runtime_checkable
class IndexJobExecutor(Protocol):
    """A resolved whole-job executor with no raw catalog authority."""

    def execute(
        self,
        context: IndexJobExecutionContext,
    ) -> IndexJobExecutionResult: ...


@runtime_checkable
class IndexJobExecutorResolver(Protocol):
    """Resolve one executor from the complete canonical job request."""

    def resolve(
        self,
        job: IndexJobRecord,
        views: tuple[IndexJobViewRecord, ...],
    ) -> IndexJobExecutor: ...


@runtime_checkable
class IndexJobObjectStoreBoundResolver(Protocol):
    """A resolver whose executors write into one declared object store."""

    @property
    def object_store(self) -> ReceiptRetainingObjectStore: ...


class IndexJobWorkerDisposition(str, Enum):
    """Stable result classes returned by one worker pass."""

    IDLE = "idle"
    SUCCEEDED = "succeeded"
    REQUEUED = "requeued"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST_AUTHORITY = "lost_authority"


@dataclass(frozen=True, slots=True)
class IndexJobWorkerRunResult:
    """Bounded identity and disposition for one worker pass."""

    disposition: IndexJobWorkerDisposition
    job_id: str | None
    attempt_count: int | None

    def __post_init__(self) -> None:
        if type(self) is not IndexJobWorkerRunResult:
            raise StorageValidationError("worker run result must use the exact model")
        if type(self.disposition) is not IndexJobWorkerDisposition:
            raise StorageValidationError(
                "worker run disposition must use the exact enum"
            )
        if self.disposition is IndexJobWorkerDisposition.IDLE:
            if self.job_id is not None or self.attempt_count is not None:
                raise StorageValidationError(
                    "idle worker result cannot identify an attempt"
                )
            return
        job_id = _bounded_exact_text(
            self.job_id,
            "worker result job ID",
            _MAX_JOB_ID_CHARS,
        )
        attempt = _exact_attempt_count(self.attempt_count)
        object.__setattr__(self, "job_id", job_id)
        object.__setattr__(self, "attempt_count", attempt)

    @classmethod
    def idle(cls) -> IndexJobWorkerRunResult:
        return cls(IndexJobWorkerDisposition.IDLE, None, None)


@runtime_checkable
class IndexJobCatalogSessionFactory(Protocol):
    """Open one thread-confined worker catalog session."""

    def __call__(self) -> AbstractContextManager[JobWorkerCatalog]: ...


@dataclass(frozen=True, slots=True)
class _AttemptAuthority:
    job_id: str
    repository_id: str
    source_revision_id: str
    ref_name: str
    request_digest: str
    attempt_count: int
    started_at_ms: int
    owner_id: str
    fencing_token: int


class _ClaimAuthorityLost(Exception):
    def __init__(self, authority: _AttemptAuthority) -> None:
        super().__init__("claimed job authority was superseded before execution")
        self.authority = authority


class _PublicationAuthorityLost(Exception):
    """Stop retained publication after its heartbeat authority was lost."""


class _PublicationCancelRequested(Exception):
    """Stop retained publication after observing durable cancellation."""


class _AttemptCausalEvidence:
    """Thread-safe causal frontier and exact worker-observed event evidence."""

    def __init__(
        self,
        authority: _AttemptAuthority,
        *,
        job_updated_at_ms: int,
        lease: RefJobLease,
        lease_duration_ms: int,
    ) -> None:
        acquired_at_ms, heartbeat_at_ms, lease_expires_at_ms = self._attest_lease_times(
            lease
        )
        if (
            acquired_at_ms != authority.started_at_ms
            or heartbeat_at_ms != acquired_at_ms
            or heartbeat_at_ms > _CATALOG_INT64_MAX - lease_duration_ms
            or lease_expires_at_ms != heartbeat_at_ms + lease_duration_ms
        ):
            raise StorageIntegrityError(
                "worker initial lease has an invalid causal shape"
            )
        self._events: dict[str, IndexJobEventRecord] = {}
        self._max_sequence: int | None = None
        self._causal_floor_ms = max(
            authority.started_at_ms,
            job_updated_at_ms,
            heartbeat_at_ms,
        )
        self._lease_acquired_at_ms = acquired_at_ms
        self._last_heartbeat_at_ms = heartbeat_at_ms
        self._last_lease_expires_at_ms = lease_expires_at_ms
        self._lease_duration_ms = lease_duration_ms
        self._lock = threading.Lock()

    @staticmethod
    def _attest_lease_times(lease: RefJobLease) -> tuple[int, int, int]:
        values = (
            lease.acquired_at_ms,
            lease.heartbeat_at_ms,
            lease.lease_expires_at_ms,
        )
        if any(
            type(value) is not int or not 0 <= value <= _CATALOG_INT64_MAX
            for value in values
        ):
            raise StorageIntegrityError(
                "worker lease returned a non-exact catalog timestamp"
            )
        return values

    @property
    def causal_floor_ms(self) -> int:
        with self._lock:
            return self._causal_floor_ms

    def observe_heartbeat(self, lease: RefJobLease) -> None:
        acquired_at_ms, heartbeat_at_ms, lease_expires_at_ms = self._attest_lease_times(
            lease
        )
        with self._lock:
            if (
                acquired_at_ms != self._lease_acquired_at_ms
                or heartbeat_at_ms < self._last_heartbeat_at_ms
                or lease_expires_at_ms <= self._last_lease_expires_at_ms
                or heartbeat_at_ms > _CATALOG_INT64_MAX - self._lease_duration_ms
                or lease_expires_at_ms < heartbeat_at_ms + self._lease_duration_ms
            ):
                raise StorageIntegrityError(
                    "worker heartbeat returned an invalid lease progression"
                )
            self._last_heartbeat_at_ms = heartbeat_at_ms
            self._last_lease_expires_at_ms = lease_expires_at_ms
            self._causal_floor_ms = max(
                self._causal_floor_ms,
                heartbeat_at_ms,
            )

    def prepare_event(
        self,
        event_key: str,
    ) -> tuple[IndexJobEventRecord | None, int, int | None]:
        """Snapshot evidence immediately before the event backend call."""

        with self._lock:
            existing = self._events.get(event_key)
            return (
                None if existing is None else replace(existing),
                self._causal_floor_ms,
                self._max_sequence,
            )

    def observe_event(
        self,
        value: IndexJobEventRecord,
        prepared: tuple[IndexJobEventRecord | None, int, int | None],
    ) -> IndexJobEventRecord:
        event = replace(value)
        prepared_existing, prepared_floor, prepared_max_sequence = prepared
        with self._lock:
            existing = self._events.get(event.event_key)
            if prepared_existing is not None:
                if existing != prepared_existing or event != prepared_existing:
                    raise StorageIntegrityError(
                        "worker event replay replaced its first observed record"
                    )
                return replace(prepared_existing)
            if existing is not None:
                raise StorageIntegrityError(
                    "worker new event raced with an observed replay key"
                )
            if (
                prepared_max_sequence is not None
                and event.sequence <= prepared_max_sequence
            ):
                raise StorageIntegrityError(
                    "worker event sequence did not advance monotonically"
                )
            if event.created_at_ms < prepared_floor:
                raise StorageIntegrityError(
                    "worker event time precedes its observed causal frontier"
                )
            self._events[event.event_key] = event
            self._max_sequence = event.sequence
            self._causal_floor_ms = max(
                self._causal_floor_ms,
                event.created_at_ms,
            )
            return replace(event)


def _attempt_authority(
    job: IndexJobRecord,
    attempt: IndexJobAttemptRecord,
) -> _AttemptAuthority:
    return _AttemptAuthority(
        job_id=job.job_id,
        repository_id=job.repository_id,
        source_revision_id=job.source_revision_id,
        ref_name=job.ref_name,
        request_digest=job.request_digest,
        attempt_count=attempt.attempt_count,
        started_at_ms=attempt.started_at_ms,
        owner_id=attempt.owner_id,
        fencing_token=attempt.fencing_token,
    )


class _StopState:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: IndexJobStopReason | None = None

    @property
    def reason(self) -> IndexJobStopReason | None:
        with self._lock:
            return self._reason

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("stop-token timeout must be a non-negative number")
        return self._event.wait(timeout)

    def set(self, reason: IndexJobStopReason) -> None:
        with self._lock:
            if self._reason is None:
                self._reason = reason
                self._event.set()


class _StopTokenView:
    """Read-only executor capability backed by the worker-owned stop state."""

    __slots__ = ("__state",)

    def __init__(self, state: _StopState) -> None:
        self.__state = state

    @property
    def reason(self) -> IndexJobStopReason | None:
        return self.__state.reason

    def is_set(self) -> bool:
        return self.__state.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.__state.wait(timeout)


class _AttemptExecutionControl:
    """Main-thread progress lane for one executor invocation."""

    def __init__(
        self,
        catalog: JobWorkerCatalog,
        authority: _AttemptAuthority,
        stop_state: _StopState,
        views: tuple[IndexJobViewRecord, ...],
        causal_evidence: _AttemptCausalEvidence,
    ) -> None:
        self._catalog = catalog
        self._authority = authority
        self._stop_state = stop_state
        self._stop_token = _StopTokenView(stop_state)
        self._thread_id = threading.get_ident()
        self._sealed = False
        self._in_progress = False
        self._fault: BaseException | None = None
        self._progress_limit = MAX_INDEX_JOB_EVENTS_PER_ATTEMPT - len(views)
        self._progress_keys: set[str] = set()
        self._view_types = frozenset(view.view_type for view in views)
        self._causal_evidence = causal_evidence
        self._lock = threading.Lock()

    @property
    def stop_token(self) -> IndexJobStopToken:
        return self._stop_token

    @property
    def fault(self) -> BaseException | None:
        with self._lock:
            return self._fault

    @property
    def causal_floor_ms(self) -> int:
        return self._causal_evidence.causal_floor_ms

    def observe_worker_event(
        self,
        event: IndexJobEventRecord,
        prepared: tuple[IndexJobEventRecord | None, int, int | None],
    ) -> IndexJobEventRecord:
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("worker event control crossed a thread boundary")
        try:
            return self._causal_evidence.observe_event(event, prepared)
        except StorageIntegrityError as exc:
            with self._lock:
                if self._fault is None:
                    self._fault = exc
            self._stop_state.set(IndexJobStopReason.CONTROL_FAULT)
            raise

    def prepare_worker_event(
        self,
        event_key: str,
    ) -> tuple[IndexJobEventRecord | None, int, int | None]:
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("worker event control crossed a thread boundary")
        return self._causal_evidence.prepare_event(event_key)

    def append_progress(
        self,
        event_key: str,
        payload: Mapping[str, Any] | None = None,
        view_type: str | None = None,
    ) -> IndexJobEventRecord:
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("worker progress control crossed a thread boundary")
        with self._lock:
            if self._in_progress:
                failure = RuntimeError("worker progress control is not reentrant")
                self._fault = failure
                self._stop_state.set(IndexJobStopReason.CONTROL_FAULT)
                raise failure
            if self._sealed:
                raise RuntimeError("worker progress control is sealed")
            if self._stop_state.is_set():
                raise RuntimeError("worker attempt has been asked to stop")
            self._in_progress = True
        try:
            event_key = _bounded_exact_text(
                event_key,
                "executor progress key",
                128,
            )
            if event_key.startswith(_WORKER_EVENT_KEY_PREFIX):
                raise StorageValidationError(
                    "executor progress key uses the reserved worker prefix"
                )
            if view_type is not None:
                view_type = _bounded_exact_text(
                    view_type,
                    "executor progress view type",
                    128,
                )
                if view_type not in self._view_types:
                    raise StorageValidationError(
                        "executor progress view type was not requested"
                    )
            with self._lock:
                is_new_key = event_key not in self._progress_keys
                if is_new_key and len(self._progress_keys) >= self._progress_limit:
                    raise StorageValidationError(
                        "executor progress exhausted its reserved event budget"
                    )
            frozen_payload = snapshot_index_job_event_payload(
                {} if payload is None else payload
            )
            expected_payload_json = canonical_json(frozen_payload)
            with self._lock:
                if self._fault is not None:
                    raise self._fault
                if self._stop_state.is_set():
                    raise RuntimeError("worker attempt has been asked to stop")

            prepared_event = self._causal_evidence.prepare_event(event_key)

            def append() -> IndexJobEventRecord:
                return self._catalog.append_job_event(
                    self._authority.job_id,
                    attempt_count=self._authority.attempt_count,
                    owner_id=self._authority.owner_id,
                    fencing_token=self._authority.fencing_token,
                    event_key=event_key,
                    payload=json.loads(expected_payload_json),
                    view_type=view_type,
                )

            try:
                try:
                    value = append()
                except (
                    PublishConflict,
                    StorageIntegrityError,
                    StorageNotFound,
                ):
                    raise
                except StorageValidationError as exc:
                    raise StorageIntegrityError(
                        "worker progress catalog rejected a prevalidated event"
                    ) from exc
                except Exception as first_failure:
                    try:
                        value = append()
                    except (
                        PublishConflict,
                        StorageIntegrityError,
                        StorageNotFound,
                    ):
                        raise
                    except StorageValidationError as exc:
                        raise StorageIntegrityError(
                            "worker progress replay failed validation after an "
                            "unknown write outcome"
                        ) from exc
                    except Exception:
                        raise first_failure
                event = _attest_event(
                    value,
                    self._authority,
                    event_key=event_key,
                    kind=IndexJobEventKind.PROGRESS,
                    view_type=view_type,
                    effective_mode=None,
                    outcome=None,
                    payload_json=expected_payload_json,
                )
                event = self.observe_worker_event(event, prepared_event)
                with self._lock:
                    self._progress_keys.add(event_key)
                return event
            except (PublishConflict, StorageNotFound):
                self._stop_state.set(IndexJobStopReason.CONTROL_CONFLICT)
                raise
            except StorageValidationError:
                raise
            except Exception as exc:
                self._fault = exc
                self._stop_state.set(IndexJobStopReason.CONTROL_FAULT)
                raise
        except BaseException as exc:  # noqa: B036 - sticky async control fault
            if isinstance(exc, Exception):
                raise
            with self._lock:
                if self._fault is None:
                    self._fault = exc
            self._stop_state.set(IndexJobStopReason.CONTROL_FAULT)
            raise
        finally:
            with self._lock:
                self._in_progress = False

    def seal(self) -> None:
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("worker progress control crossed a thread boundary")
        with self._lock:
            self._sealed = True

    def seal_and_settle(self) -> None:
        """Publish the sealed state before rethrowing an async interruption."""

        first_failure: BaseException | None = None
        while True:
            try:
                self.seal()
            except BaseException as exc:  # noqa: B036 - settle before rethrow
                if first_failure is None:
                    first_failure = exc
            with self._lock:
                if self._sealed:
                    break
        if first_failure is not None:
            raise first_failure


def _attest_heartbeat(
    value: object,
    authority: _AttemptAuthority,
) -> IndexJobAttemptHeartbeat:
    if type(value) is not IndexJobAttemptHeartbeat:
        raise StorageIntegrityError("worker heartbeat returned a non-exact record")
    try:
        lease = _detach_job_lease(value.lease)
        heartbeat = IndexJobAttemptHeartbeat(
            job_id=value.job_id,
            attempt_count=value.attempt_count,
            cancel_requested=value.cancel_requested,
            lease=lease,
        )
    except (StorageValidationError, AttributeError) as exc:
        raise StorageIntegrityError(
            "worker heartbeat returned an invalid record"
        ) from exc
    if heartbeat != value:
        raise StorageIntegrityError("worker heartbeat returned a noncanonical record")
    if (
        heartbeat.job_id != authority.job_id
        or heartbeat.attempt_count != authority.attempt_count
        or lease.repository_id != authority.repository_id
        or lease.ref_name != authority.ref_name
        or lease.job_id != authority.job_id
        or lease.owner_id != authority.owner_id
        or lease.fencing_token != authority.fencing_token
        or lease.acquired_at_ms != authority.started_at_ms
    ):
        raise StorageIntegrityError("worker heartbeat returned different authority")
    return heartbeat


def _attest_job_identity(
    value: object,
    authority: _AttemptAuthority,
) -> IndexJobRecord:
    try:
        job = _detach_job_record(value)
    except StorageValidationError as exc:
        raise StorageIntegrityError("worker catalog returned an invalid job") from exc
    if (
        job.job_id != authority.job_id
        or job.repository_id != authority.repository_id
        or job.source_revision_id != authority.source_revision_id
        or job.ref_name != authority.ref_name
        or job.request_digest != authority.request_digest
        or job.attempt_count < authority.attempt_count
    ):
        raise StorageIntegrityError("worker catalog returned a different job identity")
    return job


def _job_matches_attempt_completion(
    job: IndexJobRecord,
    completion: IndexJobAttemptCompletionRecord,
) -> bool:
    """Check the current same-attempt job state against its immutable closure."""

    if job.job_id != completion.job_id or job.attempt_count != completion.attempt_count:
        return False
    if (
        completion.outcome is IndexJobCompletion.REQUEUE
        and job.status is IndexJobStatus.CANCELLED
    ):
        return (
            job.cancel_requested
            and job.result_snapshot_id is None
            and job.error_code == "cancelled"
            and job.error_message is None
            and job.finished_at_ms is not None
            and job.updated_at_ms == job.finished_at_ms
            and job.finished_at_ms >= completion.completed_at_ms
        )
    expected_status = {
        IndexJobCompletion.REQUEUE: IndexJobStatus.QUEUED,
        IndexJobCompletion.FAILED: IndexJobStatus.FAILED,
        IndexJobCompletion.CANCELLED: IndexJobStatus.CANCELLED,
    }[completion.outcome]
    return (
        job.status is expected_status
        and job.cancel_requested is (completion.outcome is IndexJobCompletion.CANCELLED)
        and job.result_snapshot_id is None
        and job.error_code == completion.error_code
        and job.error_message == completion.error_message
        and job.updated_at_ms == completion.completed_at_ms
        and job.finished_at_ms
        == (
            None
            if completion.outcome is IndexJobCompletion.REQUEUE
            else completion.completed_at_ms
        )
    )


def _attest_successful_job_causal_time(
    job: IndexJobRecord,
    authority: _AttemptAuthority,
    *,
    causal_floor_ms: int,
) -> None:
    if (
        job.status is not IndexJobStatus.SUCCEEDED
        or job.attempt_count != authority.attempt_count
        or job.finished_at_ms is None
        or job.updated_at_ms != job.finished_at_ms
        or job.finished_at_ms < max(authority.started_at_ms, causal_floor_ms)
    ):
        raise StorageIntegrityError(
            "worker successful job has an invalid causal completion time"
        )


def _attest_event(
    value: object,
    authority: _AttemptAuthority,
    *,
    event_key: str,
    kind: IndexJobEventKind,
    view_type: str | None,
    effective_mode: IndexJobEffectiveMode | None,
    outcome: IndexJobViewOutcome | None,
    payload_json: str,
) -> IndexJobEventRecord:
    if type(value) is not IndexJobEventRecord:
        raise StorageIntegrityError("worker event returned a non-exact record")
    try:
        if type(value.kind) is not IndexJobEventKind:
            raise StorageValidationError("worker event kind must use the exact enum")
        if (
            value.effective_mode is not None
            and type(value.effective_mode) is not IndexJobEffectiveMode
        ):
            raise StorageValidationError(
                "worker event effective mode must use the exact enum"
            )
        if value.outcome is not None and type(value.outcome) is not IndexJobViewOutcome:
            raise StorageValidationError("worker event outcome must use the exact enum")
        event = replace(value)
    except (StorageValidationError, AttributeError) as exc:
        raise StorageIntegrityError("worker event returned an invalid record") from exc
    if event != value:
        raise StorageIntegrityError("worker event returned a noncanonical record")
    if (
        event.job_id != authority.job_id
        or event.attempt_count != authority.attempt_count
        or event.owner_id != authority.owner_id
        or event.fencing_token != authority.fencing_token
        or event.event_key != event_key
        or event.kind is not kind
        or event.view_type != view_type
        or event.effective_mode is not effective_mode
        or event.outcome is not outcome
        or event.payload_json != payload_json
    ):
        raise StorageIntegrityError("worker event returned a different closure")
    return event


class _HeartbeatPump:
    def __init__(
        self,
        *,
        catalog_factory: IndexJobCatalogSessionFactory,
        main_catalog: JobWorkerCatalog,
        authority: _AttemptAuthority,
        stop_state: _StopState,
        causal_evidence: _AttemptCausalEvidence,
        lease_duration_ms: int,
        interval_ms: int,
        monotonic: Callable[[], float],
    ) -> None:
        self._catalog_factory = catalog_factory
        self._main_catalog = main_catalog
        self._authority = authority
        self._stop_state = stop_state
        self._causal_evidence = causal_evidence
        self._lease_duration_ms = lease_duration_ms
        self._interval = interval_ms / 1_000
        self._monotonic = monotonic
        self._shutdown = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"codenib-job-heartbeat-{authority.attempt_count}",
            daemon=False,
        )
        self._started = False
        self.fault: BaseException | None = None

    def start(self) -> None:
        try:
            self._thread.start()
        finally:
            self._started = self._thread.ident is not None
        try:
            self._ready.wait()
        except BaseException as exc:
            try:
                self.stop_and_join()
            except BaseException as cleanup_exc:  # noqa: B036 - preserve primary
                if cleanup_exc is not exc:
                    _add_secondary_exception_note(
                        exc,
                        cleanup_exc,
                        "heartbeat cleanup also failed",
                    )
            raise exc

    def stop_and_join(self) -> None:
        first_failure: BaseException | None = None
        while not self._shutdown.is_set():
            try:
                self._shutdown.set()
            except BaseException as exc:  # noqa: B036 - settle before rethrow
                if first_failure is None:
                    first_failure = exc
        if self._started:
            while self._thread.is_alive():
                try:
                    self._thread.join()
                except BaseException as exc:  # noqa: B036 - settle before rethrow
                    if first_failure is None:
                        first_failure = exc
        if first_failure is not None:
            raise first_failure

    def _run(self) -> None:
        try:

            def run_heartbeat(catalog: JobWorkerCatalog) -> None:
                if catalog is self._main_catalog:
                    raise StorageValidationError(
                        "heartbeat requires an independent catalog session"
                    )
                if not isinstance(catalog, JobWorkerCatalog):
                    raise StorageValidationError(
                        "heartbeat session lacks worker catalog capabilities"
                    )
                deadline = self._monotonic()
                while not self._shutdown.is_set():
                    try:
                        heartbeat = _attest_heartbeat(
                            catalog.heartbeat_job_attempt(
                                self._authority.job_id,
                                attempt_count=self._authority.attempt_count,
                                owner_id=self._authority.owner_id,
                                fencing_token=self._authority.fencing_token,
                                lease_duration_ms=self._lease_duration_ms,
                            ),
                            self._authority,
                        )
                    except (PublishConflict, StorageNotFound):
                        self._stop_state.set(IndexJobStopReason.AUTHORITY_LOST)
                        break
                    self._causal_evidence.observe_heartbeat(heartbeat.lease)
                    if heartbeat.cancel_requested:
                        self._stop_state.set(IndexJobStopReason.CANCEL_REQUESTED)
                        break
                    self._ready.set()
                    deadline += self._interval
                    delay = deadline - self._monotonic()
                    if delay <= 0:
                        deadline = self._monotonic() + self._interval
                        delay = self._interval
                    if self._shutdown.wait(delay):
                        break

            _run_catalog_session(self._catalog_factory, run_heartbeat)
        except BaseException as exc:  # noqa: B036 - handed back to main thread
            self.fault = exc
            self._stop_state.set(IndexJobStopReason.HEARTBEAT_FAULT)
        finally:
            self._ready.set()


def _default_owner_id() -> str:
    return f"worker-{uuid.uuid4().hex}"


def _exact_worker_integer(
    value: object,
    field: str,
    *,
    maximum: int,
) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise StorageValidationError(
            f"{field} must be an exact integer between 1 and {maximum}"
        )
    return value


class IndexJobWorker:
    """Run at most one whole durable index job per advisory scan pass.

    The resolver and executor prepare artifacts only. This worker owns every
    catalog mutation, including claim, heartbeat, view-result events,
    non-success closure, and the sole receipt-retained success publication.
    """

    def __init__(
        self,
        *,
        catalog_factory: IndexJobCatalogSessionFactory,
        object_store: ReceiptRetainingObjectStore,
        resolver: IndexJobExecutorResolver,
        lease_duration_ms: int,
        heartbeat_interval_ms: int,
        scan_limit: int = 64,
        owner_id_factory: Callable[[], str] = _default_owner_id,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(catalog_factory):
            raise TypeError("worker catalog factory must be callable")
        if not isinstance(object_store, ReceiptRetainingObjectStore):
            raise TypeError("worker requires a receipt-retaining object store")
        if not isinstance(resolver, IndexJobExecutorResolver):
            raise TypeError("worker resolver does not implement its protocol")
        if isinstance(resolver, IndexJobObjectStoreBoundResolver):
            resolver_object_store = resolver.object_store
            if not isinstance(
                resolver_object_store,
                ReceiptRetainingObjectStore,
            ):
                raise TypeError(
                    "worker bound resolver requires a receipt-retaining object store"
                )
            if resolver_object_store is not object_store:
                raise StorageValidationError(
                    "worker and bound resolver must use the same object store"
                )
        if not callable(owner_id_factory):
            raise TypeError("worker owner ID factory must be callable")
        if not callable(monotonic):
            raise TypeError("worker monotonic clock must be callable")
        lease_duration = _exact_worker_integer(
            lease_duration_ms,
            "worker lease duration",
            maximum=_MAX_LEASE_DURATION_MS,
        )
        heartbeat_interval = _exact_worker_integer(
            heartbeat_interval_ms,
            "worker heartbeat interval",
            maximum=_MAX_LEASE_DURATION_MS,
        )
        page_limit = _exact_worker_integer(
            scan_limit,
            "worker scan limit",
            maximum=_MAX_SCAN_LIMIT,
        )
        if heartbeat_interval * 3 >= lease_duration:
            raise StorageValidationError(
                "worker heartbeat interval must be less than one third of the lease"
            )
        self._catalog_factory = catalog_factory
        self._object_store = object_store
        self._resolver = resolver
        self._lease_duration_ms = lease_duration
        self._heartbeat_interval_ms = heartbeat_interval
        self._scan_limit = page_limit
        self._owner_id_factory = owner_id_factory
        self._monotonic = monotonic
        self._run_lock = threading.Lock()

    def run_once(self) -> IndexJobWorkerRunResult:
        """Claim and execute the first candidate won from one advisory page."""

        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("index job worker is already running")
        try:
            result = _run_catalog_session(
                self._catalog_factory,
                self._run_once_in_session,
            )
            if type(result) is not IndexJobWorkerRunResult:
                raise StorageIntegrityError(
                    "worker catalog session returned no run result"
                )
            return result
        finally:
            self._run_lock.release()

    def _run_once_in_session(
        self,
        catalog: JobWorkerCatalog,
    ) -> IndexJobWorkerRunResult:
        if not isinstance(catalog, JobWorkerCatalog):
            raise StorageValidationError(
                "worker session lacks required catalog capabilities"
            )
        page = _attest_runnable_page(
            catalog.scan_runnable_jobs(limit=self._scan_limit),
            limit=self._scan_limit,
        )
        for candidate in page.jobs:
            owner = _bounded_exact_text(
                self._owner_id_factory(),
                "worker owner ID",
                256,
            )
            try:
                lease = self._acquire_candidate(
                    catalog,
                    candidate.job_id,
                    owner,
                )
            except (PublishConflict, StorageNotFound):
                continue
            try:
                return self._run_claimed_job(
                    catalog,
                    candidate.job_id,
                    owner,
                    lease,
                )
            except _ClaimAuthorityLost as lost:
                return self._run_result(
                    IndexJobWorkerDisposition.LOST_AUTHORITY,
                    lost.authority,
                )
        return IndexJobWorkerRunResult.idle()

    def _acquire_candidate(
        self,
        catalog: JobWorkerCatalog,
        job_id: str,
        owner_id: str,
    ) -> RefJobLease:
        """Retry one uncertain claim response with the same task owner."""

        try:
            return catalog.acquire_job_lease(
                job_id,
                owner_id=owner_id,
                lease_duration_ms=self._lease_duration_ms,
            )
        except (PublishConflict, StorageIntegrityError, StorageNotFound):
            raise
        except Exception as first_failure:
            try:
                return catalog.acquire_job_lease(
                    job_id,
                    owner_id=owner_id,
                    lease_duration_ms=self._lease_duration_ms,
                )
            except (PublishConflict, StorageIntegrityError, StorageNotFound):
                raise
            except Exception:
                raise first_failure

    def _run_claimed_job(
        self,
        catalog: JobWorkerCatalog,
        job_id: str,
        owner_id: str,
        lease_value: object,
    ) -> IndexJobWorkerRunResult:
        job, views, attempt, lease, authority = self._bind_claimed_attempt(
            catalog,
            job_id,
            owner_id,
            lease_value,
        )
        stop_state = _StopState()
        causal_evidence = _AttemptCausalEvidence(
            authority,
            job_updated_at_ms=job.updated_at_ms,
            lease=lease,
            lease_duration_ms=self._lease_duration_ms,
        )
        control = _AttemptExecutionControl(
            catalog,
            authority,
            stop_state,
            views,
            causal_evidence,
        )
        try:
            context = IndexJobExecutionContext(
                job=job,
                views=views,
                attempt=attempt,
                lease=lease,
                control=control,
            )
        except StorageValidationError as exc:
            raise StorageIntegrityError(
                "claimed worker attempt failed context attestation"
            ) from exc
        pump = _HeartbeatPump(
            catalog_factory=self._catalog_factory,
            main_catalog=catalog,
            authority=authority,
            stop_state=stop_state,
            causal_evidence=causal_evidence,
            lease_duration_ms=self._lease_duration_ms,
            interval_ms=self._heartbeat_interval_ms,
            monotonic=self._monotonic,
        )
        result: IndexJobExecutionResult | None = None
        failure_code: str | None = None
        phase_base_exception: BaseException | None = None
        keep_heartbeat_for_publication = False
        try:
            pump.start()
            if not stop_state.is_set():
                try:
                    executor = self._resolver.resolve(context.job, context.views)
                    if not isinstance(executor, IndexJobExecutor):
                        raise StorageValidationError(
                            "worker resolver returned an invalid executor"
                        )
                except StorageIntegrityError as exc:
                    phase_base_exception = exc
                except Exception:
                    failure_code = "worker_resolver_failed"
                except BaseException as exc:  # noqa: B036 - exact rethrow below
                    phase_base_exception = exc
                if (
                    failure_code is None
                    and phase_base_exception is None
                    and not stop_state.is_set()
                ):
                    try:
                        returned = executor.execute(context)
                        if type(returned) is not IndexJobExecutionResult:
                            failure_code = "worker_executor_incomplete"
                        else:
                            try:
                                detached = IndexJobExecutionResult(
                                    views=returned.views,
                                    retryable=returned.retryable,
                                    error_code=returned.error_code,
                                    error_message=returned.error_message,
                                )
                            except Exception:
                                failure_code = "worker_executor_incomplete"
                            else:
                                if (
                                    tuple(view.request for view in detached.views)
                                    != views
                                ):
                                    failure_code = "worker_executor_incomplete"
                                else:
                                    result = detached
                    except StorageIntegrityError as exc:
                        phase_base_exception = exc
                    except Exception:
                        failure_code = "worker_executor_failed"
                    except BaseException as exc:  # noqa: B036 - exact rethrow below
                        phase_base_exception = exc

            if (
                result is not None
                and failure_code is None
                and phase_base_exception is None
                and not stop_state.is_set()
            ):
                for ordinal, view_result in enumerate(result.views):
                    if stop_state.is_set():
                        break
                    try:
                        self._record_view_result(
                            catalog,
                            authority,
                            ordinal,
                            view_result,
                            control,
                        )
                    except (PublishConflict, StorageNotFound):
                        stop_state.set(IndexJobStopReason.CONTROL_CONFLICT)
                        result = None
                        break
        except BaseException as exc:  # noqa: B036 - cleanup before exact rethrow
            if phase_base_exception is None:
                phase_base_exception = exc
        finally:
            try:
                control.seal_and_settle()
            except BaseException as exc:  # noqa: B036 - preserve first failure
                if phase_base_exception is None:
                    phase_base_exception = exc
            keep_heartbeat_for_publication = (
                phase_base_exception is None
                and control.fault is None
                and pump.fault is None
                and not stop_state.is_set()
                and failure_code is None
                and result is not None
                and result.publishable
            )
            if not keep_heartbeat_for_publication:
                try:
                    pump.stop_and_join()
                except BaseException as exc:  # noqa: B036 - preserve first failure
                    if phase_base_exception is None:
                        phase_base_exception = exc

        if keep_heartbeat_for_publication:
            heartbeat_settled = False

            def settle_heartbeat() -> None:
                nonlocal heartbeat_settled
                pump.stop_and_join()
                heartbeat_settled = True
                if pump.fault is not None:
                    raise pump.fault

            def before_catalog_publish() -> None:
                settle_heartbeat()
                if stop_state.reason is IndexJobStopReason.AUTHORITY_LOST:
                    raise _PublicationAuthorityLost
                if stop_state.reason is IndexJobStopReason.CANCEL_REQUESTED:
                    raise _PublicationCancelRequested
                heartbeat = self._final_heartbeat(
                    catalog,
                    authority,
                    causal_evidence,
                )
                if heartbeat is None:
                    raise _PublicationAuthorityLost
                if heartbeat.cancel_requested:
                    raise _PublicationCancelRequested

            publication_failure: BaseException | None = None
            try:
                terminal = self._reconcile_attempt(
                    catalog,
                    authority,
                    causal_floor_ms=causal_evidence.causal_floor_ms,
                )
                if terminal is not None:
                    settle_heartbeat()
                    return terminal
                return self._publish_result(
                    catalog,
                    job,
                    authority,
                    result,
                    causal_evidence,
                    before_catalog_publish=before_catalog_publish,
                )
            except BaseException as exc:  # noqa: B036 - settle before rethrow
                publication_failure = exc
                raise
            finally:
                if not heartbeat_settled:
                    try:
                        pump.stop_and_join()
                    except BaseException as cleanup_exc:  # noqa: B036
                        if publication_failure is None:
                            raise
                        _add_secondary_exception_note(
                            publication_failure,
                            cleanup_exc,
                            "heartbeat cleanup also failed",
                        )

        if phase_base_exception is not None:
            raise phase_base_exception
        if control.fault is not None:
            raise control.fault
        if pump.fault is not None:
            raise pump.fault
        terminal = self._reconcile_attempt(
            catalog,
            authority,
            causal_floor_ms=causal_evidence.causal_floor_ms,
        )
        if terminal is not None:
            return terminal
        if stop_state.reason is IndexJobStopReason.AUTHORITY_LOST:
            return self._run_result(
                IndexJobWorkerDisposition.LOST_AUTHORITY,
                authority,
            )
        if stop_state.reason is IndexJobStopReason.CANCEL_REQUESTED:
            return self._complete_attempt(
                catalog,
                authority,
                outcome=IndexJobCompletion.CANCELLED,
                error_code=None,
                error_message=None,
                causal_evidence=causal_evidence,
            )
        heartbeat = self._final_heartbeat(catalog, authority, causal_evidence)
        if heartbeat is None:
            terminal = self._reconcile_attempt(
                catalog,
                authority,
                causal_floor_ms=causal_evidence.causal_floor_ms,
            )
            return terminal or self._run_result(
                IndexJobWorkerDisposition.LOST_AUTHORITY,
                authority,
            )
        if heartbeat.cancel_requested:
            return self._complete_attempt(
                catalog,
                authority,
                outcome=IndexJobCompletion.CANCELLED,
                error_code=None,
                error_message=None,
                causal_evidence=causal_evidence,
            )
        if stop_state.reason is IndexJobStopReason.CONTROL_CONFLICT:
            return self._run_result(
                IndexJobWorkerDisposition.LOST_AUTHORITY,
                authority,
            )
        if failure_code is not None:
            return self._complete_failure(
                catalog,
                job,
                authority,
                retryable=failure_code
                in {"worker_resolver_failed", "worker_executor_failed"},
                error_code=failure_code,
                error_message=None,
                causal_evidence=causal_evidence,
            )
        if result is None or not result.publishable:
            code = (
                "worker_executor_incomplete"
                if result is None or result.error_code is None
                else result.error_code
            )
            message = None if result is None else result.error_message
            retryable = False if result is None else result.retryable
            return self._complete_failure(
                catalog,
                job,
                authority,
                retryable=retryable,
                error_code=code,
                error_message=message,
                causal_evidence=causal_evidence,
            )
        return self._publish_result(
            catalog,
            job,
            authority,
            result,
            causal_evidence,
        )

    @staticmethod
    def _record_view_result(
        catalog: JobWorkerCatalog,
        authority: _AttemptAuthority,
        ordinal: int,
        result: IndexJobViewExecutionResult,
        control: _AttemptExecutionControl,
    ) -> IndexJobEventRecord:
        arguments = {
            "attempt_count": authority.attempt_count,
            "owner_id": authority.owner_id,
            "fencing_token": authority.fencing_token,
            "event_key": f"worker.view-result.{ordinal:02d}",
            "view_type": result.request.view_type,
            "effective_mode": result.effective_mode,
            "outcome": result.outcome,
        }

        def record(payload: dict[str, Any]) -> IndexJobEventRecord:
            return catalog.record_job_view_result(
                authority.job_id,
                payload=payload,
                **arguments,
            )

        first_payload = result.payload
        prepared_event = control.prepare_worker_event(arguments["event_key"])
        try:
            value = record(first_payload)
        except (PublishConflict, StorageIntegrityError, StorageNotFound):
            raise
        except StorageValidationError as exc:
            raise StorageIntegrityError(
                "worker view-result catalog rejected a prevalidated event"
            ) from exc
        except Exception as first_failure:
            try:
                value = record(result.payload)
            except (PublishConflict, StorageIntegrityError, StorageNotFound):
                raise
            except StorageValidationError as exc:
                raise StorageIntegrityError(
                    "worker view-result replay failed validation after an "
                    "unknown write outcome"
                ) from exc
            except Exception:
                raise first_failure
        event = _attest_event(
            value,
            authority,
            event_key=arguments["event_key"],
            kind=IndexJobEventKind.VIEW_RESULT,
            view_type=result.request.view_type,
            effective_mode=result.effective_mode,
            outcome=result.outcome,
            payload_json=result.payload_json,
        )
        return control.observe_worker_event(event, prepared_event)

    def _bind_claimed_attempt(
        self,
        catalog: JobWorkerCatalog,
        job_id: str,
        owner_id: str,
        lease_value: object,
    ) -> tuple[
        IndexJobRecord,
        tuple[IndexJobViewRecord, ...],
        IndexJobAttemptRecord,
        RefJobLease,
        _AttemptAuthority,
    ]:
        try:
            lease = _detach_job_lease(lease_value)
        except StorageValidationError as exc:
            raise StorageIntegrityError(
                "worker claim returned an invalid lease"
            ) from exc
        if lease.job_id != job_id or lease.owner_id != owner_id:
            raise StorageIntegrityError(
                "worker claim returned different job or owner authority"
            )
        try:
            job = _detach_job_record(catalog.get_job(job_id))
            raw_views = catalog.get_job_views(job_id)
            if type(raw_views) is not tuple:
                raise StorageValidationError(
                    "worker claim views must be an exact tuple"
                )
            expected_views = _canonical_job_views(job)
            if len(raw_views) != len(expected_views):
                raise StorageValidationError(
                    "worker claim returned a differently sized view set"
                )
            views = tuple(_detach_job_view(view) for view in raw_views)
        except (StorageNotFound, StorageValidationError) as exc:
            raise StorageIntegrityError(
                "worker claim returned invalid job data"
            ) from exc
        if lease.repository_id != job.repository_id or lease.ref_name != job.ref_name:
            raise StorageIntegrityError(
                "worker claim returned different repository or ref authority"
            )
        if job.attempt_count < 1:
            raise StorageIntegrityError("worker claim did not produce an attempted job")
        try:
            attempt = _detach_job_attempt(
                catalog.get_job_attempt(job.job_id, job.attempt_count)
            )
        except (StorageNotFound, StorageValidationError) as exc:
            raise StorageIntegrityError(
                "worker claim returned an invalid attempt"
            ) from exc
        current_authority = _attempt_authority(job, attempt)
        current_matches_claim = (
            job.status is IndexJobStatus.RUNNING
            and current_authority.attempt_count == job.attempt_count
            and current_authority.owner_id == owner_id
            and current_authority.fencing_token == lease.fencing_token
            and current_authority.started_at_ms == lease.acquired_at_ms
        )
        if not current_matches_claim:
            raise _ClaimAuthorityLost(
                self._superseded_claim_authority(
                    catalog,
                    job,
                    attempt,
                    lease,
                )
            )
        attempts, completions = self._attest_visible_claim_history(
            catalog,
            job,
        )
        if (
            not attempts
            or attempts[-1] != attempt
            or completions.get(attempt.attempt_count) is not None
        ):
            raise _ClaimAuthorityLost(
                self._superseded_claim_authority(
                    catalog,
                    job,
                    attempt,
                    lease,
                )
            )
        authority = current_authority
        try:
            context = IndexJobExecutionContext(
                job=job,
                views=views,
                attempt=attempt,
                lease=lease,
                control=_ValidationControl(),
            )
        except StorageValidationError as exc:
            raise StorageIntegrityError(
                "worker claim authority failed attestation"
            ) from exc
        return context.job, context.views, context.attempt, context.lease, authority

    @staticmethod
    def _attest_visible_claim_history(
        catalog: JobWorkerCatalog,
        job: IndexJobRecord,
    ) -> tuple[
        tuple[IndexJobAttemptRecord, ...],
        dict[int, IndexJobAttemptCompletionRecord],
    ]:
        try:
            attempts: tuple[IndexJobAttemptRecord, ...] = ()
            completion_values: tuple[IndexJobAttemptCompletionRecord, ...] = ()
            for _ in range(job.max_attempts + 1):
                raw_attempts = catalog.list_job_attempts(job.job_id)
                raw_completions = catalog.list_job_attempt_completions(job.job_id)
                if type(raw_attempts) is not tuple:
                    raise StorageValidationError(
                        "worker attempt history must be an exact tuple"
                    )
                if type(raw_completions) is not tuple:
                    raise StorageValidationError(
                        "worker completion history must be an exact tuple"
                    )
                if not 1 <= len(raw_attempts) <= _MAX_JOB_ATTEMPTS:
                    raise StorageValidationError(
                        "worker attempt history is outside its bounded size"
                    )
                if len(raw_completions) > _MAX_JOB_ATTEMPTS:
                    raise StorageValidationError(
                        "worker completion history is outside its bounded size"
                    )
                refreshed_attempts = tuple(
                    _detach_job_attempt(value) for value in raw_attempts
                )
                refreshed_completions = tuple(
                    _detach_attempt_completion(value) for value in raw_completions
                )
                if refreshed_attempts[: len(attempts)] != attempts or (
                    refreshed_completions[: len(completion_values)] != completion_values
                ):
                    raise StorageValidationError(
                        "worker claim history changed an immutable prefix"
                    )
                attempts = refreshed_attempts
                completion_values = refreshed_completions
                completion_tail = max(
                    (value.attempt_count for value in completion_values),
                    default=0,
                )
                if completion_tail <= attempts[-1].attempt_count:
                    break
            else:
                raise StorageValidationError(
                    "worker claim history did not converge within its retry bound"
                )
        except (StorageNotFound, StorageValidationError) as exc:
            raise StorageIntegrityError(
                "worker could not authenticate visible claim history"
            ) from exc
        counts = tuple(value.attempt_count for value in attempts)
        contiguous_counts = tuple(range(counts[0], counts[-1] + 1))
        if counts != contiguous_counts:
            raise StorageIntegrityError("worker claim attempt history is not canonical")
        if any(
            value.job_id != job.job_id
            or value.repository_id != job.repository_id
            or value.ref_name != job.ref_name
            or value.request_digest != job.request_digest
            or value.started_at_ms < job.created_at_ms
            or (
                job.started_at_ms is not None
                and value.started_at_ms < job.started_at_ms
            )
            or value.fencing_token < value.attempt_count
            for value in attempts
        ):
            raise StorageIntegrityError(
                "worker claim attempt history belongs to different job data"
            )
        completion_counts = tuple(value.attempt_count for value in completion_values)
        if completion_counts != tuple(sorted(set(completion_counts))) or any(
            value.job_id != job.job_id or value.attempt_count not in counts
            for value in completion_values
        ):
            raise StorageIntegrityError(
                "worker claim completion history is not canonical"
            )
        completions = {value.attempt_count: value for value in completion_values}
        for index, historical_attempt in enumerate(attempts[:-1]):
            completion = completions.get(historical_attempt.attempt_count)
            if (
                completion is None
                or completion.owner_id != historical_attempt.owner_id
                or completion.fencing_token != historical_attempt.fencing_token
                or completion.outcome is not IndexJobCompletion.REQUEUE
                or completion.completed_at_ms < historical_attempt.started_at_ms
            ):
                raise StorageIntegrityError(
                    "worker superseded claim has a conflicting attempt closure"
                )
            successor = attempts[index + 1]
            if (
                successor.started_at_ms < completion.completed_at_ms
                or successor.fencing_token <= historical_attempt.fencing_token
            ):
                raise StorageIntegrityError(
                    "worker superseded claim has invalid attempt adjacency"
                )
        expected_completion_counts = set(counts[:-1])
        if attempts[-1].attempt_count in completions:
            expected_completion_counts.add(attempts[-1].attempt_count)
        if set(completions) != expected_completion_counts:
            raise StorageIntegrityError(
                "worker claim completion history contains an impossible closure"
            )
        return attempts, completions

    @staticmethod
    def _superseded_claim_authority(
        catalog: JobWorkerCatalog,
        job: IndexJobRecord,
        current_attempt: IndexJobAttemptRecord,
        lease: RefJobLease,
    ) -> _AttemptAuthority:
        attempts, completions = IndexJobWorker._attest_visible_claim_history(
            catalog,
            job,
        )
        if (
            not attempts
            or current_attempt not in attempts
            or current_attempt.attempt_count != job.attempt_count
        ):
            raise StorageIntegrityError(
                "worker claim current attempt is absent from its history"
            )
        if job.updated_at_ms < current_attempt.started_at_ms or (
            job.finished_at_ms is not None
            and job.finished_at_ms < current_attempt.started_at_ms
        ):
            raise StorageIntegrityError(
                "worker claim current job time precedes its modeled attempt"
            )
        matches = tuple(
            value
            for value in attempts
            if value.job_id == job.job_id
            and value.repository_id == job.repository_id
            and value.ref_name == job.ref_name
            and value.request_digest == job.request_digest
            and value.owner_id == lease.owner_id
            and value.fencing_token == lease.fencing_token
            and value.started_at_ms == lease.acquired_at_ms
        )
        if len(matches) != 1:
            raise StorageIntegrityError(
                "worker claim lease lacks one exact attempt authority"
            )
        claimed_attempt = matches[0]
        latest_attempt_count = attempts[-1].attempt_count
        if latest_attempt_count > job.max_attempts:
            raise StorageIntegrityError(
                "worker claim attempt history exceeds the job retry bound"
            )
        if latest_attempt_count > job.attempt_count and job.status not in {
            IndexJobStatus.QUEUED,
            IndexJobStatus.RUNNING,
        }:
            raise StorageIntegrityError(
                "worker claim terminal job has an impossible successor attempt"
            )
        superseded = latest_attempt_count > claimed_attempt.attempt_count
        tail_attempt = attempts[-1]
        tail_completion = completions.get(tail_attempt.attempt_count)
        closed_same_attempt = (
            latest_attempt_count == claimed_attempt.attempt_count
            and job.status is not IndexJobStatus.RUNNING
            and current_attempt == claimed_attempt
        )
        closed_after_running_snapshot = (
            latest_attempt_count == claimed_attempt.attempt_count
            and job.status is IndexJobStatus.RUNNING
            and current_attempt == claimed_attempt
            and tail_completion is not None
        )
        if (
            not superseded
            and not closed_same_attempt
            and not (closed_after_running_snapshot)
        ):
            raise StorageIntegrityError(
                "worker claim returned inconsistent current authority"
            )
        if job.status is IndexJobStatus.SUCCEEDED:
            if job.cancel_requested or tail_completion is not None:
                raise StorageIntegrityError(
                    "worker closed claim returned an invalid successful job"
                )
            _attest_successful_job_causal_time(
                job,
                _attempt_authority(job, current_attempt),
                causal_floor_ms=current_attempt.started_at_ms,
            )
        elif latest_attempt_count == job.attempt_count and job.status is not (
            IndexJobStatus.RUNNING
        ):
            if tail_completion is None:
                raise StorageIntegrityError(
                    "worker closed claim lacks its exact tail closure"
                )
            if (
                tail_completion.job_id != job.job_id
                or tail_completion.attempt_count != tail_attempt.attempt_count
                or tail_completion.owner_id != tail_attempt.owner_id
                or tail_completion.fencing_token != tail_attempt.fencing_token
                or tail_completion.completed_at_ms < tail_attempt.started_at_ms
                or not _job_matches_attempt_completion(job, tail_completion)
            ):
                raise StorageIntegrityError(
                    "worker closed claim has a conflicting tail closure"
                )
        elif tail_completion is not None:
            if (
                tail_completion.job_id != job.job_id
                or tail_completion.attempt_count != tail_attempt.attempt_count
                or tail_completion.owner_id != tail_attempt.owner_id
                or tail_completion.fencing_token != tail_attempt.fencing_token
                or tail_completion.completed_at_ms < tail_attempt.started_at_ms
            ):
                raise StorageIntegrityError(
                    "worker running claim has a conflicting tail closure"
                )
            refreshed = _attest_job_identity(
                catalog.get_job(job.job_id),
                _attempt_authority(job, tail_attempt),
            )
            if refreshed.attempt_count == tail_attempt.attempt_count:
                if not _job_matches_attempt_completion(refreshed, tail_completion):
                    raise StorageIntegrityError(
                        "worker running claim closed into a conflicting state"
                    )
            elif (
                refreshed.attempt_count <= tail_attempt.attempt_count
                or tail_completion.outcome is not IndexJobCompletion.REQUEUE
                or refreshed.status
                not in {IndexJobStatus.QUEUED, IndexJobStatus.RUNNING}
                or refreshed.updated_at_ms < tail_completion.completed_at_ms
            ):
                raise StorageIntegrityError(
                    "worker running claim advanced through a conflicting closure"
                )
        return _attempt_authority(job, claimed_attempt)

    def _final_heartbeat(
        self,
        catalog: JobWorkerCatalog,
        authority: _AttemptAuthority,
        causal_evidence: _AttemptCausalEvidence,
    ) -> IndexJobAttemptHeartbeat | None:
        try:
            heartbeat = _attest_heartbeat(
                catalog.heartbeat_job_attempt(
                    authority.job_id,
                    attempt_count=authority.attempt_count,
                    owner_id=authority.owner_id,
                    fencing_token=authority.fencing_token,
                    lease_duration_ms=self._lease_duration_ms,
                ),
                authority,
            )
            causal_evidence.observe_heartbeat(heartbeat.lease)
            return heartbeat
        except (PublishConflict, StorageNotFound):
            return None

    def _complete_failure(
        self,
        catalog: JobWorkerCatalog,
        job: IndexJobRecord,
        authority: _AttemptAuthority,
        *,
        retryable: bool,
        error_code: str,
        error_message: str | None,
        causal_evidence: _AttemptCausalEvidence,
    ) -> IndexJobWorkerRunResult:
        outcome = (
            IndexJobCompletion.REQUEUE
            if retryable and authority.attempt_count < job.max_attempts
            else IndexJobCompletion.FAILED
        )
        return self._complete_attempt(
            catalog,
            authority,
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
            causal_evidence=causal_evidence,
        )

    def _complete_attempt(
        self,
        catalog: JobWorkerCatalog,
        authority: _AttemptAuthority,
        *,
        outcome: IndexJobCompletion,
        error_code: str | None,
        error_message: str | None,
        causal_evidence: _AttemptCausalEvidence,
    ) -> IndexJobWorkerRunResult:
        causal_floor_ms = causal_evidence.causal_floor_ms
        try:
            completed = catalog.complete_job_attempt(
                authority.job_id,
                attempt_count=authority.attempt_count,
                owner_id=authority.owner_id,
                fencing_token=authority.fencing_token,
                outcome=outcome,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception as exc:
            if isinstance(exc, StorageIntegrityError):
                raise
            expected_code = (
                "cancelled"
                if outcome is IndexJobCompletion.CANCELLED and error_code is None
                else error_code
            )
            if isinstance(exc, PublishConflict):
                terminal = self._reconcile_attempt(
                    catalog,
                    authority,
                    causal_floor_ms=causal_floor_ms,
                )
            else:
                terminal = self._reconcile_attempt(
                    catalog,
                    authority,
                    causal_floor_ms=causal_floor_ms,
                    expected_completion=(outcome, expected_code, error_message),
                )
            if terminal is not None:
                return terminal
            if isinstance(exc, PublishConflict):
                job = _attest_job_identity(
                    catalog.get_job(authority.job_id),
                    authority,
                )
                if (
                    type(job) is IndexJobRecord
                    and job.attempt_count == authority.attempt_count
                    and job.status is IndexJobStatus.RUNNING
                    and job.cancel_requested
                    and outcome is not IndexJobCompletion.CANCELLED
                ):
                    return self._complete_attempt(
                        catalog,
                        authority,
                        outcome=IndexJobCompletion.CANCELLED,
                        error_code=None,
                        error_message=None,
                        causal_evidence=causal_evidence,
                    )
                return self._run_result(
                    IndexJobWorkerDisposition.LOST_AUTHORITY,
                    authority,
                )
            raise
        return self._attest_completed_attempt(
            catalog,
            completed,
            authority,
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
            causal_floor_ms=causal_floor_ms,
        )

    @staticmethod
    def _attest_completed_attempt(
        catalog: JobWorkerCatalog,
        completed_value: object,
        authority: _AttemptAuthority,
        *,
        outcome: IndexJobCompletion,
        error_code: str | None,
        error_message: str | None,
        causal_floor_ms: int,
    ) -> IndexJobWorkerRunResult:
        completed = _attest_job_identity(completed_value, authority)
        expected_status = {
            IndexJobCompletion.REQUEUE: IndexJobStatus.QUEUED,
            IndexJobCompletion.FAILED: IndexJobStatus.FAILED,
            IndexJobCompletion.CANCELLED: IndexJobStatus.CANCELLED,
        }[outcome]
        expected_code = (
            "cancelled" if outcome is IndexJobCompletion.CANCELLED else error_code
        )
        expected_job_code = expected_code
        expected_job_message = error_message
        if (
            completed.attempt_count != authority.attempt_count
            or completed.status is not expected_status
            or completed.error_code != expected_job_code
            or completed.error_message != expected_job_message
        ):
            raise StorageIntegrityError(
                "worker completion returned a different job closure"
            )
        try:
            closure = _detach_attempt_completion(
                catalog.get_job_attempt_completion(
                    authority.job_id,
                    authority.attempt_count,
                )
            )
        except (StorageNotFound, StorageValidationError) as exc:
            raise StorageIntegrityError(
                "worker completion did not persist an exact attempt closure"
            ) from exc
        if (
            closure.job_id != authority.job_id
            or closure.attempt_count != authority.attempt_count
            or closure.owner_id != authority.owner_id
            or closure.fencing_token != authority.fencing_token
            or closure.outcome is not outcome
            or closure.error_code != expected_code
            or closure.error_message != error_message
            or closure.completed_at_ms < causal_floor_ms
        ):
            raise StorageIntegrityError(
                "worker completion persisted a different attempt closure"
            )
        if not _job_matches_attempt_completion(completed, closure):
            raise StorageIntegrityError(
                "worker completion returned a job inconsistent with its closure"
            )
        disposition = {
            IndexJobCompletion.REQUEUE: IndexJobWorkerDisposition.REQUEUED,
            IndexJobCompletion.FAILED: IndexJobWorkerDisposition.FAILED,
            IndexJobCompletion.CANCELLED: IndexJobWorkerDisposition.CANCELLED,
        }[outcome]
        return IndexJobWorker._run_result(disposition, authority)

    def _publish_result(
        self,
        catalog: JobWorkerCatalog,
        job: IndexJobRecord,
        authority: _AttemptAuthority,
        result: IndexJobExecutionResult,
        causal_evidence: _AttemptCausalEvidence,
        *,
        before_catalog_publish: Callable[[], None] | None = None,
    ) -> IndexJobWorkerRunResult:
        _artifacts, expected_outputs, _receipts = _preflight_job_artifacts(
            result.artifacts
        )
        pre_catalog_failure: BaseException | None = None

        def run_before_catalog_publish() -> None:
            nonlocal pre_catalog_failure
            try:
                assert before_catalog_publish is not None
                before_catalog_publish()
            except BaseException as exc:  # noqa: B036 - exact rethrow below
                pre_catalog_failure = exc
                raise

        def reconcile_conflict() -> IndexJobWorkerRunResult | None:
            causal_floor_ms = causal_evidence.causal_floor_ms
            return self._reconcile_attempt(
                catalog,
                authority,
                causal_floor_ms=causal_floor_ms,
                expected_publication_outputs=expected_outputs,
                allow_competing_non_success=True,
            )

        try:
            completed = publish_job_artifacts(
                authority.job_id,
                catalog=catalog,
                object_store=self._object_store,
                owner_id=authority.owner_id,
                fencing_token=authority.fencing_token,
                outputs=result.artifacts,
                _retention_cleanup_as_integrity=True,
                _before_catalog_publish=(
                    run_before_catalog_publish
                    if before_catalog_publish is not None
                    else None
                ),
            )
        except _PublicationCancelRequested:
            return self._complete_attempt(
                catalog,
                authority,
                outcome=IndexJobCompletion.CANCELLED,
                error_code=None,
                error_message=None,
                causal_evidence=causal_evidence,
            )
        except _PublicationAuthorityLost:
            terminal = reconcile_conflict()
            return terminal or self._run_result(
                IndexJobWorkerDisposition.LOST_AUTHORITY,
                authority,
            )
        except Exception as exc:
            if exc is pre_catalog_failure:
                raise
            if isinstance(exc, StorageIntegrityError):
                raise
            causal_floor_ms = causal_evidence.causal_floor_ms
            if isinstance(exc, PublishConflict):
                terminal = reconcile_conflict()
            else:
                terminal = self._reconcile_attempt(
                    catalog,
                    authority,
                    causal_floor_ms=causal_floor_ms,
                    expected_publication_outputs=expected_outputs,
                )
            if terminal is not None:
                return terminal
            if not isinstance(exc, PublishConflict):
                raise
            heartbeat = self._final_heartbeat(
                catalog,
                authority,
                causal_evidence,
            )
            if heartbeat is None:
                terminal = reconcile_conflict()
                return terminal or self._run_result(
                    IndexJobWorkerDisposition.LOST_AUTHORITY,
                    authority,
                )
            if heartbeat.cancel_requested:
                return self._complete_attempt(
                    catalog,
                    authority,
                    outcome=IndexJobCompletion.CANCELLED,
                    error_code=None,
                    error_message=None,
                    causal_evidence=causal_evidence,
                )
            return self._complete_failure(
                catalog,
                job,
                authority,
                retryable=False,
                error_code="worker_publication_conflict",
                error_message=None,
                causal_evidence=causal_evidence,
            )
        causal_floor_ms = causal_evidence.causal_floor_ms
        completed = _attest_job_identity(completed, authority)
        _attest_completed_publication(
            completed,
            job_id=authority.job_id,
            outputs=expected_outputs,
        )
        if (
            completed.attempt_count != authority.attempt_count
            or completed.status is not IndexJobStatus.SUCCEEDED
            or completed.cancel_requested
        ):
            raise StorageIntegrityError(
                "worker publication returned an invalid successful job"
            )
        _attest_successful_job_causal_time(
            completed,
            authority,
            causal_floor_ms=causal_floor_ms,
        )
        return self._run_result(IndexJobWorkerDisposition.SUCCEEDED, authority)

    def _reconcile_attempt(
        self,
        catalog: JobWorkerCatalog,
        authority: _AttemptAuthority,
        *,
        causal_floor_ms: int,
        expected_completion: (
            tuple[
                IndexJobCompletion,
                str | None,
                str | None,
            ]
            | None
        ) = None,
        expected_publication_outputs: tuple[IndexJobViewOutput, ...] | None = None,
        allow_competing_non_success: bool = False,
    ) -> IndexJobWorkerRunResult | None:
        def read_completion() -> IndexJobAttemptCompletionRecord | None:
            try:
                value = catalog.get_job_attempt_completion(
                    authority.job_id,
                    authority.attempt_count,
                )
            except StorageNotFound:
                return None
            try:
                detached = _detach_attempt_completion(value)
            except StorageValidationError as exc:
                raise StorageIntegrityError(
                    "worker reconciliation returned a non-exact completion"
                ) from exc
            if (
                detached.job_id != authority.job_id
                or detached.attempt_count != authority.attempt_count
                or detached.owner_id != authority.owner_id
                or detached.fencing_token != authority.fencing_token
                or detached.completed_at_ms < causal_floor_ms
            ):
                raise StorageIntegrityError(
                    "worker reconciliation returned different completion authority"
                )
            return detached

        completion = read_completion()
        try:
            job_value = catalog.get_job(authority.job_id)
        except StorageNotFound as exc:
            if completion is not None:
                raise StorageIntegrityError(
                    "worker reconciliation found a completion without its job"
                ) from exc
            return self._run_result(
                IndexJobWorkerDisposition.LOST_AUTHORITY,
                authority,
            )
        job = _attest_job_identity(job_value, authority)
        if completion is None and (
            job.attempt_count > authority.attempt_count
            or job.status not in {IndexJobStatus.RUNNING, IndexJobStatus.SUCCEEDED}
        ):
            completion = read_completion()
        if completion is not None:
            if expected_completion is not None and (
                completion.outcome is not expected_completion[0]
                or completion.error_code != expected_completion[1]
                or completion.error_message != expected_completion[2]
            ):
                raise StorageIntegrityError(
                    "worker completion response loss persisted a different closure"
                )
            if (
                expected_publication_outputs is not None
                and not allow_competing_non_success
            ):
                raise StorageIntegrityError(
                    "worker publication response loss persisted a non-success closure"
                )
            if job.attempt_count > authority.attempt_count and (
                completion.outcome is not IndexJobCompletion.REQUEUE
                or job.updated_at_ms < completion.completed_at_ms
            ):
                raise StorageIntegrityError(
                    "worker reconciliation found an impossible successor attempt"
                )
            if job.attempt_count > authority.attempt_count:
                attempts, history_completions = self._attest_visible_claim_history(
                    catalog, job
                )
                matching_attempts = tuple(
                    attempt
                    for attempt in attempts
                    if attempt.job_id == authority.job_id
                    and attempt.repository_id == authority.repository_id
                    and attempt.ref_name == authority.ref_name
                    and attempt.request_digest == authority.request_digest
                    and attempt.attempt_count == authority.attempt_count
                    and attempt.started_at_ms == authority.started_at_ms
                    and attempt.owner_id == authority.owner_id
                    and attempt.fencing_token == authority.fencing_token
                )
                if (
                    len(matching_attempts) != 1
                    or not any(
                        attempt.attempt_count == job.attempt_count
                        for attempt in attempts
                    )
                    or history_completions.get(authority.attempt_count) != completion
                ):
                    raise StorageIntegrityError(
                        "worker reconciliation found invalid successor history"
                    )
            if (
                job.attempt_count == authority.attempt_count
                and not _job_matches_attempt_completion(job, completion)
            ):
                raise StorageIntegrityError(
                    "worker reconciliation found a job inconsistent with its closure"
                )
            disposition = {
                IndexJobCompletion.REQUEUE: IndexJobWorkerDisposition.REQUEUED,
                IndexJobCompletion.FAILED: IndexJobWorkerDisposition.FAILED,
                IndexJobCompletion.CANCELLED: IndexJobWorkerDisposition.CANCELLED,
            }[completion.outcome]
            return self._run_result(disposition, authority)
        if job.attempt_count > authority.attempt_count or job.status not in {
            IndexJobStatus.RUNNING,
            IndexJobStatus.SUCCEEDED,
        }:
            raise StorageIntegrityError(
                "worker reconciliation found a missing attempt closure"
            )
        if (
            job.attempt_count == authority.attempt_count
            and job.status is IndexJobStatus.SUCCEEDED
        ):
            if expected_completion is not None or expected_publication_outputs is None:
                raise StorageIntegrityError(
                    "worker non-publication reconciliation persisted a successful "
                    "closure"
                )
            if job.cancel_requested:
                raise StorageIntegrityError(
                    "worker reconciliation returned a cancelled successful job"
                )
            _attest_completed_publication(
                job,
                job_id=authority.job_id,
                outputs=expected_publication_outputs,
            )
            _attest_successful_job_causal_time(
                job,
                authority,
                causal_floor_ms=causal_floor_ms,
            )
            return self._run_result(
                IndexJobWorkerDisposition.SUCCEEDED,
                authority,
            )
        return None

    @staticmethod
    def _run_result(
        disposition: IndexJobWorkerDisposition,
        authority: _AttemptAuthority,
    ) -> IndexJobWorkerRunResult:
        return IndexJobWorkerRunResult(
            disposition=disposition,
            job_id=authority.job_id,
            attempt_count=authority.attempt_count,
        )


class _ValidationToken:
    @property
    def reason(self) -> IndexJobStopReason | None:
        return None

    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> bool:
        return False


class _ValidationControl:
    @property
    def stop_token(self) -> IndexJobStopToken:
        return _ValidationToken()

    def append_progress(
        self,
        event_key: str,
        payload: Mapping[str, Any] | None = None,
        view_type: str | None = None,
    ) -> IndexJobEventRecord:
        raise AssertionError("validation control cannot append progress")


__all__ = [
    "IndexJobCatalogSessionFactory",
    "IndexJobExecutionContext",
    "IndexJobExecutionControl",
    "IndexJobExecutionResult",
    "IndexJobExecutor",
    "IndexJobExecutorResolver",
    "IndexJobObjectStoreBoundResolver",
    "IndexJobStopReason",
    "IndexJobStopToken",
    "IndexJobViewExecutionResult",
    "IndexJobWorkerDisposition",
    "IndexJobWorkerRunResult",
    "IndexJobWorker",
]
