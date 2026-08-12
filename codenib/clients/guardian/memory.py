# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Persistence for Guardian's task-scoped structured specification memory."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path, PurePosixPath

from ..execution import TokenUsage
from .types import (
    Evidence,
    EvidenceAuthority,
    EvidenceSourceType,
    ExplorationExperience,
    GuardianResult,
    RoundRecord,
    SpecificationMemory,
    SpecificationProvenance,
    SpecificationRecord,
    SpecificationRelations,
    SpecificationStatus,
    StageUsage,
)

_SCHEMA_VERSION = 3


def evidence_to_dict(value: Evidence) -> dict[str, object]:
    return {
        "id": value.evidence_id,
        "source_type": value.source_type.value,
        "authority": value.authority.value,
        "locator": {
            "path": value.path,
            "line_start": value.line_start,
            "line_end": value.line_end,
        },
        "observation": value.description,
        "quote": value.quote,
        "supports": list(value.supports),
        "opposes": list(value.opposes),
        "acquired_by": value.acquired_by,
        "round": value.round,
        "command": value.command,
        "output": value.output,
        "snapshot": value.snapshot,
        "blob_sha256": value.blob_sha256,
        "fresh": value.fresh,
    }


def specification_to_dict(value: SpecificationRecord) -> dict[str, object]:
    return {
        "id": value.specification_id,
        "statement": value.statement,
        "condition": value.condition,
        "scope": list(value.scope),
        "supporting_evidence": list(value.supporting_evidence),
        "counterevidence": list(value.counterevidence),
        "provenance": [
            {
                "explorer": item.explorer,
                "round": item.round,
                "candidate_id": item.candidate_id,
            }
            for item in value.provenance
        ],
        "status": value.status.value,
        "status_reason": value.status_reason,
        "unresolved_questions": list(value.unresolved_questions),
        "related_entries": {
            "equivalent": list(value.related_entries.equivalent),
            "entailed_by": list(value.related_entries.entailed_by),
            "conflicts_with": list(value.related_entries.conflicts_with),
        },
        "created_round": value.created_round,
        "updated_round": value.updated_round,
    }


def experience_to_dict(value: ExplorationExperience) -> dict[str, object]:
    return {
        "id": value.experience_id,
        "round": value.round,
        "brief_id": value.brief_id,
        "searched_locations": list(value.searched_locations),
        "actions_and_outcomes": list(value.actions_and_outcomes),
        "unsuccessful_routes": list(value.unsuccessful_routes),
        "unresolved_questions": list(value.unresolved_questions),
        "suggested_next_actions": list(value.suggested_next_actions),
        "linked_specifications": list(value.linked_specifications),
    }


def memory_to_dict(memory: SpecificationMemory) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "session_id": memory.session_id,
        "observed_snapshots": list(memory.observed_snapshots),
        "specifications": [
            specification_to_dict(item) for item in memory.specifications
        ],
        "evidence": [evidence_to_dict(item) for item in memory.evidence],
        "exploration_experiences": [
            experience_to_dict(item) for item in memory.experiences
        ],
        "round_history": [
            {
                "round": item.round,
                "brief_ids": [brief.brief_id for brief in item.briefs],
                "explorers": [output.explorer for output in item.explorer_outputs],
                "aggregation_summary": item.aggregation_summary,
                "frontier": list(item.frontier),
                "new_information": item.new_information,
                "degraded": item.degraded,
                "errors": list(item.errors),
            }
            for item in memory.rounds
        ],
        "stage_usage": [
            {
                "stage": item.stage,
                "round": item.round,
                "model": item.model,
                "usage": {
                    "input_tokens": item.usage.input_tokens,
                    "cached_input_tokens": item.usage.cached_input_tokens,
                    "cache_write_input_tokens": item.usage.cache_write_input_tokens,
                    "output_tokens": item.usage.output_tokens,
                    "reasoning_output_tokens": item.usage.reasoning_output_tokens,
                },
                "tool_calls": item.tool_calls,
            }
            for item in memory.stage_usage
        ],
    }


def _strings(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw)


