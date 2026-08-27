# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Owned background lifecycle for durable Web index-job loops."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread
from typing import Protocol, runtime_checkable

from .._owned_file_publication import _CancellationSafeRLock
from ..log_utils import get_logger
from ..storage import IndexJobSchedulerRunSummary, IndexJobSchedulerStopSignal
from .index_job_runtime import IndexJobRuntimeLoopSummary

logger = get_logger(__name__)


class IndexJobBackgroundServiceError(RuntimeError):
    """The owned worker/runtime background service failed safely."""


@runtime_checkable
class IndexJobWorkerLoop(Protocol):
    """Run the configured durable job worker until cooperatively stopped."""

    def run(
        self,
        stop_signal: IndexJobSchedulerStopSignal,
    ) -> IndexJobSchedulerRunSummary: ...


@runtime_checkable
class IndexJobActivationLoop(Protocol):
    """Run current-result reconciliation until cooperatively stopped."""

    def run(
        self,
        stop_signal: IndexJobSchedulerStopSignal,
    ) -> IndexJobRuntimeLoopSummary: ...


@dataclass(frozen=True, slots=True)
class IndexJobBackgroundServiceSummary:
    """Terminal summaries from both cooperatively stopped loops."""

    worker: IndexJobSchedulerRunSummary
    runtime: IndexJobRuntimeLoopSummary

    def __post_init__(self) -> None:
        if type(self) is not IndexJobBackgroundServiceSummary:
            raise TypeError("background service summary must use the exact type")
        if type(self.worker) is not IndexJobSchedulerRunSummary:
            raise TypeError("background service worker summary is invalid")
        if type(self.runtime) is not IndexJobRuntimeLoopSummary:
            raise TypeError("background service runtime summary is invalid")


@dataclass(frozen=True, slots=True)
class _LoopOutcome:
    summary: object | None = None
    failure: BaseException | None = None


