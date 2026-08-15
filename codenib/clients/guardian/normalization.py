# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Strict semantic normalization for Guardian model responses."""

from __future__ import annotations

import json
import re
from typing import Any

from .types import (
    CandidateSpecification,
    Evidence,
    EvidenceAuthority,
    EvidenceSourceType,
    ExplorationExperience,
    ExplorationTrace,
    ExplorerOutput,
    FindingStatus,
    GuardianFinding,
    InvestigationBrief,
    SpecificationProvenance,
    SpecificationRecord,
    SpecificationRelations,
    SpecificationStatus,
)

_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)
_IDENTIFIER_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


class GuardianResponseError(ValueError):
    """An agent completed without a valid Guardian response."""


def _object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    match = _FENCE.search(stripped)
    if match:
        stripped = match.group(1)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise GuardianResponseError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise GuardianResponseError("response must be a JSON object")
    return value


def _strings(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(str(value).strip() for value in raw if str(value).strip())


def _scoped_identifier(
    prefix: str,
    raw: object,
    *,
    explorer: str,
    round_number: int,
    index: int,
    namespace: str = "",
) -> str:
    """Turn rollout-local model identifiers into review-global identifiers.

    Models naturally reuse short identifiers such as ``C-1`` and ``EV-1``.
    Those identifiers are meaningful only within one explorer response.  Memory
    is shared across explorers and rounds, so retaining the local value permits
    later evidence to overwrite unrelated earlier evidence.
    """

    local = _IDENTIFIER_COMPONENT.sub("-", str(raw or "").strip()).strip("-._")
    if not local:
        local = str(index)
    owner = _IDENTIFIER_COMPONENT.sub("-", explorer).strip("-._") or "explorer"
    scope = _IDENTIFIER_COMPONENT.sub("-", namespace).strip("-._")
    scoped = f"-{scope}" if scope else ""
    return f"{prefix}{scoped}-R{round_number}-{owner}-{local}"


def _source(value: object) -> EvidenceSourceType:
    aliases = {
        "repository": EvidenceSourceType.DOCUMENTATION,
        "runtime": EvidenceSourceType.RUNTIME_PROBE,
        "solver": EvidenceSourceType.SOLVER_MESSAGE,
    }
    text = str(value or "documentation")
    return aliases.get(text, EvidenceSourceType(text))


def _authority(value: object, source: EvidenceSourceType) -> EvidenceAuthority:
    aliases = {
        "task": EvidenceAuthority.NORMATIVE,
        "repository": EvidenceAuthority.CONVENTIONAL,
        "test": EvidenceAuthority.CONVENTIONAL,
        "runtime": EvidenceAuthority.BEHAVIORAL,
        "solver": EvidenceAuthority.DERIVED,
    }
    text = str(value or "")
    if text in aliases:
        return aliases[text]
    if text:
        return EvidenceAuthority(text)
    if source is EvidenceSourceType.TASK:
        return EvidenceAuthority.NORMATIVE
    if source in (
        EvidenceSourceType.RUNTIME_PROBE,
        EvidenceSourceType.EXISTING_BEHAVIOR,
    ):
        return EvidenceAuthority.BEHAVIORAL
    if source in (
        EvidenceSourceType.SOLVER_MESSAGE,
        EvidenceSourceType.CANDIDATE_PATCH,
    ):
        return EvidenceAuthority.DERIVED
    return EvidenceAuthority.CONVENTIONAL


def _evidence(
    raw: object,
    *,
    explorer: str,
    round_number: int,
    supports: str = "",
    opposes: str = "",
    namespace: str = "",
) -> tuple[Evidence, ...]:
    if not isinstance(raw, list):
        return ()
    values: list[Evidence] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            continue
        try:
            source = _source(item.get("source_type") or item.get("authority"))
            authority = _authority(item.get("authority"), source)
        except ValueError:
            continue
        locator = item.get("locator") if isinstance(item.get("locator"), dict) else item
        path = str(
            locator.get("path")
            or locator.get("message_id")
            or locator.get("command_id")
            or ""
        ).strip()
        observation = str(
            item.get("observation") or item.get("description") or ""
        ).strip()
        try:
            start = max(1, int(locator.get("line_start", 1)))
            end = max(start, int(locator.get("line_end", start)))
        except (TypeError, ValueError):
            start = end = 1
        if not path or not observation:
            continue
        evidence_id = _scoped_identifier(
            "EV",
            item.get("id") or item.get("evidence_id"),
            explorer=explorer,
            round_number=round_number,
            index=index,
            namespace=namespace,
        )
        values.append(
            Evidence(
                path=path,
                line_start=start,
                line_end=end,
                description=observation,
                source_type=source,
                authority=authority,
                evidence_id=evidence_id,
                supports=tuple(filter(None, (supports,))),
                opposes=tuple(
                    dict.fromkeys(
                        (*_strings(item.get("opposes")), *filter(None, (opposes,)))
                    )
                ),
                acquired_by=explorer,
                round=round_number,
                command=str(item.get("command", "")).strip(),
                output=str(item.get("output", "")).strip(),
                quote=str(item.get("quote", "")).strip(),
            )
        )
    return tuple(values)


def parse_briefs(text: str) -> tuple[InvestigationBrief, ...]:
    rows = _object(text).get("briefs")
    if not isinstance(rows, list):
        raise GuardianResponseError("response has no briefs array")
    values = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        focus = str(row.get("focus_question", "")).strip()
        if not focus:
            continue
        values.append(
            InvestigationBrief(
                brief_id=str(row.get("id") or f"BRIEF-{index}").strip(),
                focus_question=focus,
                target_surface=_strings(row.get("target_surface")),
                evidence_to_seek=_strings(row.get("evidence_to_seek")),
                avoid_duplicating=_strings(row.get("avoid_duplicating")),
                linked_specifications=_strings(row.get("linked_specifications")),
                open_ended=bool(row.get("open_ended", False)),
            )
        )
    return tuple(values)


def parse_explorer_output(
    text: str,
    *,
    explorer: str,
    round_number: int,
    brief_id: str,
    namespace: str = "",
    max_specifications: int | None = None,
) -> ExplorerOutput:
    value = _object(text)
    rows = value.get("candidate_specifications", value.get("candidates"))
    if not isinstance(rows, list):
        raise GuardianResponseError("response has no candidate_specifications array")
    if max_specifications is not None and len(rows) > max_specifications:
        raise GuardianResponseError(
            "response has "
            f"{len(rows)} candidate specifications; maximum is {max_specifications}"
        )
    candidates = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        candidate_id = _scoped_identifier(
            "C",
            row.get("candidate_id"),
            explorer=explorer,
            round_number=round_number,
            index=index,
            namespace=namespace,
        )
        statement = str(row.get("statement", "")).strip()
        supporting = _evidence(
            row.get("supporting_evidence", row.get("evidence")),
            explorer=explorer,
            round_number=round_number,
            supports=candidate_id,
            namespace=namespace,
        )
        counter = _evidence(
            row.get("counterevidence"),
            explorer=explorer,
            round_number=round_number,
            opposes=candidate_id,
            namespace=namespace,
        )
        if not statement or not supporting:
            continue
        try:
            suggested = SpecificationStatus(
                str(row.get("suggested_status", "proposed"))
            )
        except ValueError:
            suggested = SpecificationStatus.PROPOSED
        candidates.append(
            CandidateSpecification(
                candidate_id=candidate_id,
                statement=statement,
                condition=str(row.get("condition", "")).strip(),
                scope=_strings(row.get("scope")),
                supporting_evidence=supporting,
                counterevidence=counter,
                suggested_status=suggested,
                unresolved_questions=_strings(row.get("unresolved_questions")),
                explorer=explorer,
                round=round_number,
                brief_id=brief_id,
                patch_assessment=str(row.get("patch_assessment", "")).strip(),
            )
        )
    raw_trace = value.get("exploration_trace", {})
    if not isinstance(raw_trace, dict):
        raw_trace = {}
    trace = ExplorationTrace(
        searched_locations=_strings(raw_trace.get("searched_locations")),
        actions_and_outcomes=_strings(raw_trace.get("actions_and_outcomes")),
        unsuccessful_routes=_strings(raw_trace.get("unsuccessful_routes")),
        remaining_questions=_strings(raw_trace.get("remaining_questions")),
        suggested_next_actions=_strings(raw_trace.get("suggested_next_actions")),
    )
    return ExplorerOutput(
        explorer=explorer,
        round=round_number,
        brief_id=brief_id,
        candidates=tuple(candidates),
        trace=trace,
    )


def parse_aggregation(text: str) -> tuple[str, tuple[SpecificationRecord, ...]]:
    value = _object(text)
    rows = value.get("specifications")
    if not isinstance(rows, list):
        raise GuardianResponseError("response has no specifications array")
    records = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        spec_id = str(row.get("id", "")).strip()
        statement = str(row.get("statement", "")).strip()
        reason = str(row.get("status_reason", "")).strip()
        if not spec_id or not statement or not reason:
            continue
        provenance = []
        for item in row.get("provenance", []):
            if isinstance(item, dict):
                provenance.append(
                    SpecificationProvenance(
                        explorer=str(item.get("explorer", "")),
                        round=int(item.get("round", 1)),
                        candidate_id=str(item.get("candidate_id", "")),
                    )
                )
        relationships = row.get("related_entries", {})
        if not isinstance(relationships, dict):
            relationships = {}
        try:
            status = SpecificationStatus(str(row.get("status", "proposed")))
        except ValueError:
            status = SpecificationStatus.PROPOSED
        records.append(
            SpecificationRecord(
                specification_id=spec_id,
                statement=statement,
                condition=str(row.get("condition", "")).strip(),
                scope=_strings(row.get("scope")),
                supporting_evidence=_strings(row.get("supporting_evidence")),
                counterevidence=_strings(row.get("counterevidence")),
                provenance=tuple(provenance),
                status=status,
                status_reason=reason,
                unresolved_questions=_strings(row.get("unresolved_questions")),
                related_entries=SpecificationRelations(
                    equivalent=_strings(relationships.get("equivalent")),
                    entailed_by=_strings(relationships.get("entailed_by")),
                    conflicts_with=_strings(relationships.get("conflicts_with")),
                ),
                created_round=int(row.get("created_round", 1)),
                updated_round=int(row.get("updated_round", 1)),
            )
        )
    return str(value.get("summary", "")).strip(), tuple(records)


def parse_distillation(
    text: str, *, round_number: int
) -> tuple[ExplorationExperience, ...]:
    rows = _object(text).get("experiences")
    if not isinstance(rows, list):
        raise GuardianResponseError("response has no experiences array")
    values = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        values.append(
            ExplorationExperience(
                experience_id=str(row.get("id") or f"EXP-{round_number}-{index}"),
                round=round_number,
                brief_id=str(row.get("brief_id", "")),
                searched_locations=_strings(row.get("searched_locations")),
                actions_and_outcomes=_strings(row.get("actions_and_outcomes")),
                unsuccessful_routes=_strings(row.get("unsuccessful_routes")),
                unresolved_questions=_strings(row.get("unresolved_questions")),
                suggested_next_actions=_strings(row.get("suggested_next_actions")),
                linked_specifications=_strings(row.get("linked_specifications")),
            )
        )
    return tuple(values)


def parse_frontier(text: str) -> tuple[tuple[str, ...], str]:
    value = _object(text)
    return _strings(value.get("frontier")), str(value.get("reason", "")).strip()


def parse_patch_check(
    text: str,
    *,
    specifications: tuple[SpecificationRecord, ...],
    evidence: tuple[Evidence, ...],
) -> tuple[
    str,
    tuple[GuardianFinding, ...],
    tuple[GuardianFinding, ...],
    tuple[Evidence, ...],
    tuple[str, ...],
]:
    value = _object(text)
    rows = value.get("findings")
    if not isinstance(rows, list):
        raise GuardianResponseError("response has no findings array")
    specs = {item.specification_id: item for item in specifications}
    runtime_evidence = []
    runtime_errors = []
    raw_runtime = value.get("runtime_evidence", [])
    if not isinstance(raw_runtime, list):
        runtime_errors.append("runtime_evidence: not_array")
        raw_runtime = []
    for index, row in enumerate(raw_runtime, 1):
        if not isinstance(row, dict):
            runtime_errors.append(f"runtime_evidence_row[{index}]: row_not_object")
            continue
        spec_id = str(row.get("specification_id", "")).strip()
        probe_key = str(row.get("probe_key", "")).strip()
        observation = str(row.get("observation", "")).strip()
        tool_call_id = str(row.get("tool_call_id", "")).strip()
        reason = ""
        if spec_id not in specs:
            reason = "unknown_specification"
        elif not probe_key:
            reason = "missing_probe_key"
        elif not observation:
            reason = "missing_observation"
        elif not tool_call_id:
            reason = "missing_tool_call_id"
        if reason:
            runtime_errors.append(f"runtime_evidence_row[{index}]: {reason}")
            continue
        evidence_id = str(row.get("id") or f"EV-PATCH-PROBE-{index}").strip()
        runtime_evidence.append(
            Evidence(
                evidence_id=evidence_id,
                path=f"runtime-probe:{tool_call_id}",
                line_start=1,
                line_end=1,
                description=observation,
                source_type=EvidenceSourceType.RUNTIME_PROBE,
                authority=EvidenceAuthority.BEHAVIORAL,
                supports=(),
                opposes=(),
                acquired_by="patch-checker",
                round=1,
                probe_key=probe_key,
                probe_specification_id=spec_id,
            )
        )
    evidence_by_id = {item.evidence_id: item for item in (*evidence, *runtime_evidence)}
    findings = []
    uncertain = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        spec_id = str(row.get("specification_id", "")).strip()
        spec = specs.get(spec_id)
        if spec is None:
            continue
        try:
            status = FindingStatus(str(row.get("status", "uncertain")))
        except ValueError:
            status = FindingStatus.UNCERTAIN
        evidence_ids = _strings(row.get("evidence_ids"))
        item = GuardianFinding(
            specification_id=spec_id,
            statement=spec.statement,
            condition=spec.condition,
            status=status,
            evidence=tuple(
                evidence_by_id[value]
                for value in evidence_ids
                if value in evidence_by_id
            ),
            evidence_ids=evidence_ids,
            patch_locations=_strings(row.get("patch_locations")),
            patch_assessment=str(row.get("assessment", "")).strip(),
            recommendation=str(row.get("recommendation", "")).strip(),
        )
        (uncertain if status is FindingStatus.UNCERTAIN else findings).append(item)
    for row in value.get("backlog", []):
        if not isinstance(row, dict):
            continue
        spec_id = str(row.get("specification_id", "")).strip()
        spec = specs.get(spec_id)
        if spec is None:
            continue
        uncertain.append(
            GuardianFinding(
                specification_id=spec_id,
                statement=spec.statement,
                condition=spec.condition,
                status=FindingStatus.UNCERTAIN,
                evidence=(),
                evidence_ids=(),
                patch_assessment=str(row.get("assessment", "")).strip(),
                recommendation=str(row.get("next_verification", "")).strip(),
            )
        )
    return (
        str(value.get("summary", "")).strip(),
        tuple(findings),
        tuple(uncertain),
        tuple(runtime_evidence),
        tuple(runtime_errors),
    )


__all__ = [
    "GuardianResponseError",
    "parse_aggregation",
    "parse_briefs",
    "parse_distillation",
    "parse_explorer_output",
    "parse_frontier",
    "parse_patch_check",
]
