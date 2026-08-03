# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for small-run agent feedback summaries."""

from __future__ import annotations

import json

import pytest

from codenib.eval.agent_runner.feedback_summary import (
    load_feedback_summary,
    summarize_feedback_cells,
)


def _cell(
    arm,
    *,
    files,
    answer_blocks,
    tokens,
    cost,
    turns,
    context_tokens=0,
    context_sources=None,
    success=True,
    metrics_meaningful=True,
    format_failed=False,
    trace_failures=None,
):
    return {
        "subset_id": arm,
        "success": success,
        "metrics_meaningful": metrics_meaningful,
        "format_failed": format_failed,
        "metrics": {
            "files": {5: {"recall": files}},
            "answer_blocks": {5: {"recall": answer_blocks}},
        },
        "total_tokens": tokens,
        "prompt_tokens": tokens - 100,
        "cost_usd": cost,
        "total_turns": turns,
        "trace_summary": {
            "context_tokens_estimate": context_tokens,
            "context_sources": context_sources or {},
            "failure_categories": trace_failures or [],
            "tool_call_count": 2,
            "read_count": 1,
        },
    }


def test_feedback_summary_groups_arms_and_reports_baseline_deltas():
    cells = [
        _cell(
            "grep_only",
            files=0.5,
            answer_blocks=0.25,
            tokens=1000,
            cost=0.01,
            turns=3,
        ),
        _cell(
            "route_context",
            files=1.0,
            answer_blocks=0.75,
            tokens=1200,
            cost=0.012,
            turns=2,
            context_tokens=50,
            context_sources={"lsp_route": 1},
        ),
    ]

    summary = summarize_feedback_cells(cells, baseline="grep_only", k=5)

    assert summary.cell_count == 2
    assert summary.baseline == "grep_only"
    arms = {arm.arm: arm for arm in summary.arms}
    assert arms["route_context"].files_recall_at_k == 1.0
    assert arms["route_context"].answer_blocks_recall_at_k == 0.75
    assert arms["route_context"].context_sources == {"lsp_route": 1}

    assert len(summary.deltas) == 1
    delta = summary.deltas[0]
    assert delta.arm == "route_context"
    assert delta.files_recall_delta == 0.5
    assert delta.answer_blocks_recall_delta == 0.5
    assert delta.total_tokens_delta == 200
    assert delta.cost_usd_delta == pytest.approx(0.002)
    assert delta.total_turns_delta == -1
    assert delta.context_tokens_delta == 50


def test_feedback_summary_keeps_runtime_failure_groups_separate():
    cells = [
        _cell(
            "graph",
            files=0.0,
            answer_blocks=0.0,
            tokens=0,
            cost=0.0,
            turns=0,
            success=False,
            metrics_meaningful=False,
            trace_failures=["tool_error", "answer_contract_missing"],
        ),
        _cell(
            "graph",
            files=0.0,
            answer_blocks=0.0,
            tokens=500,
            cost=0.005,
            turns=4,
            format_failed=True,
        ),
    ]

    summary = summarize_feedback_cells(cells, k=5)
    arm = summary.arms[0]

    assert arm.success_rate == 0.5
    assert arm.metrics_meaningful_rate == 0.5
    assert arm.format_fail_rate == 1.0
    assert arm.trace_failure_categories == {
        "answer_contract_missing": 1,
        "tool_error": 1,
    }
    assert arm.failure_groups == {
        "context_runtime": 1,
        "format": 2,
        "infrastructure": 1,
    }


def test_load_feedback_summary_reads_cells_directory(tmp_path):
    cells_dir = tmp_path / "cells"
    cells_dir.mkdir()
    (cells_dir / "a.json").write_text(
        json.dumps(
            _cell(
                "grep_only",
                files=1.0,
                answer_blocks=1.0,
                tokens=100,
                cost=0.001,
                turns=1,
            )
        ),
        encoding="utf-8",
    )

    summary = load_feedback_summary(tmp_path, baseline="grep_only")

    assert summary.to_dict()["cell_count"] == 1
    assert summary.to_dict()["arms"][0]["arm"] == "grep_only"
