# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for runtime-probe Pareto analysis."""

from codenib.eval.agent_runner.pareto import analyze_pareto


def _cell(
    category: str,
    arm: str,
    query_id: str,
    recall: float,
    cost_usd: float,
    total_turns: int,
):
    return {
        "success": True,
        "category": category,
        "subset_id": arm,
        "query_id": query_id,
        "metrics": {"answer_blocks": {"5": {"recall": recall}}},
        "cost_usd": cost_usd,
        "total_turns": total_turns,
    }


def test_all_pairs_duplicate_query_ids_within_each_category():
    cells = [
        _cell("behavioral", "grep_only", "same-query-id", 0.0, 1.0, 3),
        _cell("behavioral", "preinj_embed", "same-query-id", 1.0, 0.5, 2),
        _cell("traversal", "grep_only", "same-query-id", 1.0, 1.0, 3),
        _cell("traversal", "preinj_embed", "same-query-id", 0.0, 0.5, 2),
    ]

    report = analyze_pareto(cells, baseline="grep_only", boot=100, seed=0)

    assert "| ALL | preinj_embed | 2 | +0.000" in report
