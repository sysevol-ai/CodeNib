# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the reusable cost-arm aggregator."""

from __future__ import annotations

import json

from codeminer.eval.reports import cost_arm_report as agg_mod


def _cell(instance, arm, skills, rep, *, f5, tokens, scenario, calls, turns=3):
    """Build a synthetic cell record (the schema run_sweep.py emits)."""
    return {
        "instance_id": instance,
        "subset_id": arm,
        "skills": skills,
        "rep": rep,
        "scenario": scenario,
        "success": True,
        "metrics": {
            "files": {1: {"accuracy": f5}, 3: {"accuracy": f5}, 5: {"accuracy": f5}},
            "symbols": {5: {"accuracy": 0.0}},
        },
        "total_tokens": tokens,
        "total_turns": turns,
        "cost_usd": tokens * 1e-6,
        "tool_calls": [{"skill_id": s, "n_results": 5, "error": False} for s in calls],
    }


def _cell_with_trace(
    instance,
    arm,
    skills,
    rep,
    *,
    f5,
    tokens,
    scenario,
    calls,
    context_sources,
    route_context=None,
    route_tool_calls=None,
    turns=3,
):
    cell = _cell(
        instance,
        arm,
        skills,
        rep,
        f5=f5,
        tokens=tokens,
        scenario=scenario,
        calls=calls,
        turns=turns,
    )
    trace_summary = {"context_sources": context_sources}
    if route_context is not None:
        trace_summary["lsp_route_context"] = route_context
    if route_tool_calls is not None:
        trace_summary["lsp_route_tool_calls"] = route_tool_calls
    cell["trace_summary"] = trace_summary
    return cell


def test_files_at_k_mean_across_reps_then_instances():
    cells = [
        _cell(
            "i1",
            "GREP",
            ["file_read"],
            1,
            f5=1.0,
            tokens=100,
            scenario="py:x",
            calls=["grep"],
        ),
        _cell(
            "i1",
            "GREP",
            ["file_read"],
            2,
            f5=0.0,
            tokens=120,
            scenario="py:x",
            calls=["grep"],
        ),
        _cell(
            "i2",
            "GREP",
            ["file_read"],
            1,
            f5=1.0,
            tokens=200,
            scenario="py:x",
            calls=["grep"],
        ),
    ]
    agg = agg_mod.aggregate(cells, ks=[1, 3, 5], max_turns=16)
    # i1: mean(1.0,0.0)=0.5 ; i2: 1.0 ; arm mean = 0.75
    assert abs(agg["arms"]["GREP"]["files_at_k"][5] - 0.75) < 1e-9
    # tokens: min across reps then mean across instances -> mean(100,200)=150
    assert abs(agg["arms"]["GREP"]["mean_total_tokens"] - 150.0) < 1e-9


def test_easy_hard_split_from_baseline():
    cells = [
        # i_easy: baseline hits -> easy ; i_hard: baseline misses -> hard
        _cell(
            "i_easy",
            "GREP",
            ["file_read"],
            1,
            f5=1.0,
            tokens=100,
            scenario="py:x",
            calls=["grep"],
        ),
        _cell(
            "i_hard",
            "GREP",
            ["file_read"],
            1,
            f5=0.0,
            tokens=100,
            scenario="py:x",
            calls=["grep"],
        ),
        _cell(
            "i_easy",
            "GRAPH",
            ["bm25_names"],
            1,
            f5=1.0,
            tokens=300,
            scenario="py:x",
            calls=["bm25_names"],
        ),
        _cell(
            "i_hard",
            "GRAPH",
            ["bm25_names"],
            1,
            f5=1.0,
            tokens=300,
            scenario="py:x",
            calls=["bm25_names"],
        ),
    ]
    agg = agg_mod.aggregate(cells, ks=[1, 3, 5], max_turns=16)
    assert agg["baseline_arm"] == "GREP"
    assert agg["easy_instances"] == ["i_easy"]
    assert agg["hard_instances"] == ["i_hard"]
    # GRAPH recovers the hard instance (0 -> 1) -> files@5 on the hard slice is 1.0
    assert abs(agg["arms"]["GRAPH"]["files_at_5_hard"] - 1.0) < 1e-9


def test_invocation_histogram_and_offered_but_ignored():
    cells = [
        _cell(
            "i1",
            "GRAPH",
            ["bm25_names", "find_callers"],
            1,
            f5=1.0,
            tokens=100,
            scenario="s",
            calls=["bm25_names", "bm25_names"],
        ),  # find_callers unused
    ]
    agg = agg_mod.aggregate(cells, ks=[5], max_turns=16)
    hist = agg["arms"]["GRAPH"]["invocation_histogram"]
    assert hist["bm25_names"]["invocation_rate"] == 1.0
    assert hist["bm25_names"]["mean_calls_per_cell"] == 2.0
    assert "find_callers" in agg["arms"]["GRAPH"]["offered_but_ignored"]


