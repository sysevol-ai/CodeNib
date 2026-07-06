# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CLI smoke tests for the runtime-probe Pareto wrapper."""

from __future__ import annotations

import json

from scripts.agent_compile import pareto_ci


def _write_cell(cells_dir, name, category, arm, recall, cost_usd, total_turns):
    cells_dir.joinpath(name).write_text(
        json.dumps(
            {
                "success": True,
                "category": category,
                "subset_id": arm,
                "query_id": "q",
                "metrics": {"answer_blocks": {"5": {"recall": recall}}},
                "cost_usd": cost_usd,
                "total_turns": total_turns,
            }
        ),
        encoding="utf-8",
    )


def test_pareto_ci_cli_writes_report(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    cells_dir.mkdir()
    _write_cell(cells_dir, "base.json", "behavioral", "grep_only", 0.5, 1.0, 3)
    _write_cell(cells_dir, "arm.json", "behavioral", "preload", 0.5, 0.5, 2)

    rc = pareto_ci.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--baseline",
            "grep_only",
            "--boot",
            "20",
            "--seed",
            "0",
            "--output-dir",
            str(output_dir),
        ]
    )

    assert rc == 0
    assert "| behavioral | preload | 1 | +0.000" in capsys.readouterr().out
    assert (output_dir / "pareto_ci.md").exists()
