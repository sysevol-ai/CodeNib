# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the agent-compile cost-arm aggregator."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "agg",
    Path(__file__).resolve().parents[2] / "scripts" / "agent_compile" / "aggregate.py",
)
agg_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(agg_mod)


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
