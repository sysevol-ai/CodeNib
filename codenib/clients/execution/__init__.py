# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Execution adapters for installed external coding-agent harnesses.

This package owns invocation and trajectory normalization. Agent policies such
as Guardian own prompts, rollout allocation, and interpretation of responses.
"""

from .codex import CodexExecutor
from .process import (
    ExecutableNotFoundError,
    LocalProcessRunner,
    ProcessLaunchError,
    ProcessRequest,
    ProcessResult,
    ProcessRunner,
)
from .protocol import AgentExecutor
from .types import (
    AgentEvent,
    AgentEventKind,
    AgentRole,
    AgentRunError,
    AgentRunErrorCode,
    AgentRunRequest,
    AgentRunResult,
    ExecutionIsolation,
    ExecutionPolicy,
    FilesystemAccess,
    NetworkAccess,
    RunStatus,
    TokenUsage,
)

__all__ = [
    "AgentEvent",
    "AgentEventKind",
    "AgentExecutor",
    "AgentRole",
    "AgentRunError",
    "AgentRunErrorCode",
    "AgentRunRequest",
    "AgentRunResult",
    "CodexExecutor",
    "ExecutableNotFoundError",
    "ExecutionIsolation",
    "ExecutionPolicy",
    "FilesystemAccess",
    "LocalProcessRunner",
    "NetworkAccess",
    "ProcessLaunchError",
    "ProcessRequest",
    "ProcessResult",
    "ProcessRunner",
    "RunStatus",
    "TokenUsage",
]
