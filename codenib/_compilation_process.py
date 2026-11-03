# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Cancel compiler subprocesses before their build releases its lock."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

_cancelled: ContextVar[Callable[[], bool] | None] = ContextVar(
    "compilation_cancelled", default=None
)


class CompilationCancelled(BaseException):
    """Unwind the build instead of entering a provider's failure fallback."""


def check_compilation_cancelled() -> None:
    cancelled = _cancelled.get()
    if cancelled is not None and cancelled():
        raise CompilationCancelled("Graph preparation cancelled")


@contextmanager
def compilation_cancellation(cancelled: Callable[[], bool]) -> Iterator[None]:
    token = _cancelled.set(cancelled)
    try:
        check_compilation_cancelled()
        yield
        check_compilation_cancelled()
    finally:
        _cancelled.reset(token)


def run_compilation_process(cmd, *, timeout, check=True, **popen_kwargs):
    """Reap one process group before cancellation unwinds the compiler lock.

    The cancellation scope is local to the build thread. Synchronous callers
    retain the original single-communicate timeout behavior.
    """
    check_compilation_cancelled()
    if popen_kwargs.pop("capture_output", False):
        if "stdout" in popen_kwargs or "stderr" in popen_kwargs:
            raise ValueError("capture_output cannot be combined with stdout/stderr")
        popen_kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with subprocess.Popen(
        cmd, start_new_session=os.name == "posix", **popen_kwargs
    ) as proc:
        try:
            if _cancelled.get() is None:
                stdout, stderr = proc.communicate(timeout=timeout)
            else:
                deadline = time.monotonic() + timeout
                while True:
                    check_compilation_cancelled()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(cmd, timeout)
                    try:
                        stdout, stderr = proc.communicate(timeout=min(0.1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired(cmd, timeout) from None
        except BaseException:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:  # pragma: no cover - Windows runner
                    proc.kill()
            except ProcessLookupError:
                pass
            proc.communicate()
            raise
        retcode = proc.poll()
        if check and retcode:
            raise subprocess.CalledProcessError(
                retcode, cmd, output=stdout, stderr=stderr
            )
        return subprocess.CompletedProcess(cmd, retcode, stdout, stderr)
