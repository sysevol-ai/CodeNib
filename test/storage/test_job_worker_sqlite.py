# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""File-backed SQLite integration tests for the durable whole-job worker."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import pytest

import codenib.storage.sqlite_catalog as sqlite_catalog_module
from codenib.storage import (
    INDEX_JOB_REQUEST_CONTRACT,
    BlobInfo,
    IndexJobCompletion,
    IndexJobEffectiveMode,
    IndexJobEventKind,
    IndexJobExecutionContext,
    IndexJobExecutionResult,
    IndexJobExecutor,
    IndexJobRecord,
    IndexJobStatus,
    IndexJobStopReason,
    IndexJobViewArtifact,
    IndexJobViewExecutionResult,
    IndexJobViewOutcome,
    IndexJobViewRecord,
    IndexJobWorker,
    IndexJobWorkerDisposition,
    IndexJobWorkerRunResult,
    LocalCAS,
    PublishConflict,
    SQLiteCatalog,
    StorageIntegrityError,
    publish_job_artifacts,
)


class _CatalogProbe:
    def __init__(
        self,
        *,
        scan_barrier: threading.Barrier | None = None,
        heartbeat_target: int = 0,
        lose_publication_response: bool = False,
    ) -> None:
        self.lock = threading.Lock()
        self.scan_barrier = scan_barrier
        self.scan_waiters = 0
        self.first_scan_ready = threading.Event()
        self.heartbeat_target = heartbeat_target
        self.heartbeat_ready = threading.Event()
        self.heartbeats: list[tuple[int, int, object]] = []
        self.sessions: list[tuple[int, int]] = []
        self.publication_calls = 0
        self.lose_publication_response = lose_publication_response
        self.publication_response_lost = False

    def record_session(self, catalog: SQLiteCatalog) -> None:
        with self.lock:
            self.sessions.append((threading.get_ident(), id(catalog)))

    def wait_at_scan_barrier(self) -> None:
        barrier = self.scan_barrier
        if barrier is None:
            return
        with self.lock:
            should_wait = self.scan_waiters < barrier.parties
            if should_wait:
                self.scan_waiters += 1
                self.first_scan_ready.set()
        if should_wait:
            barrier.wait(timeout=5)

    def record_heartbeat(self, catalog: SQLiteCatalog, heartbeat: object) -> None:
        with self.lock:
            self.heartbeats.append((threading.get_ident(), id(catalog), heartbeat))
            if len(self.heartbeats) >= self.heartbeat_target:
                self.heartbeat_ready.set()

    def begin_publication(self) -> None:
        with self.lock:
            self.publication_calls += 1

    def consume_publication_response_loss(self) -> bool:
        with self.lock:
            if not self.lose_publication_response or self.publication_response_lost:
                return False
            self.publication_response_lost = True
            return True


class _WorkerSQLiteCatalog(SQLiteCatalog):
    def __init__(
        self,
        path: Path,
        *,
        probe: _CatalogProbe,
        clock: dict[str, int] | None,
    ) -> None:
        super().__init__(path, create=False)
        self._worker_probe = probe
        if clock is not None:
            _install_clock(self, clock)
        probe.record_session(self)

    def scan_runnable_jobs(self, *, cursor=None, limit: int = 64):
        page = super().scan_runnable_jobs(cursor=cursor, limit=limit)
        self._worker_probe.wait_at_scan_barrier()
        return page

    def heartbeat_job_attempt(self, job_id: str, **kwargs: Any):
        heartbeat = super().heartbeat_job_attempt(job_id, **kwargs)
        self._worker_probe.record_heartbeat(self, heartbeat)
        return heartbeat

    def publish_job_outputs(self, job_id: str, **kwargs: Any):
        self._worker_probe.begin_publication()
        completed = super().publish_job_outputs(job_id, **kwargs)
        if self._worker_probe.consume_publication_response_loss():
            raise RuntimeError("injected publication response loss")
        return completed


