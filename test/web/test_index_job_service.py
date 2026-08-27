# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from threading import Event, Thread

import pytest

import codenib.web.index_job_service as service_module
from codenib.storage import IndexJobSchedulerRunSummary
from codenib.web.index_job_runtime import IndexJobRuntimeLoopSummary
from codenib.web.index_job_service import (
    IndexJobBackgroundService,
    IndexJobBackgroundServiceError,
    IndexJobBackgroundServiceSummary,
)


class _Loop:
    def __init__(self, summary) -> None:
        self.summary = summary
        self.entered = Event()
        self.exited = Event()
        self.stop_signal = None

    def run(self, stop_signal):
        self.stop_signal = stop_signal
        self.entered.set()
        stop_signal.wait()
        self.exited.set()
        return self.summary


def _worker_summary() -> IndexJobSchedulerRunSummary:
    return IndexJobSchedulerRunSummary(0, 0, 0)


def _runtime_summary() -> IndexJobRuntimeLoopSummary:
    return IndexJobRuntimeLoopSummary(0, 0, 0)


def test_background_service_stops_and_joins_both_owned_loops() -> None:
    worker = _Loop(_worker_summary())
    runtime = _Loop(_runtime_summary())
    service = IndexJobBackgroundService(worker, runtime)

    service.start()
    assert runtime.entered.wait(1)
    assert worker.entered.wait(1)
    assert service.state == "running"
    assert service.healthy is True
    assert worker.stop_signal is runtime.stop_signal

    summary = service.close()

    assert summary == IndexJobBackgroundServiceSummary(
        worker=_worker_summary(),
        runtime=_runtime_summary(),
    )
    assert service.close() is summary
    assert service.state == "closed"
    assert worker.exited.is_set()
    assert runtime.exited.is_set()


def test_background_loop_fault_stops_peer_and_is_reported_after_join() -> None:
    failure = RuntimeError("private worker failure")

    class Worker:
        def run(self, _stop_signal):
            raise failure

    runtime = _Loop(_runtime_summary())
    service = IndexJobBackgroundService(Worker(), runtime)
    service.start()

    assert runtime.entered.wait(1)
    assert runtime.exited.wait(1)
    assert service.healthy is False
    with pytest.raises(IndexJobBackgroundServiceError, match="service failed") as error:
        service.close()

    assert "private" not in str(error.value)
    assert error.value.__cause__ is failure
    assert service.state == "closed"


def test_interrupted_outcome_commit_still_stops_peer_and_retains_failure() -> None:
    failure = RuntimeError("private worker failure")

    class Worker:
        def run(self, _stop_signal):
            raise failure

    class InterruptAfterCommitLock:
        def __init__(self, lock) -> None:
            self.lock = lock
            self.interrupted = False

        def run(self, callback):
            result = self.lock.run(callback)
            if not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            return result

    runtime = _Loop(_runtime_summary())
    service = IndexJobBackgroundService(Worker(), runtime)
    outcome_lock = InterruptAfterCommitLock(service._outcome_lock)
    service._outcome_lock = outcome_lock
    service.start()

    assert runtime.entered.wait(1)
    assert runtime.exited.wait(1)
    with pytest.raises(IndexJobBackgroundServiceError) as error:
        service.close()

    assert outcome_lock.interrupted is True
    assert error.value.__cause__ is failure
    assert service.state == "closed"


def test_background_loop_cannot_exit_cleanly_before_shutdown() -> None:
    class Worker:
        def run(self, _stop_signal):
            return _worker_summary()

    runtime = _Loop(_runtime_summary())
    service = IndexJobBackgroundService(Worker(), runtime)
    service.start()

    assert runtime.exited.wait(1)
    with pytest.raises(IndexJobBackgroundServiceError) as error:
        service.close()

    assert type(error.value.__cause__) is IndexJobBackgroundServiceError
    assert "exited before shutdown" in str(error.value.__cause__)


def test_partial_thread_start_failure_stops_and_joins_started_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Loop(_runtime_summary())
    service = IndexJobBackgroundService(_Loop(_worker_summary()), runtime)
    real_start = Thread.start

    def start(thread: Thread) -> None:
        if thread.name == "codenib-index-worker":
            raise RuntimeError("private start failure")
        real_start(thread)

    monkeypatch.setattr(service_module.Thread, "start", start)

    with pytest.raises(IndexJobBackgroundServiceError, match="could not start"):
        service.start()

    assert service.state == "closed"
    assert runtime.exited.is_set()
    assert service.healthy is False
    with pytest.raises(IndexJobBackgroundServiceError, match="service failed"):
        service.close()


