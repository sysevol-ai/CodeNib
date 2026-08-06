# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for portable DeepSWE result analysis."""

from __future__ import annotations

import json

import pytest

from codenib.eval.benchmarks.deepswe import add_costs, export_dashboard_data, load_rows


def _write_metadata(path, **overrides):
    path.parent.mkdir(parents=True)
    payload = {
        "task": "fixture-task",
        "baseline": "guardian",
        "job_id": path.parent.name,
        "model": "gpt-5.6-terra",
        "reasoning_effort": "medium",
        "returncode": 0,
        "reward": 1.0,
        "f2p": 0.5,
        "p2p": 1.0,
        "partial": 0.75,
        **overrides,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_add_costs_normalizes_main_and_guardian_usage():
    row = add_costs(
        {
            "model": "gpt-5.6-terra",
            "main_input_tokens": 1_000_000,
            "main_cached_input_tokens": 400_000,
            "main_output_tokens": 100_000,
            "guardian_prompt_tokens": 200_000,
            "guardian_cached_input_tokens": 100_000,
            "guardian_completion_tokens": 50_000,
        }
    )

    assert row["main_cost_usd"] == pytest.approx(3.1)
    assert row["guardian_cost_usd"] == pytest.approx(1.025)
    assert row["total_cost_usd"] == pytest.approx(4.125)


def test_load_rows_prefers_setting_layout_over_matching_legacy_row(tmp_path):
    _write_metadata(
        tmp_path / "fixture-task" / "guardian" / "job_1" / "metadata.json",
        output_dir="legacy",
    )
    _write_metadata(
        tmp_path
        / "gpt-5.6-terra_medium"
        / "fixture-task"
        / "guardian"
        / "job_1"
        / "metadata.json",
        output_dir="setting",
    )

    rows = load_rows(tmp_path)

    assert len(rows) == 1
    assert rows[0]["output_dir"] == "setting"


def test_dashboard_export_uses_packaged_deepswe_analysis(tmp_path):
    _write_metadata(
        tmp_path
        / "gpt-5.6-terra_medium"
        / "fixture-task"
        / "guardian"
        / "job_1"
        / "metadata.json"
    )

    report = export_dashboard_data(tmp_path)

    assert report["counted_job_ids"] == ["job_1", "job_2", "job_3", "job_4"]
    assert report["summary"][0]["avg_f2p"] == 0.5
    assert report["summary"][0]["avg_p2p"] == 1.0
    assert report["summary"][0]["avg_partial"] == 0.75
    assert report["trials"][0]["task"] == "fixture-task"
