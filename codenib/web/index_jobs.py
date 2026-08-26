# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Least-authority durable index-job reads for the Web control plane."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from ..storage import (
    IndexJobEventRecord,
    IndexJobRecord,
    IndexJobStatus,
    IndexJobViewRecord,
    JobQueryCatalog,
    StorageError,
    StorageNotFound,
)
from .schemas import (
    IndexJobEvent,
    IndexJobStatusResponse,
    IndexJobSurface,
    RepoIndexStatus,
)

_PRIMARY_INDEX_ORDER = {"bm25": 0, "vector": 1, "symbol_graph": 2}
_DEFAULT_EVENT_LIMIT = 64
_MAX_EVENT_LIMIT = 64
_FAILED_MESSAGES = {
    "worker_resolver_failed": "The configured index worker could not accept this job.",
    "worker_executor_failed": "The index worker failed while preparing artifacts.",
    "worker_executor_incomplete": "The index worker returned an incomplete result.",
    "worker_publication_conflict": "The indexed ref changed before publication completed.",
}
_GENERIC_FAILURE = "The index update failed. Use the job ID to inspect server logs."


class IndexJobNotFoundError(LookupError):
    """The requested Web repository or authorized durable job is absent."""


class IndexJobReadError(RuntimeError):
    """Durable job state could not be read or safely projected."""


def _canonical_text(value: object, *, label: str, max_length: int) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be exact text")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or "\x00" in normalized:
        raise ValueError(f"{label} is invalid")
    if normalized != value:
        raise ValueError(f"{label} must be canonical")
    return normalized


@dataclass(frozen=True, slots=True)
class IndexJobRepoBinding:
    """Authorize one Web repository to read one durable repository/ref."""

    repo_id: str
    repository_id: str
    ref_name: str = "main"

    def __post_init__(self) -> None:
        if type(self) is not IndexJobRepoBinding:
            raise TypeError("index job repository binding must use the exact type")
        object.__setattr__(
            self,
            "repo_id",
            _canonical_text(self.repo_id, label="Web repository ID", max_length=512),
        )
        object.__setattr__(
            self,
            "repository_id",
            _canonical_text(
                self.repository_id,
                label="storage repository ID",
                max_length=96,
            ),
        )
        object.__setattr__(
            self,
            "ref_name",
            _canonical_text(self.ref_name, label="storage ref name", max_length=512),
        )


@runtime_checkable
class IndexJobReader(Protocol):
    """Small Web-facing read contract suitable for dependency injection."""

    def active(self, repo_id: str) -> IndexJobStatusResponse | None: ...

    def get(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        event_limit: int = _DEFAULT_EVENT_LIMIT,
    ) -> IndexJobStatusResponse: ...


def _event_bounds(after_sequence: int, event_limit: int) -> tuple[int, int]:
    if type(after_sequence) is not int or not 0 <= after_sequence < 2**63:
        raise ValueError("index job event cursor is invalid")
    if type(event_limit) is not int or not 0 <= event_limit <= _MAX_EVENT_LIMIT:
        raise ValueError("index job event limit is invalid")
    return after_sequence, event_limit


def _safe_error_message(job: IndexJobRecord) -> str | None:
    if job.status is IndexJobStatus.FAILED:
        return _FAILED_MESSAGES.get(job.error_code or "", _GENERIC_FAILURE)
    if job.status is IndexJobStatus.CANCELLED:
        return "The index update was cancelled."
    if job.status is IndexJobStatus.QUEUED and job.attempt_count > 0 and job.error_code:
        return "A previous attempt failed; the index update is queued for retry."
    return None


def _safe_error_code(job: IndexJobRecord) -> str | None:
    code = job.error_code
    if code is None or code in _FAILED_MESSAGES:
        return code
    if job.status is IndexJobStatus.CANCELLED:
        return "cancelled"
    if job.status is IndexJobStatus.QUEUED:
        return "index_update_retry"
    return "index_update_failed"


