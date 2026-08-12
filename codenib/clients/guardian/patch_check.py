# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Final patch checking against supported local specifications only."""

from __future__ import annotations

from dataclasses import replace

from ..execution import (
    AgentEventKind,
    AgentExecutor,
    AgentRole,
    AgentRunRequest,
    AgentRunResult,
    ExecutionPolicy,
    FilesystemAccess,
)
from .evidence import validate_evidence
from .normalization import GuardianResponseError, parse_patch_check
from .prompts import patch_check_prompt
from .types import (
    ContextFidelity,
    Evidence,
    EvidenceAuthority,
    EvidenceSourceType,
    FindingStatus,
    GuardianConfig,
    GuardianFinding,
    GuardianRequest,
    SpecificationMemory,
    SpecificationRecord,
    SpecificationStatus,
)


def _direct_task_evidence(
    request: GuardianRequest, specifications: tuple[SpecificationRecord, ...]
) -> tuple[Evidence, ...]:
    """Expose verbatim task text as first-class normative checker evidence."""

    supported_ids = tuple(item.specification_id for item in specifications)
    return tuple(
        Evidence(
            evidence_id=f"EV-TASK-{item.context_id}",
            path=item.context_id,
            line_start=1,
            line_end=max(1, item.content.count("\n") + 1),
            description="Verbatim requirement supplied to the coding system.",
            source_type=EvidenceSourceType.TASK,
            authority=EvidenceAuthority.NORMATIVE,
            quote=item.content,
            supports=supported_ids,
        )
        for item in request.task_context
        if item.fidelity is ContextFidelity.VERBATIM
    )


def _active_failed_probes(
    request: GuardianRequest,
    memory: SpecificationMemory,
    supported: tuple[SpecificationRecord, ...],
) -> tuple[GuardianFinding, ...]:
    """Keep current-snapshot failed probes actionable until explicitly cleared."""

    evidence = {item.evidence_id: item for item in memory.evidence}
    values = []
    for specification in supported:
        failures = [
            evidence[evidence_id]
            for evidence_id in specification.counterevidence
            if evidence_id in evidence
            and evidence[evidence_id].fresh
            and evidence[evidence_id].snapshot == request.candidate_commit
            and evidence[evidence_id].source_type is EvidenceSourceType.RUNTIME_PROBE
        ]
        active = []
        for failure in failures:
            cleared = any(
                candidate.fresh
                and candidate.snapshot == request.candidate_commit
                and candidate.source_type is EvidenceSourceType.RUNTIME_PROBE
                and candidate.command
                and candidate.command == failure.command
                and candidate.round > failure.round
                for evidence_id in specification.supporting_evidence
                if evidence_id in evidence
                for candidate in (evidence[evidence_id],)
            )
            if not cleared:
                active.append(failure)
        if active:
            values.append(
                GuardianFinding(
                    specification_id=specification.specification_id,
                    statement=specification.statement,
                    condition=specification.condition,
                    status=FindingStatus.VIOLATED,
                    evidence=tuple(active),
                    evidence_ids=tuple(item.evidence_id for item in active),
                    patch_assessment=(
                        "A retained runtime probe failed for the current candidate "
                        "snapshot; no later successful run of the same probe clears it."
                    ),
                    recommendation=(
                        "Reproduce and correct the failed behavior, then rerun the "
                        "same probe successfully."
                    ),
                )
            )
    return tuple(values)


def _executed_probe(
    rollout: AgentRunResult, evidence: Evidence
) -> Evidence | None:
    """Materialize runtime evidence from its authoritative trajectory event."""

    prefix = "runtime-probe:"
    if not evidence.path.startswith(prefix):
        return None
    tool_call_id = evidence.path.removeprefix(prefix)

    for event in rollout.trajectory:
        if (
            event.kind is not AgentEventKind.TOOL
            or event.provider_type != "item.completed"
        ):
            continue
        item = event.payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        if item.get("id") != tool_call_id:
            continue
        command = item.get("command")
        output = item.get("aggregated_output")
        exit_code = item.get("exit_code")
        if (
            isinstance(command, str)
            and isinstance(output, str)
            and isinstance(exit_code, int)
        ):
            return replace(evidence, command=command, output=output)
    return None