class IndexJobBackgroundService:
    """Own one worker loop and one runtime loop through ordered shutdown.

    Either loop fault stops its peer. ``close`` joins both non-daemon threads
    before returning or reporting a sanitized failure, so callers may then
    release the shared catalog, object store, and repository registry.
    """

    def __init__(
        self,
        worker: IndexJobWorkerLoop,
        runtime: IndexJobActivationLoop,
    ) -> None:
        if not isinstance(worker, IndexJobWorkerLoop):
            raise TypeError("background service requires a worker loop")
        if not isinstance(runtime, IndexJobActivationLoop):
            raise TypeError("background service requires a runtime loop")
        self._worker = worker
        self._runtime = runtime
        self._stop = Event()
        self._lifecycle_lock = _CancellationSafeRLock()
        self._outcome_lock = _CancellationSafeRLock()
        self._state = "new"
        self._outcomes: dict[str, _LoopOutcome] = {}
        self._threads: tuple[Thread, ...] = ()
        self._summary: IndexJobBackgroundServiceSummary | None = None
        self._terminal_failure: BaseException | None = None

    @property
    def state(self) -> str:
        """Return the exact lifecycle state for health and tests."""

        return self._lifecycle_lock.run(lambda: self._state)

    @property
    def healthy(self) -> bool:
        """Return whether no owned loop has reported a terminal fault."""

        def read() -> bool:
            terminal_failure = self._terminal_failure
            return self._outcome_lock.run(
                lambda: terminal_failure is None
                and not any(
                    outcome.failure is not None for outcome in self._outcomes.values()
                )
            )

        return self._lifecycle_lock.run(read)

    def _record_outcome(
        self,
        name: str,
        *,
        summary: object | None = None,
        failure: BaseException | None = None,
    ) -> None:
        def record() -> bool:
            if name in self._outcomes:
                self._outcomes[name] = _LoopOutcome(
                    failure=IndexJobBackgroundServiceError(
                        "background index loop reported more than one outcome"
                    )
                )
                return False
            self._outcomes[name] = _LoopOutcome(summary=summary, failure=failure)
            return True

        recorded = self._outcome_lock.run(record)
        if not recorded:
            self._stop.set()
            return
        if failure is not None:
            self._stop.set()
            try:
                logger.error(
                    "Durable index %s loop failed",
                    name,
                    exc_info=(type(failure), failure, failure.__traceback__),
                )
            except Exception:
                # The shared stop is authoritative; logging is best effort.
                pass

    def _stop_and_join(self, threads: tuple[Thread, ...]) -> None:
        """Settle every started thread before rethrowing an interruption."""

        first_failure: BaseException | None = None
        while True:
            try:
                stopped = self._stop.is_set()
            except BaseException as failure:  # noqa: B036 - retry state read
                if first_failure is None:
                    first_failure = failure
                continue
            if stopped:
                break
            try:
                self._stop.set()
            except BaseException as failure:  # noqa: B036 - settle before rethrow
                if first_failure is None:
                    first_failure = failure
        for thread in reversed(threads):
            while True:
                try:
                    started = thread.ident is not None
                except BaseException as failure:  # noqa: B036 - retry state read
                    if first_failure is None:
                        first_failure = failure
                    continue
                if not started:
                    break
                try:
                    alive = thread.is_alive()
                except BaseException as failure:  # noqa: B036 - retry state read
                    if first_failure is None:
                        first_failure = failure
                    continue
                if not alive:
                    break
                try:
                    thread.join()
                except BaseException as failure:  # noqa: B036 - settle all loops
                    if first_failure is None:
                        first_failure = failure
        if first_failure is not None:
            raise first_failure

    def _run_worker(self) -> None:
        try:
            summary = self._worker.run(self._stop)
            if type(summary) is not IndexJobSchedulerRunSummary:
                raise IndexJobBackgroundServiceError(
                    "background worker returned an invalid summary"
                )
            if not self._stop.is_set():
                raise IndexJobBackgroundServiceError(
                    "background worker exited before shutdown"
                )
        except BaseException as failure:  # noqa: B036 - retain thread fault
            self._record_outcome("worker", failure=failure)
        else:
            self._record_outcome("worker", summary=summary)

    def _run_runtime(self) -> None:
        try:
            summary = self._runtime.run(self._stop)
            if type(summary) is not IndexJobRuntimeLoopSummary:
                raise IndexJobBackgroundServiceError(
                    "background runtime returned an invalid summary"
                )
            if not self._stop.is_set():
                raise IndexJobBackgroundServiceError(
                    "background runtime exited before shutdown"
                )
        except BaseException as failure:  # noqa: B036 - retain thread fault
            self._record_outcome("runtime", failure=failure)
        else:
            self._record_outcome("runtime", summary=summary)

    def _start_locked(self) -> None:
        if self._state != "new":
            raise RuntimeError("background index service can start only once")
        self._state = "starting"
        runtime_thread = Thread(
            target=self._run_runtime,
            name="codenib-index-runtime",
            daemon=False,
        )
        worker_thread = Thread(
            target=self._run_worker,
            name="codenib-index-worker",
            daemon=False,
        )
        # Retain both candidate threads before the first launch. If an
        # asynchronous exception lands after ``Thread.start`` launches a
        # thread, cleanup must not depend on interruptible post-start
        # bookkeeping to remember that ownership.
        self._threads = (runtime_thread, worker_thread)
        try:
            runtime_thread.start()
            worker_thread.start()
        except BaseException as failure:  # noqa: B036 - join partial start
            self._terminal_failure = failure
            try:
                self._stop_and_join(self._threads)
            except BaseException as cleanup_failure:  # noqa: B036
                if cleanup_failure is not failure:
                    add_note = getattr(failure, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "background service startup cleanup also failed: "
                            f"{type(cleanup_failure).__name__}"
                        )
            self._state = "closed"
            raise IndexJobBackgroundServiceError(
                "background index service could not start"
            ) from failure
        self._state = "running"

    def start(self) -> None:
        """Start the runtime thread before the worker execution thread."""

        self._lifecycle_lock.run(self._start_locked)

    def _close_locked(self) -> IndexJobBackgroundServiceSummary:
        if self._state == "new":
            self._state = "closed"
            self._summary = IndexJobBackgroundServiceSummary(
                worker=IndexJobSchedulerRunSummary(0, 0, 0),
                runtime=IndexJobRuntimeLoopSummary(0, 0, 0),
            )
        elif self._state in {"running", "starting"}:
            self._state = "stopping"
            try:
                self._stop_and_join(self._threads)
            finally:
                self._state = "closed"
        elif self._state not in {"stopping", "closed"}:
            raise RuntimeError("background index service state is invalid")

        if self._summary is not None:
            return self._summary
        if self._terminal_failure is not None:
            raise IndexJobBackgroundServiceError(
                "background index service failed"
            ) from self._terminal_failure
        outcomes = self._outcome_lock.run(lambda: dict(self._outcomes))
        failure = next(
            (
                outcome.failure
                for name in ("runtime", "worker")
                if (outcome := outcomes.get(name)) is not None
                and outcome.failure is not None
            ),
            None,
        )
        if failure is not None:
            self._terminal_failure = failure
            raise IndexJobBackgroundServiceError(
                "background index service failed"
            ) from failure
        worker = outcomes.get("worker")
        runtime = outcomes.get("runtime")
        if worker is None or runtime is None:
            raise RuntimeError("background index service outcome is incomplete")
        self._summary = IndexJobBackgroundServiceSummary(
            worker=worker.summary,  # type: ignore[arg-type]
            runtime=runtime.summary,  # type: ignore[arg-type]
        )
        return self._summary

    def close(self) -> IndexJobBackgroundServiceSummary:
        """Stop and join both loops before shared resources may be released."""

        return self._lifecycle_lock.run(self._close_locked)


__all__ = [
    "IndexJobActivationLoop",
    "IndexJobBackgroundService",
    "IndexJobBackgroundServiceError",
    "IndexJobBackgroundServiceSummary",
    "IndexJobWorkerLoop",
]
