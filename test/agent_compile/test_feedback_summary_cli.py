# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CLI smoke tests for small-run feedback summaries."""

from __future__ import annotations

import json

from scripts.agent_compile import feedback_summary


def _write_cell(cells_dir, name, arm, recall, tokens, context_sources=None):
    cells_dir.joinpath(name).write_text(
        json.dumps(
            {
                "success": True,
                "metrics_meaningful": True,
                "subset_id": arm,
                "format_failed": False,
                "metrics": {
                    "files": {"5": {"recall": recall}},
                    "answer_blocks": {"5": {"recall": recall}},
                },
                "total_tokens": tokens,
                "prompt_tokens": tokens - 10,
                "cost_usd": 0.01,
                "total_turns": 2,
                "trace_summary": {
                    "context_tokens_estimate": 25 if context_sources else 0,
                    "context_sources": context_sources or {},
                    "failure_categories": [],
                    "tool_call_count": 1,
                    "read_count": 1,
                },
            }
        ),
        encoding="utf-8",
    )


def test_feedback_summary_cli_prints_report_and_writes_json(tmp_path, capsys):
    run_dir = tmp_path / "run"
    cells_dir = run_dir / "cells"
    output_json = run_dir / "feedback_summary.json"
    cells_dir.mkdir(parents=True)
    _write_cell(cells_dir, "base.json", "grep_only", 0.5, 100)
    _write_cell(
        cells_dir,
        "route.json",
        "route_context",
        1.0,
        130,
        context_sources={"lsp_route": 1},
    )

    rc = feedback_summary.main(
        [
            str(run_dir),
            "--baseline",
            "grep_only",
            "--output-json",
            str(output_json),
        ]
    )

    out = capsys.readouterr().out
    payload = json.loads(output_json.read_text(encoding="utf-8"))

    assert rc == 0
    assert "# Agent feedback summary" in out
    assert "| route_context | 1 | 100%" in out
    assert "lsp_route:1" in out
    assert "+0.500" in out
    assert payload["results"][0]["summary"]["baseline"] == "grep_only"