def _evidence(raw: object) -> Evidence:
    if not isinstance(raw, dict):
        raise ValueError("evidence must be an object")
    locator = raw.get("locator", {})
    if not isinstance(locator, dict):
        raise ValueError("evidence locator must be an object")
    return Evidence(
        path=str(locator.get("path", "")),
        line_start=int(locator.get("line_start", 1)),
        line_end=int(locator.get("line_end", 1)),
        description=str(raw.get("observation", "")),
        source_type=EvidenceSourceType(str(raw.get("source_type", "documentation"))),
        authority=EvidenceAuthority(str(raw.get("authority", "conventional"))),
        evidence_id=str(raw.get("id", "")),
        supports=_strings(raw.get("supports")),
        opposes=_strings(raw.get("opposes")),
        acquired_by=str(raw.get("acquired_by", "")),
        round=int(raw.get("round", 1)),
        command=str(raw.get("command", "")),
        output=str(raw.get("output", "")),
        quote=str(raw.get("quote", "")),
        snapshot=str(raw.get("snapshot", "")),
        blob_sha256=str(raw.get("blob_sha256", "")),
        fresh=bool(raw.get("fresh", False)),
    )


def _specification(raw: object) -> SpecificationRecord:
    if not isinstance(raw, dict):
        raise ValueError("specification must be an object")
    relations = raw.get("related_entries", {})
    if not isinstance(relations, dict):
        relations = {}
    provenance = raw.get("provenance", [])
    if not isinstance(provenance, list):
        raise ValueError("specification provenance must be an array")
    return SpecificationRecord(
        specification_id=str(raw.get("id", "")),
        statement=str(raw.get("statement", "")),
        condition=str(raw.get("condition", "")),
        scope=_strings(raw.get("scope")),
        supporting_evidence=_strings(raw.get("supporting_evidence")),
        counterevidence=_strings(raw.get("counterevidence")),
        provenance=tuple(
            SpecificationProvenance(
                explorer=str(item.get("explorer", "")),
                round=int(item.get("round", 1)),
                candidate_id=str(item.get("candidate_id", "")),
            )
            for item in provenance
            if isinstance(item, dict)
        ),
        status=SpecificationStatus(str(raw.get("status", "proposed"))),
        status_reason=str(raw.get("status_reason", "")),
        unresolved_questions=_strings(raw.get("unresolved_questions")),
        related_entries=SpecificationRelations(
            equivalent=_strings(relations.get("equivalent")),
            entailed_by=_strings(relations.get("entailed_by")),
            conflicts_with=_strings(relations.get("conflicts_with")),
        ),
        created_round=int(raw.get("created_round", 1)),
        updated_round=int(raw.get("updated_round", 1)),
    )


def _experience(raw: object) -> ExplorationExperience:
    if not isinstance(raw, dict):
        raise ValueError("exploration experience must be an object")
    return ExplorationExperience(
        experience_id=str(raw.get("id", "")),
        round=int(raw.get("round", 1)),
        brief_id=str(raw.get("brief_id", "")),
        searched_locations=_strings(raw.get("searched_locations")),
        actions_and_outcomes=_strings(raw.get("actions_and_outcomes")),
        unsuccessful_routes=_strings(raw.get("unsuccessful_routes")),
        unresolved_questions=_strings(raw.get("unresolved_questions")),
        suggested_next_actions=_strings(raw.get("suggested_next_actions")),
        linked_specifications=_strings(raw.get("linked_specifications")),
    )


