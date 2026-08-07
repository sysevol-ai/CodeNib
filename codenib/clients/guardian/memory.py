# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Controller-owned persistence for Guardian's local-specification memory."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from .types import (
    Evidence,
    EvidenceAuthority,
    FindingStatus,
    GuardianMemory,
    GuardianResult,
    RememberedEvidence,
    RememberedSpecification,
    SpecificationAssessment,
)

_SCHEMA_VERSION = 1
_MEMORY_ID = re.compile(r"^spec:[0-9a-f]{16}$")


def _specification_id(statement: str, condition: str) -> str:
    value = f"{statement.strip().casefold()}\0{condition.strip().casefold()}"
    return f"spec:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def _blob_sha256(workspace: Path, path: str) -> str:
    relative = PurePosixPath(path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return ""
    target = workspace.joinpath(*relative.parts)
    try:
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(workspace) or not resolved.is_file():
            return ""
        return hashlib.sha256(resolved.read_bytes()).hexdigest()
    except (OSError, RuntimeError):
        return ""


def _evidence_to_dict(value: RememberedEvidence) -> dict[str, object]:
    evidence = value.evidence
    return {
        "path": evidence.path,
        "line_start": evidence.line_start,
        "line_end": evidence.line_end,
        "description": evidence.description,
        "authority": evidence.authority.value,
        "snapshot": value.snapshot,
        "blob_sha256": value.blob_sha256,
    }


def _assessment_to_dict(value: SpecificationAssessment) -> dict[str, object]:
    return {
        "snapshot": value.snapshot,
        "status": value.status.value,
        "patch_assessment": value.patch_assessment,
        "recommendation": value.recommendation,
        "confidence": value.confidence,
    }


def _memory_to_dict(memory: GuardianMemory) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "observed_snapshots": list(memory.observed_snapshots),
        "specifications": [
            {
                "memory_id": specification.memory_id,
                "statement": specification.statement,
                "condition": specification.condition,
                "evidence": [
                    _evidence_to_dict(evidence) for evidence in specification.evidence
                ],
                "assessments": [
                    _assessment_to_dict(assessment)
                    for assessment in specification.assessments
                ],
            }
            for specification in memory.specifications
        ],
    }


def _load_evidence(raw: object) -> RememberedEvidence:
    if not isinstance(raw, dict):
        raise ValueError("remembered evidence must be an object")
    authority = EvidenceAuthority(str(raw.get("authority", "repository")))
    return RememberedEvidence(
        evidence=Evidence(
            path=str(raw["path"]),
            line_start=int(raw["line_start"]),
            line_end=int(raw["line_end"]),
            description=str(raw["description"]),
            authority=authority,
        ),
        snapshot=str(raw["snapshot"]),
        blob_sha256=str(raw["blob_sha256"]),
    )


def _load_assessment(raw: object) -> SpecificationAssessment:
    if not isinstance(raw, dict):
        raise ValueError("specification assessment must be an object")
    return SpecificationAssessment(
        snapshot=str(raw["snapshot"]),
        status=FindingStatus(str(raw["status"])),
        patch_assessment=str(raw.get("patch_assessment", "")),
        recommendation=str(raw.get("recommendation", "")),
        confidence=float(raw.get("confidence", 0.0)),
    )


def _memory_from_dict(raw: object) -> GuardianMemory:
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("unsupported Guardian memory schema")
    snapshots = raw.get("observed_snapshots", [])
    specifications = raw.get("specifications", [])
    if not isinstance(snapshots, list) or not isinstance(specifications, list):
        raise ValueError("Guardian memory collections must be arrays")
    values: list[RememberedSpecification] = []
    for item in specifications:
        if not isinstance(item, dict):
            raise ValueError("remembered specification must be an object")
        memory_id = str(item.get("memory_id", ""))
        if not _MEMORY_ID.fullmatch(memory_id):
            raise ValueError("remembered specification has an invalid id")
        evidence = item.get("evidence", [])
        assessments = item.get("assessments", [])
        if not isinstance(evidence, list) or not isinstance(assessments, list):
            raise ValueError("remembered specification history must be arrays")
        values.append(
            RememberedSpecification(
                memory_id=memory_id,
                statement=str(item["statement"]),
                condition=str(item.get("condition", "")),
                evidence=tuple(_load_evidence(value) for value in evidence),
                assessments=tuple(_load_assessment(value) for value in assessments),
            )
        )
    return GuardianMemory(
        specifications=tuple(values),
        observed_snapshots=tuple(str(value) for value in snapshots),
    )


