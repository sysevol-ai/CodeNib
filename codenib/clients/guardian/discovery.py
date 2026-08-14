# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Parallel brief-driven local-specification exploration."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

from ..execution import (
    AgentEventKind,
    AgentExecutor,
    AgentRole,
    AgentRunRequest,
    AgentRunResult,
    ExecutionPolicy,
    FilesystemAccess,
)
from .normalization import GuardianResponseError, parse_explorer_output
from .prompts import explorer_prompt, explorer_repair_prompt
from .types import (
    ExplorationTrace,
    ExplorerAttempt,
    ExplorerOutput,
    GuardianConfig,
    GuardianRequest,
    InvestigationBrief,
    SpecificationMemory,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _used_tools(rollout: AgentRunResult) -> bool:
    return any(event.kind is AgentEventKind.TOOL for event in rollout.trajectory)


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

    # Round/explorer labels already make identifiers unique within one review.
    # Add the candidate snapshot only after memory has crossed a review-cycle
    # boundary, where the labels intentionally restart from round 1.
    identifier_namespace = (
        request.candidate_commit[:12] if memory.observed_snapshots else ""
    )

    async def one(
        index: int, brief: InvestigationBrief
    ) -> tuple[ExplorerOutput, tuple[AgentRunResult, ...], tuple[str, ...]]:
        label = f"explorer-{round_number}-{index + 1}"
        stage = "exploration" if round_number == 1 else "investigation"
        relevant_experience = tuple(
            item
            for item in memory.experiences
            if set(item.linked_specifications).intersection(brief.linked_specifications)
        )
        started_at = _now()
        rollout = await executor.run(
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
        attempts = [
            ExplorerAttempt(
                attempt=1,
                kind="initial",
                response=rollout.final_message,
                succeeded=rollout.succeeded,
                error=(rollout.error.message if rollout.error else ""),
                started_at=started_at,
                finished_at=_now(),
                duration_seconds=rollout.duration_seconds,
                used_tools=_used_tools(rollout),
            )
        ]
        rollouts = [rollout]
        if not rollout.succeeded:
            message = rollout.error.message if rollout.error else rollout.status.value
            return (
                ExplorerOutput(
                    explorer=label,
                    round=round_number,
                    brief_id=brief.brief_id,
                    candidates=(),
                    trace=ExplorationTrace(),
                    error=message,
                    attempts=tuple(attempts),
                ),
                tuple(rollouts),
                (f"{label}: {message}",),
            )
        try:
            output = parse_explorer_output(
                rollout.final_message,
                explorer=label,
                round_number=round_number,
                brief_id=brief.brief_id,
                namespace=identifier_namespace,
            )
        except GuardianResponseError as exc:
            error = str(exc)
            attempts[0] = replace(attempts[0], succeeded=False, error=error)
            output = None
            for repair_number in range(1, config.max_explorer_repairs + 1):
                repair_started_at = _now()
                repair = await executor.run(
                    AgentRunRequest(
                        instruction=explorer_repair_prompt(
                            rollout.final_message, validation_error=error
                        ),
                        workspace=request.workspace,
                        role=AgentRole.REVISION,
                        timeout_seconds=config.rollout_timeout_seconds,
                        model=config.explorer_model,
                        reasoning_effort=config.explorer_reasoning_effort,
                        policy=ExecutionPolicy(
                            filesystem=FilesystemAccess.READ_ONLY,
                            isolation=config.execution_isolation,
                        ),
                        metadata={
                            "guardian_stage": "explorer_repair",
                            "round": str(round_number),
                            "brief": brief.brief_id,
                            "explorer": label,
                            "attempt": str(repair_number),
                        },
                    )
                )
                rollouts.append(repair)
                used_tools = _used_tools(repair)
                repair_error = ""
                repaired = None
                if not repair.succeeded:
                    repair_error = (
                        repair.error.message if repair.error else repair.status.value
                    )
                elif used_tools:
                    repair_error = "structural repair used tools"
                else:
                    try:
                        repaired = parse_explorer_output(
                            repair.final_message,
                            explorer=label,
                            round_number=round_number,
                            brief_id=brief.brief_id,
                            namespace=identifier_namespace,
                        )
                    except GuardianResponseError as repair_exc:
                        repair_error = str(repair_exc)
                attempts.append(
                    ExplorerAttempt(
                        attempt=repair_number + 1,
                        kind="structural_repair",
                        response=repair.final_message,
                        succeeded=repaired is not None,
                        error=repair_error,
                        started_at=repair_started_at,
                        finished_at=_now(),
                        duration_seconds=repair.duration_seconds,
                        used_tools=used_tools,
                    )
                )
                if repaired is not None:
                    output = repaired
                    break
                error = repair_error or error
            if output is None:
                return (
                    ExplorerOutput(
                        explorer=label,
                        round=round_number,
                        brief_id=brief.brief_id,
                        candidates=(),
                        trace=ExplorationTrace(),
                        error=error,
                        attempts=tuple(attempts),
                    ),
                    tuple(rollouts),
                    (f"{label}: {error}",),
                )
        return replace(output, attempts=tuple(attempts)), tuple(rollouts), ()

    completed = tuple(
        await asyncio.gather(*(one(index, brief) for index, brief in enumerate(briefs)))
    )
    return (
        tuple(item[0] for item in completed),
        tuple(rollout for item in completed for rollout in item[1]),
        tuple(error for item in completed for error in item[2]),
    )


__all__ = ["discover"]
