# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from threading import Barrier, Event, Thread

import pytest

from codenib.web.index_job_activation import (
    IndexJobActivationError,
    IndexJobRuntimeActivation,
)
from codenib.web.index_job_runtime import (
    IndexJobRuntimeLoopSummary,
    IndexJobRuntimeReconciliationLoop,
)


def _activation(generation: int) -> IndexJobRuntimeActivation:
    return IndexJobRuntimeActivation(
        repo_id="demo",
        repository_id="repo_" + "a" * 64,
        ref_name="main",
        job_id="job_" + chr(ord("a") + generation) * 64,
        attempt_count=1,
        snapshot_id="snapshot_" + chr(ord("d") + generation) * 64,
        ref_generation=generation,
        ref_updated_at=f"2026-08-27T00:00:0{generation}+00:00",
        finished_at_ms=generation,
    )


class _StopSignal:
    def __init__(self, *, stop_after_waits: int) -> None:
        self.stop_after_waits = stop_after_waits
        self.waits: list[float | None] = []

    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return len(self.waits) >= self.stop_after_waits


def test_runtime_loop_reconciles_immediately_and_periodically() -> None:
    class Reconciler:
        calls = 0

        def reconcile_all(self):
            self.calls += 1
            return (_activation(self.calls),)

    reconciler = Reconciler()
    stop = _StopSignal(stop_after_waits=2)
    loop = IndexJobRuntimeReconciliationLoop(
        reconciler,
        poll_interval_ms=250,
    )

    assert loop.run(stop) == IndexJobRuntimeLoopSummary(
        pass_count=2,
        failure_count=0,
        activation_count=2,
    )
    assert reconciler.calls == 2
    assert stop.waits == [0.25, 0.25]


def test_runtime_loop_retries_sanitized_activation_failures() -> None:
    failure = IndexJobActivationError("durable current result unavailable")

    class Reconciler:
        calls = 0

        def reconcile_all(self):
            self.calls += 1
            if self.calls == 1:
                raise failure
            return (_activation(1),)

    failures = []
    stop = _StopSignal(stop_after_waits=2)
    loop = IndexJobRuntimeReconciliationLoop(
        Reconciler(),
        poll_interval_ms=100,
        on_failure=failures.append,
    )

    assert loop.run(stop) == IndexJobRuntimeLoopSummary(2, 1, 1)
    assert failures == [failure]
    assert stop.waits == [0.1, 0.1]


def test_runtime_loop_stops_before_catalog_access() -> None:
    class Reconciler:
        def reconcile_all(self):
            pytest.fail("stopped runtime loop reached the catalog")

    stop = Event()
    stop.set()

    assert IndexJobRuntimeReconciliationLoop(Reconciler()).run(stop) == (
        IndexJobRuntimeLoopSummary(0, 0, 0)
    )


@pytest.mark.parametrize("returned", (None, [], (object(),)))
def test_runtime_loop_rejects_invalid_reconciler_results(returned) -> None:
    class Reconciler:
        def reconcile_all(self):
            return returned

    with pytest.raises(IndexJobActivationError, match="invalid activations"):
        IndexJobRuntimeReconciliationLoop(Reconciler()).run(
            Event(),
            max_passes=1,
        )


def test_runtime_loop_rejects_concurrent_runs() -> None:
    entered = Barrier(2)
    release = Event()

    class Reconciler:
        def reconcile_all(self):
            entered.wait()
            release.wait()
            return ()

    loop = IndexJobRuntimeReconciliationLoop(Reconciler())
    errors = []

    def run() -> None:
        try:
            loop.run(Event(), max_passes=1)
        except BaseException as exc:  # noqa: B036 - asserted by the test
            errors.append(exc)

    thread = Thread(target=run)
    thread.start()
    entered.wait()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            loop.run(Event(), max_passes=1)
    finally:
        release.set()
        thread.join()
    assert errors == []