class GuardianMemoryStore:
    """Single-writer event journal with an atomic materialized memory view."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.state_path = self.root / "memory.json"
        self.events_path = self.root / "events.jsonl"

    def load(self, workspace: Path | None = None) -> GuardianMemory:
        if not self.state_path.exists():
            return GuardianMemory()
        try:
            memory = _memory_from_dict(
                json.loads(self.state_path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Guardian memory cannot be loaded: {exc}") from exc
        if workspace is None:
            return memory
        workspace = Path(workspace).resolve()
        return replace(
            memory,
            specifications=tuple(
                replace(
                    specification,
                    evidence=tuple(
                        replace(
                            remembered,
                            fresh=(
                                bool(remembered.blob_sha256)
                                and _blob_sha256(workspace, remembered.evidence.path)
                                == remembered.blob_sha256
                            ),
                        )
                        for remembered in specification.evidence
                    ),
                )
                for specification in memory.specifications
            ),
        )

    def update(self, result: GuardianResult, workspace: Path) -> GuardianMemory:
        """Merge one successful review into memory and append an audit event."""

        memory = self.load()
        by_id = {
            specification.memory_id: specification
            for specification in memory.specifications
        }
        by_statement = {
            specification.statement.strip().casefold(): specification.memory_id
            for specification in memory.specifications
        }
        candidate_conditions = {
            candidate.statement.strip().casefold(): candidate.condition
            for candidate in result.candidates
        }
        changed: list[str] = []
        assessed = result.assessments or result.findings + result.backlog
        for finding in assessed:
            statement_key = finding.statement.strip().casefold()
            condition = finding.condition or candidate_conditions.get(statement_key, "")
            supplied_id = (
                finding.memory_id if _MEMORY_ID.fullmatch(finding.memory_id) else ""
            )
            memory_id = (
                supplied_id
                if supplied_id in by_id
                else by_statement.get(statement_key)
                or _specification_id(finding.statement, condition)
            )
            previous = by_id.get(memory_id)
            remembered_evidence = tuple(
                RememberedEvidence(
                    evidence=evidence,
                    snapshot=result.candidate_commit,
                    blob_sha256=_blob_sha256(workspace, evidence.path),
                    fresh=True,
                )
                for evidence in finding.evidence
            )
            assessment = SpecificationAssessment(
                snapshot=result.candidate_commit,
                status=finding.status,
                patch_assessment=finding.patch_assessment,
                recommendation=finding.recommendation,
                confidence=finding.confidence,
            )
            evidence_history = previous.evidence if previous else ()
            assessment_history = previous.assessments if previous else ()
            value = RememberedSpecification(
                memory_id=memory_id,
                statement=finding.statement,
                condition=condition or (previous.condition if previous else ""),
                evidence=evidence_history + remembered_evidence,
                assessments=assessment_history + (assessment,),
            )
            by_id[memory_id] = value
            by_statement[statement_key] = memory_id
            changed.append(memory_id)

        snapshots = memory.observed_snapshots
        if result.candidate_commit not in snapshots:
            snapshots += (result.candidate_commit,)
        updated = GuardianMemory(
            specifications=tuple(
                sorted(by_id.values(), key=lambda value: value.memory_id)
            ),
            observed_snapshots=snapshots,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        serialized = _memory_to_dict(updated)
        specification_rows = serialized["specifications"]
        if not isinstance(specification_rows, list):
            raise RuntimeError("serialized Guardian specifications must be a list")
        event: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "sequence": len(snapshots),
            "base_commit": result.base_commit,
            "candidate_commit": result.candidate_commit,
            "updates": [
                next(
                    value
                    for value in specification_rows
                    if isinstance(value, dict)
                    if value["memory_id"] == memory_id
                )
                for memory_id in changed
            ],
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(serialized, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)
        return updated


__all__ = ["GuardianMemoryStore"]
