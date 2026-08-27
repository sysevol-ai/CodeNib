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


def test_interrupted_before_launcher_transfer_cancels_without_waiting_for_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Loop(_runtime_summary())

    class PreLaunchInterruptThread(Thread):
        def start(self) -> None:
            if self.name == "codenib-index-runtime-launcher":
                raise KeyboardInterrupt
            Thread.start(self)

    monkeypatch.setattr(service_module, "Thread", PreLaunchInterruptThread)
    service = IndexJobBackgroundService(_Loop(_worker_summary()), runtime)

    with pytest.raises(IndexJobBackgroundServiceError, match="could not start"):
        service.start()

    assert service.state == "closed"
    assert runtime.entered.is_set() is False
    with pytest.raises(IndexJobBackgroundServiceError, match="service failed"):
        service.close()


def test_interrupted_unclaimed_launcher_cancels_a_late_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher_exited = Event()
    runtime = _Loop(_runtime_summary())

    class DelayedLauncherThread(Thread):
        _delayed = False

        def start(self) -> None:
            if self.name == "codenib-index-runtime-launcher" and not self._delayed:
                self._delayed = True

                def launch_later() -> None:
                    Event().wait(0.05)
                    Thread.start(self)
                    self.join()
                    launcher_exited.set()

                Thread(
                    target=launch_later,
                    name="test-delayed-index-launcher",
                    daemon=True,
                ).start()
                raise KeyboardInterrupt
            Thread.start(self)

    monkeypatch.setattr(service_module, "Thread", DelayedLauncherThread)
    service = IndexJobBackgroundService(_Loop(_worker_summary()), runtime)

    with pytest.raises(IndexJobBackgroundServiceError, match="could not start"):
        service.start()

    assert launcher_exited.wait(1)
    assert service.state == "closed"
    assert runtime.entered.is_set() is False
    with pytest.raises(IndexJobBackgroundServiceError, match="service failed"):
        service.close()


def test_interrupted_claimed_launch_waits_for_transfer_and_joins_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_start_entered = Event()
    allow_actual_start = Event()
    runtime = _Loop(_runtime_summary())

    class ClaimedLaunchThread(Thread):
        def start(self) -> None:
            if self.name == "codenib-index-runtime":
                actual_start_entered.set()
                allow_actual_start.wait()
                Thread.start(self)
                return
            if self.name == "codenib-index-runtime-launcher":
                Thread.start(self)
                assert actual_start_entered.wait(1)

                def release_actual_start() -> None:
                    Event().wait(0.05)
                    allow_actual_start.set()

                Thread(
                    target=release_actual_start,
                    name="test-release-index-launch",
                    daemon=True,
                ).start()
                raise KeyboardInterrupt
            Thread.start(self)

    monkeypatch.setattr(service_module, "Thread", ClaimedLaunchThread)
    service = IndexJobBackgroundService(_Loop(_worker_summary()), runtime)

    with pytest.raises(IndexJobBackgroundServiceError, match="could not start"):
        service.start()

    assert actual_start_entered.is_set()
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


def test_close_replays_settlement_after_interrupted_state_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _Loop(_worker_summary())
    runtime = _Loop(_runtime_summary())
    service = IndexJobBackgroundService(worker, runtime)
    service.start()
    assert runtime.entered.wait(1)
    assert worker.entered.wait(1)
    real_set = service._stop.set
    real_current_thread = service_module.current_thread
    real_is_alive = Thread.is_alive
    interruptions = {"stop": False, "current": False, "thread": False}
    current_reads = 0

    def set_stop() -> None:
        if not interruptions["stop"]:
            interruptions["stop"] = True
            real_set()
            raise KeyboardInterrupt
        real_set()

    def read_current_thread() -> Thread:
        nonlocal current_reads
        current_reads += 1
        if current_reads > 1 and not interruptions["current"]:
            interruptions["current"] = True
            raise KeyboardInterrupt
        return real_current_thread()

    def is_alive(thread: Thread) -> bool:
        if thread.name == "codenib-index-worker" and not interruptions["thread"]:
            interruptions["thread"] = True
            raise KeyboardInterrupt
        return real_is_alive(thread)

    monkeypatch.setattr(service._stop, "set", set_stop)
    monkeypatch.setattr(service_module, "current_thread", read_current_thread)
    monkeypatch.setattr(service_module.Thread, "is_alive", is_alive)

    with pytest.raises(KeyboardInterrupt):
        service.close()

    assert interruptions == {"stop": True, "current": True, "thread": True}
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
