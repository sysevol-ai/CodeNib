# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import codenib.web.app as web_app
from codenib.storage import (
    INDEX_JOB_REQUEST_CONTRACT,
    IndexJobEffectiveMode,
    IndexJobEventKind,
    IndexJobEventRecord,
    IndexJobRecord,
    IndexJobRequest,
    IndexJobStatus,
    IndexJobViewOutcome,
    StorageNotFound,
)
from codenib.web.index_jobs import (
    CatalogIndexJobReader,
    IndexJobNotFoundError,
    IndexJobReadError,
    IndexJobRepoBinding,
    overlay_active_job,
)
from codenib.web.schemas import (
    IndexJobStatusResponse,
    IndexJobSurface,
    IndexSurfaceStatus,
    RepoIndexStatus,
)

_STORAGE_REPO = "repo_" + "a" * 64
_OTHER_STORAGE_REPO = "repo_" + "b" * 64
_SOURCE = "src_" + "c" * 64
_PROFILE = "profile_" + "d" * 64
_INDEXED = "e" * 40


def _request(
    *,
    repository_id: str = _STORAGE_REPO,
    idempotency_key: str = "request",
) -> IndexJobRequest:
    return IndexJobRequest.create(
        repository_id,
        _SOURCE,
        idempotency_key,
        {
            "contract": INDEX_JOB_REQUEST_CONTRACT,
            "views": {
                "bm25": {
                    "profile_id": _PROFILE,
                    "requested_mode": "full",
                    "required": True,
                }
            },
        },
    )


def _job(
    *,
    status: IndexJobStatus = IndexJobStatus.RUNNING,
    repository_id: str = _STORAGE_REPO,
    idempotency_key: str = "request",
    error_code: str | None = None,
    error_message: str | None = None,
) -> IndexJobRecord:
    request = _request(
        repository_id=repository_id,
        idempotency_key=idempotency_key,
    )
    running = status is IndexJobStatus.RUNNING
    terminal = status in {
        IndexJobStatus.SUCCEEDED,
        IndexJobStatus.FAILED,
        IndexJobStatus.CANCELLED,
    }
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
        status=status,
        cancel_requested=status is IndexJobStatus.CANCELLED,
        attempt_count=0 if status is IndexJobStatus.QUEUED else 1,
        result_snapshot_id=(
            "snapshot_" + "f" * 64 if status is IndexJobStatus.SUCCEEDED else None
        ),
        error_code=error_code,
        error_message=error_message,
        created_at_ms=10,
        updated_at_ms=12 if running or terminal else 10,
        started_at_ms=11 if running or terminal else None,
        finished_at_ms=12 if terminal else None,
    )


class _Catalog:
    def __init__(
        self,
        jobs: tuple[IndexJobRecord, ...],
        events: tuple[IndexJobEventRecord, ...] = (),
        *,
        active_job_id: str | None = None,
    ) -> None:
        self.jobs = {job.job_id: job for job in jobs}
        self.views = {
            job.job_id: _request(
                repository_id=job.repository_id,
                idempotency_key=job.idempotency_key,
            ).view_requests
            for job in jobs
        }
        self.events = events
        self.active_job_id = active_job_id
        self.event_calls: list[tuple[str, int, int]] = []

    def get_job(self, job_id: str) -> IndexJobRecord:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise StorageNotFound("job not found") from exc

    def get_job_views(self, job_id: str):
        return self.views[job_id]

    def find_active_job(self, repository_id: str, ref_name: str = "main"):
        if self.active_job_id is None:
            return None
        job = self.jobs[self.active_job_id]
        assert (job.repository_id, job.ref_name) == (repository_id, ref_name)
        return job

    def list_job_events(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 128,
    ):
        self.event_calls.append((job_id, after_sequence, limit))
        return tuple(
            event
            for event in self.events
            if event.job_id == job_id and event.sequence > after_sequence
        )[:limit]


class _UnattestedEventCatalog(_Catalog):
    def list_job_events(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 128,
    ):
        self.event_calls.append((job_id, after_sequence, limit))
        return self.events


def _reader(catalog: _Catalog) -> CatalogIndexJobReader:
    @contextmanager
    def factory():
        yield catalog

    return CatalogIndexJobReader(
        factory,
        (IndexJobRepoBinding("demo", _STORAGE_REPO),),
    )


