# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Background graph preparation for an on-demand MCP connection.

With ``--defer-graph`` the server starts from BM25 alone. Preparing the
symbol graph means installing language providers and running the compiler,
which can take minutes on a large repository. Doing that inside the first
graph query made that query the slowest call an agent ever saw. This module
runs the same preparation in one background thread instead, so the build
overlaps the agent's own reading and an exploration call never waits on it
for longer than a short, explicit bound.

The thread runs :func:`codenib.codegraph_preparation.prepare_graph`, which
keeps its checkout guard and the compiler's cache lock. It publishes only
through the compiler's manifest; the MCP connection picks the graph up on its
next call through the normal reload path. One source fingerprint is prepared
at most once per connection: a failure stays recorded until the indexed
source changes.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .._compilation_process import CompilationCancelled, compilation_cancellation
from ..repository_source_selection import RepositorySourceSelection

logger = logging.getLogger(__name__)

IDLE = "idle"
RUNNING = "running"
READY = "ready"
FAILED = "failed"
CANCELLED = "cancelled"

_MAX_ERROR_CHARS = 400


@dataclass(frozen=True, slots=True)
class GraphTarget:
    """The exact indexed source one preparation must match."""

    repository: Path
    languages: tuple[str, ...]
    source_selection: RepositorySourceSelection
    source_fingerprint: str
    cache_dir: Path | None = None


def _default_prepare(target: GraphTarget) -> None:
    from ..codegraph_preparation import prepare_graph

    prepare_graph(
        target.repository,
        languages=list(target.languages),
        source_selection=target.source_selection,
        expected_source_fingerprint=target.source_fingerprint,
        cache_dir=target.cache_dir,
    )


class GraphWarmup:
    """Cancel and join the connection's build before releasing its owner.

    Close linearizes under ``_condition`` when ``_closed`` is set; no new
    worker can start afterward. Join runs outside that condition so the
    worker can report completion. Its subprocess is reaped before its build
    releases the compiler lock. The server may hold its context lock while
    closing; the worker never acquires that lock.
    """

    def __init__(self, prepare: Callable[[GraphTarget], None] = _default_prepare):
        self._prepare = prepare
        self._condition = threading.Condition()
        self._state = IDLE
        self._target: GraphTarget | None = None
        self._error: str | None = None
        self._started: float | None = None
        self._finished: float | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def state(self) -> str:
        with self._condition:
            return self._state

    def running(self) -> bool:
        return self.state == RUNNING

    def start(self, target: GraphTarget, *, force: bool = False) -> str:
        """Start preparing *target* unless that source is already handled.

        Returns the state after the call. A running build is never replaced.
        Without *force*, a finished or failed build for the same fingerprint
        is not repeated; a different fingerprint (the index was rebuilt)
        starts a new build. *force* rebuilds whenever nothing is running.
        """

        with self._condition:
            if self._closed:
                raise RuntimeError("Graph preparation owner is closed")
            if self._state == RUNNING:
                return self._state
            same_source = (
                self._target is not None
                and self._target.source_fingerprint == target.source_fingerprint
            )
            if same_source and not force and self._state in (READY, FAILED):
                return self._state
            self._state = RUNNING
            self._target = target
            self._error = None
            self._started = time.monotonic()
            self._finished = None
            self._thread = threading.Thread(
                target=self._run,
                args=(target,),
                name="codenib-graph-warmup",
                daemon=False,
            )
            self._thread.start()
            return self._state

    def _run(self, target: GraphTarget) -> None:
        logger.info("Preparing the symbol graph in the background")
        try:
            with compilation_cancellation(self._stop.is_set):
                self._prepare(target)
        except CompilationCancelled as exc:
            outcome, error = CANCELLED, str(exc)
        except BaseException as exc:  # noqa: B036 - record every failure
            message = " ".join(str(exc).split()) or type(exc).__name__
            outcome, error = FAILED, message[:_MAX_ERROR_CHARS]
            logger.warning("Background graph preparation failed: %s", error)
        else:
            outcome, error = READY, None
            logger.info("Background graph preparation finished")
        with self._condition:
            self._state = outcome
            self._error = error
            self._finished = time.monotonic()
            self._condition.notify_all()

    def close(self) -> None:
        """Cancel subprocesses and join before returning ownership."""
        with self._condition:
            self._closed = True
            self._stop.set()
            worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join()

    def wait(self, timeout: float | None) -> str:
        """Wait up to *timeout* seconds (forever for ``None``) for an outcome."""

        with self._condition:
            self._condition.wait_for(lambda: self._state != RUNNING, timeout)
            return self._state

    def status(self) -> dict[str, Any]:
        """Describe the current build for a diagnostic."""

        with self._condition:
            now = time.monotonic()
            started = self._started
            finished = self._finished if self._finished is not None else now
            return {
                "state": self._state,
                "elapsed_seconds": (
                    round(finished - started, 1) if started is not None else None
                ),
                "error": self._error,
            }


__all__ = [
    "CANCELLED",
    "FAILED",
    "IDLE",
    "READY",
    "RUNNING",
    "GraphTarget",
    "GraphWarmup",
]
