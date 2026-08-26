# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit coverage for cursor-fair durable index-job scheduling."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from codenib.storage.job_scheduler import (
    IndexJobSchedulerRunSummary,
    IndexJobWorkerScheduler,
)
from codenib.storage.job_worker import (
    IndexJobWorkerDisposition,
    IndexJobWorkerPageResult,
    IndexJobWorkerRunResult,
)
from codenib.storage.models import (
    IndexJobRunnableCursor,
    StorageIntegrityError,
    StorageValidationError,
)


def _cursor(sequence: int) -> IndexJobRunnableCursor:
    return IndexJobRunnableCursor(sequence, f"job-{sequence}")


def _idle(cursor: IndexJobRunnableCursor | None) -> IndexJobWorkerPageResult:
    return IndexJobWorkerPageResult(IndexJobWorkerRunResult.idle(), cursor)


def _succeeded(sequence: int) -> IndexJobWorkerPageResult:
    return IndexJobWorkerPageResult(
        IndexJobWorkerRunResult(
            IndexJobWorkerDisposition.SUCCEEDED,
            f"job-{sequence}",
            1,
        ),
        _cursor(sequence),
    )


@dataclass
class _ScriptedWorker:
    pages: list[object]
    cursors: list[IndexJobRunnableCursor | None] = field(default_factory=list)

    def run_page(
        self,
        *,
        cursor: IndexJobRunnableCursor | None = None,
    ) -> IndexJobWorkerPageResult:
        self.cursors.append(cursor)
        if not self.pages:
            raise AssertionError("scheduler requested an unexpected page")
        page = self.pages.pop(0)
        if isinstance(page, BaseException):
            raise page
        return page  # type: ignore[return-value]


@dataclass
class _StopSignal:
    stopped: bool = False
    stop_on_wait: int | None = None
    waits: list[float] = field(default_factory=list)

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None
        self.waits.append(timeout)
        if self.stop_on_wait is not None and len(self.waits) >= self.stop_on_wait:
            self.stopped = True
        return self.stopped


def test_scheduler_traverses_all_pages_before_completing_one_cycle() -> None:
    first = _cursor(1)
    second = _cursor(2)
    third = _cursor(3)
    worker = _ScriptedWorker(
        [
            _idle(first),
            _idle(second),
            _succeeded(3),
            _idle(None),
        ]
    )
    stop = _StopSignal()
    observed: list[IndexJobWorkerRunResult] = []
    scheduler = IndexJobWorkerScheduler(
        worker=worker,
        initial_idle_delay_ms=100,
        max_idle_delay_ms=400,
        on_result=observed.append,
    )

    summary = scheduler.run(stop, max_cycles=1)

    assert summary == IndexJobSchedulerRunSummary(4, 1, 1)
    assert worker.cursors == [None, first, second, third]
    assert [result.job_id for result in observed] == ["job-3"]
    assert stop.waits == []


def test_scheduler_backs_off_only_after_complete_idle_cycles() -> None:
    worker = _ScriptedWorker([_idle(None) for _ in range(4)])
    stop = _StopSignal()
    scheduler = IndexJobWorkerScheduler(
        worker=worker,
        initial_idle_delay_ms=100,
        max_idle_delay_ms=400,
    )

    summary = scheduler.run(stop, max_cycles=4)

    assert summary == IndexJobSchedulerRunSummary(4, 4, 0)
    assert stop.waits == pytest.approx([0.1, 0.2, 0.4])


def test_scheduler_resets_backoff_after_a_cycle_processes_work() -> None:
    worker = _ScriptedWorker(
        [
            _idle(None),
            _succeeded(1),
            _idle(None),
            _idle(None),
            _idle(None),
        ]
    )
    stop = _StopSignal()
    scheduler = IndexJobWorkerScheduler(
        worker=worker,
        initial_idle_delay_ms=100,
        max_idle_delay_ms=800,
    )

    summary = scheduler.run(stop, max_cycles=4)

    assert summary == IndexJobSchedulerRunSummary(5, 4, 1)
    assert stop.waits == pytest.approx([0.1, 0.1])


def test_scheduler_honors_preexisting_and_wait_time_stop_requests() -> None:
    stopped_worker = _ScriptedWorker([])
    stopped = _StopSignal(stopped=True)
    scheduler = IndexJobWorkerScheduler(worker=stopped_worker)

    assert scheduler.run(stopped) == IndexJobSchedulerRunSummary(0, 0, 0)
    assert stopped_worker.cursors == []

    waiting_worker = _ScriptedWorker([_idle(None)])
    waiting = _StopSignal(stop_on_wait=1)
    scheduler = IndexJobWorkerScheduler(
        worker=waiting_worker,
        initial_idle_delay_ms=250,
    )
    assert scheduler.run(waiting) == IndexJobSchedulerRunSummary(1, 1, 0)
    assert waiting.waits == pytest.approx([0.25])


def test_scheduler_propagates_worker_failure_without_idle_backoff() -> None:
    failure = StorageIntegrityError("catalog integrity alarm")
    worker = _ScriptedWorker([failure])
    stop = _StopSignal()
    scheduler = IndexJobWorkerScheduler(worker=worker)

    with pytest.raises(StorageIntegrityError) as raised:
        scheduler.run(stop)

    assert raised.value is failure
    assert stop.waits == []


def test_scheduler_rejects_nonadvancing_worker_continuation() -> None:
    cursor = _cursor(2)
    worker = _ScriptedWorker([_idle(cursor), _idle(cursor)])
    scheduler = IndexJobWorkerScheduler(worker=worker)

    with pytest.raises(StorageIntegrityError, match="did not advance"):
        scheduler.run(_StopSignal())

    assert worker.cursors == [None, cursor]


@pytest.mark.parametrize(
    ("initial_delay", "maximum_delay"),
    ((0, 1), (True, 1), (2, 1), (1, 86_400_001)),
)
def test_scheduler_rejects_invalid_idle_delay_bounds(
    initial_delay: object,
    maximum_delay: object,
) -> None:
    with pytest.raises(StorageValidationError):
        IndexJobWorkerScheduler(
            worker=_ScriptedWorker([]),
            initial_idle_delay_ms=initial_delay,  # type: ignore[arg-type]
            max_idle_delay_ms=maximum_delay,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("cycle_limit", (0, True, 2**63))
def test_scheduler_rejects_invalid_cycle_limits(cycle_limit: object) -> None:
    scheduler = IndexJobWorkerScheduler(worker=_ScriptedWorker([]))

    with pytest.raises(StorageValidationError):
        scheduler.run(
            _StopSignal(stopped=True),
            max_cycles=cycle_limit,  # type: ignore[arg-type]
        )
