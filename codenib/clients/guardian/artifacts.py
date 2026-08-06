# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Human-readable and durable Guardian result rendering."""

from __future__ import annotations

from .types import GuardianFinding, GuardianResult


def _finding_markdown(finding: GuardianFinding, index: int) -> list[str]:
    lines = [
        f"## {index}. {finding.statement}",
        "",
        f"Status: `{finding.status.value}` · confidence: {finding.confidence:.2f}",
        "",
        finding.patch_assessment or "No patch assessment supplied.",
        "",
    ]
    if finding.recommendation:
        lines.extend((f"Recommendation: {finding.recommendation}", ""))
    lines.append("Evidence:")
    lines.append("")
    for evidence in finding.evidence:
        location = f"{evidence.path}:{evidence.line_start}"
        lines.append(
            f"- `{location}` ({evidence.authority.value}): {evidence.description}"
        )
    lines.append("")
    return lines


def render_markdown(result: GuardianResult) -> str:
    lines = [
        "# Repository Guardian Report",
        "",
        f"Candidate: `{result.candidate_commit}`",
        f"Analysis status: `{result.status.value}`",
        f"Discovered candidates: {len(result.candidates)}",
        f"Admitted findings: {len(result.findings)}",
        f"Backlog: {len(result.backlog)}",
        "",
    ]
    if result.summary:
        lines.extend((result.summary, ""))
    if result.errors:
        lines.extend(("## Analysis limitations", ""))
        lines.extend(f"- {error}" for error in result.errors)
        lines.append("")
    if not result.findings:
        lines.extend(
            (
                "No evidence-backed violated local specification was admitted.",
                "",
            )
        )
    else:
        for index, finding in enumerate(result.findings, 1):
            lines.extend(_finding_markdown(finding, index))
    if result.backlog:
        lines.extend(("# Uncertain backlog", ""))
        for index, finding in enumerate(result.backlog, 1):
            lines.extend(_finding_markdown(finding, index))
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["render_markdown"]
