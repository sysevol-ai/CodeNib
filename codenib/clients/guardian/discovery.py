# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Parallel brief-driven local-specification exploration."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from ..execution import (
    AgentExecutor,
    AgentRole,
    AgentRunRequest,
    AgentRunResult,
    ExecutionPolicy,
    FilesystemAccess,
)
from .normalization import GuardianResponseError, parse_explorer_output
from .prompts import explorer_prompt
from .types import (
    ExplorationTrace,
    ExplorerOutput,
    GuardianConfig,
    GuardianRequest,
    InvestigationBrief,
    SpecificationMemory,
)


def _slice_memory(
    memory: SpecificationMemory, brief: InvestigationBrief
) -> SpecificationMemory:
    linked = set(brief.linked_specifications)
    if not linked:
        return replace(
            memory, specifications=(), evidence=(), experiences=(), rounds=()
        )
    specifications = tuple(
        item for item in memory.specifications if item.specification_id in linked
    )
    evidence_ids = {
        evidence_id
        for item in specifications
        for evidence_id in item.supporting_evidence + item.counterevidence
    }
    return replace(
        memory,
        specifications=specifications,
        evidence=tuple(
            item for item in memory.evidence if item.evidence_id in evidence_ids
        ),
        experiences=(),
        rounds=(),
    )


async def discover(
    request: GuardianRequest,
    config: GuardianConfig,
    executor: AgentExecutor,
    memory: SpecificationMemory,
    briefs: tuple[InvestigationBrief, ...],
    *,
    round_number: int,
) -> tuple[tuple[ExplorerOutput, ...], tuple[AgentRunResult, ...], tuple[str, ...]]:
    """Run isolated explorers concurrently and retain traces and provenance."""

    async def one(index: int, brief: InvestigationBrief) -> AgentRunResult:
        label = f"explorer-{round_number}-{index + 1}"
        stage = "exploration" if round_number == 1 else "investigation"
        relevant_experience = tuple(
            item
            for item in memory.experiences
            if set(item.linked_specifications).intersection(brief.linked_specifications)
        )
        return await executor.run(
            AgentRunRequest(
                instruction=explorer_prompt(
                    request,
                    brief,
                    explorer=label,
                    round_number=round_number,
                    memory=_slice_memory(memory, brief),
                    experiences=relevant_experience,
                    max_specifications=config.max_specifications_per_explorer,
                ),
                workspace=request.workspace,
                role=AgentRole.EXPLORER,
                timeout_seconds=config.rollout_timeout_seconds,
                model=config.explorer_model,
                reasoning_effort=config.explorer_reasoning_effort,
                policy=ExecutionPolicy(
                    filesystem=FilesystemAccess.READ_ONLY,
                    isolation=config.execution_isolation,
                ),
                metadata={
                    "guardian_stage": stage,
                    "round": str(round_number),
                    "brief": brief.brief_id,
                    "explorer": label,
                },
            )
        )

    rollouts = tuple(
        await asyncio.gather(*(one(index, brief) for index, brief in enumerate(briefs)))
    )
    outputs = []
    errors = []
    for index, (brief, rollout) in enumerate(zip(briefs, rollouts, strict=True)):
        label = f"explorer-{round_number}-{index + 1}"
        if not rollout.succeeded:
            message = rollout.error.message if rollout.error else rollout.status.value
            errors.append(f"{label}: {message}")
            outputs.append(
                ExplorerOutput(
                    explorer=label,
                    round=round_number,
                    brief_id=brief.brief_id,
                    candidates=(),
                    trace=ExplorationTrace(),
                    error=message,
                )
            )
            continue
        try:
            outputs.append(
                parse_explorer_output(
                    rollout.final_message,
                    explorer=label,
                    round_number=round_number,
                    brief_id=brief.brief_id,
                )
            )
        except GuardianResponseError as exc:
            errors.append(f"{label}: {exc}")
            outputs.append(
                ExplorerOutput(
                    explorer=label,
                    round=round_number,
                    brief_id=brief.brief_id,
                    candidates=(),
                    trace=ExplorationTrace(),
                    error=str(exc),
                )
            )
    return tuple(outputs), rollouts, tuple(errors)


__all__ = ["discover"]
