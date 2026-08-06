# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Strict semantic normalization for Guardian agent responses."""

from __future__ import annotations

import json
import re
from typing import Any

from .types import (
    Evidence,
    EvidenceAuthority,
    FindingStatus,
    GuardianFinding,
    LocalSpecification,
)

_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


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


def _evidence(raw: object) -> tuple[Evidence, ...]:
    if not isinstance(raw, list):
        return ()
    values: list[Evidence] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            authority = EvidenceAuthority(str(item.get("authority", "repository")))
        except ValueError:
            authority = EvidenceAuthority.REPOSITORY
        try:
            start = max(1, int(item.get("line_start", 1)))
            end = max(start, int(item.get("line_end", start)))
        except (TypeError, ValueError):
            start = end = 1
        path = str(item.get("path", "")).strip()
        description = str(item.get("description", "")).strip()
        if path and description:
            values.append(
                Evidence(
                    path=path,
                    line_start=start,
                    line_end=end,
                    description=description,
                    authority=authority,
                )
            )
    return tuple(values)


def _confidence(value: object) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def parse_candidates(text: str, *, explorer: str) -> tuple[LocalSpecification, ...]:
    rows = _object(text).get("candidates")
    if not isinstance(rows, list):
        raise GuardianResponseError("response has no candidates array")
    values: list[LocalSpecification] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        statement = str(row.get("statement", "")).strip()
        evidence = _evidence(row.get("evidence"))
        if not statement or not evidence:
            continue
        values.append(
            LocalSpecification(
                statement=statement,
                condition=str(row.get("condition", "")).strip(),
                evidence=evidence,
                patch_assessment=str(row.get("patch_assessment", "")).strip(),
                confidence=_confidence(row.get("confidence")),
                uncertainty=str(row.get("uncertainty", "")).strip(),
                explorer=explorer,
            )
        )
    return tuple(values)


def parse_findings(
    text: str,
) -> tuple[str, tuple[GuardianFinding, ...]]:
    value = _object(text)
    rows = value.get("findings")
    if not isinstance(rows, list):
        raise GuardianResponseError("response has no findings array")
    findings: list[GuardianFinding] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            status = FindingStatus(str(row.get("status", "uncertain")))
        except ValueError:
            status = FindingStatus.UNCERTAIN
        statement = str(row.get("statement", "")).strip()
        evidence = _evidence(row.get("evidence"))
        if not statement or not evidence:
            continue
        findings.append(
            GuardianFinding(
                statement=statement,
                status=status,
                evidence=evidence,
                patch_assessment=str(row.get("patch_assessment", "")).strip(),
                recommendation=str(row.get("recommendation", "")).strip(),
                confidence=_confidence(row.get("confidence")),
            )
        )
    return str(value.get("summary", "")).strip(), tuple(findings)


__all__ = ["GuardianResponseError", "parse_candidates", "parse_findings"]
