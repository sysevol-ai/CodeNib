# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Guardian orchestration for local-specification discovery and admission."""

from __future__ import annotations

from ..execution import AgentExecutor, CodexExecutor
from .aggregation import aggregate
from .discovery import discover
from .types import (
    EvidenceAuthority,
    FindingStatus,
    GuardianConfig,
    GuardianRequest,
    GuardianResult,
    ReviewStatus,
)


class GuardianAgent:
    """Review a candidate patch without implementing changes itself."""

    def __init__(
        self,
        config: GuardianConfig,
        *,
        executor: AgentExecutor | None = None,
    ) -> None:
        self.config = config
        self.executor = executor or CodexExecutor()

    async def review(self, request: GuardianRequest) -> GuardianResult:
        candidates, explorer_rollouts, errors = await discover(
            request, self.config, self.executor
        )
        if not candidates:
            return GuardianResult(
                base_commit=request.base_commit,
                candidate_commit=request.candidate_commit,
                status=ReviewStatus.FAILED,
                candidates=(),
                rollouts=explorer_rollouts,
                errors=errors or ("no evidence-backed candidates were produced",),
            )

        summary, assessed, aggregation_rollout, aggregation_error = await aggregate(
            request, self.config, self.executor, candidates
        )
        all_errors = errors + ((aggregation_error,) if aggregation_error else ())
        rollouts = explorer_rollouts + (aggregation_rollout,)
        if aggregation_error:
            return GuardianResult(
                base_commit=request.base_commit,
                candidate_commit=request.candidate_commit,
                status=ReviewStatus.FAILED,
                candidates=candidates,
                rollouts=rollouts,
                errors=all_errors,
            )

        findings = tuple(
            finding
            for finding in assessed
            if finding.status is FindingStatus.VIOLATED
            and finding.confidence >= 0.70
            and any(
                evidence.authority is not EvidenceAuthority.SOLVER
                for evidence in finding.evidence
            )
        )[: self.config.max_findings]
        backlog = tuple(
            finding
            for finding in assessed
            if (
                finding.status is FindingStatus.UNCERTAIN
                or (
                    finding.status is FindingStatus.VIOLATED and finding not in findings
                )
            )
            and any(
                evidence.authority is not EvidenceAuthority.SOLVER
                for evidence in finding.evidence
            )
        )
        status = ReviewStatus.DEGRADED if errors else ReviewStatus.COMPLETE
        return GuardianResult(
            base_commit=request.base_commit,
            candidate_commit=request.candidate_commit,
            status=status,
            candidates=candidates,
            findings=findings,
            backlog=backlog,
            summary=summary,
            rollouts=rollouts,
            errors=all_errors,
        )


__all__ = ["GuardianAgent"]
