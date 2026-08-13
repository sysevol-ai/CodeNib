# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Strong-model merge and deterministic status adjudication."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from ..execution import (
    AgentExecutor,
    AgentRole,
    AgentRunRequest,
    AgentRunResult,
    ExecutionPolicy,
    FilesystemAccess,
)
from .normalization import GuardianResponseError, parse_aggregation
from .prompts import aggregation_prompt
from .types import (
    CandidateSpecification,
    ContextFidelity,
    Evidence,
    EvidenceAuthority,
    EvidenceSourceType,
    GuardianConfig,
    GuardianRequest,
    SpecificationMemory,
    SpecificationRecord,
    SpecificationStatus,
    TaskContextSource,
)


def _specification_key(statement: str, condition: str) -> tuple[str, str]:
    return statement.strip().casefold(), condition.strip().casefold()


def _specification_id(statement: str, condition: str) -> str:
    value = "\0".join(_specification_key(statement, condition))
    return f"LS-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def _merge_specification_records(
    existing: tuple[SpecificationRecord, ...],
    incoming: tuple[SpecificationRecord, ...],
) -> tuple[SpecificationRecord, ...]:
    """Merge records without allowing model-selected IDs to rewrite identity."""

    by_id = {item.specification_id: item for item in existing}
    by_key = {
        _specification_key(item.statement, item.condition): item.specification_id
        for item in existing
    }
    for record in incoming:
        key = _specification_key(record.statement, record.condition)
        supplied = by_id.get(record.specification_id)
        if supplied is not None and _specification_key(
            supplied.statement, supplied.condition
        ) != key:
            specification_id = by_key.get(key) or _specification_id(
                record.statement, record.condition
            )
        else:
            specification_id = by_key.get(key) or record.specification_id
        previous = by_id.get(specification_id)
        value = replace(
            record,
            specification_id=specification_id,
            statement=previous.statement if previous else record.statement,
            condition=previous.condition if previous else record.condition,
        )
        by_id[specification_id] = value
        by_key[key] = specification_id
    return tuple(by_id.values())


def _materialize_task_evidence(
    request: GuardianRequest,
    records: tuple[SpecificationRecord, ...],
) -> tuple[tuple[SpecificationRecord, ...], tuple[Evidence, ...]]:
    """Canonicalize direct-task citations and retain their normative source."""

    contexts = {
        item.context_id: item
        for item in request.task_context
        if item.fidelity is ContextFidelity.VERBATIM
        and item.source is not TaskContextSource.SOLVER_SUMMARY
    }
    aliases = {
        f"EV-TASK-{context_id}": context_id for context_id in contexts
    }
    normalized = tuple(
        replace(
            record,
            supporting_evidence=tuple(
                dict.fromkeys(
                    aliases.get(evidence_id, evidence_id)
                    for evidence_id in record.supporting_evidence
                )
            ),
        )
        for record in records
    )
    evidence = []
    for context_id, context in contexts.items():
        supported = tuple(
            record.specification_id
            for record in normalized
            if context_id in record.supporting_evidence
        )
        if not supported:
            continue
        evidence.append(
            Evidence(
                evidence_id=context_id,
                path=context_id,
                line_start=1,
                line_end=max(1, context.content.count("\n") + 1),
                description="Verbatim requirement supplied to the coding system.",
                source_type=EvidenceSourceType.TASK,
                authority=EvidenceAuthority.NORMATIVE,
                quote=context.content,
                supports=supported,
                acquired_by="controller",
                fresh=True,
            )
        )
    return normalized, tuple(evidence)


def _evidence_status(
    record: SpecificationRecord, evidence: dict[str, Evidence]
) -> tuple[SpecificationStatus, str]:
    supporting = [
        evidence[value]
        for value in record.supporting_evidence
        if value in evidence and evidence[value].fresh
    ]
    counter = [evidence[value] for value in record.counterevidence if value in evidence]
    if not supporting:
        return SpecificationStatus.REJECTED, "no validated supporting evidence remains"
    if all(
        item.authority is EvidenceAuthority.DERIVED
        or item.source_type
        in (EvidenceSourceType.SOLVER_MESSAGE, EvidenceSourceType.CANDIDATE_PATCH)
        for item in supporting
    ):
        return (
            SpecificationStatus.REJECTED,
            "support comes only from solver interpretation or the candidate patch",
        )
    direct_counter = any(
        item.authority in (EvidenceAuthority.NORMATIVE, EvidenceAuthority.CONVENTIONAL)
        for item in counter
    )
    if direct_counter:
        return (
            SpecificationStatus.CONTESTED,
            "unresolved direct counterevidence remains",
        )
    if any(item.authority is EvidenceAuthority.NORMATIVE for item in supporting):
        return (
            SpecificationStatus.SUPPORTED,
            "direct normative evidence entails the property",
        )
    conventional_sources = {
        (item.source_type.value, item.path)
        for item in supporting
        if item.authority is EvidenceAuthority.CONVENTIONAL
    }
    if len(conventional_sources) >= 2:
        return (
            SpecificationStatus.SUPPORTED,
            "two independent conventional sources support the property "
            "without direct counterevidence",
        )
    return (
        SpecificationStatus.PROPOSED,
        "evidence is relevant but does not meet the supported promotion rule",
    )


