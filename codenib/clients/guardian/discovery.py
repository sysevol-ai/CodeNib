# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Parallel local-specification exploration."""

from __future__ import annotations

import asyncio

from ..execution import (
    AgentExecutor,
    AgentRole,
    AgentRunRequest,
    AgentRunResult,
    ExecutionPolicy,
    FilesystemAccess,
)
from .normalization import GuardianResponseError, parse_candidates
from .prompts import explorer_prompt
from .types import GuardianConfig, GuardianRequest, LocalSpecification


async def discover(
    request: GuardianRequest,
    config: GuardianConfig,
    executor: AgentExecutor,
) -> tuple[tuple[LocalSpecification, ...], tuple[AgentRunResult, ...], tuple[str, ...]]:
    """Run independent explorers concurrently and retain per-rollout provenance."""

    async def one(index: int) -> AgentRunResult:
        return await executor.run(
            AgentRunRequest(
                instruction=explorer_prompt(request, index),
                workspace=request.workspace,
                role=AgentRole.EXPLORER,
                timeout_seconds=config.rollout_timeout_seconds,
                model=config.explorer_model,
                reasoning_effort=config.explorer_reasoning_effort,
                policy=ExecutionPolicy(
                    filesystem=FilesystemAccess.READ_ONLY,
                    isolation=config.execution_isolation,
                ),
                metadata={"guardian_stage": "discovery", "explorer": str(index + 1)},
            )
        )

    rollouts = tuple(
        await asyncio.gather(*(one(index) for index in range(config.explorer_count)))
    )
    candidates: list[LocalSpecification] = []
    errors: list[str] = []
    for index, rollout in enumerate(rollouts):
        label = f"explorer_{index + 1}"
        if not rollout.succeeded:
            message = rollout.error.message if rollout.error else rollout.status.value
            errors.append(f"{label}: {message}")
            continue
        try:
            candidates.extend(parse_candidates(rollout.final_message, explorer=label))
        except GuardianResponseError as exc:
            errors.append(f"{label}: {exc}")
    return tuple(candidates), rollouts, tuple(errors)


__all__ = ["discover"]
