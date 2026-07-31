# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the resumable DeepSWE solo matrix runner."""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from scripts.guardian import deepswe_solo_matrix as matrix

REPO_ROOT = Path(__file__).resolve().parents[2]


def _args(tmp_path, *extra):
    codeminer_root = tmp_path / "codeminer"
    deepswe_root = tmp_path / "deep-swe"
    (codeminer_root / "deepsweguardian").mkdir(parents=True)
    (codeminer_root / "deepsweguardian" / "task_venv_profile.sh").touch()
    for task in matrix.DEFAULT_TASKS:
        (deepswe_root / "tasks" / task).mkdir(parents=True)
    return matrix.parse_args(
        [
            "--codeminer-root",
            str(codeminer_root),
            "--deepswe-root",
            str(deepswe_root),
            "--jobs-dir",
            str(deepswe_root / "jobs"),
            "--output-root",
            str(tmp_path / "outputs"),
            *extra,
        ]
    )


def test_default_plan_has_sixty_unique_fixed_slots(tmp_path):
    args = _args(tmp_path)

    plan = matrix._build_plan(args)

    assert len(plan) == 60
    assert len({trial.key for trial in plan}) == 60
    assert {trial.model for trial in plan} == set(matrix.DEFAULT_MODELS)
    assert {trial.task for trial in plan} == set(matrix.DEFAULT_TASKS)
    assert {trial.repeat_index for trial in plan} == {1, 2, 3, 4}
    assert args.concurrency == 3
    assert len({trial.model for trial in plan[:3]}) == 3
    assert len({trial.task for trial in plan[:3]}) == 3


def test_each_trial_has_an_isolated_pier_jobs_directory(tmp_path):
    args = _args(tmp_path)
    plan = matrix._build_plan(args)

    jobs_dirs = [matrix._trial_jobs_dir(args, trial) for trial in plan]

    assert len(jobs_dirs) == len(set(jobs_dirs))
    assert all(args.jobs_dir in path.parents for path in jobs_dirs)


def test_matching_metadata_makes_a_slot_resumable(tmp_path):
    args = _args(tmp_path)
    trial = matrix.Trial(
        model="gpt-5.6-terra",
        task="igel-persist-feature-schema",
        repeat_index=1,
    )
    trial_dir = matrix._trial_dir(args, trial)
    trial_dir.mkdir(parents=True)
    (trial_dir / "metadata.json").write_text(
        json.dumps(
            {
                "model": trial.model,
                "task": trial.task,
                "baseline": "solo",
                "job_id": trial.job_id,
                "reasoning_effort": args.reasoning_effort,
                "returncode": 0,
            }
        ),
        encoding="utf-8",
    )

    status = matrix._initial_status(args, trial)

    assert status["status"] == "recorded"
    assert status["returncode"] == 0


def test_mismatched_or_malformed_metadata_does_not_skip_slot(tmp_path):
    args = _args(tmp_path)
    trial = matrix.Trial(
        model="gpt-5.6-terra",
        task="igel-persist-feature-schema",
        repeat_index=1,
    )
    trial_dir = matrix._trial_dir(args, trial)
    trial_dir.mkdir(parents=True)
    (trial_dir / "metadata.json").write_text("{bad json", encoding="utf-8")
    assert matrix._initial_status(args, trial)["status"] == "pending"

    (trial_dir / "metadata.json").write_text(
        json.dumps(
            {
                "model": "different-model",
                "task": trial.task,
                "baseline": "solo",
                "job_id": trial.job_id,
                "reasoning_effort": args.reasoning_effort,
            }
        ),
        encoding="utf-8",
    )
    assert matrix._initial_status(args, trial)["status"] == "pending"


def test_archive_preserves_metadata_without_double_count_filename(tmp_path):
    args = _args(tmp_path)
    trial = matrix.Trial(
        model="gpt-5.6-terra",
        task="igel-persist-feature-schema",
        repeat_index=1,
    )
    trial_dir = matrix._trial_dir(args, trial)
    trial_dir.mkdir(parents=True)
    (trial_dir / "metadata.json").write_text("{}", encoding="utf-8")
    (trial_dir / "pier_stdout.txt").write_text("kept", encoding="utf-8")

    archived = matrix._archive_trial_dir(args, trial)

    assert archived is not None
    assert not trial_dir.exists()
    assert (archived / "archived_metadata.json").exists()
    assert not (archived / "metadata.json").exists()
    assert (archived / "pier_stdout.txt").read_text(encoding="utf-8") == "kept"


