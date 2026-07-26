# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Render a Guardian cycle from graded hypotheses.

``Finding`` is a report view, not a separately persisted ontology. The
authoritative object is :class:`guardian.loop.state.Hypothesis`, and only its
``grade == "finding"`` subset is rendered in the findings section.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Evidence:
    """A ranked code location retained for backwards-compatible render detail."""

    file: str
    node_name: str
    type: str
    start_line: Optional[int]
    end_line: Optional[int]
    score: Optional[float]


from .investigator import LLMUsage  # noqa: E402


@dataclass
class Finding:
    """Read-only report view of a hypothesis at grade ``finding``."""

    id: str
    claim: str
    consequence: str
    remedy: str
    origin: str
    locus: List[str]
    evidence_refs: List[str]
    confidence: float
    supersedes: List[str] = field(default_factory=list)
    evidence: List[Evidence] = field(default_factory=list)
    narrative: str = ""

    @property
    def title(self) -> str:
        return self.claim

    @property
    def detail(self) -> str:
        return self.consequence

    @property
    def kind(self) -> str:
        """Compatibility view for MCP/dashboard consumers."""
        return self.origin

    @property
    def hypothesis(self) -> str:
        return self.claim

    @property
    def verdict(self) -> str:
        return "confirmed"


@dataclass
class BacklogItem:
    """A non-reportable hypothesis retained for future cycles."""

    id: str
    claim: str
    consequence: str
    remedy: str
    grade: str
    origin: str
    locus: List[str]
    evidence_refs: List[str]
    confidence: float


@dataclass
class Retraction:
    """A refuted hypothesis, rendered only when it retracts prior work."""

    id: str
    claim: str
    locus: List[str]
    evidence_refs: List[str]
    supersedes: List[str]


@dataclass
class GuardianReport:
    """The output of one stateful Guardian cycle."""

    repo: str
    commit: str
    generated_at: str
    churn_window: str
    file_count: int = 0
    capabilities: Dict[str, bool] = field(default_factory=dict)
    findings: List[Finding] = field(default_factory=list)
    backlog: List[BacklogItem] = field(default_factory=list)
    retractions: List[Retraction] = field(default_factory=list)
    tests_ran: bool = False
    tests_summary: str = ""
    llm_usage: Optional[LLMUsage] = None
    outer_llm_usage: Optional[LLMUsage] = None
    inner_llm_usage: Optional[LLMUsage] = None
    llm_model: str = ""
    llm_backend: str = ""
    llm_transport_history: List[str] = field(default_factory=list)
    retriever: str = ""
    cycle_no: int = 0
    exit_reason: str = ""
    report_summary: str = ""
    degraded: bool = False
    analysis_status: str = "complete"
    decision_log: List[dict] = field(default_factory=list)
    compaction_events: int = 0
    trace_metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def report_views(
    hypotheses: list,
) -> tuple[List[Finding], List[BacklogItem], List[Retraction]]:
    """Project the canonical hypothesis set into three report sections."""
    findings: List[Finding] = []
    backlog: List[BacklogItem] = []
    retractions: List[Retraction] = []
    superseded_ids = {
        superseded for hypothesis in hypotheses for superseded in hypothesis.supersedes
    }
    for item in hypotheses:
        if item.id in superseded_ids:
            continue
        if item.grade == "finding":
            findings.append(
                Finding(
                    id=item.id,
                    claim=item.claim,
                    consequence=item.consequence,
                    remedy=item.remedy,
                    origin=item.origin,
                    locus=list(item.locus),
                    evidence_refs=list(item.evidence),
                    confidence=item.confidence,
                    supersedes=list(item.supersedes),
                )
            )
        elif item.grade in {"conjecture", "supported", "deferred"}:
            backlog.append(
                BacklogItem(
                    id=item.id,
                    claim=item.claim,
                    consequence=item.consequence,
                    remedy=item.remedy,
                    grade=item.grade,
                    origin=item.origin,
                    locus=list(item.locus),
                    evidence_refs=list(item.evidence),
                    confidence=item.confidence,
                )
            )
        elif item.grade == "refuted" and item.supersedes:
            retractions.append(
                Retraction(
                    id=item.id,
                    claim=item.claim,
                    locus=list(item.locus),
                    evidence_refs=list(item.evidence),
                    supersedes=list(item.supersedes),
                )
            )
    return findings, backlog, retractions


