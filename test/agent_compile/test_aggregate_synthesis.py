# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for codenib-synthesis aggregation."""

from __future__ import annotations

from scripts.agent_compile import aggregate_synthesis as agg


def _cell(arm: str, trace_summary):
    return {
        "success": True,
        "category": "symbol_hint",
        "subset_id": arm,
        "query_id": "q1",
        "metrics": {
            "answer_blocks": {
                "5": {"accuracy": 1.0, "recall": 1.0},
            },
            "retrieval_blocks": {
                "10": {"recall": 1.0},
            },
        },
        "total_turns": 2,
        "total_tokens": 100,
        "cost_usd": 0.01,
        "trace_summary": trace_summary,
    }


def test_render_reports_lsp_route_exposure_metrics():
    cells = [
        _cell(
            "dynamic",
            {
                "lsp_route_tool_calls": [
                    {
                        "duration_ms": 21.0,
                        "model_can_use_ms": 2333.0,
                        "extra_model_round_trips": 1,
                        "result_count": 5,
                    }
                ],
            },
        ),
        _cell(
            "preload",
            {
                "lsp_route_context": {
                    "status": "offered",
                    "duration_ms": 47.0,
                    "route_visible_ms": 47.0,
                    "route_count": 4,
                },
            },
        ),
    ]

    report = agg.render(agg.aggregate(cells))

    assert "## LSP route exposure" in report
    assert "| symbol_hint | dynamic | 100% | 0% | 5.0 | 21.0 | 2333.0 | 1.0 |" in report
    assert "| symbol_hint | preload | 0% | 100% | 4.0 | 47.0 | 47.0 | 0.0 |" in report
