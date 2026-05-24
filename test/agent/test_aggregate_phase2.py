# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Phase-2 agent-compile aggregator (issue #133)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "agg2",
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "agent_compile"
    / "aggregate_phase2.py",
)
agg2 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(agg2)


def _cell(instance, subset, skills, rep, *, f5, tokens, scenario, calls, turns=3):
    """Build a synthetic cell record."""
    return {
        "instance_id": instance,
        "subset_id": subset,
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
            "A0",
            ["bm25_search"],
            1,
            f5=1.0,
            tokens=100,
            scenario="py:x",
            calls=["bm25_search"],
        ),
        _cell(
            "i1",
            "A0",
            ["bm25_search"],
            2,
            f5=0.0,
            tokens=120,
            scenario="py:x",
            calls=["bm25_search"],
        ),
        _cell(
            "i2",
            "A0",
            ["bm25_search"],
            1,
            f5=1.0,
            tokens=200,
            scenario="py:x",
            calls=["bm25_search"],
        ),
    ]
    agg = agg2.aggregate(cells, ks=[1, 3, 5], max_turns=20)
    # i1: mean(1.0,0.0)=0.5 ; i2: 1.0 ; subset mean = 0.75
    assert abs(agg["subsets"]["A0"]["files_at_k"][5] - 0.75) < 1e-9
    # tokens: min across reps then mean across instances -> mean(100,200)=150
    assert abs(agg["subsets"]["A0"]["mean_total_tokens"] - 150.0) < 1e-9


def test_easy_hard_split_from_a0():
    cells = [
        # i_easy: A0 hits -> easy ; i_hard: A0 misses -> hard
        _cell(
            "i_easy",
            "A0",
            ["bm25_search"],
            1,
            f5=1.0,
            tokens=100,
            scenario="py:x",
            calls=["bm25_search"],
        ),
        _cell(
            "i_hard",
            "A0",
            ["bm25_search"],
            1,
            f5=0.0,
            tokens=100,
            scenario="py:x",
            calls=["bm25_search"],
        ),
        _cell(
            "i_easy",
            "A6",
            ["bm25_search"],
            1,
            f5=1.0,
            tokens=300,
            scenario="py:x",
            calls=["bm25_search"],
        ),
        _cell(
            "i_hard",
            "A6",
            ["bm25_search"],
            1,
            f5=1.0,
            tokens=300,
            scenario="py:x",
            calls=["bm25_search"],
        ),
    ]
    agg = agg2.aggregate(cells, ks=[1, 3, 5], max_turns=20)
    assert agg["easy_instances"] == ["i_easy"]
    assert agg["hard_instances"] == ["i_hard"]
    # A6 lifts on the hard slice (0 -> 1) so it stays eligible
    assert agg["subsets"]["A6"]["compile_eligible"] is True
    assert abs(agg["subsets"]["A6"]["files_at_5_hard"] - 1.0) < 1e-9


def test_saturation_guard_disqualifies_easy_only_lift():
    # A_extra beats A0 only on the easy instance; no lift on hard -> ineligible.
    cells = [
        _cell(
            "e",
            "A0",
            ["bm25_search"],
            1,
            f5=1.0,
            tokens=100,
            scenario="s",
            calls=["bm25_search"],
        ),
        _cell(
            "h",
            "A0",
            ["bm25_search"],
            1,
            f5=0.0,
            tokens=100,
            scenario="s",
            calls=["bm25_search"],
        ),
        _cell(
            "e",
            "AX",
            ["bm25_search", "x"],
            1,
            f5=1.0,
            tokens=500,
            scenario="s",
            calls=["bm25_search"],
        ),
        _cell(
            "h",
            "AX",
            ["bm25_search", "x"],
            1,
            f5=0.0,
            tokens=500,
            scenario="s",
            calls=["bm25_search"],
        ),
    ]
    agg = agg2.aggregate(cells, ks=[1, 3, 5], max_turns=20)
    # AX hard files@5 == A0 hard files@5 (both 0) and easy not greater -> ineligible
    assert agg["subsets"]["AX"]["compile_eligible"] is False


def test_invocation_histogram_and_forced_but_ignored():
    cells = [
        _cell(
            "i1",
            "A5",
            ["bm25_search", "graph_expand"],
            1,
            f5=1.0,
            tokens=100,
            scenario="s",
            calls=["bm25_search", "bm25_search"],
        ),  # graph_expand never called
    ]
    agg = agg2.aggregate(cells, ks=[5], max_turns=20)
    hist = agg["subsets"]["A5"]["invocation_histogram"]
    assert hist["bm25_search"]["invocation_rate"] == 1.0
    assert hist["bm25_search"]["mean_calls_per_cell"] == 2.0
    assert "graph_expand" in agg["subsets"]["A5"]["forced_but_ignored"]


def test_compile_table_picks_cheapest_meeting_tau():
    # A6 files@5 = 1.0 ; tau = 1.0 - 0.05 = 0.95. A0 (cheap) also hits 1.0 -> pick A0.
    cells = [
        _cell(
            "i1",
            "A0",
            ["bm25_search"],
            1,
            f5=1.0,
            tokens=100,
            scenario="py:s",
            calls=["bm25_search"],
        ),
        _cell(
            "i1",
            "A6",
            ["bm25_search", "embedding_search"],
            1,
            f5=1.0,
            tokens=900,
            scenario="py:s",
            calls=["bm25_search"],
        ),
    ]
    agg = agg2.aggregate(cells, ks=[1, 3, 5], max_turns=20)
    table, rationale = agg2.derive_compile_table(agg, cells, tau_margin=0.05)
    assert table["py:s"] == ["bm25_search"]
    assert rationale["scenarios"]["py:s"]["chosen_subset"] == "A0"


def test_pareto_front_excludes_dominated():
    cells = [
        _cell(
            "i1",
            "A0",
            ["bm25_search"],
            1,
            f5=1.0,
            tokens=100,
            scenario="s",
            calls=["bm25_search"],
        ),
        # A6 same accuracy, more tokens -> dominated by A0
        _cell("i1", "A6", ["x"], 1, f5=1.0, tokens=900, scenario="s", calls=["x"]),
    ]
    agg = agg2.aggregate(cells, ks=[1, 3, 5], max_turns=20)
    front = agg2.pareto_front(agg)
    assert "A0" in front
    assert "A6" not in front
