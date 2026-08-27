# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Owned background lifecycle for durable Web index-job loops."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread, current_thread
from typing import Callable, Protocol, TypeVar, runtime_checkable

from .._owned_file_publication import _CancellationSafeRLock
from ..log_utils import get_logger
from ..storage import IndexJobSchedulerRunSummary, IndexJobSchedulerStopSignal
from .index_job_runtime import IndexJobRuntimeLoopSummary

logger = get_logger(__name__)
_LifecycleResult = TypeVar("_LifecycleResult")


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
    token: object
    summary: object | None = None
    failure: BaseException | None = None


class _ThreadLaunch:
    """Gate one non-daemon start behind a cancellation-safe daemon launcher."""

    def __init__(self, thread: Thread) -> None:
        self.thread = thread
        self._lock = _CancellationSafeRLock()
        self._complete = Event()
        self._state = "pending"
        self._failure: BaseException | None = None

    def _resolve_locked(self) -> None:
        if self._state != "pending":
            return
        try:
            self.thread.start()
        except BaseException as failure:  # noqa: B036 - publish launch result
            self._failure = failure
            self._state = "failed"
        else:
            self._state = "launched"

    def _launch(self) -> None:
        # SIGINT is delivered to the main thread, so the actual non-daemon
        # Thread.start runs to a definite result here. The lock spans that call:
        # cancellation either changes pending to cancelled first, or waits for
        # the launched/failed result before shared resources can be released.
        while True:
            try:
                self._lock.run(self._resolve_locked)
                break
            except BaseException:  # noqa: B036 - retry internal publication
                continue
        while True:
            try:
                self._complete.set()
                return
            except BaseException:  # noqa: B036 - retry internal publication
                continue

    def start(self) -> None:
        launcher = Thread(
            target=self._launch,
            name=f"{self.thread.name}-launcher",
            daemon=True,
        )
        launcher.start()
        completed = self._complete.wait()
        if type(completed) is not bool or not completed:
            raise IndexJobBackgroundServiceError(
                "thread launcher returned an invalid completion decision"
            )
        state, failure = self._lock.run(lambda: (self._state, self._failure))
        if state == "launched":
            return
        if state == "failed" and failure is not None:
            raise failure
        raise IndexJobBackgroundServiceError(
            "thread launcher completed without transferring ownership"
        )

    def cancel_and_was_launched(self) -> bool:
        """Cancel a pending launch or wait for an in-progress launch result."""

        def cancel() -> bool:
            if self._state == "pending":
                self._state = "cancelled"
            return self._state == "launched"

        return self._lock.run(cancel)


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
        # Start and close serialize here, but background health/state reads do
        # not. Joins therefore never hold the lifecycle lock a loop may read.
        self._settlement_lock = _CancellationSafeRLock()
        self._outcome_lock = _CancellationSafeRLock()
        self._state = "new"
        self._outcomes: dict[str, _LoopOutcome] = {}
        self._threads: tuple[_ThreadLaunch, ...] = ()
        self._start_token: object | None = None
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
        token = object()
        handoff_failure: BaseException | None = None

        def remember_handoff_failure(candidate: BaseException) -> None:
            nonlocal handoff_failure
            if handoff_failure is None:
                handoff_failure = candidate

        # A clean summary is only valid after the shared stop was already
        # observed. Reasserting it for every terminal outcome closes the
        # fault-to-peer handoff before any interruptible outcome bookkeeping.
        while True:
            try:
                self._stop.set()
                break
            except BaseException as stop_failure:  # noqa: B036 - retry handoff
                remember_handoff_failure(stop_failure)

        def record() -> bool:
            effective_failure = failure if failure is not None else handoff_failure
            outcome = _LoopOutcome(
                token=token,
                summary=None if effective_failure is not None else summary,
                failure=effective_failure,
            )
            existing = self._outcomes.get(name)
            if existing is not None and existing.token is not token:
                self._outcomes[name] = _LoopOutcome(
                    token=object(),
                    failure=IndexJobBackgroundServiceError(
                        "background index loop reported more than one outcome"
                    ),
                )
                return False
            # Replacing our own token is idempotent when lock release was
            # interrupted after the first callback committed. It also upgrades
            # a clean summary to the bookkeeping interruption on retry.
            self._outcomes[name] = outcome
            return True

        while True:
            try:
                recorded = self._outcome_lock.run(record)
                break
            except BaseException as record_failure:  # noqa: B036 - retry commit
                remember_handoff_failure(record_failure)

        if not recorded:
            return
        effective_failure = failure if failure is not None else handoff_failure
        if effective_failure is None:
            return
        try:
            logger.error(
                "Durable index %s loop failed",
                name,
                exc_info=(
                    type(effective_failure),
                    effective_failure,
                    effective_failure.__traceback__,
                ),
            )
        except Exception:
            # The shared stop and retained outcome are authoritative; logging
            # is best effort.
            pass

    def _stop_and_join(
        self,
        launches: tuple[_ThreadLaunch, ...],
    ) -> BaseException | None:
        """Replay settlement until every transferred non-daemon thread joins."""

        first_failure: BaseException | None = None
        while True:
            try:
                self._stop.set()
                owned_current = current_thread()
                for launch in reversed(launches):
                    thread = launch.thread
                    if thread is owned_current:
                        raise IndexJobBackgroundServiceError(
                            "an owned index loop cannot join itself"
                        )
                    if not launch.cancel_and_was_launched():
                        continue
                    thread.join()
                    if thread.is_alive():
                        raise IndexJobBackgroundServiceError(
                            "an owned index loop remained active after join"
                        )
                return first_failure
            except BaseException as failure:  # noqa: B036 - replay whole settle
                if first_failure is None:
                    first_failure = failure

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

    def _build_launches(self) -> tuple[_ThreadLaunch, ...]:
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
        return (
            _ThreadLaunch(runtime_thread),
            _ThreadLaunch(worker_thread),
        )

    def _claim_start_locked(
        self,
        token: object,
        launches: tuple[_ThreadLaunch, ...],
    ) -> bool:
        if self._state != "new" or self._start_token is not None:
            return False
        # Retain the candidates before publishing ownership. An interruption
        # before the token write is harmless because no launch was attempted;
        # after it, the caller can identify and settle this exact start call.
        self._threads = launches
        self._start_token = token
        self._state = "starting"
        return True

    def _finish_start_locked(self, token: object) -> bool:
        if self._start_token is not token:
            return False
        if self._state == "running":
            return True
        if self._state != "starting":
            return False
        self._state = "running"
        return True

    def _begin_failed_start_locked(
        self,
        token: object,
        failure: BaseException,
    ) -> tuple[bool, bool, tuple[_ThreadLaunch, ...]]:
        if self._start_token is not token:
            return False, False, ()
        if self._terminal_failure is None:
            self._terminal_failure = failure
        if self._state == "closed":
            return True, False, ()
        self._state = "stopping"
        return True, True, self._threads

    def _finish_failed_start_locked(self, token: object) -> None:
        if self._start_token is token:
            self._state = "closed"

    @staticmethod
    def _add_cleanup_note(primary: BaseException, cleanup: BaseException) -> None:
        if cleanup is primary:
            return
        add_note = getattr(primary, "add_note", None)
        if callable(add_note):
            add_note(
                "background service startup cleanup also failed: "
                f"{type(cleanup).__name__}"
            )

    def _lifecycle_retry(
        self,
        callback: Callable[[], _LifecycleResult],
        primary: BaseException,
    ) -> _LifecycleResult:
        while True:
            try:
                return self._lifecycle_lock.run(callback)
            except BaseException as transition_failure:  # noqa: B036 - retry state
                self._add_cleanup_note(primary, transition_failure)

    def _settle_owned_start_failure(
        self,
        token: object,
        primary: BaseException,
    ) -> bool:
        owned, needs_join, launches = self._lifecycle_retry(
            lambda: self._begin_failed_start_locked(token, primary),
            primary,
        )
        if not owned:
            return False
        if needs_join:
            cleanup_failure = self._stop_and_join(launches)
            if cleanup_failure is not None:
                self._add_cleanup_note(primary, cleanup_failure)
            self._lifecycle_retry(
                lambda: self._finish_failed_start_locked(token),
                primary,
            )
        return True

    def _start_serialized(self, token: object) -> None:
        launches = self._build_launches()
        try:
            claimed = self._lifecycle_lock.run(
                lambda: self._claim_start_locked(token, launches)
            )
            if not claimed:
                raise RuntimeError("background index service can start only once")
            for launch in launches:
                launch.start()
            published = self._lifecycle_lock.run(
                lambda: self._finish_start_locked(token)
            )
            if not published:
                raise RuntimeError("background index service start state changed")
        except BaseException as failure:  # noqa: B036 - join partial start
            self._settle_owned_start_failure(token, failure)
            raise

    def start(self) -> None:
        """Start the runtime thread before the worker execution thread."""

        token = object()
        try:
            self._settlement_lock.run(lambda: self._start_serialized(token))
        except BaseException as failure:  # noqa: B036 - recover lock-release window
            primary = failure
            while True:
                try:
                    owned = self._settlement_lock.run(
                        lambda: self._settle_owned_start_failure(token, primary)
                    )
                    break
                except BaseException as cleanup_failure:  # noqa: B036 - retry settle
                    self._add_cleanup_note(primary, cleanup_failure)
            if not owned:
                raise
            raise IndexJobBackgroundServiceError(
                "background index service could not start"
            ) from primary

    def _begin_close_locked(self) -> tuple[bool, tuple[_ThreadLaunch, ...]]:
        if self._state == "new":
            self._state = "closed"
            self._summary = IndexJobBackgroundServiceSummary(
                worker=IndexJobSchedulerRunSummary(0, 0, 0),
                runtime=IndexJobRuntimeLoopSummary(0, 0, 0),
            )
            return False, ()
        if self._state in {"running", "starting", "stopping"}:
            self._state = "stopping"
            return True, self._threads
        if self._state != "closed":
            raise RuntimeError("background index service state is invalid")
        return False, ()

    def _closed_result_locked(self) -> IndexJobBackgroundServiceSummary:
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

    def _finish_close_locked(self) -> None:
        self._state = "closed"

    def _close_serialized(self) -> IndexJobBackgroundServiceSummary:
        interruption: BaseException | None = None

        def remember(candidate: BaseException) -> None:
            nonlocal interruption
            if interruption is None:
                interruption = candidate

        while True:
            try:
                needs_join, launches = self._lifecycle_lock.run(
                    self._begin_close_locked
                )
                break
            except BaseException as state_failure:  # noqa: B036 - retry transition
                remember(state_failure)
        if needs_join:
            settlement_failure = self._stop_and_join(launches)
            if settlement_failure is not None:
                remember(settlement_failure)
            while True:
                try:
                    self._lifecycle_lock.run(self._finish_close_locked)
                    break
                except BaseException as state_failure:  # noqa: B036 - retry publish
                    remember(state_failure)
        if interruption is not None:
            raise interruption
        return self._lifecycle_lock.run(self._closed_result_locked)

    def _called_from_owned_thread(self) -> bool:
        current = current_thread()
        return self._lifecycle_lock.run(
            lambda: any(launch.thread is current for launch in self._threads)
        )

    def close(self) -> IndexJobBackgroundServiceSummary:
        """Stop and join both loops before shared resources may be released."""

        if self._called_from_owned_thread():
            raise IndexJobBackgroundServiceError(
                "background index service cannot close from an owned loop"
            )
        return self._settlement_lock.run(self._close_serialized)


__all__ = [
    "IndexJobActivationLoop",
    "IndexJobBackgroundService",
    "IndexJobBackgroundServiceError",
    "IndexJobBackgroundServiceSummary",
    "IndexJobWorkerLoop",
]
