# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Strong-controller selection of the next specification frontier."""

from __future__ import annotations

from ..execution import (
    AgentExecutor,
    AgentRole,
    AgentRunRequest,
    AgentRunResult,
    ExecutionPolicy,
    FilesystemAccess,
)
from .normalization import GuardianResponseError, parse_frontier
from .prompts import frontier_prompt
from .types import GuardianConfig, GuardianRequest, SpecificationMemory


async def select_frontier(
    request: GuardianRequest,
    config: GuardianConfig,
    executor: AgentExecutor,
    memory: SpecificationMemory,
    *,
    next_round: int,
) -> tuple[tuple[str, ...], AgentRunResult, str | None]:
    rollout = await executor.run(
        AgentRunRequest(
            instruction=frontier_prompt(memory, next_round=next_round),
            workspace=request.workspace,
            role=AgentRole.AGGREGATOR,
            timeout_seconds=config.rollout_timeout_seconds,
            model=config.aggregator_model,
            reasoning_effort=config.aggregator_reasoning_effort,
            policy=ExecutionPolicy(
                filesystem=FilesystemAccess.READ_ONLY,
                isolation=config.execution_isolation,
            ),
            metadata={"guardian_stage": "frontier", "round": str(next_round)},
        )
    )
    if not rollout.succeeded:
        message = rollout.error.message if rollout.error else rollout.status.value
        return (), rollout, f"frontier for round {next_round}: {message}"
    try:
        frontier, _ = parse_frontier(rollout.final_message)
    except GuardianResponseError as exc:
        return (), rollout, f"frontier for round {next_round}: {exc}"
    known = {item.specification_id for item in memory.specifications}
    unknown = tuple(value for value in frontier if value not in known)
    if unknown:
        return (
            (),
            rollout,
            f"frontier contains unknown specifications: {', '.join(unknown)}",
        )
    return tuple(dict.fromkeys(frontier)), rollout, None


__all__ = ["select_frontier"]
