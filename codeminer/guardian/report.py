# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Report step: render one cycle's findings as Markdown (+ a JSON sidecar).

Phase 1 reports are **non-modifying**: findings + ranked evidence only, never
candidate patches or diffs (that is RFC Phase 4). The renderer uses the inline
pipe-table style from ``scripts/agent_compile/aggregate.py``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

from .investigate import Evidence


@dataclass
class Finding:
    """One signal plus the evidence Guardian gathered around it.

    kind: ``"churn"`` or ``"test_failure"``.
    narrative: Plain-text LLM analysis; empty string when LLM step is skipped.
    """

    kind: str
    title: str
    detail: str = ""
    evidence: List[Evidence] = field(default_factory=list)
    narrative: str = ""


@dataclass
class GuardianReport:
    """The output of a single Guardian cycle."""

    repo: str
    commit: str
    generated_at: str
    churn_window: str
    file_count: int = 0
    capabilities: Dict[str, bool] = field(default_factory=dict)
    findings: List[Finding] = field(default_factory=list)
    tests_ran: bool = False
    tests_summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    """Return Markdown pipe-table lines (matches aggregate.py's style)."""
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return out


def _evidence_rows(evidence: List[Evidence]) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for e in evidence:
        loc = e.file
        if e.start_line is not None:
            loc = f"{e.file}:{e.start_line}"
            if e.end_line is not None and e.end_line != e.start_line:
                loc += f"-{e.end_line}"
        score = f"{e.score:.3f}" if isinstance(e.score, (int, float)) else "—"
        rows.append([loc, e.node_name or "—", e.type or "—", score])
    return rows


def render_markdown(report: GuardianReport) -> str:
    """Render a :class:`GuardianReport` as a Markdown document.

    The output deliberately contains **no patches or diffs** — Guardian proposes
    by reporting, humans decide and apply.
    """
    L: List[str] = []
    L.append(f"# Repository Guardian Report — {report.repo}")
    L.append("")
    L.append(f"- **Commit:** `{report.commit[:12] or '(unknown)'}`")
    L.append(f"- **Generated:** {report.generated_at}")
    L.append(f"- **Churn window:** {report.churn_window}")
    L.append(f"- **Indexed files:** {report.file_count}")
    if report.capabilities:
        caps = ", ".join(k for k, v in sorted(report.capabilities.items()) if v) or "—"
        L.append(f"- **Index capabilities:** {caps}")
    if report.tests_ran:
        L.append(f"- **Tests:** {report.tests_summary or 'ran'}")
    L.append("")
    L.append(
        "_Non-modifying report: findings and evidence only — no changes were "
        "made to the repository._"
    )
    L.append("")

    if not report.findings:
        L.append("## Findings")
        L.append("")
        L.append("No findings this cycle.")
        return "\n".join(L) + "\n"

    L.append(f"## Findings ({len(report.findings)})")
    L.append("")
    for i, finding in enumerate(report.findings, start=1):
        L.append(f"### {i}. {finding.title}  _({finding.kind})_")
        L.append("")
        if finding.detail:
            L.append(finding.detail)
            L.append("")
        if finding.narrative:
            L.append("**LLM Analysis:**")
            L.append("")
            L.append(finding.narrative)
            L.append("")
        if finding.evidence:
            L.append("**Evidence (ranked code locations):**")
            L.append("")
            L.extend(
                _table(
                    ["Location", "Symbol", "Type", "Score"],
                    _evidence_rows(finding.evidence),
                )
            )
        else:
            L.append("_No retrieval evidence attached._")
        L.append("")

    return "\n".join(L).rstrip() + "\n"
