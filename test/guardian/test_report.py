# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Tests for grade-filtered Guardian report rendering."""

from codeminer.guardian.loop import Hypothesis
from codeminer.guardian.report import GuardianReport, render_markdown, report_views


def _hypothesis(*, grade, suffix="", supersedes=None):
    evidence = []
    if grade in {"finding", "supported", "refuted"}:
        evidence = [f"probe-valid:1:{suffix or '1'}"]
    elif grade == "resolved":
        evidence = [f"resolved:commit2:{suffix or 'fixed by regression guard'}"]
    return Hypothesis.create(
        claim=f"parse{suffix} accepts invalid input",
        consequence="invalid state reaches callers",
        remedy="validate input and add a regression test",
        origin="exploration",
        locus=[f"agent/runner.py:parse{suffix}"],
        evidence=evidence,
        grade=grade,
        cycle_no=1,
        supersedes=supersedes or [],
        confidence=0.8,
    )


def _sample_report():
    hypotheses = [
        _hypothesis(grade="finding"),
        _hypothesis(grade="supported", suffix="_supported"),
        _hypothesis(
            grade="refuted",
            suffix="_old",
            supersedes=["prior-finding-id"],
        ),
    ]
    findings, backlog, retractions = report_views(hypotheses)
    return GuardianReport(
        repo="/repo",
        commit="abcdef1234567890",
        generated_at="2026-06-26 00:00:00 UTC",
        churn_window="90 days ago",
        file_count=123,
        capabilities={"sparse_search": True, "dense_search": True},
        findings=findings,
        backlog=backlog,
        retractions=retractions,
        cycle_no=1,
        exit_reason="ReportSubmitted",
        report_summary="One verified issue and one supported hypothesis.",
        analysis_status="complete",
    )


def test_render_includes_metadata_and_actionable_finding():
    markdown = render_markdown(_sample_report())
    assert "# Repository Guardian Report — /repo" in markdown
    assert "`abcdef123456`" in markdown
    assert "Indexed files:** 123" in markdown
    assert "parse accepts invalid input" in markdown
    assert "Remedy:** validate input" in markdown
    assert "agent/runner.py:parse" in markdown
    assert "Analysis status:** complete" in markdown


def test_render_makes_degraded_analysis_explicit():
    report = _sample_report()
    report.degraded = True
    report.analysis_status = "degraded"

    markdown = render_markdown(report)

    assert "Analysis status:** degraded" in markdown
    assert "excluded (analysis was degraded)" in markdown


def test_only_finding_grade_reaches_findings_section():
    report = _sample_report()
    assert len(report.findings) == 1
    assert len(report.backlog) == 1
    assert len(report.retractions) == 1
    markdown = render_markdown(report)
    assert "## Findings (1)" in markdown
    assert "## Backlog (1)" in markdown
    assert "## Retractions (1)" in markdown


def test_resolved_hypothesis_is_closed_not_left_in_backlog():
    resolved = _hypothesis(grade="resolved", suffix="_fixed")

    findings, backlog, retractions = report_views([resolved])

    assert findings == []
    assert backlog == []
    assert retractions == []


def test_render_states_non_modifying_and_has_no_patches():
    markdown = render_markdown(_sample_report()).lower()
    assert "non-modifying" in markdown
    assert "diff --git" not in markdown
    assert "+++ " not in markdown
    assert "```diff" not in markdown


def test_render_handles_no_findings():
    report = GuardianReport(
        repo="/repo",
        commit="",
        generated_at="2026-06-26 00:00:00 UTC",
        churn_window="90 days ago",
    )
    markdown = render_markdown(report)
    assert "No verified actionable findings this cycle." in markdown
    assert "(unknown)" in markdown


def test_to_dict_roundtrips_report_views():
    payload = _sample_report().to_dict()
    assert payload["file_count"] == 123
    assert payload["findings"][0]["origin"] == "exploration"
    assert payload["findings"][0]["remedy"].startswith("validate")
    assert payload["backlog"][0]["grade"] == "supported"


def test_superseded_hypothesis_is_not_reported_twice():
    old = _hypothesis(grade="finding", suffix="_old")
    replacement = Hypothesis.create(
        claim="parse replacement accepts invalid input",
        consequence="invalid state reaches callers",
        remedy="validate input and add a regression test",
        origin="memory",
        locus=["agent/runner.py:parse_old"],
        evidence=["probe-valid:2:1"],
        grade="finding",
        cycle_no=2,
        supersedes=[old.id],
    )
    findings, backlog, retractions = report_views([old, replacement])
    assert [item.id for item in findings] == [replacement.id]
    assert backlog == []
    assert retractions == []