async def check_patch(
    request: GuardianRequest,
    config: GuardianConfig,
    executor: AgentExecutor,
    memory: SpecificationMemory,
) -> tuple[
    str,
    tuple[GuardianFinding, ...],
    tuple[GuardianFinding, ...],
    tuple[Evidence, ...],
    AgentRunResult | None,
    str | None,
]:
    supported = tuple(
        item
        for item in memory.specifications
        if item.status is SpecificationStatus.SUPPORTED
    )
    proposed = tuple(
        item
        for item in memory.specifications
        if item.status is SpecificationStatus.PROPOSED
    )
    task_evidence = _direct_task_evidence(request, supported + proposed)
    checkable = supported + (proposed if task_evidence else ())
    if not checkable:
        backlog = tuple(
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
                    else "Gather stronger task or independent repository evidence."
                ),
            )
            for item in memory.specifications
            if item.status
            in (
                SpecificationStatus.PROPOSED,
                SpecificationStatus.CONTESTED,
            )
        )
        return (
            "No supported local specification was available for definite patch checking.",
            (),
            backlog,
            (),
            None,
            None,
        )

    rollout = await executor.run(
        AgentRunRequest(
            instruction=patch_check_prompt(
                request, memory, max_findings=config.max_findings
            ),
            workspace=request.workspace,
            role=AgentRole.VERIFIER,
            timeout_seconds=config.rollout_timeout_seconds,
            model=config.aggregator_model,
            reasoning_effort=config.aggregator_reasoning_effort,
            policy=ExecutionPolicy(
                filesystem=FilesystemAccess.READ_ONLY,
                isolation=config.execution_isolation,
            ),
            metadata={"guardian_stage": "patch_check"},
        )
    )
    if not rollout.succeeded:
        message = rollout.error.message if rollout.error else rollout.status.value
        return "", (), (), (), rollout, f"patch checker: {message}"
    try:
        summary, findings, backlog, runtime_evidence = parse_patch_check(
            rollout.final_message,
            specifications=memory.specifications,
            evidence=memory.evidence + task_evidence,
        )
    except GuardianResponseError as exc:
        return "", (), (), (), rollout, f"patch checker: {exc}"

    validated_runtime = {}
    for item in runtime_evidence:
        executed = _executed_probe(rollout, item)
        if executed is None:
            continue
        scoped = replace(
            executed,
            evidence_id=(
                f"EV-PATCH-{request.candidate_commit[:12]}-{item.evidence_id}"
            ),
            snapshot=request.candidate_commit,
        )
        checked, _ = validate_evidence(request, scoped)
        if checked is not None:
            validated_runtime[item.evidence_id] = checked
    if runtime_evidence:
        runtime_ids = {item.evidence_id for item in runtime_evidence}
        findings = tuple(
            replace(
                item,
                evidence=tuple(
                    validated_runtime.get(evidence.evidence_id, evidence)
                    for evidence in item.evidence
                    if evidence.source_type is not EvidenceSourceType.RUNTIME_PROBE
                    or evidence.evidence_id in validated_runtime
                ),
                evidence_ids=tuple(
                    (
                        validated_runtime[evidence_id].evidence_id
                        if evidence_id in validated_runtime
                        else evidence_id
                    )
                    for evidence_id in item.evidence_ids
                    if evidence_id not in runtime_ids
                    or evidence_id in validated_runtime
                ),
            )
            for item in findings
        )

    supported_ids = {item.specification_id for item in supported}
    proposed_ids = {item.specification_id for item in proposed}
    records_by_specification = {item.specification_id: item for item in supported}
    admitted = []
    for item in findings:
        if item.status is not FindingStatus.VIOLATED or not item.evidence:
            continue
        cites_task = any(
            evidence.authority is EvidenceAuthority.NORMATIVE
            and evidence.source_type is EvidenceSourceType.TASK
            for evidence in item.evidence
        )
        record = records_by_specification.get(item.specification_id)
        linked_support = record is not None and any(
            evidence.evidence_id
            in set(record.supporting_evidence + record.counterevidence)
            or item.specification_id in evidence.supports
            or item.specification_id in evidence.opposes
            for evidence in item.evidence
        )
        if linked_support or (
            cites_task and item.specification_id in supported_ids.union(proposed_ids)
        ):
            admitted.append(item)
    monotonic = _active_failed_probes(request, memory, supported)
    admitted_by_specification = {item.specification_id: item for item in admitted}
    for item in monotonic:
        admitted_by_specification[item.specification_id] = item
    admitted = tuple(admitted_by_specification.values())
    definite = admitted[: config.max_findings]
    admitted_specification_ids = {item.specification_id for item in definite}
    uncertainty = [
        item
        for item in backlog
        if item.specification_id not in admitted_specification_ids
    ]
    uncertainty.extend(
        item
        for item in findings
        if item.specification_id not in admitted_specification_ids
        and (
            item.status is FindingStatus.UNCERTAIN
            or item.specification_id not in supported_ids
        )
    )
    uncertainty.extend(
        replace(
            item,
            status=FindingStatus.UNCERTAIN,
            patch_assessment=(
                item.patch_assessment
                + " The patch checker did not cite evidence linked to this "
                "supported specification."
            ).strip(),
            recommendation="Recheck the specification-to-evidence relationship.",
        )
        for item in findings
        if item.status is FindingStatus.VIOLATED
        and item.specification_id in supported_ids.union(proposed_ids)
        and item.specification_id not in admitted_specification_ids
    )
    represented_ids = {item.specification_id for item in findings} | {
        item.specification_id for item in backlog
    }
    uncertainty.extend(
        GuardianFinding(
            specification_id=item.specification_id,
            statement=item.statement,
            condition=item.condition,
            status=FindingStatus.UNCERTAIN,
            evidence=(),
            evidence_ids=item.supporting_evidence,
            patch_assessment=(
                "The patch checker omitted this supported specification, so no "
                "patch verdict is justified."
            ),
            recommendation=(
                "Inspect the candidate patch against this specification explicitly."
            ),
        )
        for item in supported
        if item.specification_id not in represented_ids
    )
    unique_uncertainty = {}
    for item in uncertainty:
        unique_uncertainty[item.specification_id] = item
    return (
        summary,
        definite,
        tuple(unique_uncertainty.values()),
        tuple(validated_runtime.values()),
        rollout,
        None,
    )


__all__ = ["check_patch"]
