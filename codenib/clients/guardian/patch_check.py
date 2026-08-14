# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Final patch checking against supported local specifications only."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace

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
from .prompts import patch_check_prompt, patch_check_repair_prompt
from .types import (
    ContextFidelity,
    Evidence,
    EvidenceAuthority,
    EvidenceSourceType,
    FindingStatus,
    GuardianConfig,
    GuardianFinding,
    GuardianRequest,
    ProbeCheckMetrics,
    ProbeOutcome,
    SpecificationMemory,
    SpecificationRecord,
    SpecificationStatus,
)


@dataclass(frozen=True, slots=True)
class PatchCheckExecution:
    """Patch-check invocations and controller-derived probe accounting."""

    rollouts: tuple[AgentRunResult, ...] = ()
    metrics: ProbeCheckMetrics = ProbeCheckMetrics()
    assessed_specification_ids: tuple[str, ...] = ()


def _direct_task_evidence(request: GuardianRequest) -> tuple[Evidence, ...]:
    """Expose verbatim task text as first-class normative checker evidence."""

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
            # The checker may cite this exact contract to adjudicate a proposed
            # specification, but the controller must not pre-assert that the
            # contract entails every specification merely by exposing it.
            supports=(),
        )
        for item in request.task_context
        if item.fidelity is ContextFidelity.VERBATIM
    )