def _surface(view: IndexJobViewRecord) -> IndexJobSurface:
    if type(view) is not IndexJobViewRecord:
        raise IndexJobReadError("catalog returned an invalid index job view")
    if view.view_type not in _PRIMARY_INDEX_ORDER:
        raise IndexJobReadError("catalog job contains a non-Web index surface")
    return IndexJobSurface(
        index_type=view.view_type,
        requested_mode=view.requested_mode.value,
        required=view.required,
    )


def _event(event: IndexJobEventRecord) -> IndexJobEvent:
    if type(event) is not IndexJobEventRecord:
        raise IndexJobReadError("catalog returned an invalid index job event")
    if event.view_type is not None and event.view_type not in _PRIMARY_INDEX_ORDER:
        raise IndexJobReadError("catalog event contains a non-Web index surface")
    return IndexJobEvent(
        sequence=event.sequence,
        attempt_count=event.attempt_count,
        event_key=event.event_key,
        kind=event.kind.value,
        index_type=event.view_type,
        effective_mode=(
            None if event.effective_mode is None else event.effective_mode.value
        ),
        outcome=None if event.outcome is None else event.outcome.value,
        payload=event.payload,
        created_at_ms=event.created_at_ms,
    )


def _project_job(
    catalog: JobQueryCatalog,
    binding: IndexJobRepoBinding,
    job: IndexJobRecord,
    *,
    after_sequence: int,
    event_limit: int,
) -> IndexJobStatusResponse:
    if type(job) is not IndexJobRecord:
        raise IndexJobReadError("catalog returned an invalid index job")
    if (job.repository_id, job.ref_name) != (
        binding.repository_id,
        binding.ref_name,
    ):
        raise IndexJobReadError("catalog job escaped its Web repository binding")
    views = catalog.get_job_views(job.job_id)
    if type(views) is not tuple or any(view.job_id != job.job_id for view in views):
        raise IndexJobReadError("catalog returned incoherent index job views")
    surfaces = sorted(
        (_surface(view) for view in views),
        key=lambda item: _PRIMARY_INDEX_ORDER[item.index_type],
    )
    if not surfaces or len(surfaces) > len(_PRIMARY_INDEX_ORDER):
        raise IndexJobReadError("catalog returned an invalid Web index job shape")

    raw_events = (
        ()
        if event_limit == 0
        else catalog.list_job_events(
            job.job_id,
            after_sequence=after_sequence,
            limit=event_limit,
        )
    )
    if type(raw_events) is not tuple:
        raise IndexJobReadError("catalog returned an invalid index job event page")
    events = [_event(event) for event in raw_events]
    sequences = [event.sequence for event in events]
    if sequences != sorted(set(sequences)) or any(
        sequence <= after_sequence for sequence in sequences
    ):
        raise IndexJobReadError("catalog returned an incoherent index job event page")
    next_sequence = sequences[-1] if sequences else after_sequence
    return IndexJobStatusResponse(
        job_id=job.job_id,
        repo_id=binding.repo_id,
        status=job.status.value,
        cancel_requested=job.cancel_requested,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        indexes=surfaces,
        result_snapshot_id=job.result_snapshot_id,
        error_code=_safe_error_code(job),
        error_message=_safe_error_message(job),
        created_at_ms=job.created_at_ms,
        updated_at_ms=job.updated_at_ms,
        started_at_ms=job.started_at_ms,
        finished_at_ms=job.finished_at_ms,
        events=events,
        next_event_sequence=next_sequence,
    )


def _safe_project_job(
    catalog: JobQueryCatalog,
    binding: IndexJobRepoBinding,
    job: IndexJobRecord,
    *,
    after_sequence: int,
    event_limit: int,
) -> IndexJobStatusResponse:
    try:
        return _project_job(
            catalog,
            binding,
            job,
            after_sequence=after_sequence,
            event_limit=event_limit,
        )
    except IndexJobReadError:
        raise
    except (TypeError, ValueError) as exc:
        raise IndexJobReadError("catalog job could not be safely projected") from exc


