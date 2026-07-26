# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for Guardian DeepSWE ablation command construction."""

from pathlib import Path

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
