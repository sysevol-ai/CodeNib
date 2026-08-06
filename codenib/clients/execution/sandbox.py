# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Process-runner adapter backed by a fresh :mod:`codenib.sandbox` session."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path, PurePosixPath
from typing import Mapping

from ...sandbox import ExecRequest, SandboxPolicy, SandboxProvider, SandboxSpec
from ...sandbox.protocol import SandboxError
from .process import ProcessLaunchError, ProcessRequest, ProcessResult


def _source_revision(workspace: Path) -> str:
    try:
        return subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
            },
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProcessLaunchError(
            f"cannot resolve sandbox source revision: {exc}"
        ) from exc


class SandboxProcessRunner:
    """Run every process request in an independent copied repository.

    The source checkout is never mounted into model-controlled commands.  A
    rollout may write to its private workspace, but closing the session drops
    those changes.  This is the useful read-only guarantee for reviewers: they
    cannot mutate either the solver checkout or another rollout's view.

    ``staged_files`` is intended for controller-provided runtime material such
    as a short-lived CLI credential.  The files exist only in the disposable
    workspace and are not copied back or included in the returned process log.
    """

    def __init__(
        self,
        *,
        provider: SandboxProvider,
        image: str,
        platform: str,
        policy: SandboxPolicy,
        staged_files: Mapping[str | PurePosixPath, bytes] | None = None,
        environment: Mapping[str, str] | None = None,
        task_id_prefix: str = "guardian",
    ) -> None:
        self._provider = provider
        self._image = image
        self._platform = platform
        self._policy = policy
        self._staged_files = {
            PurePosixPath(path): bytes(content)
            for path, content in (staged_files or {}).items()
        }
        self._environment = dict(environment or {})
        self._task_id_prefix = task_id_prefix

    @staticmethod
    def _container_argv(request: ProcessRequest) -> tuple[str, ...]:
        host_workspace = str(request.cwd)
        return tuple(
            "/workspace" if value == host_workspace else value for value in request.argv
        )

    async def run(self, request: ProcessRequest) -> ProcessResult:
        revision = _source_revision(request.cwd)
        spec = SandboxSpec(
            source_dir=request.cwd,
            source_revision=revision,
            image=self._image,
            platform=self._platform,
            task_id=f"{self._task_id_prefix}-{revision[:12]}",
            policy=self._policy,
        )

        def execute() -> ProcessResult:
            try:
                with self._provider.create(spec) as session:
                    for path, content in self._staged_files.items():
                        session.write_file(path, content, create_parents=True)
                    result = session.execute(
                        ExecRequest(
                            argv=self._container_argv(request),
                            cwd=PurePosixPath("."),
                            stdin=request.stdin,
                            environment={**self._environment, **request.environment},
                            timeout_seconds=request.timeout_seconds,
                        )
                    )
            except (SandboxError, OSError, ValueError) as exc:
                raise ProcessLaunchError(f"sandbox execution failed: {exc}") from exc
            return ProcessResult(
                argv=tuple(request.argv),
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_seconds=result.duration_ms / 1000.0,
                timed_out=result.timed_out,
                stdout_truncated=result.stdout_truncated or result.output_limited,
                stderr_truncated=result.stderr_truncated or result.output_limited,
            )

        return await asyncio.to_thread(execute)


__all__ = ["SandboxProcessRunner"]