class _SQLiteSessionFactory:
    def __init__(
        self,
        path: Path,
        probe: _CatalogProbe,
        *,
        clock: dict[str, int] | None = None,
    ) -> None:
        self.path = path
        self.probe = probe
        self.clock = clock

    def __call__(self) -> _WorkerSQLiteCatalog:
        return _WorkerSQLiteCatalog(
            self.path,
            probe=self.probe,
            clock=self.clock,
        )


class _TrackingCAS(LocalCAS):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self._retained_lock = threading.Lock()
        self.retained_calls: list[tuple[BlobInfo, ...]] = []

    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], Any],
    ) -> Any:
        with self._retained_lock:
            self.retained_calls.append(expected)
        return super().retain_receipts(expected, callback)


class _CleanupFailingCAS(_TrackingCAS):
    def __init__(self, root: Path, failure: BaseException) -> None:
        super().__init__(root)
        self.failure = failure
        self.callback_completed = False

    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], Any],
    ) -> Any:
        super().retain_receipts(expected, callback)
        self.callback_completed = True
        raise self.failure


class _StaticResolver:
    def __init__(self, executor: IndexJobExecutor) -> None:
        self.executor = executor
        self.calls = 0
        self._lock = threading.Lock()

    def resolve(
        self,
        job: IndexJobRecord,
        views: tuple[IndexJobViewRecord, ...],
    ) -> IndexJobExecutor:
        assert tuple(view.job_id for view in views) == (job.job_id,) * len(views)
        with self._lock:
            self.calls += 1
        return self.executor


