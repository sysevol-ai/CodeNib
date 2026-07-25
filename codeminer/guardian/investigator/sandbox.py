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
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from typing import List, Protocol, Tuple, runtime_checkable


@runtime_checkable
class SandboxHandle(Protocol):
    """Minimal interface the investigator needs to run things in the sandbox."""

    repo_path: str

    def run_command(self, cmd: List[str], *, timeout: int = 60) -> Tuple[int, str]: ...

    def write_file(self, rel_path: str, content: str) -> None: ...

    def read_file(self, rel_path: str) -> str: ...


@dataclass
class WorktreeSandbox:
    """Sandbox backed by a plain git worktree directory on the host.

    The investigator writes test files and runs commands directly in the
    worktree.  This is the ``--sandbox worktree`` debug mode; the container
    sandbox (Hour 5) wraps the same interface.
    """

    repo_path: str

    def run_command(self, cmd: List[str], *, timeout: int = 60) -> Tuple[int, str]:
        try:
            result = subprocess.run(
                cmd,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (result.stdout + "\n" + result.stderr).strip()
            return result.returncode, output
        except subprocess.TimeoutExpired:
            return 1, f"(command timed out after {timeout}s)"
        except Exception as exc:  # noqa: BLE001
            return 1, f"(command error: {exc})"

    def write_file(self, rel_path: str, content: str) -> None:
        full_path = os.path.join(self.repo_path, rel_path)
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def read_file(self, rel_path: str) -> str:
        full_path = os.path.join(self.repo_path, rel_path)
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
