# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for Guardian DeepSWE ablation command construction."""

import json
from pathlib import Path

from scripts.guardian import deepswe_export_dashboard_data as dashboard
from scripts.guardian import deepswe_guardian_ablation as ablation


def test_no_budget_limit_is_forwarded_to_guardian_agent(tmp_path):
    args = ablation.parse_args(
        [
            "--model",
            "gpt-5.6-terra",
            "--reasoning-effort",
            "medium",
            "--tasks",
            "fixture-task",
            "--guardian-no-budget-limit",
        ]
    )

    command = ablation._build_pier_command(
        args,
        task="fixture-task",
        baseline="guardian",
        logs_dir=Path(tmp_path),
    )

    assert args.guardian_no_budget_limit is True
    assert "guardian_no_budget_limit=true" in command
    assert not any(part.startswith("guardian_budget_tokens=") for part in command)


def test_finite_budget_remains_the_default(tmp_path):
    args = ablation.parse_args(
        [
            "--model",
            "gpt-5.6-terra",
            "--reasoning-effort",
            "medium",
            "--tasks",
            "fixture-task",
        ]
    )

    command = ablation._build_pier_command(
        args,
        task="fixture-task",
        baseline="guardian",
        logs_dir=Path(tmp_path),
    )

    assert args.guardian_no_budget_limit is False
    assert "guardian_budget_tokens=50000" in command


def test_task_virtualenv_profile_is_mounted_in_both_arms(tmp_path):
    args = ablation.parse_args(
        [
            "--model",
            "gpt-5.6-terra",
            "--reasoning-effort",
            "medium",
            "--tasks",
            "fixture-task",
            "--codeminer-root",
            str(tmp_path),
        ]
    )

    for baseline in ("solo", "guardian"):
        command = ablation._build_pier_command(
            args,
            task="fixture-task",
            baseline=baseline,
            logs_dir=tmp_path / baseline,
        )
        mounts = json.loads(command[command.index("--mounts-json") + 1])
        profile = next(
            item
            for item in mounts
            if item["target"] == "/etc/profile.d/zz-deepswe-task-venv.sh"
        )
        assert profile["source"] == str(
            tmp_path / "deepsweguardian" / "task_venv_profile.sh"
        )


def test_guardian_run_status_aggregates_every_cycle(tmp_path):
    episodes = tmp_path / "guardian_episodes"
    rows = [
        {
            "commit": "first",
            "findings": 1,
            "backlog": 2,
            "degraded": True,
            "analysis_status": "degraded",
            "exit_reason": "ReportSubmitted",
            "llm_tokens": {
                "prompt": 100,
                "cached_input": 60,
                "completion": 10,
                "total": 110,
            },
        },
        {
            "commit": "second",
            "findings": 0,
            "backlog": 0,
            "degraded": False,
            "analysis_status": "complete",
            "exit_reason": "ReportSubmitted",
            "llm_tokens": {
                "prompt": 200,
                "cached_input": 150,
                "completion": 20,
                "total": 220,
            },
        },
    ]
    for index, row in enumerate(rows, 1):
        path = episodes / f"{index:04d}_commit" / "status.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(row), encoding="utf-8")

    status = ablation._guardian_run_status(tmp_path)

    assert status["commit"] == "second"
    assert status["findings"] == 0
    assert status["backlog"] == 0
    assert status["cycle_count"] == 2
    assert status["exit_reasons"] == ["ReportSubmitted", "ReportSubmitted"]
    assert status["degraded"] is True
    assert status["analysis_status"] == "degraded"
    assert status["llm_tokens"] == {
        "prompt": 300,
        "cached_input": 210,
        "completion": 30,
        "total": 330,
    }


def test_guardian_run_status_preserves_incomplete_cycle_health(tmp_path):
    episodes = tmp_path / "guardian_episodes"
    for index, analysis_status in enumerate(("complete", "incomplete"), 1):
        path = episodes / f"{index:04d}_commit" / "status.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "analysis_status": analysis_status,
                    "exit_reason": (
                        "ReportSubmitted"
                        if analysis_status == "complete"
                        else "NoProgress"
                    ),
                    "llm_tokens": {"total": index},
                }
            ),
            encoding="utf-8",
        )

    status = ablation._guardian_run_status(tmp_path)

    assert status["analysis_status"] == "incomplete"
    assert status["llm_tokens"]["total"] == 3


def test_dashboard_preserves_guardian_analysis_health_fields():
    row = dashboard._trial_row(
        {
            "baseline": "guardian",
            "guardian_findings": 0,
            "guardian_backlog": 3,
            "guardian_high_confidence_backlog": 2,
            "guardian_degraded": True,
            "guardian_analysis_status": "degraded",
            "guardian_cycle_count": 3,
            "guardian_exit_reasons": ["ReportSubmitted"] * 3,
            "guardian_total_tokens": 291_925,
        }
    )

    assert row["guardian_findings"] == 0
    assert row["guardian_backlog"] == 3
    assert row["guardian_high_confidence_backlog"] == 2
    assert row["guardian_degraded"] is True
    assert row["guardian_analysis_status"] == "degraded"
    assert row["guardian_cycle_count"] == 3
    assert row["guardian_exit_reasons"] == ["ReportSubmitted"] * 3
    assert row["guardian_total_tokens"] == 291_925