def test_focused_lsp_route_adoption_separates_tool_calls_from_context():
    cells = [
        _cell_with_trace(
            "i1",
            "lsp_route_skill",
            ["lsp_route"],
            1,
            f5=1.0,
            tokens=100,
            scenario="s",
            calls=["lsp_route", "read"],
            context_sources={"lsp_route": 1},
            route_tool_calls=[
                {
                    "turn": 1,
                    "status": "ok",
                    "result_count": 5,
                    "duration_ms": 42.0,
                    "arguments": {"symbols": ["F523"], "top_k": 5},
                }
            ],
        ),
        _cell_with_trace(
            "i1",
            "lsp_route_preload",
            ["read", "grep", "glob", "bash"],
            1,
            f5=1.0,
            tokens=80,
            scenario="s",
            calls=["read"],
            context_sources={"lsp_route": 1},
            route_context={"status": "offered", "seed_source": "query"},
            route_tool_calls=[],
        ),
    ]

    agg = agg_mod.aggregate(cells, ks=[5], max_turns=16)
    dynamic = agg["arms"]["lsp_route_skill"]["focused_adoption"]["lsp_route"]
    preload = agg["arms"]["lsp_route_preload"]["focused_adoption"]["lsp_route"]

    assert dynamic["tool_invocation_rate"] == 1.0
    assert dynamic["mean_tool_calls_per_cell"] == 1.0
    assert dynamic["context_offer_rate"] == 0.0
    assert dynamic["touch_rate"] == 1.0
    assert preload["tool_invocation_rate"] == 0.0
    assert preload["context_offer_rate"] == 1.0
    assert preload["mean_context_entries_per_cell"] == 1.0
    assert preload["touch_rate"] == 1.0
    rows = {(row["instance_id"], row["arm"]): row for row in agg["per_instance_lsp"]}
    assert rows[("i1", "lsp_route_skill")]["lsp_route_tool_calls"] == [
        {
            "turn": 1,
            "status": "ok",
            "result_count": 5,
            "duration_ms": 42.0,
            "arguments": {"symbols": ["F523"], "top_k": 5},
        }
    ]
    assert rows[("i1", "lsp_route_skill")]["lsp_tool_duration_ms"] == 42.0

    report = agg_mod.render_markdown(agg, front=[], ks=[5])
    assert "## Focused LSP-route adoption" in report
    assert "| lsp_route_skill | 100% | 1.00 | 0% | 0.00 | 100%" in report


def test_per_instance_lsp_feedback_reports_context_and_baseline_delta():
    cells = [
        _cell(
            "i1",
            "grep_only",
            ["read", "grep"],
            1,
            f5=1.0,
            tokens=100,
            scenario="py:x",
            calls=["grep", "read"],
            turns=10,
        ),
        _cell_with_trace(
            "i1",
            "lsp_route_preload",
            ["read", "grep", "glob", "bash"],
            1,
            f5=1.0,
            tokens=80,
            scenario="py:x",
            calls=["read"],
            context_sources={"lsp_route": 1},
            route_context={
                "status": "offered",
                "seeds": ["F523"],
                "seed_source": "symbol",
                "route_preview": [
                    {
                        "location": "fixes.rs:67-106",
                        "symbol": "remove_unused_positional_arguments_from_format_call",
                        "type": "function",
                    }
                ],
                "duration_ms": 7.5,
            },
            turns=8,
        ),
    ]

    agg = agg_mod.aggregate(cells, ks=[5], max_turns=16, baseline="grep_only")
    rows = {(row["instance_id"], row["arm"]): row for row in agg["per_instance_lsp"]}
    preload = rows[("i1", "lsp_route_preload")]

    assert preload["lsp_context_entries"] == 1.0
    assert preload["lsp_context_status"] == "offered"
    assert preload["lsp_route_seeds"] == ["F523"]
    assert preload["lsp_route_preview"] == [
        {
            "location": "fixes.rs:67-106",
            "symbol": "remove_unused_positional_arguments_from_format_call",
            "type": "function",
        }
    ]
    assert preload["lsp_context_duration_ms"] == 7.5
    assert preload["lsp_context_visible_turn"] == 0
    assert preload["lsp_context_visible_ms"] == 7.5
    assert preload["lsp_touched"] is True
    assert preload["delta_turns_vs_baseline"] == -2
    assert preload["delta_tokens_vs_baseline"] == -20

    report = agg_mod.render_markdown(agg, front=[], ks=[5])
    assert "## Per-instance LSP-route feedback" in report
    assert (
        "| i1 | lsp_route_preload | py:x | 0.00 | 1.00 | n/a | 7.5 | "
        "n/a | 0.0 | n/a | 7.5 | n/a | offered | n/a | symbol | yes | "
        "8.0 | -2.0 |"
    ) in report
    assert "| lsp_route_preload | 0% | 0.00 | 100% | 1.00 | 100%" in report


def test_pareto_front_excludes_dominated():
    cells = [
        _cell(
            "i1",
            "GREP",
            ["file_read"],
            1,
            f5=1.0,
            tokens=100,
            scenario="s",
            calls=["grep"],
        ),
        # EVERYTHING: same accuracy, more tokens -> dominated by GREP
        _cell(
            "i1", "EVERYTHING", ["x"], 1, f5=1.0, tokens=900, scenario="s", calls=["x"]
        ),
    ]
    agg = agg_mod.aggregate(cells, ks=[1, 3, 5], max_turns=16)
    front = agg_mod.pareto_front(agg)
    assert "GREP" in front
    assert "EVERYTHING" not in front


def test_write_report_writes_metrics_and_markdown(tmp_path):
    cells = [
        _cell(
            "i1",
            "grep_only",
            ["read", "grep"],
            1,
            f5=1.0,
            tokens=100,
            scenario="py:x",
            calls=["grep"],
        )
    ]

    markdown = agg_mod.write_report(
        cells=cells,
        output_dir=tmp_path,
        metrics_k=[5],
        max_turns=16,
        baseline="grep_only",
    )

    assert "# Agent-compile cost-arm report" in markdown
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == markdown
    metrics = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["cell_count"] == 1
    assert metrics["aggregate"]["baseline_arm"] == "grep_only"
    assert metrics["pareto_front"] == ["grep_only"]
