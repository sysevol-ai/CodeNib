# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Backend-neutral sandbox provider and session protocols."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Protocol, Sequence, runtime_checkable

from .types import (
    ArtifactBundle,
    DiffResult,
    ExecRequest,
    ExecResult,
    SandboxCapabilities,
    SandboxMetadata,
    SandboxSpec,
)


class SandboxError(RuntimeError):
    """Base error for sandbox infrastructure or policy failures."""


class SandboxUnavailableError(SandboxError):
    """Raised when the configured runtime or image is unavailable."""


class SandboxPolicyError(SandboxError):
    """Raised when a request would weaken an enforced policy."""


class SandboxClosedError(SandboxError):
    """Raised when an operation targets a closed session."""


@runtime_checkable
class SandboxSession(Protocol):
    """One isolated, copied repository workspace."""

    @property
    def sandbox_id(self) -> str: ...

    @property
    def capabilities(self) -> SandboxCapabilities: ...

    @property
    def metadata(self) -> SandboxMetadata: ...

    def execute(self, request: ExecRequest) -> ExecResult: ...

    def read_file(
        self,
        path: str | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ) -> bytes: ...

    def write_file(
        self,
        path: str | PurePosixPath,
        data: bytes,
        *,
        create_parents: bool = False,
    ) -> None: ...

    def get_diff(self, *, max_bytes: int | None = None) -> DiffResult: ...

    def collect_artifacts(
        self,
        paths: Sequence[str | PurePosixPath],
        destination: Path,
        *,
        max_bytes: int | None = None,
    ) -> ArtifactBundle: ...

    def close(self) -> None: ...

    def __enter__(self) -> "SandboxSession": ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


@runtime_checkable
class SandboxProvider(Protocol):
    """Factory for backend-specific sandbox sessions."""

    @property
    def capabilities(self) -> SandboxCapabilities: ...

    def create(self, spec: SandboxSpec) -> SandboxSession: ...


__all__ = [
    "SandboxClosedError",
    "SandboxError",
    "SandboxPolicyError",
    "SandboxProvider",
    "SandboxSession",
    "SandboxUnavailableError",
]
