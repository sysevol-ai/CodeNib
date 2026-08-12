# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end orchestration for multi-round local-specification search."""

from __future__ import annotations

import subprocess
from dataclasses import replace

from ..execution import AgentEventKind, AgentExecutor, AgentRunResult, CodexExecutor
from .aggregation import aggregate
from .artifacts import persist_artifacts
from .discovery import discover
from .distillation import distill
from .evidence import revalidate_memory_evidence, validate_candidates
from .frontier import select_frontier
from .patch_check import check_patch
from .planning import plan_briefs
from .provenance import validate_request_provenance
from .types import (
    CandidateSpecification,
    EvidenceSourceType,
    ExplorerOutput,
    GuardianConfig,
    GuardianRequest,
    GuardianResult,
    ReviewStatus,
    RoundRecord,
    SpecificationStatus,
    StageUsage,
)


def _workspace_state(request: GuardianRequest) -> bytes:
    return subprocess.run(
        ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
        cwd=request.workspace,
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout


def _tool_calls(rollout: AgentRunResult) -> int:
    return sum(event.kind is AgentEventKind.TOOL for event in rollout.trajectory)


def _retain_patch_probe_evidence(memory, findings, probes=()):
    """Carry final-check probes into cross-cycle specification memory."""

    probes = {
        evidence.evidence_id: evidence
        for evidence in (
            *probes,
            *(
                evidence
                for finding in findings
                for evidence in finding.evidence
                if evidence.source_type is EvidenceSourceType.RUNTIME_PROBE
            ),
        )
    }
    if not probes:
        return memory
    evidence_by_id = {item.evidence_id: item for item in memory.evidence}
    evidence_by_id.update(probes)
    records = []
    for record in memory.specifications:
        supporting = list(record.supporting_evidence)
        counter = list(record.counterevidence)
        for probe in probes.values():
            if record.specification_id in probe.supports:
                supporting.append(probe.evidence_id)
            if record.specification_id in probe.opposes:
                counter.append(probe.evidence_id)
        records.append(
            replace(
                record,
                supporting_evidence=tuple(dict.fromkeys(supporting)),
                counterevidence=tuple(dict.fromkeys(counter)),
            )
        )
    return replace(
        memory,
        specifications=tuple(records),
        evidence=tuple(
            sorted(evidence_by_id.values(), key=lambda item: item.evidence_id)
        ),
    )


class GuardianAgent:
    """Review a candidate patch without modifying production code."""

    def __init__(
        self,
        config: GuardianConfig,
        *,
        executor: AgentExecutor | None = None,
    ) -> None:
        self.config = config
        self.executor = executor or CodexExecutor()

    async def review(self, request: GuardianRequest) -> GuardianResult:
        errors: list[str] = []
        try:
            initial_state = _workspace_state(request)
        except (OSError, subprocess.SubprocessError):
            initial_state = b""
            errors.append("canonical repository state could not be captured")
        rollouts: list[AgentRunResult] = []
        rollout_stages: list[str] = []
        all_candidates: list[CandidateSpecification] = []
        round_records: list[RoundRecord] = []
        memory = request.memory
        prior_rounds = memory.rounds

        def record(
            rollout: AgentRunResult | None,
            *,
            stage: str,
            round_number: int,
            model: str,
        ) -> None:
            nonlocal memory
            if rollout is None:
                return
            rollouts.append(rollout)
            rollout_stages.append(stage)
            memory = replace(
                memory,
                stage_usage=memory.stage_usage
                + (
                    StageUsage(
                        stage=stage,
                        round=round_number,
                        model=model,
                        usage=rollout.usage,
                        tool_calls=_tool_calls(rollout),
                    ),
                ),
            )

        def result(
            status: ReviewStatus,
            *,
            summary: str = "",
            findings=(),
            uncertainty=(),
        ) -> GuardianResult:
            try:
                changed = _workspace_state(request) != initial_state
            except (OSError, subprocess.SubprocessError):
                changed = True
            if (
                changed
                and "canonical repository changed during Guardian review" not in errors
            ):
                errors.append("canonical repository changed during Guardian review")
                status = ReviewStatus.FAILED
            final = GuardianResult(
                base_commit=request.base_commit,
                candidate_commit=request.candidate_commit,
                status=status,
                memory=replace(memory, rounds=prior_rounds + tuple(round_records)),
                candidates=tuple(all_candidates),
                findings=tuple(findings),
                uncertain_specifications=tuple(uncertainty),
                summary=summary,
                rollouts=tuple(rollouts),
                rollout_stages=tuple(rollout_stages),
                rounds=tuple(round_records),
                artifact_dir=request.artifact_dir,
                errors=tuple(errors),
            )
            persist_artifacts(request, self.config, final)
            return final

        if errors:
            return result(ReviewStatus.FAILED)
        provenance_error = validate_request_provenance(request)
        if provenance_error:
            errors.append(provenance_error)
            return result(ReviewStatus.FAILED)
        if (
            request.previous_session_id
            and request.previous_session_id != memory.session_id
        ):
            errors.append("previous Guardian session identifier does not match memory")
            return result(ReviewStatus.FAILED)

        revalidated, evidence_errors = revalidate_memory_evidence(
            request, memory.evidence
        )
        memory = replace(memory, evidence=revalidated)
        errors.extend(evidence_errors)
        frontier: tuple[str, ...] = ()
        degraded = bool(evidence_errors)

        for round_number in range(1, self.config.max_rounds + 1):
            briefs, planning_rollout, planning_error = await plan_briefs(
                request,
                self.config,
                self.executor,
                memory,
                round_number=round_number,
                frontier=frontier,
            )
            record(
                planning_rollout,
                stage="planning",
                round_number=round_number,
                model=self.config.aggregator_model,
            )
            if planning_error:
                errors.append(planning_error)
                if round_number > 1 and memory.specifications:
                    degraded = True
                    break
                return result(ReviewStatus.FAILED)

            outputs, explorer_rollouts, explorer_errors = await discover(
                request,
                self.config,
                self.executor,
                memory,
                briefs,
                round_number=round_number,
            )
            for rollout in explorer_rollouts:
                discovery_stage = (
                    "exploration" if round_number == 1 else "investigation"
                )
                record(
                    rollout,
                    stage=discovery_stage,
                    round_number=round_number,
                    model=self.config.explorer_model,
                )
            if outputs and all(output.error for output in outputs):
                errors.extend(explorer_errors)
                round_records.append(
                    RoundRecord(
                        round=round_number,
                        briefs=briefs,
                        explorer_outputs=outputs,
                        aggregation_summary="",
                        frontier=(),
                        new_information=False,
                        degraded=True,
                        errors=explorer_errors,
                        specifications=memory.specifications,
                    )
                )
                return result(ReviewStatus.FAILED)
            errors.extend(explorer_errors)
            degraded = degraded or bool(explorer_errors)

            validated_outputs: list[ExplorerOutput] = []
            round_candidates: list[CandidateSpecification] = []
            validation_errors: list[str] = []
            for output in outputs:
                candidates, candidate_errors = validate_candidates(
                    request, output.candidates
                )
                validation_errors.extend(candidate_errors)
                validated_outputs.append(replace(output, candidates=candidates))
                round_candidates.extend(candidates)
            errors.extend(validation_errors)
            # Malformed evidence is isolated to that evidence item (and, if it
            # was the candidate's only support, that candidate). It is a useful
            # analysis warning but must not degrade otherwise valid explorers or
            # the complete review.
            all_candidates.extend(round_candidates)

            before = {
                (item.specification_id, item.updated_round, item.status)
                for item in memory.specifications
            }
            aggregation_summary, memory, aggregation_rollout, aggregation_error = (
                await aggregate(
                    request,
                    self.config,
                    self.executor,
                    memory,
                    tuple(round_candidates),
                    round_number=round_number,
                )
            )
            record(
                aggregation_rollout,
                stage="aggregation",
                round_number=round_number,
                model=self.config.aggregator_model,
            )
            if aggregation_error:
                errors.append(aggregation_error)
                round_records.append(
                    RoundRecord(
                        round=round_number,
                        briefs=briefs,
                        explorer_outputs=tuple(validated_outputs),
                        aggregation_summary="",
                        frontier=(),
                        new_information=False,
                        degraded=True,
                        errors=tuple(
                            explorer_errors
                            + tuple(validation_errors)
                            + (aggregation_error,)
                        ),
                        specifications=memory.specifications,
                    )
                )
                return result(ReviewStatus.FAILED)

            memory, distillation_rollout, distillation_error = await distill(
                request,
                self.config,
                self.executor,
                memory,
                tuple(validated_outputs),
                round_number=round_number,
            )
            record(
                distillation_rollout,
                stage="distillation",
                round_number=round_number,
                model=self.config.aggregator_model,
            )
            if distillation_error:
                errors.append(distillation_error)
                degraded = True

            after = {
                (item.specification_id, item.updated_round, item.status)
                for item in memory.specifications
            }
            new_information = before != after
            next_frontier: tuple[str, ...] = ()
            round_errors = tuple(explorer_errors) + tuple(validation_errors)
            round_degraded = bool(explorer_errors)
            if distillation_error:
                round_errors += (distillation_error,)
                round_degraded = True

            if round_number < self.config.max_rounds and new_information:
                next_frontier, frontier_rollout, frontier_error = await select_frontier(
                    request,
                    self.config,
                    self.executor,
                    memory,
                    next_round=round_number + 1,
                )
                record(
                    frontier_rollout,
                    stage="frontier",
                    round_number=round_number,
                    model=self.config.aggregator_model,
                )
                if frontier_error:
                    errors.append(frontier_error)
                    degraded = True
                    round_degraded = True
                    round_errors += (frontier_error,)

            round_records.append(
                RoundRecord(
                    round=round_number,
                    briefs=briefs,
                    explorer_outputs=tuple(validated_outputs),
                    aggregation_summary=aggregation_summary,
                    frontier=next_frontier,
                    new_information=new_information,
                    degraded=round_degraded,
                    errors=round_errors,
                    specifications=memory.specifications,
                    experiences=tuple(
                        item
                        for item in memory.experiences
                        if item.round == round_number
                    ),
                )
            )
            memory = replace(memory, rounds=prior_rounds + tuple(round_records))
            if (
                round_number >= self.config.max_rounds
                or not new_information
                or not next_frontier
            ):
                break
            frontier = next_frontier

        (
            summary,
            findings,
            uncertainty,
            patch_evidence,
            patch_rollout,
            patch_error,
        ) = await check_patch(request, self.config, self.executor, memory)
        record(
            patch_rollout,
            stage="patch_check",
            round_number=0,
            model=self.config.aggregator_model,
        )
        if patch_error:
            errors.append(patch_error)
            return result(ReviewStatus.FAILED, summary=summary)

        memory = _retain_patch_probe_evidence(
            memory, findings + uncertainty, patch_evidence
        )

        # Proposed and contested specifications must remain visible even when the
        # patch checker did not explicitly mention them.
        reported_uncertainty = {item.specification_id for item in uncertainty}
        reported_findings = {item.specification_id for item in findings}
        for item in memory.specifications:
            if (
                item.status
                in (
                    SpecificationStatus.PROPOSED,
                    SpecificationStatus.CONTESTED,
                )
                and item.specification_id not in reported_uncertainty
                and item.specification_id not in reported_findings
            ):
                from .types import FindingStatus, GuardianFinding

                uncertainty += (
                    GuardianFinding(
                        specification_id=item.specification_id,
                        statement=item.statement,
                        condition=item.condition,
                        status=FindingStatus.UNCERTAIN,
                        evidence=(),
                        evidence_ids=item.supporting_evidence,
                        patch_assessment=item.status_reason,
                        recommendation=(
                            item.unresolved_questions[0]
                            if item.unresolved_questions
                            else "Gather decisive evidence for the competing interpretation."
                        ),
                    ),
                )

        return result(
            ReviewStatus.DEGRADED if degraded else ReviewStatus.COMPLETE,
            summary=summary,
            findings=findings,
            uncertainty=uncertainty,
        )


__all__ = ["GuardianAgent"]