def test_interrupted_pre_ident_start_waits_for_child_entry_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Loop(_runtime_summary())
    delay = Event()

    class DelayedStartThread(Thread):
        _delayed = False

        def start(self) -> None:
            if self.name == "codenib-index-runtime" and not self._delayed:
                self._delayed = True

                def launch_after_interruption() -> None:
                    delay.wait(0.05)
                    Thread.start(self)

                Thread(
                    target=launch_after_interruption,
                    name="test-delayed-index-launch",
                    daemon=True,
                ).start()
                raise KeyboardInterrupt
            Thread.start(self)

    monkeypatch.setattr(service_module, "Thread", DelayedStartThread)
    service = IndexJobBackgroundService(_Loop(_worker_summary()), runtime)

    with pytest.raises(IndexJobBackgroundServiceError, match="could not start"):
        service.start()

    assert service.state == "closed"
    assert runtime.exited.is_set()
    with pytest.raises(IndexJobBackgroundServiceError, match="service failed"):
        service.close()


def test_interrupted_start_serialization_release_settles_running_loops() -> None:
    class InterruptAfterRunLock:
        def __init__(self, lock) -> None:
            self.lock = lock
            self.interrupted = False

        def run(self, callback):
            result = self.lock.run(callback)
            if not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            return result

    worker = _Loop(_worker_summary())
    runtime = _Loop(_runtime_summary())
    service = IndexJobBackgroundService(worker, runtime)
    settlement_lock = InterruptAfterRunLock(service._settlement_lock)
    service._settlement_lock = settlement_lock

    with pytest.raises(IndexJobBackgroundServiceError, match="could not start"):
        service.start()

    assert settlement_lock.interrupted is True
    assert service.state == "closed"
    assert worker.exited.is_set()
    assert runtime.exited.is_set()
    with pytest.raises(IndexJobBackgroundServiceError, match="service failed"):
        service.close()


def test_interrupted_running_publication_settles_started_loops() -> None:
    class InterruptRunningPublicationLock:
        def __init__(self, lock, service) -> None:
            self.lock = lock
            self.service = service
            self.interrupted = False

        def run(self, callback):
            result = self.lock.run(callback)
            if self.service._state == "running" and not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            return result

    worker = _Loop(_worker_summary())
    runtime = _Loop(_runtime_summary())
    service = IndexJobBackgroundService(worker, runtime)
    lifecycle_lock = InterruptRunningPublicationLock(
        service._lifecycle_lock,
        service,
    )
    service._lifecycle_lock = lifecycle_lock

    with pytest.raises(IndexJobBackgroundServiceError, match="could not start"):
        service.start()

    assert lifecycle_lock.interrupted is True
    assert service.state == "closed"
    assert worker.exited.is_set()
    assert runtime.exited.is_set()


def test_owned_loop_cannot_close_and_is_joined_by_external_shutdown() -> None:
    holder = {}

    class Worker:
        def __init__(self) -> None:
            self.entered = Event()

        def run(self, _stop_signal):
            self.entered.set()
            holder["service"].close()
            raise AssertionError("owned close unexpectedly returned")

    worker = Worker()
    runtime = _Loop(_runtime_summary())
    service = IndexJobBackgroundService(worker, runtime)
    holder["service"] = service
    service.start()

    assert worker.entered.wait(1)
    assert runtime.exited.wait(1)
    with pytest.raises(IndexJobBackgroundServiceError) as error:
        service.close()

    assert type(error.value.__cause__) is IndexJobBackgroundServiceError
    assert "cannot close from an owned loop" in str(error.value.__cause__)
    assert service.state == "closed"


def test_close_releases_lifecycle_lock_while_loops_observe_state() -> None:
    holder = {}
    observations = []

    class StateReadingLoop(_Loop):
        def run(self, stop_signal):
            self.stop_signal = stop_signal
            self.entered.set()
            stop_signal.wait()
            service = holder["service"]
            observations.append((service.state, service.healthy))
            self.exited.set()
            return self.summary

    worker = StateReadingLoop(_worker_summary())
    runtime = _Loop(_runtime_summary())
    service = IndexJobBackgroundService(worker, runtime)
    holder["service"] = service
    service.start()
    assert worker.entered.wait(1)
    assert runtime.entered.wait(1)

    summary = service.close()

    assert summary == IndexJobBackgroundServiceSummary(
        worker=_worker_summary(),
        runtime=_runtime_summary(),
    )
    assert observations == [("stopping", True)]


