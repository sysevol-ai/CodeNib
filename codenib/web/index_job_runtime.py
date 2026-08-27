# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Retrying runtime reconciliation loop for durable Web index results."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Callable, Protocol, runtime_checkable

from ..storage import IndexJobSchedulerStopSignal
from .index_job_activation import (
    IndexJobActivationError,
    IndexJobReconciliationPassError,
    IndexJobRuntimeActivation,
)

_MAX_LOOP_COUNT = 2**63 - 1
_MAX_POLL_INTERVAL_MS = 86_400_000


def _positive_integer(value: object, label: str, *, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be an exact integer between 1 and {maximum}")
    return value


def _add_count(value: int, amount: int, label: str) -> int:
    if type(amount) is not int or amount < 0 or amount > _MAX_LOOP_COUNT - value:
        raise IndexJobActivationError(f"{label} exhausted")
    return value + amount


def _increment(value: int, label: str) -> int:
    return _add_count(value, 1, label)


@runtime_checkable
class IndexJobRuntimeReconciler(Protocol):
    """Reconcile every configured durable current result into Web runtime."""

    def reconcile_all(self) -> tuple[IndexJobRuntimeActivation, ...]: ...


@dataclass(frozen=True, slots=True)
class IndexJobRuntimeLoopSummary:
    """Bounded reconciliation counters after cooperative loop shutdown."""

    pass_count: int
    failure_count: int
    activation_count: int

    def __post_init__(self) -> None:
        if type(self) is not IndexJobRuntimeLoopSummary:
            raise TypeError("runtime loop summary must use the exact type")
        for value, label in (
            (self.pass_count, "runtime reconciliation pass count"),
            (self.failure_count, "runtime reconciliation failure count"),
            (self.activation_count, "runtime activation count"),
        ):
            if type(value) is not int or not 0 <= value <= _MAX_LOOP_COUNT:
                raise ValueError(f"{label} must be a non-negative integer")
        if self.failure_count > self.pass_count:
            raise ValueError("runtime reconciliation failures exceed passes")


class IndexJobRuntimeReconciliationLoop:
    """Poll durable current results and retry sanitized activation failures."""

    def __init__(
        self,
        reconciler: IndexJobRuntimeReconciler,
        *,
        poll_interval_ms: int = 1_000,
        on_failure: Callable[[IndexJobActivationError], None] | None = None,
    ) -> None:
        if not isinstance(reconciler, IndexJobRuntimeReconciler):
            raise TypeError("runtime loop requires a current-result reconciler")
        interval = _positive_integer(
            poll_interval_ms,
            "runtime reconciliation poll interval",
            maximum=_MAX_POLL_INTERVAL_MS,
        )
        if on_failure is not None and not callable(on_failure):
            raise TypeError("runtime reconciliation failure callback must be callable")
        self._reconciler = reconciler
        self._poll_interval_ms = interval
        self._on_failure = on_failure
        self._run_lock = Lock()

    @staticmethod
    def _is_stopped(stop_signal: IndexJobSchedulerStopSignal) -> bool:
        try:
            stopped = stop_signal.is_set()
        except Exception as exc:
            raise IndexJobActivationError(
                "runtime reconciliation stop-state check failed"
            ) from exc
        if type(stopped) is not bool:
            raise IndexJobActivationError(
                "runtime reconciliation stop-state check returned an invalid result"
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
            raise IndexJobActivationError(
                "runtime reconciliation stop wait failed"
            ) from exc
        if type(stopped) is not bool:
            raise IndexJobActivationError(
                "runtime reconciliation stop wait returned an invalid result"
            )
        return stopped

    @staticmethod
    def _attest_activations(
        value: object,
    ) -> tuple[IndexJobRuntimeActivation, ...]:
        if type(value) is not tuple or any(
            type(item) is not IndexJobRuntimeActivation for item in value
        ):
            raise IndexJobActivationError(
                "runtime reconciler returned invalid activations"
            )
        return value

    def _report_failure(self, failure: IndexJobActivationError) -> None:
        callback = self._on_failure
        if callback is None:
            return
        try:
            result = callback(failure)
        except Exception as exc:
            raise IndexJobActivationError(
                "runtime reconciliation failure callback failed"
            ) from exc
        if result is not None:
            raise IndexJobActivationError(
                "runtime reconciliation failure callback returned a value"
            )

    def run(
        self,
        stop_signal: IndexJobSchedulerStopSignal,
        *,
        max_passes: int | None = None,
    ) -> IndexJobRuntimeLoopSummary:
        """Run immediate and periodic reconciliation until cooperatively stopped."""

        if not isinstance(stop_signal, IndexJobSchedulerStopSignal):
            raise TypeError("runtime loop stop signal is invalid")
        pass_limit = (
            None
            if max_passes is None
            else _positive_integer(
                max_passes,
                "runtime reconciliation pass limit",
                maximum=_MAX_LOOP_COUNT,
            )
        )
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("runtime reconciliation loop is already running")
        try:
            return self._run_locked(stop_signal, pass_limit)
        finally:
            self._run_lock.release()

    def _run_locked(
        self,
        stop_signal: IndexJobSchedulerStopSignal,
        pass_limit: int | None,
    ) -> IndexJobRuntimeLoopSummary:
        pass_count = 0
        failure_count = 0
        activation_count = 0
        while not self._is_stopped(stop_signal):
            try:
                returned = self._reconciler.reconcile_all()
            except IndexJobActivationError as failure:
                if type(failure) is IndexJobReconciliationPassError:
                    activation_count = _add_count(
                        activation_count,
                        failure.completed_activation_count,
                        "runtime activation count",
                    )
                failure_count = _increment(
                    failure_count,
                    "runtime reconciliation failure count",
                )
                self._report_failure(failure)
            else:
                activations = self._attest_activations(returned)
                for _activation in activations:
                    activation_count = _increment(
                        activation_count,
                        "runtime activation count",
                    )
            pass_count = _increment(pass_count, "runtime reconciliation pass count")
            if pass_limit is not None and pass_count >= pass_limit:
                break
            if self._wait_for_stop(
                stop_signal,
                self._poll_interval_ms / 1_000.0,
            ):
                break
        return IndexJobRuntimeLoopSummary(
            pass_count=pass_count,
            failure_count=failure_count,
            activation_count=activation_count,
        )


__all__ = [
    "IndexJobRuntimeLoopSummary",
    "IndexJobRuntimeReconciler",
    "IndexJobRuntimeReconciliationLoop",
]
