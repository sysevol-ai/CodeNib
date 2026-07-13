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
import subprocess
from dataclasses import dataclass
from typing import List, Protocol, Tuple, runtime_checkable


@runtime_checkable
class SandboxHandle(Protocol):
    """Minimal interface the investigator needs to run things in the sandbox."""

    repo_path: str

    def run_command(
        self, cmd: List[str], *, timeout: int = 60
    ) -> Tuple[int, str]: ...

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
