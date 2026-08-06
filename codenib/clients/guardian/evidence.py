# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Workspace-backed validation for model-produced Guardian evidence."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

from .types import Evidence, GuardianFinding, LocalSpecification


def _validate_location(
    workspace: Path, evidence: Evidence
) -> tuple[Evidence | None, str]:
    raw_path = evidence.path.strip()
    if "\\" in raw_path:
        return None, "path must use repository-relative POSIX separators"

    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return None, "path is not repository-relative"

    target = workspace.joinpath(*relative.parts)
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "path does not resolve to a repository file"
    if not resolved.is_relative_to(workspace) or not resolved.is_file():
        return None, "path does not resolve to a repository file"

    try:
        with resolved.open("rb") as handle:
            for line_number, _ in enumerate(handle, start=1):
                if line_number >= evidence.line_end:
                    return replace(evidence, path=relative.as_posix()), ""
    except OSError:
        return None, "repository file could not be read"
    return None, "line range exceeds the repository file"


def _validated_evidence(
    workspace: Path,
    evidence: tuple[Evidence, ...],
    *,
    label: str,
) -> tuple[tuple[Evidence, ...], tuple[str, ...]]:
    valid: list[Evidence] = []
    errors: list[str] = []
    for index, item in enumerate(evidence, start=1):
        checked, error = _validate_location(workspace, item)
        if checked is not None:
            valid.append(checked)
        else:
            errors.append(f"{label} evidence {index} ({item.path!r}): {error}")
    return tuple(valid), tuple(errors)


def validate_candidates(
    workspace: Path,
    candidates: tuple[LocalSpecification, ...],
) -> tuple[tuple[LocalSpecification, ...], tuple[str, ...]]:
    """Discard candidate evidence that cannot be verified in *workspace*."""

    valid: list[LocalSpecification] = []
    errors: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        evidence, evidence_errors = _validated_evidence(
            workspace,
            candidate.evidence,
            label=f"{candidate.explorer or 'explorer'} candidate {index}",
        )
        errors.extend(evidence_errors)
        if evidence:
            valid.append(replace(candidate, evidence=evidence))
    return tuple(valid), tuple(errors)


def validate_findings(
    workspace: Path,
    findings: tuple[GuardianFinding, ...],
) -> tuple[tuple[GuardianFinding, ...], tuple[str, ...]]:
    """Discard aggregate findings without a verifiable source location."""

    valid: list[GuardianFinding] = []
    errors: list[str] = []
    for index, finding in enumerate(findings, start=1):
        evidence, evidence_errors = _validated_evidence(
            workspace,
            finding.evidence,
            label=f"aggregator finding {index}",
        )
        errors.extend(evidence_errors)
        if evidence:
            valid.append(replace(finding, evidence=evidence))
    return tuple(valid), tuple(errors)


__all__ = ["validate_candidates", "validate_findings"]
