# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for frozen feedback-suite plan materialization."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.agent_compile.plan_feedback_suite import build_plan, main


def test_feedback_suite_plan_materializes_real_route_gate(tmp_path, capsys):
    suite_file = Path(
        "scripts/agent_compile/feedback_suites/haiku_static_lsp_route_q2.yaml"
    )

    rc = main(
        [
            "--suite-file",
            str(suite_file),
            "--output-dir",
            str(tmp_path),
            "--write-plan",
            "--print-json",
        ]
    )
    captured = capsys.readouterr()
    plan = json.loads(captured.out)
    written = json.loads((tmp_path / "feedback_suite_plan.json").read_text())

    assert rc == 0
    assert captured.err == ""
    assert plan["suite_id"] == "haiku_static_lsp_route_q2"
    assert plan["promotion_profiles"] == ["route_lifecycle"]
    assert plan["synthesis_configs"] == ["Python", "Go", "Rust"]
    assert plan["categories"] == ["traversal"]
    assert plan["arms"] == ["grep_only", "preinj_graph_scheduled"]
    assert plan["expected_queries"] == 6
    assert plan["expected_cells"] == 24
    assert plan["max_cells"] == 30
    assert [command["name"] for command in plan["commands"]] == [
        "preflight",
        "sweep",
        "aggregate",
    ]
    assert "run_synthesis_sweep.py" in plan["commands"][1]["command"]
    assert "--promotion-profile route_lifecycle" in plan["commands"][2]["command"]
    assert written == plan


def test_feedback_suite_plan_rejects_expected_cell_mismatch(tmp_path):
    config = tmp_path / "suite_config.yaml"
    suite = tmp_path / "suite.yaml"
    config.write_text(
        "\n".join(
            [
                "sweep_id: unit_suite",
                "reps: 2",
                "subsets:",
                "  grep_only: [read]",
                "  preinj_graph_scheduled: [read]",
            ]
        ),
        encoding="utf-8",
    )
    suite.write_text(
        "\n".join(
            [
                "suite_id: unit_suite",
                f"config: {config}",
                f"output_dir: {tmp_path / 'out'}",
                "promotion_profiles: [route_lifecycle]",
                "synthesis_configs: [Python]",
                "categories: [traversal]",
                "max_queries_per_category: 1",
                "reps: 2",
                "expected_queries: 1",
                "expected_cells: 3",
            ]
        ),
        encoding="utf-8",
    )

    try:
        build_plan(suite_file=suite)
    except RuntimeError as exc:
        assert "expected_cells does not match" in str(exc)
    else:
        raise AssertionError("expected cell mismatch to fail")
