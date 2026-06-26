# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for codeminer.guardian.report rendering."""

from codeminer.guardian.investigate import Evidence
from codeminer.guardian.report import Finding, GuardianReport, render_markdown


def _sample_report():
    return GuardianReport(
        repo="/repo",
        commit="abcdef1234567890",
        generated_at="2026-06-26 00:00:00 UTC",
        churn_window="90 days ago",
        file_count=123,
        capabilities={"sparse_search": True, "dense_search": True},
        findings=[
            Finding(
                kind="churn",
                title="High-churn file: agent/runner.py",
                detail="Changed in **42** commits over 90 days ago.",
                evidence=[
                    Evidence("agent/runner.py", "query", "function", 892, 900, 0.873),
                ],
            )
        ],
    )


def test_render_includes_metadata_and_evidence():
    md = render_markdown(_sample_report())
    assert "# Repository Guardian Report — /repo" in md
    assert "`abcdef123456`" in md  # commit truncated to 12
    assert "Indexed files:** 123" in md
    assert "agent/runner.py:892-900" in md
    assert "0.873" in md
    assert "| Location | Symbol | Type | Score |" in md


def test_render_states_non_modifying_and_has_no_patches():
    md = render_markdown(_sample_report()).lower()
    assert "non-modifying" in md
    # The invariant: a Phase-1 report never proposes patches/diffs.
    assert "diff --git" not in md
    assert "+++ " not in md
    assert "```diff" not in md


def test_render_handles_no_findings():
    report = GuardianReport(
        repo="/repo",
        commit="",
        generated_at="2026-06-26 00:00:00 UTC",
        churn_window="90 days ago",
    )
    md = render_markdown(report)
    assert "No findings this cycle." in md
    assert "(unknown)" in md


def test_to_dict_roundtrips_findings():
    d = _sample_report().to_dict()
    assert d["file_count"] == 123
    assert d["findings"][0]["kind"] == "churn"
    assert d["findings"][0]["evidence"][0]["file"] == "agent/runner.py"