def adjudicate_records(
    records: tuple[SpecificationRecord, ...], evidence: tuple[Evidence, ...]
) -> tuple[SpecificationRecord, ...]:
    """Apply non-negotiable evidence rules after model aggregation."""

    evidence_by_id = {item.evidence_id: item for item in evidence}
    preliminary = {
        record.specification_id: _evidence_status(record, evidence_by_id)
        for record in records
    }
    values = []
    for record in records:
        status, reason = preliminary[record.specification_id]
        credible_conflicts = tuple(
            target
            for target in record.related_entries.conflicts_with
            if target != record.specification_id
            and target in preliminary
            and preliminary[target][0] is not SpecificationStatus.REJECTED
        )
        if credible_conflicts and status is not SpecificationStatus.REJECTED:
            status = SpecificationStatus.CONTESTED
            reason = (
                "credible competing interpretations remain linked by direct "
                "conflict: " + ", ".join(sorted(credible_conflicts))
            )
        values.append(replace(record, status=status, status_reason=reason))
    return tuple(values)


def _link_official_specifications(
    evidence: dict[str, Evidence], records: tuple[SpecificationRecord, ...]
) -> dict[str, Evidence]:
    """Materialize candidate-to-official evidence links without losing origin."""

    support_links: dict[str, set[str]] = {}
    opposition_links: dict[str, set[str]] = {}
    for record in records:
        for provenance in record.provenance:
            support_links.setdefault(provenance.candidate_id, set()).add(
                record.specification_id
            )
            opposition_links.setdefault(provenance.candidate_id, set()).add(
                record.specification_id
            )

    linked: dict[str, Evidence] = {}
    for evidence_id, item in evidence.items():
        supports = set(item.supports)
        opposes = set(item.opposes)
        for value in tuple(supports):
            supports.update(support_links.get(value, ()))
        for value in tuple(opposes):
            opposes.update(opposition_links.get(value, ()))
        linked[evidence_id] = replace(
            item,
            supports=tuple(sorted(supports)),
            opposes=tuple(sorted(opposes)),
        )
    return linked


def _drop_unlinked_references(
    records: tuple[SpecificationRecord, ...], evidence: dict[str, Evidence]
) -> tuple[SpecificationRecord, ...]:
    """Prevent an aggregator from attaching unrelated evidence by identifier."""

    values = []
    for record in records:
        supporting = tuple(
            evidence_id
            for evidence_id in record.supporting_evidence
            if evidence_id in evidence
            and record.specification_id in evidence[evidence_id].supports
        )
        counter = tuple(
            evidence_id
            for evidence_id in record.counterevidence
            if evidence_id in evidence
            and record.specification_id in evidence[evidence_id].opposes
        )
        values.append(
            replace(
                record,
                supporting_evidence=tuple(dict.fromkeys(supporting)),
                counterevidence=tuple(dict.fromkeys(counter)),
            )
        )
    return tuple(values)


def _merge_memory(
    request: GuardianRequest,
    memory: SpecificationMemory,
    records: tuple[SpecificationRecord, ...],
    candidates: tuple[CandidateSpecification, ...],
    *,
    snapshot: str,
) -> SpecificationMemory:
    evidence_by_id = {item.evidence_id: item for item in memory.evidence}
    for candidate in candidates:
        for item in candidate.supporting_evidence + candidate.counterevidence:
            evidence_by_id[item.evidence_id] = replace(item, snapshot=snapshot)
    records, task_evidence = _materialize_task_evidence(request, records)
    for item in task_evidence:
        evidence_by_id[item.evidence_id] = replace(item, snapshot=snapshot)
    combined = _merge_specification_records(memory.specifications, records)
    evidence_by_id = _link_official_specifications(evidence_by_id, combined)
    combined = _drop_unlinked_references(combined, evidence_by_id)
    combined = adjudicate_records(combined, tuple(evidence_by_id.values()))
    snapshots = memory.observed_snapshots
    if snapshot not in snapshots:
        snapshots += (snapshot,)
    return replace(
        memory,
        specifications=tuple(sorted(combined, key=lambda item: item.specification_id)),
        evidence=tuple(
            sorted(evidence_by_id.values(), key=lambda item: item.evidence_id)
        ),
        observed_snapshots=snapshots,
    )


async def aggregate(
    request: GuardianRequest,
    config: GuardianConfig,
    executor: AgentExecutor,
    memory: SpecificationMemory,
    candidates: tuple[CandidateSpecification, ...],
    *,
    round_number: int,
) -> tuple[str, SpecificationMemory, AgentRunResult, str | None]:
    rollout = await executor.run(
        AgentRunRequest(
            instruction=aggregation_prompt(
                request, memory, candidates, round_number=round_number
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
            metadata={"guardian_stage": "aggregation", "round": str(round_number)},
        )
    )
    if not rollout.succeeded:
        message = rollout.error.message if rollout.error else rollout.status.value
        return "", memory, rollout, f"round {round_number} aggregator: {message}"
    try:
        summary, records = parse_aggregation(rollout.final_message)
    except GuardianResponseError as exc:
        return "", memory, rollout, f"round {round_number} aggregator: {exc}"
    represented = {
        item.candidate_id for record in records for item in record.provenance
    }
    missing = {candidate.candidate_id for candidate in candidates} - represented
    if missing:
        return (
            "",
            memory,
            rollout,
            "round "
            f"{round_number} aggregator omitted candidates: {', '.join(sorted(missing))}",
        )
    return (
        summary,
        _merge_memory(
            request,
            memory,
            records,
            candidates,
            snapshot=request.candidate_commit,
        ),
        rollout,
        None,
    )


__all__ = ["adjudicate_records", "aggregate"]
