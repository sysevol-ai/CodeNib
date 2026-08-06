# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for Guardian DeepSWE ablation command construction."""

import json
from pathlib import Path

from scripts.guardian.deepswe import ablation
from scripts.guardian.deepswe import export_dashboard as dashboard


def test_local_specification_rollout_controls_are_forwarded(tmp_path):
    args = ablation.parse_args(
        [
            "--model",
            "gpt-5.6-terra",
            "--reasoning-effort",
            "medium",
            "--tasks",
            "fixture-task",
            "--guardian-explorer-count",
            "3",
            "--guardian-max-findings",
            "4",
            "--guardian-max-cycles",
            "2",
            "--guardian-rollout-timeout",
            "123",
        ]
    )

    command = ablation._build_pier_command(
        args,
        task="fixture-task",
        baseline="guardian",
        logs_dir=Path(tmp_path),
    )

    assert "guardian_explorer_count=3" in command
    assert "guardian_max_findings=4" in command
    assert "guardian_max_cycles=2" in command
    assert "guardian_rollout_timeout=123.0" in command


def test_reframed_guardian_defaults_are_explicit(tmp_path):
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

    assert "guardian_explorer_count=2" in command
    assert "guardian_max_findings=5" in command
    assert "guardian_max_cycles=3" in command
    assert "guardian_rollout_timeout=600.0" in command


def test_baseline_selection_can_run_only_guardian(tmp_path, monkeypatch):
    task = tmp_path / "deep-swe" / "tasks" / "fixture-task"
    task.mkdir(parents=True)
    seen = []
    monkeypatch.setattr(
        ablation,
        "_run_trial",
        lambda _args, **kwargs: seen.append(kwargs) or {},
    )

    assert (
        ablation.main(
            [
                "--model",
                "gpt-5.6-terra",
                "--reasoning-effort",
                "medium",
                "--tasks",
                "fixture-task",
                "--runs",
                "1",
                "--baselines",
                "guardian",
                "--deepswe-root",
                str(tmp_path / "deep-swe"),
                "--output-root",
                str(tmp_path / "outputs"),
            ]
        )
        == 0
    )
    assert seen == [{"task": "fixture-task", "baseline": "guardian", "repeat_index": 1}]


def test_task_virtualenv_profile_is_mounted_in_both_arms(tmp_path):
    args = ablation.parse_args(
        [
            "--model",
            "gpt-5.6-terra",
            "--reasoning-effort",
            "medium",
            "--tasks",
            "fixture-task",
            "--codenib-root",
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
            tmp_path
            / "scripts"
            / "guardian"
            / "deepswe"
            / "harness"
            / "task_venv_profile.sh"
        )


def test_guardian_mounts_controller_outputs_read_only(tmp_path):
    args = ablation.parse_args(
        [
            "--model",
            "gpt-5.6-terra",
            "--reasoning-effort",
            "medium",
            "--tasks",
            "fixture-task",
            "--codenib-root",
            str(tmp_path),
        ]
    )
    command = ablation._build_pier_command(
        args,
        task="fixture-task",
        baseline="guardian",
        logs_dir=tmp_path / "logs",
    )
    assert (
        f"guardian_host_exchange_dir={tmp_path / 'logs' / 'guardian_exchange'}"
        in command
    )
    mounts = json.loads(command[command.index("--mounts-json") + 1])
    by_target = {item["target"]: item for item in mounts}
    assert set(by_target) == {
        "/logs/agent",
        "/etc/profile.d/zz-deepswe-task-venv.sh",
        "/logs/agent/guardian_exchange/responses",
        "/logs/agent/guardian_exchange/latest",
        "/logs/agent/guardian_episodes",
    }
    assert by_target["/logs/agent"].get("read_only") is None
    assert by_target["/logs/agent/guardian_exchange/responses"]["read_only"] is True
    assert by_target["/logs/agent/guardian_exchange/latest"]["read_only"] is True
    assert by_target["/logs/agent/guardian_episodes"]["read_only"] is True


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


def test_guardian_run_status_does_not_count_limit_response_as_review(tmp_path):
    episodes = tmp_path / "guardian_episodes"
    rows = [
        {
            "analysis_status": "complete",
            "exit_reason": "ReviewCompleted",
            "review_performed": True,
            "terminal": True,
            "termination_reason": "max_cycles_reached",
        },
        {
            "analysis_status": "not_run",
            "exit_reason": "ReviewLimitReached",
            "review_performed": False,
            "terminal": True,
            "termination_reason": "max_cycles_reached",
        },
    ]
    for index, row in enumerate(rows, 1):
        path = episodes / f"{index:04d}_commit" / "status.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(row), encoding="utf-8")

    status = ablation._guardian_run_status(tmp_path)

    assert status["cycle_count"] == 1
    assert status["terminal"] is True
    assert status["termination_reason"] == "max_cycles_reached"


def test_guardian_run_status_orders_sha_directories_by_cycle_index(tmp_path):
    episodes = tmp_path / "guardian_episodes"
    rows = [
        (
            "ffffffffffffffffffffffffffffffffffffffff",
            {
                "commit": "first",
                "cycle_index": 1,
                "terminal": False,
                "analysis_status": "complete",
                "exit_reason": "ReviewCompleted",
            },
        ),
        (
            "0000000000000000000000000000000000000000",
            {
                "commit": "second",
                "cycle_index": 2,
                "terminal": True,
                "termination_reason": "max_cycles_reached",
                "analysis_status": "complete",
                "exit_reason": "ReviewCompleted",
            },
        ),
    ]
    for directory, row in rows:
        path = episodes / directory / "status.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(row), encoding="utf-8")

    status = ablation._guardian_run_status(tmp_path)

    assert status["commit"] == "second"
    assert status["cycle_count"] == 2
    assert status["terminal"] is True
    assert status["termination_reason"] == "max_cycles_reached"


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


def test_dashboard_task_rows_include_average_test_metrics():
    rows = dashboard._task_rows(
        [
            {
                "task": "fixture-task",
                "baseline": "solo",
                "model": "fixture-model",
                "reasoning_effort": "medium",
                "pass_rate": 0.25,
                "avg_f2p": 0.5,
                "avg_p2p": 0.75,
                "avg_partial": 0.625,
                "n_trials": 4,
                "n_recorded_trials": 4,
            },
            {
                "task": "fixture-task",
                "baseline": "guardian",
                "model": "fixture-model",
                "reasoning_effort": "medium",
                "pass_rate": 0.5,
                "avg_f2p": 0.625,
                "avg_p2p": 1.0,
                "avg_partial": 0.75,
                "avg_total_cost_usd": 1.25,
                "n_trials": 4,
                "n_recorded_trials": 4,
            },
        ]
    )

    assert rows == [
        {
            "task": "fixture-task",
            "model": "fixture-model",
            "reasoning_effort": "medium",
            "solo_pass_rate": 0.25,
            "guardian_pass_rate": 0.5,
            "solo_avg_f2p": 0.5,
            "guardian_avg_f2p": 0.625,
            "solo_avg_p2p": 0.75,
            "guardian_avg_p2p": 1.0,
            "solo_avg_partial": 0.625,
            "guardian_avg_partial": 0.75,
            "delta_pass_rate": 0.25,
            "solo_trials": 4,
            "guardian_trials": 4,
            "solo_recorded_trials": 4,
            "guardian_recorded_trials": 4,
            "guardian_avg_cost_usd": 1.25,
        }
    ]
