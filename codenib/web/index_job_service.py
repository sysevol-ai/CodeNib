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


@dataclass(slots=True)
class _ThreadLaunch:
    """Retain one candidate before launch and its child-entry handshake."""

    thread: Thread
    entered: Event
    attempted: bool = False
    start_returned: bool = False
    start_failure: BaseException | None = None

    def start(self) -> None:
        self.attempted = True
        try:
            self.thread.start()
        except BaseException as failure:  # noqa: B036 - retain ambiguous launch
            self.start_failure = failure
            raise
        self.start_returned = True


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

    def _stop_and_join(self, launches: tuple[_ThreadLaunch, ...]) -> None:
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
        owned_current = current_thread()
        for launch in reversed(launches):
            thread = launch.thread
            if thread is owned_current:
                raise IndexJobBackgroundServiceError(
                    "an owned index loop cannot join itself"
                )
            if not launch.attempted:
                continue

            launched = False
            while True:
                try:
                    entered = launch.entered.is_set()
                except BaseException as failure:  # noqa: B036 - retry state read
                    if first_failure is None:
                        first_failure = failure
                    continue
                if entered or launch.start_returned:
                    launched = True
                    break
                try:
                    identified = thread.ident is not None
                except BaseException as failure:  # noqa: B036 - retry state read
                    if first_failure is None:
                        first_failure = failure
                    continue
                try:
                    alive = thread.is_alive()
                except BaseException as failure:  # noqa: B036 - retry state read
                    if first_failure is None:
                        first_failure = failure
                    continue
                if identified or alive:
                    launched = True
                    break
                start_failure = launch.start_failure
                if start_failure is not None and isinstance(start_failure, Exception):
                    # CPython's ordinary start failures occur before ownership
                    # transfers to an OS thread. Cancellation-class failures
                    # remain ambiguous and must wait for the child handshake.
                    break
                try:
                    signaled = launch.entered.wait(0.05)
                except BaseException as failure:  # noqa: B036 - await ownership
                    if first_failure is None:
                        first_failure = failure
                    continue
                if type(signaled) is not bool:
                    if first_failure is None:
                        first_failure = IndexJobBackgroundServiceError(
                            "thread launch handshake returned an invalid decision"
                        )
                    continue
                if signaled:
                    launched = True
                    break
            if not launched:
                continue
            while True:
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

    def _run_worker(self, entered: Event) -> None:
        try:
            entered.set()
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

    def _run_runtime(self, entered: Event) -> None:
        try:
            entered.set()
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
        runtime_entered = Event()
        worker_entered = Event()
        runtime_thread = Thread(
            target=lambda: self._run_runtime(runtime_entered),
            name="codenib-index-runtime",
            daemon=False,
        )
        worker_thread = Thread(
            target=lambda: self._run_worker(worker_entered),
            name="codenib-index-worker",
            daemon=False,
        )
        return (
            _ThreadLaunch(runtime_thread, runtime_entered),
            _ThreadLaunch(worker_thread, worker_entered),
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
            try:
                self._stop_and_join(launches)
            except BaseException as cleanup_failure:  # noqa: B036 - keep primary
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
            try:
                self._stop_and_join(launches)
            except BaseException as failure:  # noqa: B036 - settled before rethrow
                remember(failure)
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