def _events(job: IndexJobRecord) -> tuple[IndexJobEventRecord, ...]:
    return (
        IndexJobEventRecord.create(
            sequence=1,
            job_id=job.job_id,
            attempt_count=1,
            event_key="capture-started",
            kind=IndexJobEventKind.PROGRESS,
            owner_id="private-worker",
            fencing_token=7,
            view_type="bm25",
            payload={
                "changed_files": 2,
                "phase": "credential-bearing internal detail",
            },
            created_at_ms=11,
        ),
        IndexJobEventRecord.create(
            sequence=2,
            job_id=job.job_id,
            attempt_count=1,
            event_key="bm25-result",
            kind=IndexJobEventKind.VIEW_RESULT,
            owner_id="private-worker",
            fencing_token=7,
            view_type="bm25",
            effective_mode=IndexJobEffectiveMode.FULL,
            outcome=IndexJobViewOutcome.SUCCEEDED,
            payload={"documents": 8},
            created_at_ms=12,
        ),
    )


def _active_response(job: IndexJobRecord) -> IndexJobStatusResponse:
    return IndexJobStatusResponse(
        job_id=job.job_id,
        repo_id="repo",
        status=job.status.value,
        cancel_requested=job.cancel_requested,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        indexes=[
            IndexJobSurface(
                index_type="bm25",
                requested_mode="full",
                required=True,
            )
        ],
        created_at_ms=job.created_at_ms,
        updated_at_ms=job.updated_at_ms,
        started_at_ms=job.started_at_ms,
        finished_at_ms=job.finished_at_ms,
        next_event_sequence=0,
    )


def test_catalog_reader_projects_bounded_events_without_worker_authority() -> None:
    job = _job()
    catalog = _Catalog((job,), _events(job))

    status = _reader(catalog).get(job.job_id, after_sequence=0, event_limit=2)

    assert status.repo_id == "demo"
    assert status.status == "running"
    assert status.next_event_sequence == 2
    assert [event.kind for event in status.events] == ["progress", "view_result"]
    assert [event.event_key for event in status.events] == [
        "progress-1",
        "view-result-2",
    ]
    assert status.events[0].index_type == "bm25"
    assert status.events[0].payload == {"changed_files": 2}
    assert status.events[1].effective_mode == "full"
    assert status.events[1].payload == {"documents": 8}
    assert catalog.event_calls == [(job.job_id, 0, 2)]
    serialized = status.model_dump_json()
    assert "private-worker" not in serialized
    assert "fencing" not in serialized
    assert "credential-bearing" not in serialized
    assert "capture-started" not in serialized
    assert "bm25-result" not in serialized


def test_catalog_reader_rereads_job_after_events_that_advance_attempt() -> None:
    first = _job()
    second = replace(first, attempt_count=2, updated_at_ms=13)
    event = replace(
        _events(first)[0],
        attempt_count=2,
        event_key="private-attempt-two-phase",
        created_at_ms=13,
    )

    class AdvancingCatalog(_Catalog):
        def list_job_events(
            self,
            job_id: str,
            *,
            after_sequence: int = 0,
            limit: int = 128,
        ):
            self.event_calls.append((job_id, after_sequence, limit))
            self.jobs[job_id] = second
            return (event,)

    status = _reader(AdvancingCatalog((first,))).get(first.job_id)

    assert status.attempt_count == 2
    assert status.events[0].attempt_count == 2
    assert status.events[0].event_key == "progress-1"
    assert "private-attempt-two-phase" not in status.model_dump_json()


def test_catalog_reader_rejects_events_outside_the_attested_job() -> None:
    job = _job()
    event = _events(job)[0]
    other = _job(idempotency_key="other")
    invalid = (
        (replace(event, job_id=other.job_id), "escaped"),
        (replace(event, attempt_count=2), "future"),
        (replace(event, view_type="vector"), "unrequested"),
    )

    for candidate, message in invalid:
        catalog = _UnattestedEventCatalog((job,), (candidate,))
        with pytest.raises(IndexJobReadError, match=message):
            _reader(catalog).get(job.job_id)


def test_catalog_reader_rejects_an_event_page_over_its_requested_limit() -> None:
    job = _job()
    catalog = _UnattestedEventCatalog((job,), _events(job))

    with pytest.raises(IndexJobReadError, match="exceeds"):
        _reader(catalog).get(job.job_id, event_limit=1)


def test_catalog_reader_hides_raw_executor_error_messages() -> None:
    job = _job(
        status=IndexJobStatus.FAILED,
        error_code="worker_executor_failed",
        error_message="credential-bearing internal detail",
    )

    status = _reader(_Catalog((job,))).get(job.job_id)

    assert status.error_code == "worker_executor_failed"
    assert status.error_message == "The index worker failed while preparing artifacts."
    assert "credential-bearing" not in status.model_dump_json()