def _active_failed_probes(
    request: GuardianRequest,
    memory: SpecificationMemory,
    supported: tuple[SpecificationRecord, ...],
    current_probes: tuple[Evidence, ...] = (),
) -> tuple[GuardianFinding, ...]:
    """Keep current-snapshot failed probes actionable until explicitly cleared."""

    evidence = {item.evidence_id: item for item in (*memory.evidence, *current_probes)}
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
            clearing_candidates = tuple(
                candidate
                for candidate in current_probes
                if specification.specification_id in candidate.supports
            ) + tuple(
                evidence[evidence_id]
                for evidence_id in specification.supporting_evidence
                if evidence_id in evidence
            )
            cleared = any(
                candidate.fresh
                and candidate.snapshot == request.candidate_commit
                and candidate.source_type is EvidenceSourceType.RUNTIME_PROBE
                and candidate.probe_key
                and candidate.probe_key == failure.probe_key
                and candidate.round > failure.round
                for candidate in clearing_candidates
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


_PROBE_MARKER = re.compile(
    r"GUARDIAN_PROBE_KEY=(?P<quote>['\"]?)(?P<key>[A-Za-z0-9_.:-]+)(?P=quote)"
    r"[ \t]+GUARDIAN_SPECIFICATION_ID=(?P<spec_quote>['\"]?)"
    r"(?P<spec>[A-Za-z0-9_.:-]+)(?P=spec_quote)"
)


def _command_probe_identity(command: str) -> tuple[str, str] | None:
    match = _PROBE_MARKER.search(command)
    if match is None:
        return None
    return match.group("key"), match.group("spec")


def _executed_probe(rollout: AgentRunResult, evidence: Evidence) -> Evidence | None:
    """Materialize runtime evidence from its authoritative trajectory event."""

    prefix = "runtime-probe:"
    if not evidence.path.startswith(prefix):
        return None
    tool_call_id = evidence.path.removeprefix(prefix)

    exact = []
    marked = []
    for event in rollout.trajectory:
        if (
            event.kind is not AgentEventKind.TOOL
            or event.provider_type != "item.completed"
        ):
            continue
        item = event.payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        command = item.get("command")
        output = item.get("aggregated_output")
        exit_code = item.get("exit_code")
        if not (
            isinstance(command, str)
            and isinstance(output, str)
            and isinstance(exit_code, int)
            and not isinstance(exit_code, bool)
        ):
            continue
        candidate = (command, output, exit_code)
        if item.get("id") == tool_call_id:
            exact.append(candidate)
        if _command_probe_identity(command) == (
            evidence.probe_key,
            evidence.probe_specification_id,
        ):
            marked.append(candidate)
    candidates = exact or marked
    if len(candidates) != 1:
        return None
    command, output, exit_code = candidates[0]
    if exit_code == 0:
        outcome = ProbeOutcome.SATISFIED
    elif exit_code == 10:
        outcome = ProbeOutcome.VIOLATED
    else:
        outcome = ProbeOutcome.INCONCLUSIVE
    return replace(
        evidence,
        command=command,
        output=output,
        exit_code=exit_code,
        probe_outcome=outcome,
        supports=(
            (evidence.probe_specification_id,)
            if outcome is ProbeOutcome.SATISFIED
            else ()
        ),
        opposes=(
            (evidence.probe_specification_id,)
            if outcome is ProbeOutcome.VIOLATED
            else ()
        ),
    )


def _command_catalog(rollout: AgentRunResult) -> tuple[dict[str, object], ...]:
    """Describe already-executed commands without exposing mutable capabilities."""

    values = []
    for event in rollout.trajectory:
        if (
            event.kind is not AgentEventKind.TOOL
            or event.provider_type != "item.completed"
        ):
            continue
        item = event.payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        command = item.get("command")
        exit_code = item.get("exit_code")
        if not isinstance(command, str) or not isinstance(exit_code, int):
            continue
        identity = _command_probe_identity(command)
        values.append(
            {
                "tool_call_id": item.get("id"),
                "command": command,
                "exit_code": exit_code,
                "probe_key": identity[0] if identity else "",
                "specification_id": identity[1] if identity else "",
            }
        )
    return tuple(values)


def _protocol_errors(
    rollout: AgentRunResult,
    runtime_evidence: tuple[Evidence, ...],
    parse_errors: tuple[str, ...],
    *,
    max_probes: int,
    allowed_probe_specification_ids: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Return deterministic reasons why declared probes cannot be admitted."""

    errors = list(parse_errors)
    executed_probes = sum(
        bool(item["probe_key"] and item["specification_id"])
        for item in _command_catalog(rollout)
    )
    for item in runtime_evidence[max_probes:]:
        errors.append(f"runtime_evidence_id[{item.evidence_id}]: probe_budget_exceeded")
    if executed_probes > max_probes and executed_probes > len(runtime_evidence):
        errors.append(
            f"runtime_evidence: undeclared_probe_budget_exceeded "
            f"({executed_probes} > {max_probes})"
        )
    tool_call_counts = Counter(
        item.path.removeprefix("runtime-probe:") for item in runtime_evidence
    )
    probe_key_counts = Counter(item.probe_key for item in runtime_evidence)
    specification_counts = Counter(
        item.probe_specification_id for item in runtime_evidence
    )
    evidence_id_counts = Counter(item.evidence_id for item in runtime_evidence)
    for item in runtime_evidence:
        tool_call_id = item.path.removeprefix("runtime-probe:")
        prefix = f"runtime_evidence_id[{item.evidence_id}]"
        if (
            allowed_probe_specification_ids is not None
            and item.probe_specification_id not in allowed_probe_specification_ids
        ):
            errors.append(f"{prefix}: outside_probe_frontier")
        elif tool_call_counts[tool_call_id] != 1:
            errors.append(f"{prefix}: duplicate_tool_call_id")
        elif probe_key_counts[item.probe_key] != 1:
            errors.append(f"{prefix}: duplicate_probe_key")
        elif specification_counts[item.probe_specification_id] != 1:
            errors.append(f"{prefix}: duplicate_specification_probe")
        elif evidence_id_counts[item.evidence_id] != 1:
            errors.append(f"{prefix}: duplicate_evidence_id")
        elif _executed_probe(rollout, item) is None:
            errors.append(f"{prefix}: unlinked_execution")
    return tuple(errors)


def _reason(error: str) -> str:
    return error.rsplit(":", 1)[-1].split(" (", 1)[0].strip().replace(" ", "_")


_PROCESS_ONLY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:must|should|needs? to)\s+(?:be\s+)?(?:covered|tested)\b",
        r"\b(?:test|testing)\s+coverage\b",
        r"\b(?:unit|integration|end-to-end|e2e|regression)\s+tests?\s+"
        r"(?:must|should|need|are required)\b",
        r"\badd(?:ed|ing)?\s+(?:unit|integration|end-to-end|e2e|regression)?\s*tests?\b",
    )
)


def _task_backed(
    specification: SpecificationRecord, evidence_by_id: dict[str, Evidence]
) -> bool:
    return any(
        evidence_by_id[evidence_id].source_type is EvidenceSourceType.TASK
        and evidence_by_id[evidence_id].authority is EvidenceAuthority.NORMATIVE
        for evidence_id in specification.supporting_evidence
        if evidence_id in evidence_by_id
    )


def is_patch_review_specification(
    specification: SpecificationRecord, evidence_by_id: dict[str, Evidence]
) -> bool:
    """Return whether a record is a software property rather than process advice."""

    if _task_backed(specification, evidence_by_id):
        return True
    text = f"{specification.statement} {specification.condition}"
    return not any(pattern.search(text) for pattern in _PROCESS_ONLY_PATTERNS)


def _patch_specifications(
    request: GuardianRequest,
    memory: SpecificationMemory,
) -> tuple[SpecificationRecord, ...]:
    """Expose every reviewable contract; probing is budgeted separately."""

    evidence_by_id = {item.evidence_id: item for item in memory.evidence}

    def priority(item: SpecificationRecord) -> tuple[int, int]:
        current_failed_probe = any(
            evidence_id in evidence_by_id
            and evidence_by_id[evidence_id].source_type
            is EvidenceSourceType.RUNTIME_PROBE
            and evidence_by_id[evidence_id].probe_outcome is ProbeOutcome.VIOLATED
            and evidence_by_id[evidence_id].snapshot == request.candidate_commit
            for evidence_id in item.counterevidence
        )
        if current_failed_probe:
            return (0, 0)
        if _task_backed(item, evidence_by_id):
            return (1, 0)
        if item.counterevidence:
            return (2, 0)
        return (3, 0)

    supported = [
        item
        for item in memory.specifications
        if item.status is SpecificationStatus.SUPPORTED
        and is_patch_review_specification(item, evidence_by_id)
    ]
    proposed = [
        item
        for item in memory.specifications
        if item.status is SpecificationStatus.PROPOSED
        and is_patch_review_specification(item, evidence_by_id)
    ]
    supported.sort(key=priority)
    proposed.sort(key=priority)
    return tuple(supported + proposed)


_PUBLIC_LIFECYCLE_TERMS = (
    "evaluate",
    "predict",
    "/predict",
    "served",
    "public path",
    "model call",
    "persisted model",
    "load and apply",
)
_ARTIFACT_LIFECYCLE_TERMS = (
    "persist",
    "artifact",
    "description.json",
    "feature_schema",
    "results directory",
    "export",
)


def _probe_frontier(
    request: GuardianRequest,
    memory: SpecificationMemory,
    specifications: tuple[SpecificationRecord, ...],
    *,
    limit: int,
) -> tuple[SpecificationRecord, ...]:
    """Rank executable contracts without hiding non-probed specifications."""

    evidence_by_id = {item.evidence_id: item for item in memory.evidence}

    def priority(item: SpecificationRecord) -> tuple[int, int, int, str]:
        text = f"{item.statement} {item.condition}".casefold()
        current_violation = any(
            evidence_id in evidence_by_id
            and evidence_by_id[evidence_id].source_type
            is EvidenceSourceType.RUNTIME_PROBE
            and evidence_by_id[evidence_id].probe_outcome is ProbeOutcome.VIOLATED
            and evidence_by_id[evidence_id].snapshot == request.candidate_commit
            for evidence_id in item.counterevidence
        )
        current_probe = any(
            evidence_id in evidence_by_id
            and evidence_by_id[evidence_id].source_type
            is EvidenceSourceType.RUNTIME_PROBE
            and evidence_by_id[evidence_id].snapshot == request.candidate_commit
            for evidence_id in item.supporting_evidence + item.counterevidence
        )
        lifecycle_rank = (
            0
            if any(term in text for term in _PUBLIC_LIFECYCLE_TERMS)
            else 1 if any(term in text for term in _ARTIFACT_LIFECYCLE_TERMS) else 2
        )
        return (
            0 if current_violation else 1,
            lifecycle_rank,
            1 if current_probe else 0,
            item.specification_id,
        )

    return tuple(sorted(specifications, key=priority)[:limit])


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
    PatchCheckExecution,
    str | None,
]:
    all_supported = tuple(
        item
        for item in memory.specifications
        if item.status is SpecificationStatus.SUPPORTED
    )
    all_proposed = tuple(
        item
        for item in memory.specifications
        if item.status is SpecificationStatus.PROPOSED
    )
    selected = _patch_specifications(request, memory)
    selected_ids = {item.specification_id for item in selected}
    supported = tuple(
        item for item in all_supported if item.specification_id in selected_ids
    )
    proposed = tuple(
        item for item in all_proposed if item.specification_id in selected_ids
    )
    task_evidence = _direct_task_evidence(request)
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
            and is_patch_review_specification(
                item, {evidence.evidence_id: evidence for evidence in memory.evidence}
            )
        )
        return (
            "No supported local specification was available for definite patch checking.",
            (),
            backlog,
            (),
            PatchCheckExecution(),
            None,
        )

    probe_frontier = _probe_frontier(
        request,
        memory,
        checkable,
        limit=config.max_patch_probes,
    )
    allowed_probe_ids = frozenset(item.specification_id for item in probe_frontier)

    rollout = await executor.run(
        AgentRunRequest(
            instruction=patch_check_prompt(
                request,
                memory,
                max_findings=config.max_findings,
                max_probes=config.max_patch_probes,
                specifications=checkable,
                probe_specifications=probe_frontier,
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
        return (
            "",
            (),
            (),
            (),
            PatchCheckExecution(rollouts=(rollout,)),
            f"patch checker: {message}",
        )

    repair_rollout = None
    repair_attempted = False
    repair_accepted = False
    response = rollout.final_message
    try:
        parsed = parse_patch_check(
            response,
            specifications=checkable,
            evidence=memory.evidence + task_evidence,
        )
        primary_errors = _protocol_errors(
            rollout,
            parsed[3],
            parsed[4],
            max_probes=config.max_patch_probes,
            allowed_probe_specification_ids=allowed_probe_ids,
        )
    except GuardianResponseError as exc:
        parsed = None
        primary_errors = (f"response: {exc}",)
    initial_parsed = parsed
    initial_errors = primary_errors

    if primary_errors:
        repair_attempted = True
        repair_rollout = await executor.run(
            AgentRunRequest(
                instruction=patch_check_repair_prompt(
                    response,
                    validation_errors=primary_errors,
                    command_catalog=_command_catalog(rollout),
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
                metadata={"guardian_stage": "patch_check_repair"},
            )
        )
        repair_used_tools = any(
            event.kind is AgentEventKind.TOOL for event in repair_rollout.trajectory
        )
        if repair_rollout.succeeded and not repair_used_tools:
            try:
                repaired = parse_patch_check(
                    repair_rollout.final_message,
                    specifications=checkable,
                    evidence=memory.evidence + task_evidence,
                )
                repaired_errors = _protocol_errors(
                    rollout,
                    repaired[3],
                    repaired[4],
                    max_probes=config.max_patch_probes,
                    allowed_probe_specification_ids=allowed_probe_ids,
                )
                if parsed is None or len(repaired_errors) < len(primary_errors):
                    parsed = repaired
                    primary_errors = repaired_errors
                    repair_accepted = True
            except GuardianResponseError:
                pass

    executions = tuple(item for item in (rollout, repair_rollout) if item is not None)
    if parsed is None:
        metrics = ProbeCheckMetrics(
            rejected_by_reason=(("malformed_response", 1),),
            repair_attempted=repair_attempted,
            repair_accepted=repair_accepted,
        )
        return (
            "",
            (),
            (),
            (),
            PatchCheckExecution(rollouts=executions, metrics=metrics),
            f"patch checker: {primary_errors[0]}",
        )

    summary, findings, backlog, runtime_evidence, parse_errors = parsed
    protocol_errors = _protocol_errors(
        rollout,
        runtime_evidence,
        parse_errors,
        max_probes=config.max_patch_probes,
        allowed_probe_specification_ids=allowed_probe_ids,
    )

    invalid_probe_ids = {
        error.removeprefix("runtime_evidence_id[").split("]", 1)[0]
        for error in protocol_errors
        if error.startswith("runtime_evidence_id[")
    }
    validated_runtime = {}
    validation_errors = []
    for item in runtime_evidence:
        if item.evidence_id in invalid_probe_ids:
            continue
        executed = _executed_probe(rollout, item)
        if executed is None:
            continue
        prior_sequence = max(
            (
                evidence.round
                for evidence in memory.evidence
                if evidence.source_type is EvidenceSourceType.RUNTIME_PROBE
                and evidence.probe_key == item.probe_key
            ),
            default=0,
        )
        scoped = replace(
            executed,
            evidence_id=(
                f"EV-PATCH-{request.candidate_commit[:12]}-{item.evidence_id}"
            ),
            snapshot=request.candidate_commit,
            round=prior_sequence + 1,
        )
        checked, error = validate_evidence(request, scoped)
        if checked is not None:
            validated_runtime[item.evidence_id] = checked
        else:
            validation_errors.append(
                f"runtime_evidence_id[{item.evidence_id}]: evidence_validation"
                + (f" ({error})" if error else "")
            )
    if runtime_evidence:
        runtime_ids = {item.evidence_id for item in runtime_evidence}
        normalized_findings = []
        for item in findings:
            claimed_runtime_ids = tuple(
                evidence_id
                for evidence_id in item.evidence_ids
                if evidence_id in runtime_ids
            )
            valid_probes = tuple(
                validated_runtime[evidence_id]
                for evidence_id in claimed_runtime_ids
                if evidence_id in validated_runtime
                and item.specification_id
                in (
                    validated_runtime[evidence_id].supports
                    + validated_runtime[evidence_id].opposes
                )
            )
            status = item.status
            assessment = item.patch_assessment
            recommendation = item.recommendation
            if claimed_runtime_ids:
                outcomes = {probe.probe_outcome for probe in valid_probes}
                if not valid_probes:
                    status = FindingStatus.UNCERTAIN
                    assessment = (
                        assessment
                        + " The cited probe was invalid, duplicated, inconclusive, "
                        "or not uniquely linked to this specification."
                    ).strip()
                elif ProbeOutcome.VIOLATED in outcomes:
                    status = FindingStatus.VIOLATED
                elif ProbeOutcome.INCONCLUSIVE in outcomes:
                    status = FindingStatus.UNCERTAIN
                else:
                    status = FindingStatus.SATISFIED
                if status is not item.status and valid_probes:
                    probe_keys = ", ".join(
                        sorted(probe.probe_key for probe in valid_probes)
                    )
                    assessment = (
                        f"Authoritative execution of runtime probe(s) {probe_keys} "
                        f"produced a {status.value} outcome, overriding the patch "
                        f"checker's declared {item.status.value} verdict. Inspect "
                        "the retained command and output before changing the patch."
                    )
                    recommendation = (
                        "Reproduce the focused probe and verify that its assertion "
                        "tests only this specification before acting on the result."
                    )
            normalized_findings.append(
                replace(
                    item,
                    status=status,
                    patch_assessment=assessment,
                    recommendation=recommendation,
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
            )
        findings = tuple(normalized_findings)

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
    monotonic = _active_failed_probes(
        request, memory, all_supported, tuple(validated_runtime.values())
    )
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
        and item.status is FindingStatus.UNCERTAIN
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
    accepted_probes = tuple(validated_runtime.values())
    rejection_counts = Counter(
        _reason(error) for error in (*protocol_errors, *validation_errors)
    )
    final_declared = len(runtime_evidence) + len(parse_errors)
    if initial_parsed is None:
        initial_declared = 1
    else:
        initial_declared = len(initial_parsed[3]) + len(initial_parsed[4])
    removed_by_repair = (
        max(0, initial_declared - final_declared) if repair_accepted else 0
    )
    if removed_by_repair:
        rejection_counts["removed_by_repair"] += removed_by_repair
    repaired_count = (
        max(
            0,
            len(initial_errors) - len(protocol_errors) - removed_by_repair,
        )
        if repair_accepted
        else 0
    )
    metrics = ProbeCheckMetrics(
        declared=initial_declared if repair_attempted else final_declared,
        accepted=len(accepted_probes),
        satisfied=sum(
            item.probe_outcome is ProbeOutcome.SATISFIED for item in accepted_probes
        ),
        violated=sum(
            item.probe_outcome is ProbeOutcome.VIOLATED for item in accepted_probes
        ),
        inconclusive=sum(
            item.probe_outcome is ProbeOutcome.INCONCLUSIVE for item in accepted_probes
        ),
        rejected_by_reason=tuple(sorted(rejection_counts.items())),
        repaired=repaired_count,
        repair_attempted=repair_attempted,
        repair_accepted=repair_accepted,
    )
    return (
        summary,
        definite,
        tuple(unique_uncertainty.values()),
        accepted_probes,
        PatchCheckExecution(
            rollouts=executions,
            metrics=metrics,
            assessed_specification_ids=tuple(sorted(represented_ids)),
        ),
        None,
    )


__all__ = [
    "PatchCheckExecution",
    "check_patch",
    "is_patch_review_specification",
]
