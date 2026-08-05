# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Provider-neutral value types for external agent execution."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence


class AgentRole(str, Enum):
    """Guardian role assigned to one external-agent rollout."""

    EXPLORER = "explorer"
    AGGREGATOR = "aggregator"
    VERIFIER = "verifier"
    REVISION = "revision"


class FilesystemAccess(str, Enum):
    """Filesystem capability requested from the agent harness."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class NetworkAccess(str, Enum):
    """Network guarantees understood by the local execution layer.

    Native Codex sandbox modes do not provide a portable egress guarantee, so
    the first execution backend only accepts ``INHERIT``. Network isolation is
    the responsibility of an outer :mod:`codenib.sandbox` environment.
    """

    INHERIT = "inherit"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Capabilities that an executor must enforce or reject explicitly."""

    filesystem: FilesystemAccess = FilesystemAccess.READ_ONLY
    network: NetworkAccess = NetworkAccess.INHERIT

    def __post_init__(self) -> None:
        object.__setattr__(self, "filesystem", FilesystemAccess(self.filesystem))
        object.__setattr__(self, "network", NetworkAccess(self.network))


def _validate_string_mapping(
    values: Mapping[str, str], *, name: str
) -> Mapping[str, str]:
    clean: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key or "\x00" in key or "=" in key:
            raise ValueError(f"{name} contains an invalid key: {key!r}")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError(f"{name} contains an invalid value for {key!r}")
        clean[key] = value
    return MappingProxyType(clean)


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    """One normalized request to an installed coding-agent harness."""

    instruction: str
    workspace: Path
    role: AgentRole
    timeout_seconds: float
    model: str | None = None
    reasoning_effort: str | None = None
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    environment: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.instruction, str) or not self.instruction.strip():
            raise ValueError("instruction must be a non-empty string")
        workspace = Path(self.workspace).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"workspace must be an existing directory: {workspace}")
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "role", AgentRole(self.role))
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a finite positive number")
        for name in ("model", "reasoning_effort"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value.strip() or "\x00" in value
            ):
                raise ValueError(f"{name} must be a non-empty, NUL-free string")
        object.__setattr__(
            self,
            "environment",
            _validate_string_mapping(self.environment, name="environment"),
        )
        object.__setattr__(
            self,
            "metadata",
            _validate_string_mapping(self.metadata, name="metadata"),
        )


class RunStatus(str, Enum):
    """Operational outcome of an external-agent invocation."""

    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class AgentEventKind(str, Enum):
    """Small common vocabulary for normalized harness events."""

    LIFECYCLE = "lifecycle"
    MESSAGE = "message"
    TOOL = "tool"
    USAGE = "usage"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One normalized trajectory event with its original payload retained."""

    sequence: int
    kind: AgentEventKind
    provider_type: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", AgentEventKind(self.kind))
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Provider-neutral token accounting for one complete agent run."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class AgentRunErrorCode(str, Enum):
    """Operational failure categories kept separate from agent quality."""

    UNSUPPORTED_POLICY = "unsupported_policy"
    EXECUTABLE_NOT_FOUND = "executable_not_found"
    LAUNCH_FAILED = "launch_failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    CLI_EXIT = "cli_exit"
    MALFORMED_EVENT_STREAM = "malformed_event_stream"
    EMPTY_RESPONSE = "empty_response"


@dataclass(frozen=True, slots=True)
class AgentRunError:
    code: AgentRunErrorCode
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", AgentRunErrorCode(self.code))


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Normalized result returned to Guardian policy code."""

    status: RunStatus
    final_message: str
    trajectory: Sequence[AgentEvent]
    usage: TokenUsage
    duration_seconds: float
    raw_output: str
    stderr: str
    exit_code: int | None
    error: AgentRunError | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", RunStatus(self.status))
        object.__setattr__(self, "trajectory", tuple(self.trajectory))

    @property
    def succeeded(self) -> bool:
        return self.status is RunStatus.COMPLETED


__all__ = [
    "AgentEvent",
    "AgentEventKind",
    "AgentRole",
    "AgentRunError",
    "AgentRunErrorCode",
    "AgentRunRequest",
    "AgentRunResult",
    "ExecutionPolicy",
    "FilesystemAccess",
    "NetworkAccess",
    "RunStatus",
    "TokenUsage",
]
