# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Procedural exploration-experience distillation."""

from __future__ import annotations

from dataclasses import replace

from ..execution import (
    AgentExecutor,
    AgentRole,
    AgentRunRequest,
    AgentRunResult,
    ExecutionPolicy,
    FilesystemAccess,
)
from .normalization import GuardianResponseError, parse_distillation
from .prompts import distillation_prompt
from .types import ExplorerOutput, GuardianConfig, GuardianRequest, SpecificationMemory


async def distill(
    request: GuardianRequest,
    config: GuardianConfig,
    executor: AgentExecutor,
    memory: SpecificationMemory,
    outputs: tuple[ExplorerOutput, ...],
    *,
    round_number: int,
) -> tuple[SpecificationMemory, AgentRunResult, str | None]:
    rows = tuple(
        {
            "explorer": item.explorer,
            "brief_id": item.brief_id,
            "searched_locations": list(item.trace.searched_locations),
            "actions_and_outcomes": list(item.trace.actions_and_outcomes),
            "unsuccessful_routes": list(item.trace.unsuccessful_routes),
            "remaining_questions": list(item.trace.remaining_questions),
            "suggested_next_actions": list(item.trace.suggested_next_actions),
        }
        for item in outputs
        if not item.error
    )
    rollout = await executor.run(
        AgentRunRequest(
            instruction=distillation_prompt(rows, round_number=round_number),
            workspace=request.workspace,
            role=AgentRole.AGGREGATOR,
            timeout_seconds=config.rollout_timeout_seconds,
            model=config.aggregator_model,
            reasoning_effort=config.aggregator_reasoning_effort,
            policy=ExecutionPolicy(
                filesystem=FilesystemAccess.READ_ONLY,
                isolation=config.execution_isolation,
            ),
            metadata={"guardian_stage": "distillation", "round": str(round_number)},
        )
    )
    if not rollout.succeeded:
        message = rollout.error.message if rollout.error else rollout.status.value
        return memory, rollout, f"round {round_number} distillation: {message}"
    try:
        experiences = parse_distillation(
            rollout.final_message, round_number=round_number
        )
    except GuardianResponseError as exc:
        return memory, rollout, f"round {round_number} distillation: {exc}"
    # The parser can construct only experiences, so distillation cannot mutate specs.
    return (
        replace(memory, experiences=memory.experiences + experiences),
        rollout,
        None,
    )


__all__ = ["distill"]