def test_catalog_reader_masks_unknown_executor_error_codes() -> None:
    job = _job(
        status=IndexJobStatus.FAILED,
        error_code="private-adapter-detail",
        error_message="private failure",
    )

    status = _reader(_Catalog((job,))).get(job.job_id)

    assert status.error_code == "index_update_failed"
    assert status.error_message == (
        "The index update failed. Use the job ID to inspect server logs."
    )
    assert "private-adapter" not in status.model_dump_json()


def test_catalog_reader_authorizes_jobs_through_repository_bindings() -> None:
    allowed = _job()
    other = _job(
        repository_id=_OTHER_STORAGE_REPO,
        idempotency_key="other",
    )
    reader = _reader(_Catalog((allowed, other), active_job_id=allowed.job_id))

    assert reader.active("demo").job_id == allowed.job_id
    with pytest.raises(IndexJobNotFoundError):
        reader.get(other.job_id)
    with pytest.raises(IndexJobNotFoundError):
        reader.get("job_" + "f" * 64)


def test_overlay_marks_only_requested_surface_without_mutating_snapshot() -> None:
    status = RepoIndexStatus(
        repo_id="repo",
        last_indexed_commit=_INDEXED,
        current_head=_INDEXED,
        indexes=[
            IndexSurfaceStatus(
                index_type=index_type,
                state="stale" if index_type == "bm25" else "built",
                stale=index_type == "bm25",
                update_mode="rebuild",
                updates_enabled=True,
            )
            for index_type in ("bm25", "vector", "symbol_graph")
        ],
    )
    job = _job()

    overlaid = overlay_active_job(status, _active_response(job))

    assert status.indexes[0].state == "stale"
    assert overlaid.indexes[0].state == "updating"
    assert overlaid.indexes[0].stale is True
    assert overlaid.indexes[0].job_id == job.job_id
    assert [index.state for index in overlaid.indexes[1:]] == ["built", "built"]


def test_index_job_endpoint_uses_injected_reader(monkeypatch) -> None:
    job = _job()
    expected = _reader(_Catalog((job,))).get(job.job_id)
    calls: list[tuple[str, int, int]] = []

    class Reader:
        def active(self, repo_id: str):
            return None

        def get(
            self,
            job_id: str,
            *,
            after_sequence: int = 0,
            event_limit: int = 64,
        ):
            calls.append((job_id, after_sequence, event_limit))
            return expected

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(web_app.app.state, "index_job_reader", Reader(), raising=False)
    monkeypatch.setattr(web_app.asyncio, "to_thread", inline)

    response = asyncio.run(web_app.index_job_status(job.job_id, 4, 7))

    assert response == expected
    assert calls == [(job.job_id, 4, 7)]


def test_index_job_endpoint_is_unavailable_without_reader(monkeypatch) -> None:
    monkeypatch.delattr(web_app.app.state, "index_job_reader", raising=False)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(web_app.index_job_status("job_" + "f" * 64, 0, 64))

    assert raised.value.status_code == 503


def test_repo_status_releases_bundle_pin_before_active_job_read(monkeypatch) -> None:
    events: list[str] = []
    entry = SimpleNamespace(
        status="fresh",
        commit=_INDEXED,
        built_at="2026-08-26T00:00:00+00:00",
        metadata={},
    )
    manifest = SimpleNamespace(
        indexes={"bm25": entry},
        last_indexed_commit=_INDEXED,
        commit=_INDEXED,
        index_is_current=lambda index_type: index_type == "bm25",
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(instance_id="repo", repo_dir="/repo"),
        manifest=manifest,
    )
    job = _job()

    class Registry:
        @contextmanager
        def pin(self, repo_id: str):
            events.append("pin-enter")
            try:
                yield bundle
            finally:
                events.append("pin-exit")

    class Reader:
        def active(self, repo_id: str):
            events.append("active")
            return _active_response(job)

        def get(self, job_id: str, **kwargs):
            raise AssertionError("not used")

    def head(_path):
        events.append("head")
        return _INDEXED

    async def inline(function, *args, **kwargs):
        events.append("thread")
        return function(*args, **kwargs)

    monkeypatch.setattr(web_app.app.state, "registry", Registry(), raising=False)
    monkeypatch.setattr(web_app.app.state, "index_job_reader", Reader(), raising=False)
    monkeypatch.setattr(web_app.app.state, "index_head_resolver", head, raising=False)
    monkeypatch.setattr(web_app.asyncio, "to_thread", inline)

    response = asyncio.run(web_app.index_status("repo"))

    assert response.indexes[0].state == "updating"
    assert events == [
        "pin-enter",
        "thread",
        "head",
        "pin-exit",
        "thread",
        "active",
    ]