class _SuccessfulExecutor:
    def __init__(
        self,
        object_store: LocalCAS,
        *,
        heartbeat_gate: threading.Event | None = None,
        append_progress: bool = False,
    ) -> None:
        self.object_store = object_store
        self.heartbeat_gate = heartbeat_gate
        self.append_progress = append_progress
        self.calls = 0
        self.contexts: list[IndexJobExecutionContext] = []
        self.last_result: IndexJobExecutionResult | None = None
        self._lock = threading.Lock()

    def execute(self, context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        with self._lock:
            self.calls += 1
            self.contexts.append(context)
        if self.append_progress:
            context.control.append_progress(
                "executor.prepared",
                {"view_count": len(context.views)},
            )
        if self.heartbeat_gate is not None:
            assert self.heartbeat_gate.wait(timeout=5)
        result = _successful_result(context, self.object_store)
        with self._lock:
            self.last_result = result
        return result


class _CancellationExecutor:
    def __init__(self, object_store: LocalCAS) -> None:
        self.object_store = object_store
        self.entered = threading.Event()
        self.observed = threading.Event()

    def execute(self, context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        self.entered.set()
        assert context.control.stop_token.wait(timeout=30)
        assert context.control.stop_token.reason is IndexJobStopReason.CANCEL_REQUESTED
        self.observed.set()
        # Return otherwise publishable bytes to prove the worker gives the
        # durable cancellation marker precedence over a late executor result.
        return _successful_result(context, self.object_store)


class _RetryableFailureExecutor:
    def execute(self, context: IndexJobExecutionContext) -> IndexJobExecutionResult:
        del context
        raise RuntimeError("executor detail must not be persisted")


def _install_clock(catalog: SQLiteCatalog, clock: dict[str, int]) -> None:
    catalog._connection.create_function(
        "julianday",
        1,
        lambda _value: 2440587.5 + clock["ms"] / 86_400_000,
    )


def _create_job(
    path: Path,
    *,
    view_types: tuple[str, ...] = ("bm25", "vector"),
    idempotency_key: str = "worker-job",
    max_attempts: int = 3,
    clock: dict[str, int] | None = None,
) -> tuple[IndexJobRecord, dict[str, str]]:
    with SQLiteCatalog(path) as catalog:
        if clock is not None:
            _install_clock(catalog, clock)
        repository_id = catalog.create_repository("owner/job-worker")
        source_revision_id = catalog.create_source_revision(
            repository_id,
            commit_sha="a" * 40,
            tree_sha="b" * 64,
        )
        profiles = {
            view_type: catalog.create_view_profile(
                view_type,
                {"builder": "worker-test", "view": view_type},
            )
            for view_type in view_types
        }
        request = {
            "contract": INDEX_JOB_REQUEST_CONTRACT,
            "views": {
                view_type: {
                    "profile_id": profiles[view_type],
                    "requested_mode": "full",
                    "required": True,
                }
                for view_type in view_types
            },
        }
        job = catalog.create_job(
            repository_id,
            source_revision_id,
            idempotency_key,
            request,
            max_attempts=max_attempts,
        )
    return job, profiles


def _successful_result(
    context: IndexJobExecutionContext,
    object_store: LocalCAS,
) -> IndexJobExecutionResult:
    results = []
    for view in context.views:
        payload = (
            f"{context.job.job_id}:{context.attempt.attempt_count}:" f"{view.view_type}"
        ).encode()
        receipt = object_store.put_bytes(payload)
        artifact = IndexJobViewArtifact.create(
            view.view_type,
            view.profile_id,
            receipt,
            schema_version=f"{view.view_type}.worker-test.v1",
            media_type=f"application/x-test-{view.view_type}",
            metadata={"attempt": context.attempt.attempt_count},
        )
        results.append(
            IndexJobViewExecutionResult.create(
                view,
                effective_mode=IndexJobEffectiveMode.FULL,
                outcome=IndexJobViewOutcome.SUCCEEDED,
                artifact=artifact,
                payload={"prepared": True},
            )
        )
    return IndexJobExecutionResult(tuple(results), retryable=False)


def _worker(
    factory: _SQLiteSessionFactory,
    object_store: LocalCAS,
    resolver: _StaticResolver,
    *,
    owner_id: str,
    heartbeat_interval_ms: int = 5,
) -> IndexJobWorker:
    return IndexJobWorker(
        catalog_factory=factory,
        object_store=object_store,
        resolver=resolver,
        lease_duration_ms=60_000,
        heartbeat_interval_ms=heartbeat_interval_ms,
        owner_id_factory=lambda: owner_id,
    )


def test_two_view_job_records_events_and_publishes_once(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite3"
    job, _profiles = _create_job(path)
    probe = _CatalogProbe()
    factory = _SQLiteSessionFactory(path, probe)
    with _TrackingCAS(tmp_path / "cas") as object_store:
        executor = _SuccessfulExecutor(object_store, append_progress=True)
        worker = _worker(
            factory,
            object_store,
            _StaticResolver(executor),
            owner_id="task-two-view",
        )

        result = worker.run_once()

        assert result == IndexJobWorkerRunResult(
            IndexJobWorkerDisposition.SUCCEEDED,
            job.job_id,
            1,
        )
        assert executor.calls == 1
        assert probe.publication_calls == 1
        assert len(object_store.retained_calls) == 1
        assert len(object_store.retained_calls[0]) == 2
        assert tuple(
            receipt.digest for receipt in object_store.retained_calls[0]
        ) == tuple(sorted(receipt.digest for receipt in object_store.retained_calls[0]))

        with SQLiteCatalog(path, create=False) as catalog:
            completed = catalog.get_job(job.job_id)
            assert completed.status is IndexJobStatus.SUCCEEDED
            assert completed.attempt_count == 1
            assert catalog.list_job_attempt_completions(job.job_id) == ()
            events = catalog.list_job_events(job.job_id)
            assert tuple(event.kind for event in events) == (
                IndexJobEventKind.PROGRESS,
                IndexJobEventKind.VIEW_RESULT,
                IndexJobEventKind.VIEW_RESULT,
            )
            assert tuple(event.view_type for event in events[1:]) == (
                "bm25",
                "vector",
            )
            assert all(
                event.outcome is IndexJobViewOutcome.SUCCEEDED for event in events[1:]
            )
            resolved = catalog.resolve_ref(job.repository_id)
            assert tuple(resolved["manifest"]["views"]) == ("bm25", "vector")


def test_slow_executor_observes_repeated_independent_heartbeats(
    tmp_path: Path,
) -> None:
    path = tmp_path / "heartbeat.sqlite3"
    job, _profiles = _create_job(path, view_types=("bm25",))
    probe = _CatalogProbe(heartbeat_target=2)
    factory = _SQLiteSessionFactory(path, probe)
    main_thread = threading.get_ident()
    with _TrackingCAS(tmp_path / "cas") as object_store:
        executor = _SuccessfulExecutor(
            object_store,
            heartbeat_gate=probe.heartbeat_ready,
        )
        result = _worker(
            factory,
            object_store,
            _StaticResolver(executor),
            owner_id="task-heartbeat",
        ).run_once()

        assert result.disposition is IndexJobWorkerDisposition.SUCCEEDED
        assert result.job_id == job.job_id
        assert len(probe.heartbeats) >= 3
        first_thread, first_session, first = probe.heartbeats[0]
        second_thread, second_session, second = probe.heartbeats[1]
        assert first_thread == second_thread
        assert first_thread != main_thread
        assert first_session == second_session
        assert first.lease.lease_expires_at_ms < second.lease.lease_expires_at_ms
        assert len({session for _thread, session in probe.sessions}) >= 2


def test_running_cancel_is_observed_and_closes_only_cancelled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "cancel.sqlite3"
    job, _profiles = _create_job(path, view_types=("bm25",))
    probe = _CatalogProbe()
    factory = _SQLiteSessionFactory(path, probe)
    main_thread = threading.get_ident()
    with _TrackingCAS(tmp_path / "cas") as object_store:
        executor = _CancellationExecutor(object_store)
        copy_started = threading.Event()
        heartbeat_ready_to_acquire = threading.Event()
        allow_heartbeat_acquire = threading.Event()
        heartbeat_attempted = threading.Event()
        coordination = sqlite_catalog_module._catalog_path_coordination(path.resolve())
        original_copy = sqlite_catalog_module._copy_validation_source
        original_lock_acquire = sqlite_catalog_module._CancellationSafeRLock._acquire

        def observe_heartbeat_lock(lock) -> None:
            if (
                lock is coordination.lock
                and threading.current_thread().name.startswith("codenib-job-heartbeat-")
                and executor.entered.is_set()
                and not heartbeat_attempted.is_set()
            ):
                heartbeat_ready_to_acquire.set()
                assert allow_heartbeat_acquire.wait(timeout=30)
                heartbeat_attempted.set()
            original_lock_acquire(lock)

        def slow_cancellation_wal_copy(
            descriptor,
            expected,
            destination,
            *,
            label,
        ):
            if (
                label == "WAL sidecar"
                and threading.get_ident() == main_thread
                and executor.entered.is_set()
                and not copy_started.is_set()
            ):
                assert coordination.lock.held_by_current_thread()
                copy_started.set()
                allow_heartbeat_acquire.set()
                assert heartbeat_attempted.wait(timeout=30)
            return original_copy(
                descriptor,
                expected,
                destination,
                label=label,
            )

        monkeypatch.setattr(
            sqlite_catalog_module._CancellationSafeRLock,
            "_acquire",
            observe_heartbeat_lock,
        )
        monkeypatch.setattr(
            sqlite_catalog_module,
            "_copy_validation_source",
            slow_cancellation_wal_copy,
        )
        worker = _worker(
            factory,
            object_store,
            _StaticResolver(executor),
            owner_id="task-cancel",
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker.run_once)
            try:
                assert executor.entered.wait(timeout=30)
                assert heartbeat_ready_to_acquire.wait(timeout=30)
                with SQLiteCatalog(path, create=False) as catalog:
                    requested = catalog.request_job_cancel(job.job_id)
                    assert requested.cancel_requested
            finally:
                allow_heartbeat_acquire.set()
            result = future.result(timeout=30)

        assert copy_started.is_set()
        assert heartbeat_ready_to_acquire.is_set()
        assert heartbeat_attempted.is_set()
        assert executor.observed.is_set()
        assert result.disposition is IndexJobWorkerDisposition.CANCELLED
        assert probe.publication_calls == 0
        assert object_store.retained_calls == []
        with SQLiteCatalog(path, create=False) as catalog:
            completed = catalog.get_job(job.job_id)
            assert completed.status is IndexJobStatus.CANCELLED
            assert completed.cancel_requested
            closure = catalog.get_job_attempt_completion(job.job_id, 1)
            assert closure.outcome is IndexJobCompletion.CANCELLED
            assert catalog.list_job_events(job.job_id) == ()


def test_two_workers_scanning_same_job_execute_it_once(tmp_path: Path) -> None:
    path = tmp_path / "contended.sqlite3"
    job, _profiles = _create_job(path, view_types=("bm25",))
    probe = _CatalogProbe(scan_barrier=threading.Barrier(2))
    factory = _SQLiteSessionFactory(path, probe)
    with _TrackingCAS(tmp_path / "cas") as object_store:
        executor = _SuccessfulExecutor(object_store)
        resolver = _StaticResolver(executor)
        first = _worker(
            factory,
            object_store,
            resolver,
            owner_id="task-contender-a",
        )
        second = _worker(
            factory,
            object_store,
            resolver,
            owner_id="task-contender-b",
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first.run_once)
            assert probe.first_scan_ready.wait(timeout=5)
            second_future = pool.submit(second.run_once)
            futures = (first_future, second_future)
            results = tuple(future.result(timeout=10) for future in futures)

        assert {result.disposition for result in results} == {
            IndexJobWorkerDisposition.IDLE,
            IndexJobWorkerDisposition.SUCCEEDED,
        }
        assert executor.calls == 1
        assert resolver.calls == 1
        assert probe.publication_calls == 1
        assert len(object_store.retained_calls) == 1
        with SQLiteCatalog(path, create=False) as catalog:
            completed = catalog.get_job(job.job_id)
            assert completed.status is IndexJobStatus.SUCCEEDED
            assert completed.attempt_count == 1
            assert len(catalog.list_job_attempts(job.job_id)) == 1


def test_expired_takeover_fences_the_old_publication_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "takeover.sqlite3"
    clock = {"ms": 1_000}
    job, _profiles = _create_job(
        path,
        view_types=("bm25",),
        clock=clock,
    )
    with SQLiteCatalog(path, create=False) as catalog:
        _install_clock(catalog, clock)
        stale = catalog.acquire_job_lease(
            job.job_id,
            owner_id="stale-task",
            lease_duration_ms=10,
        )
    clock["ms"] = stale.lease_expires_at_ms + 1_000

    probe = _CatalogProbe()
    factory = _SQLiteSessionFactory(path, probe, clock=clock)
    with _TrackingCAS(tmp_path / "cas") as object_store:
        executor = _SuccessfulExecutor(object_store)
        result = _worker(
            factory,
            object_store,
            _StaticResolver(executor),
            owner_id="takeover-task",
        ).run_once()

        assert result.disposition is IndexJobWorkerDisposition.SUCCEEDED
        assert result.attempt_count == 2
        assert executor.last_result is not None
        with SQLiteCatalog(path, create=False) as catalog:
            _install_clock(catalog, clock)
            first_closure = catalog.get_job_attempt_completion(job.job_id, 1)
            assert first_closure.outcome is IndexJobCompletion.REQUEUE
            assert first_closure.error_code == "lease_expired"
            completed = catalog.get_job(job.job_id)
            assert completed.status is IndexJobStatus.SUCCEEDED
            assert completed.attempt_count == 2
            preserved_ref = catalog.resolve_ref(job.repository_id)

            with pytest.raises(PublishConflict):
                publish_job_artifacts(
                    job.job_id,
                    catalog=catalog,
                    object_store=object_store,
                    owner_id="stale-task",
                    fencing_token=stale.fencing_token,
                    outputs=executor.last_result.artifacts,
                )

            assert catalog.resolve_ref(job.repository_id) == preserved_ref
            assert catalog.get_job(job.job_id) == completed


def test_publication_response_loss_reconciles_durable_success(
    tmp_path: Path,
) -> None:
    path = tmp_path / "response-loss.sqlite3"
    job, _profiles = _create_job(path, view_types=("bm25",))
    probe = _CatalogProbe(lose_publication_response=True)
    factory = _SQLiteSessionFactory(path, probe)
    with _TrackingCAS(tmp_path / "cas") as object_store:
        executor = _SuccessfulExecutor(object_store)
        result = _worker(
            factory,
            object_store,
            _StaticResolver(executor),
            owner_id="task-response-loss",
        ).run_once()

        assert probe.publication_response_lost
        assert probe.publication_calls == 1
        assert len(object_store.retained_calls) == 1
        assert result == IndexJobWorkerRunResult(
            IndexJobWorkerDisposition.SUCCEEDED,
            job.job_id,
            1,
        )
        with SQLiteCatalog(path, create=False) as catalog:
            completed = catalog.get_job(job.job_id)
            assert completed.status is IndexJobStatus.SUCCEEDED
            assert completed.result_snapshot_id is not None
            assert catalog.list_job_attempt_completions(job.job_id) == ()


def test_retention_cleanup_failure_surfaces_after_durable_success(
    tmp_path: Path,
) -> None:
    path = tmp_path / "retention-cleanup-failure.sqlite3"
    job, _profiles = _create_job(path, view_types=("bm25",))
    probe = _CatalogProbe()
    factory = _SQLiteSessionFactory(path, probe)
    failure = RuntimeError("retention cleanup failed")
    with _CleanupFailingCAS(tmp_path / "cas", failure) as object_store:
        executor = _SuccessfulExecutor(object_store)

        with pytest.raises(StorageIntegrityError, match="retention failed") as raised:
            _worker(
                factory,
                object_store,
                _StaticResolver(executor),
                owner_id="task-retention-cleanup",
            ).run_once()

        assert raised.value.__cause__ is failure
        assert object_store.callback_completed
        assert len(object_store.retained_calls) == 1
        assert probe.publication_calls == 1
        with SQLiteCatalog(path, create=False) as catalog:
            completed = catalog.get_job(job.job_id)
            assert completed.status is IndexJobStatus.SUCCEEDED
            assert completed.result_snapshot_id is not None
            assert catalog.list_job_attempt_completions(job.job_id) == ()


def test_retryable_executor_failure_returns_the_exact_requeue_closure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "requeue.sqlite3"
    job, _profiles = _create_job(path, view_types=("bm25",), max_attempts=3)
    probe = _CatalogProbe()
    factory = _SQLiteSessionFactory(path, probe)
    with _TrackingCAS(tmp_path / "cas") as object_store:
        result = _worker(
            factory,
            object_store,
            _StaticResolver(_RetryableFailureExecutor()),
            owner_id="task-requeue",
        ).run_once()

        assert result == IndexJobWorkerRunResult(
            IndexJobWorkerDisposition.REQUEUED,
            job.job_id,
            1,
        )
        assert probe.publication_calls == 0
        assert object_store.retained_calls == []
        with SQLiteCatalog(path, create=False) as catalog:
            queued = catalog.get_job(job.job_id)
            assert queued.status is IndexJobStatus.QUEUED
            assert queued.error_code == "worker_executor_failed"
            assert queued.error_message is None
            closure = catalog.get_job_attempt_completion(job.job_id, 1)
            assert closure.outcome is IndexJobCompletion.REQUEUE
            assert closure.error_code == "worker_executor_failed"
            assert closure.error_message is None
