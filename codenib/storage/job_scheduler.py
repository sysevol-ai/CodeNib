# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Cursor-fair scheduling for bounded durable index-job worker pages."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .job_worker import (
    IndexJobWorkerDisposition,
    IndexJobWorkerPageResult,
    IndexJobWorkerRunResult,
)
from .models import (
    IndexJobRunnableCursor,
    IndexJobRunnableCycle,
    StorageIntegrityError,
    StorageValidationError,
)

_MAX_SCHEDULER_DELAY_MS = 86_400_000
_MAX_SCHEDULER_COUNT = 2**63 - 1


def _exact_positive_integer(value: object, field: str, *, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise StorageValidationError(
            f"{field} must be an exact integer between 1 and {maximum}"
        )
    return value


def _exact_nonnegative_count(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SCHEDULER_COUNT:
        raise StorageValidationError(
            f"{field} must be an exact non-negative signed 64-bit integer"
        )
    return value


def _attest_page_result(value: object) -> IndexJobWorkerPageResult:
    if type(value) is not IndexJobWorkerPageResult:
        raise StorageIntegrityError("scheduler worker returned a non-exact page result")
    try:
        run = value.result
        normalized_run = IndexJobWorkerRunResult(
            run.disposition,
            run.job_id,
            run.attempt_count,
        )
        cursor = value.continuation_cursor
        normalized_cursor = (
            None
            if cursor is None
            else IndexJobRunnableCursor(cursor.created_at_ms, cursor.job_id)
        )
        normalized = IndexJobWorkerPageResult(normalized_run, normalized_cursor)
    except (AttributeError, StorageValidationError) as exc:
        raise StorageIntegrityError(
            "scheduler worker returned an invalid page result"
        ) from exc
    if normalized != value:
        raise StorageIntegrityError(
            "scheduler worker returned a noncanonical page result"
        )
    return normalized


def _attest_cycle(value: object) -> IndexJobRunnableCycle:
    if type(value) is not IndexJobRunnableCycle:
        raise StorageIntegrityError("scheduler worker returned a non-exact cycle")
    try:
        normalized = IndexJobRunnableCycle(value.max_job_sequence)
    except (AttributeError, StorageValidationError) as exc:
        raise StorageIntegrityError(
            "scheduler worker returned an invalid cycle"
        ) from exc
    if normalized != value:
        raise StorageIntegrityError("scheduler worker returned a noncanonical cycle")
    return normalized


@runtime_checkable
class IndexJobPageWorker(Protocol):
    """Freeze and traverse bounded worker pages using one cycle watermark."""

    def begin_cycle(self) -> IndexJobRunnableCycle: ...

    def run_page(
        self,
        *,
        cursor: IndexJobRunnableCursor | None = None,
        cycle: IndexJobRunnableCycle,
    ) -> IndexJobWorkerPageResult: ...


@runtime_checkable
class IndexJobSchedulerStopSignal(Protocol):
    """Threading-event-shaped cooperative scheduler stop signal."""

    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


@dataclass(frozen=True, slots=True)
class IndexJobSchedulerRunSummary:
    """Bounded counters returned after cooperative or configured shutdown."""

    page_count: int
    cycle_count: int
    job_count: int

    def __post_init__(self) -> None:
        if type(self) is not IndexJobSchedulerRunSummary:
            raise StorageValidationError("scheduler summary must use the exact model")
        pages = _exact_nonnegative_count(self.page_count, "scheduler page count")
        cycles = _exact_nonnegative_count(self.cycle_count, "scheduler cycle count")
        jobs = _exact_nonnegative_count(self.job_count, "scheduler job count")
        if cycles > pages or jobs > pages:
            raise StorageValidationError(
                "scheduler cycles and jobs cannot exceed its page count"
            )
        object.__setattr__(self, "page_count", pages)
        object.__setattr__(self, "cycle_count", cycles)
        object.__setattr__(self, "job_count", jobs)


class IndexJobWorkerScheduler:
    """Traverse frozen worker cycles with fair cursor wrap and idle backoff."""

    def __init__(
        self,
        *,
        worker: IndexJobPageWorker,
        initial_idle_delay_ms: int = 250,
        max_idle_delay_ms: int = 5_000,
        on_result: Callable[[IndexJobWorkerRunResult], None] | None = None,
    ) -> None:
        if not isinstance(worker, IndexJobPageWorker):
            raise TypeError("scheduler worker does not implement its protocol")
        initial_delay = _exact_positive_integer(
            initial_idle_delay_ms,
            "scheduler initial idle delay",
            maximum=_MAX_SCHEDULER_DELAY_MS,
        )
        maximum_delay = _exact_positive_integer(
            max_idle_delay_ms,
            "scheduler maximum idle delay",
            maximum=_MAX_SCHEDULER_DELAY_MS,
        )
        if maximum_delay < initial_delay:
            raise StorageValidationError(
                "scheduler maximum idle delay cannot be below its initial delay"
            )
        if on_result is not None and not callable(on_result):
            raise TypeError("scheduler result callback must be callable")
        self._worker = worker
        self._initial_idle_delay_ms = initial_delay
        self._max_idle_delay_ms = maximum_delay
        self._on_result = on_result
        self._run_lock = threading.Lock()

    def run(
        self,
        stop_signal: IndexJobSchedulerStopSignal,
        *,
        max_cycles: int | None = None,
    ) -> IndexJobSchedulerRunSummary:
        """Run until stopped or after complete frozen-keyspace cycles."""

        if not isinstance(stop_signal, IndexJobSchedulerStopSignal):
            raise TypeError("scheduler stop signal does not implement its protocol")
        cycle_limit = (
            None
            if max_cycles is None
            else _exact_positive_integer(
                max_cycles,
                "scheduler cycle limit",
                maximum=_MAX_SCHEDULER_COUNT,
            )
        )
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("index job scheduler is already running")
        try:
            return self._run_locked(stop_signal, cycle_limit)
        finally:
            self._run_lock.release()

    def _run_locked(
        self,
        stop_signal: IndexJobSchedulerStopSignal,
        cycle_limit: int | None,
    ) -> IndexJobSchedulerRunSummary:
        cursor: IndexJobRunnableCursor | None = None
        cycle: IndexJobRunnableCycle | None = None
        page_count = 0
        cycle_count = 0
        job_count = 0
        cycle_has_job = False
        idle_delay_ms = self._initial_idle_delay_ms

        while not self._is_stopped(stop_signal):
            if cycle is None:
                cycle = _attest_cycle(self._worker.begin_cycle())
            page = _attest_page_result(
                self._worker.run_page(cursor=cursor, cycle=cycle)
            )
            page_count = self._increment(page_count, "scheduler page count")
            result = page.result
            if result.disposition is not IndexJobWorkerDisposition.IDLE:
                job_count = self._increment(job_count, "scheduler job count")
                cycle_has_job = True
                if self._on_result is not None:
                    self._on_result(result)

            continuation = page.continuation_cursor
            if continuation is not None:
                if cursor is not None and (
                    continuation.created_at_ms,
                    continuation.job_id,
                ) <= (cursor.created_at_ms, cursor.job_id):
                    raise StorageIntegrityError(
                        "scheduler worker continuation did not advance"
                    )
                cursor = continuation
                continue

            cursor = None
            cycle = None
            cycle_count = self._increment(cycle_count, "scheduler cycle count")
            if cycle_limit is not None and cycle_count >= cycle_limit:
                break
            if cycle_has_job:
                cycle_has_job = False
                idle_delay_ms = self._initial_idle_delay_ms
                continue
            if self._wait_for_stop(stop_signal, idle_delay_ms / 1_000.0):
                break
            idle_delay_ms = min(
                idle_delay_ms * 2,
                self._max_idle_delay_ms,
            )

        return IndexJobSchedulerRunSummary(page_count, cycle_count, job_count)

    @staticmethod
    def _increment(value: int, field: str) -> int:
        if value >= _MAX_SCHEDULER_COUNT:
            raise StorageIntegrityError(f"{field} exhausted")
        return value + 1

    @staticmethod
    def _is_stopped(stop_signal: IndexJobSchedulerStopSignal) -> bool:
        try:
            stopped = stop_signal.is_set()
        except Exception as exc:
            raise StorageIntegrityError("scheduler stop-state check failed") from exc
        if type(stopped) is not bool:
            raise StorageIntegrityError(
                "scheduler stop-state check returned a non-exact decision"
            )
        return stopped

    @staticmethod
    def _wait_for_stop(
        stop_signal: IndexJobSchedulerStopSignal,
        timeout: float,
    ) -> bool:
        try:
            stopped = stop_signal.wait(timeout)
        except Exception as exc:
            raise StorageIntegrityError("scheduler stop wait failed") from exc
        if type(stopped) is not bool:
            raise StorageIntegrityError(
                "scheduler stop wait returned a non-exact decision"
            )
        return stopped


__all__ = [
    "IndexJobPageWorker",
    "IndexJobSchedulerRunSummary",
    "IndexJobSchedulerStopSignal",
    "IndexJobWorkerScheduler",
]