def test_context_file_builds_reproducible_pier_prompt_template(tmp_path):
    context_file = tmp_path / "review.md"
    context_file.write_text("Challenge unsupported beliefs.", encoding="utf-8")
    args = _args(tmp_path, "--context-file", str(context_file))
    matrix._validate(args)
    matrix._prepare_context_template(args)
    trial = matrix.Trial(
        model="gpt-5.6-terra",
        task="igel-persist-feature-schema",
        repeat_index=1,
    )

    template = args.prompt_template_path.read_text(encoding="utf-8")
    command = matrix.ablation._build_pier_command(
        matrix._ablation_args(args, trial),
        task=trial.task,
        baseline="solo",
        logs_dir=matrix._trial_dir(args, trial) / "agent_logs",
    )

    assert template.startswith("{{ instruction }}")
    assert "Challenge unsupported beliefs." in template
    assert len(args.context_injection_sha256) == 64
    assert f"prompt_template_path={args.prompt_template_path}" in command


def test_task_context_directory_builds_distinct_templates(tmp_path):
    tasks = matrix.DEFAULT_TASKS[:2]
    context_dir = tmp_path / "contexts"
    context_dir.mkdir()
    for index, task in enumerate(tasks):
        (context_dir / f"{task}.md").write_text(
            f"Task-specific obligation {index}.",
            encoding="utf-8",
        )
    args = _args(
        tmp_path,
        "--tasks",
        *tasks,
        "--task-context-dir",
        str(context_dir),
    )
    matrix._validate(args)
    matrix._prepare_context_template(args)

    assert set(args.prompt_template_paths_by_task) == set(tasks)
    assert len(set(args.prompt_template_paths_by_task.values())) == 2
    assert len(set(args.context_injection_sha256_by_task.values())) == 2
    for index, task in enumerate(tasks):
        trial = matrix.Trial(
            model="gpt-5.6-terra",
            task=task,
            repeat_index=1,
        )
        trial_args = matrix._ablation_args(args, trial)
        template = trial_args.prompt_template_path.read_text(encoding="utf-8")
        assert f"Task-specific obligation {index}." in template
        assert trial_args.context_injection_source == (
            context_dir / f"{task}.md"
        ).resolve()
        assert (
            trial_args.context_injection_sha256
            == args.context_injection_sha256_by_task[task]
        )


def test_run_plan_limits_parallel_trials_to_configured_concurrency(
    tmp_path, monkeypatch
):
    args = _args(
        tmp_path,
        "--tasks",
        *matrix.DEFAULT_TASKS[:3],
        "--runs",
        "1",
        "--concurrency",
        "3",
    )
    matrix._prepare_context_template(args)
    plan = matrix._build_plan(args)[:6]
    statuses = {trial.key: matrix._initial_status(args, trial) for trial in plan}
    lock = threading.Lock()
    active = 0
    maximum_active = 0
    seen_jobs_dirs = set()

    def fake_run_trial(trial_args, *, task, baseline, repeat_index):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            seen_jobs_dirs.add(trial_args.jobs_dir)
        time.sleep(0.03)
        with lock:
            active -= 1
        return {
            "returncode": 0,
            "reward": 1.0,
            "f2p": 1.0,
            "p2p": 1.0,
            "partial": 1.0,
        }

    monkeypatch.setattr(matrix.ablation, "_run_trial", fake_run_trial)

    run_count, launcher_errors = matrix._run_plan(
        args,
        plan,
        statuses,
        started_at="2026-01-01T00:00:00",
    )

    assert run_count == 6
    assert launcher_errors == 0
    assert maximum_active == 3
    assert len(seen_jobs_dirs) == 6
    assert {status["status"] for status in statuses.values()} == {"recorded"}


def test_script_can_be_invoked_directly():
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "guardian" / "deepswe_solo_matrix.py"),
            "--help",
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "--rerun-recorded" in proc.stdout
