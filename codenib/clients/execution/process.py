# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shell-free, bounded asynchronous subprocess execution."""

from __future__ import annotations

import asyncio
import math
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence, runtime_checkable


class ProcessLaunchError(RuntimeError):
    """A process could not be created."""


class ExecutableNotFoundError(ProcessLaunchError):
    """The requested executable does not exist or cannot be resolved."""


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    argv: Sequence[str]
    cwd: Path
    stdin: bytes | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 300.0
    max_output_bytes: int = 8 * 1024**2

    def __post_init__(self) -> None:
        if isinstance(self.argv, (str, bytes)):
            raise TypeError("argv must be a sequence of strings")
        argv = tuple(self.argv)
        if not argv or any(
            not isinstance(value, str) or not value or "\x00" in value
            for value in argv
        ):
            raise ValueError("argv must contain non-empty, NUL-free strings")
        cwd = Path(self.cwd).expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError(f"cwd must be an existing directory: {cwd}")
        if self.stdin is not None and not isinstance(self.stdin, bytes):
            raise TypeError("stdin must be bytes when provided")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a finite positive number")
        if (
            isinstance(self.max_output_bytes, bool)
            or not isinstance(self.max_output_bytes, int)
            or self.max_output_bytes <= 0
        ):
            raise ValueError("max_output_bytes must be a positive integer")
        environment = dict(self.environment)
        for key, value in environment.items():
            if (
                not isinstance(key, str)
                or not key
                or "\x00" in key
                or "=" in key
                or not isinstance(value, str)
                or "\x00" in value
            ):
                raise ValueError(f"invalid process environment entry: {key!r}")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "environment", MappingProxyType(environment))


@dataclass(frozen=True, slots=True)
class ProcessResult:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@runtime_checkable
class ProcessRunner(Protocol):
    async def run(self, request: ProcessRequest) -> ProcessResult: ...


async def _read_bounded(
    stream: asyncio.StreamReader, limit: int
) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    retained = 0
    truncated = False
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        available = max(0, limit - retained)
        if available:
            kept = chunk[:available]
            chunks.append(kept)
            retained += len(kept)
        if len(chunk) > available:
            truncated = True
    return b"".join(chunks), truncated


async def _write_stdin(
    stream: asyncio.StreamWriter | None, content: bytes | None
) -> None:
    if stream is None:
        return
    try:
        if content:
            stream.write(content)
            await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stream.close()


async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Kill the isolated process group created for one agent invocation."""

    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - exercised on Windows runners
            process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


class LocalProcessRunner:
    """Run an argv directly on the host without invoking a shell."""

    async def run(self, request: ProcessRequest) -> ProcessResult:
        started = time.monotonic()
        environment = os.environ.copy()
        environment.update(request.environment)
        try:
            process = await asyncio.create_subprocess_exec(
                *request.argv,
                cwd=str(request.cwd),
                env=environment,
                stdin=(
                    asyncio.subprocess.PIPE
                    if request.stdin is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except FileNotFoundError as exc:
            raise ExecutableNotFoundError(str(exc)) from exc
        except OSError as exc:
            raise ProcessLaunchError(str(exc)) from exc

        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(
            _read_bounded(process.stdout, request.max_output_bytes)
        )
        stderr_task = asyncio.create_task(
            _read_bounded(process.stderr, request.max_output_bytes)
        )
        stdin_task = asyncio.create_task(_write_stdin(process.stdin, request.stdin))
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=request.timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            await _kill_process_tree(process)
        except asyncio.CancelledError:
            await _kill_process_tree(process)
            await asyncio.gather(stdout_task, stderr_task, stdin_task)
            raise

        stdout_result, stderr_result, _ = await asyncio.gather(
            stdout_task, stderr_task, stdin_task
        )
        stdout, stdout_truncated = stdout_result
        stderr, stderr_truncated = stderr_result
        return ProcessResult(
            argv=tuple(request.argv),
            exit_code=process.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )


__all__ = [
    "ExecutableNotFoundError",
    "LocalProcessRunner",
    "ProcessLaunchError",
    "ProcessRequest",
    "ProcessResult",
    "ProcessRunner",
]