class CatalogIndexJobReader:
    """Open short, thread-confined read sessions for durable Web job status."""

    def __init__(
        self,
        catalog_factory: Callable[[], AbstractContextManager[JobQueryCatalog]],
        bindings: tuple[IndexJobRepoBinding, ...],
    ) -> None:
        if not callable(catalog_factory):
            raise TypeError("index job catalog factory must be callable")
        if type(bindings) is not tuple or not bindings:
            raise ValueError("index job reader requires repository bindings")
        if any(type(binding) is not IndexJobRepoBinding for binding in bindings):
            raise TypeError("index job reader bindings must use exact values")
        by_repo: dict[str, IndexJobRepoBinding] = {}
        by_storage: dict[tuple[str, str], IndexJobRepoBinding] = {}
        for binding in bindings:
            storage_key = (binding.repository_id, binding.ref_name)
            if binding.repo_id in by_repo or storage_key in by_storage:
                raise ValueError("index job reader bindings must be unique")
            by_repo[binding.repo_id] = binding
            by_storage[storage_key] = binding
        self._catalog_factory = catalog_factory
        self._by_repo = by_repo
        self._by_storage = by_storage

    @staticmethod
    def _require_catalog(value: object) -> JobQueryCatalog:
        if not isinstance(value, JobQueryCatalog):
            raise IndexJobReadError("catalog does not implement read-only job queries")
        return value

    def active(self, repo_id: str) -> IndexJobStatusResponse | None:
        normalized = _canonical_text(
            repo_id,
            label="Web repository ID",
            max_length=512,
        )
        binding = self._by_repo.get(normalized)
        if binding is None:
            raise IndexJobNotFoundError("Web repository has no durable job binding")
        try:
            with self._catalog_factory() as value:
                catalog = self._require_catalog(value)
                job = catalog.find_active_job(
                    binding.repository_id,
                    binding.ref_name,
                )
                if job is None:
                    return None
                return _safe_project_job(
                    catalog,
                    binding,
                    job,
                    after_sequence=0,
                    event_limit=0,
                )
        except IndexJobReadError:
            raise
        except (OSError, sqlite3.Error, StorageError) as exc:
            raise IndexJobReadError("durable index job status is unavailable") from exc

    def get(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        event_limit: int = _DEFAULT_EVENT_LIMIT,
    ) -> IndexJobStatusResponse:
        normalized = _canonical_text(job_id, label="index job ID", max_length=80)
        cursor, limit = _event_bounds(after_sequence, event_limit)
        try:
            with self._catalog_factory() as value:
                catalog = self._require_catalog(value)
                try:
                    job = catalog.get_job(normalized)
                except StorageNotFound as exc:
                    raise IndexJobNotFoundError(
                        "durable index job was not found"
                    ) from exc
                if type(job) is not IndexJobRecord:
                    raise IndexJobReadError("catalog returned an invalid index job")
                binding = self._by_storage.get((job.repository_id, job.ref_name))
                if binding is None:
                    raise IndexJobNotFoundError(
                        "durable index job is outside the Web repository bindings"
                    )
                return _safe_project_job(
                    catalog,
                    binding,
                    job,
                    after_sequence=cursor,
                    event_limit=limit,
                )
        except (IndexJobNotFoundError, IndexJobReadError):
            raise
        except (OSError, sqlite3.Error, StorageError) as exc:
            raise IndexJobReadError("durable index job status is unavailable") from exc


def overlay_active_job(
    status: RepoIndexStatus,
    active_job: IndexJobStatusResponse | None,
) -> RepoIndexStatus:
    """Mark requested primary surfaces updating without mutating the snapshot."""

    if active_job is None:
        return status
    if active_job.repo_id != status.repo_id:
        raise ValueError("active index job belongs to another Web repository")
    if active_job.status not in {"queued", "running"}:
        return status
    requested = {surface.index_type for surface in active_job.indexes}
    indexes = [
        (
            index.model_copy(
                update={"state": "updating", "job_id": active_job.job_id},
                deep=True,
            )
            if index.index_type in requested
            else index.model_copy(deep=True)
        )
        for index in status.indexes
    ]
    return RepoIndexStatus.model_validate(
        {**status.model_dump(), "indexes": [index.model_dump() for index in indexes]}
    )


__all__ = [
    "CatalogIndexJobReader",
    "IndexJobNotFoundError",
    "IndexJobReadError",
    "IndexJobReader",
    "IndexJobRepoBinding",
    "overlay_active_job",
]
