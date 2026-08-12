# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Strong-model generation of task-specific investigation briefs."""

from __future__ import annotations

from ..execution import (
    AgentExecutor,
    AgentRole,
    AgentRunRequest,
    AgentRunResult,
    ExecutionPolicy,
    FilesystemAccess,
)
from .normalization import GuardianResponseError, parse_briefs
from .prompts import planning_prompt
from .types import (
    GuardianConfig,
    GuardianRequest,
    InvestigationBrief,
    SpecificationMemory,
)


async def plan_briefs(
    request: GuardianRequest,
    config: GuardianConfig,
    executor: AgentExecutor,
    memory: SpecificationMemory,
    *,
    round_number: int,
    frontier: tuple[str, ...] = (),
) -> tuple[tuple[InvestigationBrief, ...], AgentRunResult, str | None]:
    count = (
        config.explorer_count if round_number == 1 else config.targeted_explorer_count
    )
    rollout = await executor.run(
        AgentRunRequest(
            instruction=planning_prompt(
                request,
                memory,
                round_number=round_number,
                brief_count=count,
                frontier=frontier,
            ),
            workspace=request.workspace,
            role=AgentRole.AGGREGATOR,
            timeout_seconds=config.rollout_timeout_seconds,
            model=config.aggregator_model,
            reasoning_effort=config.aggregator_reasoning_effort,
            policy=ExecutionPolicy(
                filesystem=FilesystemAccess.READ_ONLY,
                isolation=config.execution_isolation,
            ),
            metadata={"guardian_stage": "planning", "round": str(round_number)},
        )
    )
    if not rollout.succeeded:
        message = rollout.error.message if rollout.error else rollout.status.value
        return (), rollout, f"round {round_number} planner: {message}"
    try:
        briefs = parse_briefs(rollout.final_message)
    except GuardianResponseError as exc:
        return (), rollout, f"round {round_number} planner: {exc}"
    if len(briefs) != count:
        return (
            (),
            rollout,
            (
                f"round {round_number} planner returned {len(briefs)} briefs; expected {count}"
            ),
        )
    if len({brief.focus_question.casefold() for brief in briefs}) != len(briefs):
        return (), rollout, f"round {round_number} planner returned duplicate briefs"
    if round_number == 1 and sum(brief.open_ended for brief in briefs) != 1:
        return (), rollout, "round 1 planner must reserve exactly one open-ended brief"
    if round_number > 1 and frontier:
        linked = {
            specification
            for brief in briefs
            for specification in brief.linked_specifications
        }
        if not linked or not linked.issubset(set(frontier)):
            return (
                (),
                rollout,
                (f"round {round_number} briefs must target only the selected frontier"),
            )
    return briefs, rollout, None


__all__ = ["plan_briefs"]