def memory_from_dict(raw: object) -> SpecificationMemory:
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("unsupported Guardian memory schema")
    specifications = raw.get("specifications", [])
    evidence = raw.get("evidence", [])
    experiences = raw.get("exploration_experiences", [])
    stage_usage = raw.get("stage_usage", [])
    round_history = raw.get("round_history", [])
    if not all(
        isinstance(value, list)
        for value in (
            specifications,
            evidence,
            experiences,
            stage_usage,
            round_history,
        )
    ):
        raise ValueError("Guardian memory collections must be arrays")
    usage = []
    for item in stage_usage:
        if not isinstance(item, dict) or not isinstance(item.get("usage"), dict):
            raise ValueError("stage usage must be an object")
        row = item["usage"]
        usage.append(
            StageUsage(
                stage=str(item.get("stage", "")),
                round=int(item.get("round", 0)),
                model=str(item.get("model", "")),
                usage=TokenUsage(
                    **{
                        key: int(row.get(key, 0))
                        for key in (
                            "input_tokens",
                            "cached_input_tokens",
                            "cache_write_input_tokens",
                            "output_tokens",
                            "reasoning_output_tokens",
                        )
                    }
                ),
                tool_calls=int(item.get("tool_calls", 0)),
            )
        )
    return SpecificationMemory(
        session_id=str(raw.get("session_id", "")),
        specifications=tuple(_specification(item) for item in specifications),
        evidence=tuple(_evidence(item) for item in evidence),
        experiences=tuple(_experience(item) for item in experiences),
        rounds=tuple(
            RoundRecord(
                round=int(item.get("round", 1)),
                briefs=(),
                explorer_outputs=(),
                aggregation_summary=str(item.get("aggregation_summary", "")),
                frontier=_strings(item.get("frontier")),
                new_information=bool(item.get("new_information", False)),
                degraded=bool(item.get("degraded", False)),
                errors=_strings(item.get("errors")),
            )
            for item in round_history
            if isinstance(item, dict)
        ),
        stage_usage=tuple(usage),
        observed_snapshots=_strings(raw.get("observed_snapshots")),
    )


def _current_digest(workspace: Path, path: str) -> str:
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts:
        return ""
    try:
        target = workspace.joinpath(*relative.parts).resolve(strict=True)
        if not target.is_relative_to(workspace) or not target.is_file():
            return ""
        return __import__("hashlib").sha256(target.read_bytes()).hexdigest()
    except (OSError, RuntimeError):
        return ""


class GuardianMemoryStore:
    """Single-writer journal and atomic materialized memory view."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.state_path = self.root / "memory.json"
        self.events_path = self.root / "events.jsonl"

    def load(self, workspace: Path | None = None) -> SpecificationMemory:
        if not self.state_path.exists():
            return SpecificationMemory()
        try:
            memory = memory_from_dict(
                json.loads(self.state_path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"Guardian memory cannot be loaded: {exc}") from exc
        if workspace is None:
            return memory
        workspace = Path(workspace).resolve()
        return replace(
            memory,
            evidence=tuple(
                (
                    replace(
                        item,
                        fresh=(
                            not item.blob_sha256
                            or _current_digest(workspace, item.path) == item.blob_sha256
                        ),
                    )
                    if item.source_type
                    not in (
                        EvidenceSourceType.TASK,
                        EvidenceSourceType.SOLVER_MESSAGE,
                        EvidenceSourceType.CANDIDATE_PATCH,
                        EvidenceSourceType.RUNTIME_PROBE,
                    )
                    else item
                )
                for item in memory.evidence
            ),
        )

    def update(self, result: GuardianResult, workspace: Path) -> SpecificationMemory:
        del workspace  # Evidence was validated and hashed in the review workspace.
        previous = self.load()
        memory = result.memory
        if (
            previous.observed_snapshots
            and not memory.specifications
            and not memory.evidence
            and not memory.experiences
            and not memory.stage_usage
        ):
            memory = previous
        if result.candidate_commit not in memory.observed_snapshots:
            memory = replace(
                memory,
                observed_snapshots=memory.observed_snapshots
                + (result.candidate_commit,),
            )
        self.root.mkdir(parents=True, exist_ok=True)
        serialized = memory_to_dict(memory)
        event = {
            "schema_version": _SCHEMA_VERSION,
            "sequence": len(memory.observed_snapshots),
            "session_id": memory.session_id,
            "base_commit": result.base_commit,
            "candidate_commit": result.candidate_commit,
            "specification_count": len(memory.specifications),
            "evidence_count": len(memory.evidence),
            "round_count": len(result.rounds),
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(serialized, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.state_path)
        return memory


__all__ = [
    "GuardianMemoryStore",
    "evidence_to_dict",
    "memory_from_dict",
    "memory_to_dict",
    "specification_to_dict",
]
