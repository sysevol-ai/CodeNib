# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Sandbox abstractions for the Repository Guardian investigator.

Contains the SandboxHandle protocol and the WorktreeSandbox implementation.
Extracted from codeminer.guardian.investigator (flat module) into the
investigator/ sub-package.
"""

from __future__ import annotations

import os
import errno
import ctypes
import ctypes.util
import resource
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol, runtime_checkable

from .types import CommandResult, ProcessStatus

_OUTPUT_LIMIT = 30_000
_SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "SYSTEMROOT",
    }
)


@runtime_checkable
class SandboxHandle(Protocol):
    """Minimal interface the investigator needs to run things in the sandbox."""

    repo_path: str

    def run_command(
        self,
        cmd: List[str],
        *,
        timeout: int = 60,
        env: Optional[Dict[str, str]] = None,
    ) -> CommandResult: ...

    def write_file(self, rel_path: str, content: str) -> None: ...

    def read_file(self, rel_path: str) -> str: ...


@dataclass(frozen=True)
class ReadOnlySourceHandle:
    """Path-validated read-only view of a disposable source snapshot."""

    repo_path: str

    def read_file(self, rel_path: str) -> str:
        if os.path.isabs(rel_path):
            raise ValueError("source paths must be relative")
        root = os.path.realpath(self.repo_path)
        target = os.path.realpath(os.path.join(root, rel_path))
        if os.path.commonpath([root, target]) != root:
            raise ValueError("source path escapes the disposable root")
        try:
            with open(target, encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return ""


@dataclass
class WorktreeSandbox:
    """Sandbox backed by a plain git worktree directory on the host.

    The investigator writes test files and runs commands directly in the
    worktree.  This is the ``--sandbox worktree`` debug mode; the container
    sandbox (Hour 5) wraps the same interface.
    """

    repo_path: str

    cpu_seconds: int = 60
    memory_bytes: int = 2 * 1024 * 1024 * 1024
    max_processes: int = 128

    @property
    def source_handle(self) -> ReadOnlySourceHandle:
        return ReadOnlySourceHandle(self.repo_path)

    def _commit(self) -> str:
        snapshot_commit = getattr(self, "snapshot_commit", "")
        if snapshot_commit:
            return str(snapshot_commit)
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def _safe_path(self, rel_path: str) -> str:
        if os.path.isabs(rel_path):
            raise ValueError("sandbox paths must be relative")
        root = os.path.realpath(self.repo_path)
        target = os.path.realpath(os.path.join(root, rel_path))
        if os.path.commonpath([root, target]) != root:
            raise ValueError("sandbox path escapes the disposable root")
        return target

    def _probe_env(self, explicit: Optional[Dict[str, str]]) -> Dict[str, str]:
        scrubbed = {
            key: value for key, value in os.environ.items() if key in _SAFE_ENV_KEYS
        }
        sandbox_tmp = self._safe_path(".guardian/tmp")
        os.makedirs(sandbox_tmp, exist_ok=True)
        scrubbed.update(
            {
                "HOME": self.repo_path,
                "TMPDIR": sandbox_tmp,
                "NO_PROXY": "*",
                "no_proxy": "*",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        for key, value in (explicit or {}).items():
            upper = key.upper()
            if any(
                marker in upper
                for marker in (
                    "TOKEN",
                    "SECRET",
                    "PASSWORD",
                    "ACCESS_KEY",
                    "API_KEY",
                    "PRIVATE_KEY",
                )
            ):
                continue
            if upper.endswith(("_KEY", "_CREDENTIALS")):
                continue
            scrubbed[key] = value
        return scrubbed

    def _preexec(self) -> None:
        # Load and install seccomp before lowering address-space limits.  The
        # parent process may already map more than the probe's final allowance.
        _deny_network_syscalls()
        resource.setrlimit(
            resource.RLIMIT_CPU, (self.cpu_seconds, self.cpu_seconds + 1)
        )
        resource.setrlimit(resource.RLIMIT_AS, (self.memory_bytes, self.memory_bytes))
        resource.setrlimit(
            resource.RLIMIT_NPROC, (self.max_processes, self.max_processes)
        )
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    def run_command(
        self,
        cmd: List[str],
        *,
        timeout: int = 60,
        env: Optional[Dict[str, str]] = None,
    ) -> CommandResult:
        command = [str(part) for part in cmd]
        commit = self._commit()
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._probe_env(env),
                close_fds=True,
                preexec_fn=self._preexec,
                check=False,
            )
            if result.returncode < 0:
                status = ProcessStatus.SIGNALED
                signal = -result.returncode
                exit_code = None
            else:
                status = ProcessStatus.EXITED
                signal = None
                exit_code = result.returncode
            return CommandResult(
                status=status,
                exit_code=exit_code,
                signal=signal,
                command=command,
                cwd=self.repo_path,
                commit=commit,
                stdout=result.stdout[-_OUTPUT_LIMIT:],
                stderr=result.stderr[-_OUTPUT_LIMIT:],
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                status=ProcessStatus.TIMED_OUT,
                exit_code=None,
                signal=None,
                command=command,
                cwd=self.repo_path,
                commit=commit,
                stdout=_decode_timeout_stream(exc.stdout),
                stderr=_decode_timeout_stream(exc.stderr),
            )
        except subprocess.SubprocessError as exc:
            status = (
                ProcessStatus.SANDBOX_FAILED
                if "preexec_fn" in str(exc)
                else ProcessStatus.SPAWN_FAILED
            )
            return CommandResult(
                status=status,
                exit_code=None,
                signal=None,
                command=command,
                cwd=self.repo_path,
                commit=commit,
                stderr=str(exc)[:_OUTPUT_LIMIT],
            )
        except OSError as exc:
            return CommandResult(
                status=ProcessStatus.SPAWN_FAILED,
                exit_code=None,
                signal=None,
                command=command,
                cwd=self.repo_path,
                commit=commit,
                stderr=str(exc)[:_OUTPUT_LIMIT],
            )
        except Exception as exc:  # noqa: BLE001 - sandbox setup is a typed result
            return CommandResult(
                status=ProcessStatus.SANDBOX_FAILED,
                exit_code=None,
                signal=None,
                command=command,
                cwd=self.repo_path,
                commit=commit,
                stderr=str(exc)[:_OUTPUT_LIMIT],
            )

    def write_file(self, rel_path: str, content: str) -> None:
        full_path = self._safe_path(rel_path)
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def read_file(self, rel_path: str) -> str:
        full_path = self._safe_path(rel_path)
        try:
            with open(full_path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""


class PriorSnapshotSandbox(WorktreeSandbox):
    """Temporary read/write overlay populated from a prior git snapshot."""

    def __init__(self, repo_path: str, revision: str = "HEAD^") -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="guardian-prior-")
        super().__init__(self._temp_dir)
        resolved = subprocess.run(
            ["git", "rev-parse", revision],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        self.snapshot_commit = (
            resolved.stdout.strip() if resolved.returncode == 0 else revision
        )
        archive_path = os.path.join(self._temp_dir, "_snapshot.tar")
        try:
            with open(archive_path, "wb") as archive:
                result = subprocess.run(
                    ["git", "archive", "--format=tar", revision],
                    cwd=repo_path,
                    stdout=archive,
                    stderr=subprocess.PIPE,
                    check=False,
                )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace")
                raise ValueError(
                    f"cannot materialize prior revision {revision!r}: {detail}"
                )
            with tarfile.open(archive_path) as archive:
                root = os.path.realpath(self._temp_dir)
                for member in archive.getmembers():
                    target = os.path.realpath(os.path.join(root, member.name))
                    if os.path.commonpath([root, target]) != root:
                        raise ValueError(
                            "prior snapshot archive contains an unsafe path"
                        )
                archive.extractall(self._temp_dir)
        except Exception:
            self.close()
            raise
        finally:
            if os.path.exists(archive_path):
                os.unlink(archive_path)

    def close(self) -> None:
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def __enter__(self) -> "PriorSnapshotSandbox":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class CurrentSnapshotSandbox(PriorSnapshotSandbox):
    """Temporary read/write overlay populated from the current git snapshot."""

    def __init__(self, repo_path: str) -> None:
        super().__init__(repo_path, revision="HEAD")


def _decode_timeout_stream(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[-_OUTPUT_LIMIT:]
    return str(value)[-_OUTPUT_LIMIT:]


def _deny_network_syscalls() -> None:
    """Install a child-only seccomp filter that rejects socket creation.

    The disposable snapshot already provides filesystem isolation.  Blocking
    socket and socketpair, with ``close_fds=True``, prevents the probe process
    and its descendants from obtaining a network endpoint.
    """

    library_name = ctypes.util.find_library("seccomp")
    if not library_name:
        raise RuntimeError("libseccomp is required for restricted probe execution")
    library = ctypes.CDLL(library_name, use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]

    allow = 0x7FFF0000
    deny = 0x00050000 | errno.EPERM
    context = library.seccomp_init(allow)
    if not context:
        raise RuntimeError("seccomp_init failed")
    try:
        for syscall in (b"socket", b"socketpair"):
            number = library.seccomp_syscall_resolve_name(syscall)
            if number >= 0 and library.seccomp_rule_add(context, deny, number, 0) != 0:
                raise RuntimeError(f"cannot block syscall {syscall.decode()}")
        if library.seccomp_load(context) != 0:
            raise RuntimeError("seccomp_load failed")
    finally:
        library.seccomp_release(context)