def _render_locations(locations: List[str]) -> str:
    return ", ".join(f"`{item}`" for item in locations) or "—"


def render_markdown(report: GuardianReport) -> str:
    """Render findings, backlog, retractions, and auditable exit metadata."""
    lines: List[str] = [
        f"# Repository Guardian Report — {report.repo}",
        "",
        f"- **Commit:** `{report.commit[:12] or '(unknown)'}`",
        f"- **Generated:** {report.generated_at}",
        f"- **Cycle:** {report.cycle_no or '—'}",
        f"- **Exit:** {report.exit_reason or '—'}",
        f"- **Analysis status:** {report.analysis_status}",
        f"- **Churn window:** {report.churn_window}",
        f"- **Indexed files:** {report.file_count}",
    ]
    if report.degraded:
        lines.append("- **Ablation eligibility:** excluded (analysis was degraded)")
    if report.retriever:
        lines.append(f"- **Retriever:** {report.retriever}")
    if report.llm_model:
        backend = f" via {report.llm_backend}" if report.llm_backend else ""
        lines.append(f"- **LLM model:** `{report.llm_model}`{backend}")
    if report.llm_transport_history:
        lines.append(
            "- **LLM transport history:** " + ", ".join(report.llm_transport_history)
        )
    if report.capabilities:
        enabled = ", ".join(
            key for key, value in sorted(report.capabilities.items()) if value
        )
        lines.append(f"- **Index capabilities:** {enabled or '—'}")
    if report.tests_ran:
        lines.append(f"- **Tests:** {report.tests_summary or 'ran'}")
    if report.llm_usage and report.llm_usage.total_tokens:
        usage = report.llm_usage
        lines.append(
            f"- **LLM tokens:** {usage.total_tokens:,} total "
            f"(prompt: {usage.prompt_tokens:,} / "
            f"cached input: {usage.cached_input_tokens:,} / "
            f"completion: {usage.completion_tokens:,})"
        )
    lines.extend(
        [
            f"- **Compaction events:** {report.compaction_events}",
            "",
            "_Non-modifying report: findings and evidence only — no changes were "
            "made to the repository._",
            "",
        ]
    )
    if report.trace_metrics:
        lines.extend(
            [
                "## Trace Metrics",
                "",
                *[
                    f"- **{key}:** {value}"
                    for key, value in sorted(report.trace_metrics.items())
                ],
                "",
            ]
        )
    if report.report_summary:
        lines.extend(["## Cycle Summary", "", report.report_summary, ""])

    lines.extend([f"## Findings ({len(report.findings)})", ""])
    if not report.findings:
        lines.extend(["No verified actionable findings this cycle.", ""])
    for index, finding in enumerate(report.findings, start=1):
        lines.extend(
            [
                f"### {index}. {finding.claim}",
                "",
                f"- **Consequence:** {finding.consequence}",
                f"- **Remedy:** {finding.remedy}",
                f"- **Origin:** {finding.origin}",
                f"- **Locus:** {_render_locations(finding.locus)}",
                f"- **Confidence:** {finding.confidence:.2f}",
                f"- **Evidence:** {', '.join(finding.evidence_refs) or '—'}",
                "",
            ]
        )

    lines.extend([f"## Backlog ({len(report.backlog)})", ""])
    if not report.backlog:
        lines.extend(["No supported, deferred, or open hypotheses.", ""])
    for index, item in enumerate(report.backlog, start=1):
        lines.extend(
            [
                f"### {index}. {item.claim}  _({item.grade})_",
                "",
                f"- **Consequence:** {item.consequence}",
                f"- **Proposed remedy:** {item.remedy}",
                f"- **Locus:** {_render_locations(item.locus)}",
                "",
            ]
        )

    if report.retractions:
        lines.extend([f"## Retractions ({len(report.retractions)})", ""])
        for item in report.retractions:
            lines.extend(
                [
                    f"- **{item.claim}** — refuted; supersedes "
                    f"{', '.join(item.supersedes)}",
                ]
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "BacklogItem",
    "Evidence",
    "Finding",
    "GuardianReport",
    "Retraction",
    "render_markdown",
    "report_views",
]
