# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Strong-model evidence admission for explorer candidates."""

from __future__ import annotations

from ..execution import (
    AgentExecutor,
    AgentRole,
    AgentRunRequest,
    AgentRunResult,
    ExecutionPolicy,
    FilesystemAccess,
)
from .normalization import GuardianResponseError, parse_findings
from .prompts import aggregation_prompt
from .types import GuardianConfig, GuardianFinding, GuardianRequest, LocalSpecification


async def aggregate(
    request: GuardianRequest,
    config: GuardianConfig,
    executor: AgentExecutor,
    candidates: tuple[LocalSpecification, ...],
) -> tuple[str, tuple[GuardianFinding, ...], AgentRunResult, str | None]:
    rollout = await executor.run(
        AgentRunRequest(
            instruction=aggregation_prompt(request, candidates, config.max_findings),
            workspace=request.workspace,
            role=AgentRole.AGGREGATOR,
            timeout_seconds=config.rollout_timeout_seconds,
            model=config.aggregator_model,
            reasoning_effort=config.aggregator_reasoning_effort,
            policy=ExecutionPolicy(
                filesystem=FilesystemAccess.READ_ONLY,
                isolation=config.execution_isolation,
            ),
            metadata={"guardian_stage": "aggregation"},
        )
    )
    if not rollout.succeeded:
        message = rollout.error.message if rollout.error else rollout.status.value
        return "", (), rollout, f"aggregator: {message}"
    try:
        summary, findings = parse_findings(rollout.final_message)
    except GuardianResponseError as exc:
        return "", (), rollout, f"aggregator: {exc}"
    return summary, findings, rollout, None


__all__ = ["aggregate"]
