# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Protocol implemented by installed external-agent backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .types import AgentRunRequest, AgentRunResult


@runtime_checkable
class AgentExecutor(Protocol):
    """Run one request through an external agent's native inner loop."""

    async def run(self, request: AgentRunRequest) -> AgentRunResult: ...


__all__ = ["AgentExecutor"]