def test_interrupted_close_transition_still_stops_and_joins_loops() -> None:
    class InterruptStoppingPublicationLock:
        def __init__(self, lock, service) -> None:
            self.lock = lock
            self.service = service
            self.interrupted = False

        def run(self, callback):
            result = self.lock.run(callback)
            if self.service._state == "stopping" and not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            return result

    worker = _Loop(_worker_summary())
    runtime = _Loop(_runtime_summary())
    service = IndexJobBackgroundService(worker, runtime)
    service.start()
    assert runtime.entered.wait(1)
    assert worker.entered.wait(1)
    lifecycle_lock = InterruptStoppingPublicationLock(
        service._lifecycle_lock,
        service,
    )
    service._lifecycle_lock = lifecycle_lock

    with pytest.raises(KeyboardInterrupt):
        service.close()

    assert lifecycle_lock.interrupted is True
    assert worker.exited.is_set()
    assert runtime.exited.is_set()
    assert service.state == "closed"
    assert service.close() == IndexJobBackgroundServiceSummary(
        worker=_worker_summary(),
        runtime=_runtime_summary(),
    )


def test_close_retries_interrupted_stop_and_thread_state_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _Loop(_worker_summary())
    runtime = _Loop(_runtime_summary())
    service = IndexJobBackgroundService(worker, runtime)
    service.start()
    assert runtime.entered.wait(1)
    assert worker.entered.wait(1)
    real_is_set = service._stop.is_set
    real_is_alive = Thread.is_alive
    interruptions = {"stop": False, "thread": False}

    def is_set() -> bool:
        if not interruptions["stop"]:
            interruptions["stop"] = True
            raise KeyboardInterrupt
        return real_is_set()

    def is_alive(thread: Thread) -> bool:
        if thread.name == "codenib-index-worker" and not interruptions["thread"]:
            interruptions["thread"] = True
            raise KeyboardInterrupt
        return real_is_alive(thread)

    monkeypatch.setattr(service._stop, "is_set", is_set)
    monkeypatch.setattr(service_module.Thread, "is_alive", is_alive)

    with pytest.raises(KeyboardInterrupt):
        service.close()

    assert interruptions == {"stop": True, "thread": True}
    assert worker.exited.is_set()
    assert runtime.exited.is_set()
    assert service.state == "closed"
    assert service.close() == IndexJobBackgroundServiceSummary(
        worker=_worker_summary(),
        runtime=_runtime_summary(),
    )


def test_close_settles_both_threads_before_rethrowing_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _Loop(_worker_summary())
    runtime = _Loop(_runtime_summary())
    service = IndexJobBackgroundService(worker, runtime)
    service.start()
    assert runtime.entered.wait(1)
    assert worker.entered.wait(1)
    real_join = Thread.join
    interrupted = False

    def join(thread: Thread, *args, **kwargs) -> None:
        nonlocal interrupted
        if thread.name == "codenib-index-worker" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        real_join(thread, *args, **kwargs)

    monkeypatch.setattr(service_module.Thread, "join", join)

    with pytest.raises(KeyboardInterrupt):
        service.close()

    assert worker.exited.is_set()
    assert runtime.exited.is_set()
    assert service.state == "closed"
    assert service.close() == IndexJobBackgroundServiceSummary(
        worker=_worker_summary(),
        runtime=_runtime_summary(),
    )


def test_background_service_closes_cleanly_before_start_and_rejects_restart() -> None:
    service = IndexJobBackgroundService(
        _Loop(_worker_summary()),
        _Loop(_runtime_summary()),
    )

    assert service.close() == IndexJobBackgroundServiceSummary(
        worker=_worker_summary(),
        runtime=_runtime_summary(),
    )
    with pytest.raises(RuntimeError, match="only once"):
        service.start()


def test_second_start_is_rejected_without_stopping_running_service() -> None:
    worker = _Loop(_worker_summary())
    runtime = _Loop(_runtime_summary())
    service = IndexJobBackgroundService(worker, runtime)
    service.start()
    assert runtime.entered.wait(1)
    assert worker.entered.wait(1)

    with pytest.raises(RuntimeError, match="only once"):
        service.start()

    assert service.state == "running"
    assert service.healthy is True
    assert worker.exited.is_set() is False
    assert runtime.exited.is_set() is False
    assert service.close() == IndexJobBackgroundServiceSummary(
        worker=_worker_summary(),
        runtime=_runtime_summary(),
    )
