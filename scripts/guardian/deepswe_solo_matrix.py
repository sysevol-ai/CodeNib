#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Run the initial DeepSWE solo baseline matrix.

The default matrix is the five Python tasks selected for the Guardian study,
three Codex model variants, and four fixed trial slots per setting: 60 slots in
total. Up to three trials run concurrently by default and use the same artifact
contract as ``deepswe_guardian_ablation.py``.

The runner is resumable. A slot with valid ``metadata.json`` is treated as a
recorded trial, including a trial whose Pier return code is non-zero. An
interrupted slot without valid metadata is archived before it is retried.

Example:

    /home/xiangye/miniconda3/envs/codeminer/bin/python \
      scripts/guardian/deepswe_solo_matrix.py --dry-run

Remove ``--dry-run`` to launch the pending trials. Use ``--rerun-recorded`` only
when all selected slots should be replaced with fresh trials; old artifacts are
archived rather than deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.guardian import deepswe_guardian_ablation as ablation

DEFAULT_TASKS = (
    "igel-persist-feature-schema",
    "textual-kitty-key-phases",
    "ipython-session-bundle-replay",
    "fastapi-implicit-head-options",
    "sqlite-utils-safe-import-checkpoints",
)
DEFAULT_MODELS = (
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
)
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_RUNS = 4
DEFAULT_CONCURRENCY = 3
DEFAULT_CODEMINER_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = DEFAULT_CODEMINER_ROOT / "data" / "deepswe_outputs"


@dataclass(frozen=True)
class Trial:
    model: str
    task: str
    repeat_index: int

    @property
    def job_id(self) -> str:
        return f"job_{self.repeat_index}"

    @property
    def key(self) -> str:
        return f"{self.model}|{self.task}|{self.job_id}"


def _trial_dir(args: argparse.Namespace, trial: Trial) -> Path:
    setting = ablation._setting_slug(trial.model, args.reasoning_effort)
    return args.output_root / setting / trial.task / "solo" / trial.job_id


def _trial_jobs_dir(args: argparse.Namespace, trial: Trial) -> Path:
    """Return a Pier jobs directory isolated from every concurrent trial."""

    setting = ablation._setting_slug(trial.model, args.reasoning_effort)
    output_key = hashlib.sha256(
        str(args.output_root.resolve()).encode("utf-8")
    ).hexdigest()[:12]
    return (
        args.jobs_dir
        / "_codeminer_matrix"
        / output_key
        / setting
        / trial.task
        / trial.job_id
    )


def _recorded_metadata(
    args: argparse.Namespace,
    trial: Trial,
) -> dict[str, Any] | None:
    row = ablation._load_json(_trial_dir(args, trial) / "metadata.json")
    if not row:
        return None
    expected = {
        "model": trial.model,
        "task": trial.task,
        "baseline": "solo",
        "job_id": trial.job_id,
        "reasoning_effort": args.reasoning_effort,
    }
    if args.context_file is not None:
        expected["context_injection_sha256"] = args.context_injection_sha256
    if any(row.get(field) != value for field, value in expected.items()):
        return None
    return row


def _build_plan(args: argparse.Namespace) -> list[Trial]:
    plan: list[Trial] = []
    for repeat_index in range(1, args.runs + 1):
        # Stagger model/task pairs so the default three-worker wave exercises
        # different models and, when available, different tasks.
        for task_offset in range(len(args.tasks)):
            for model_offset, model in enumerate(args.models):
                task = args.tasks[(task_offset + model_offset) % len(args.tasks)]
                plan.append(
                    Trial(
                        model=model,
                        task=task,
                        repeat_index=repeat_index,
                    )
                )
    return plan


def _archive_trial_dir(
    args: argparse.Namespace,
    trial: Trial,
) -> Path | None:
    source = _trial_dir(args, trial)
    if not source.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    archive_root = args.output_root / "_matrix_runs" / "archived_trials"
    setting = ablation._setting_slug(trial.model, args.reasoning_effort)
    destination = archive_root / setting / trial.task / f"{trial.job_id}_{timestamp}"
    suffix = 1
    while destination.exists():
        suffix += 1
        destination = (
            archive_root / setting / trial.task / f"{trial.job_id}_{timestamp}_{suffix}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    ablation._repair_output_permissions(source)
    shutil.move(str(source), str(destination))

    # Aggregate scans recurse through the output root looking for this exact
    # filename. Preserve the row without counting an archived attempt twice.
    archived_metadata = destination / "metadata.json"
    if archived_metadata.exists():
        archived_metadata.rename(destination / "archived_metadata.json")
    return destination


def _write_state(
    args: argparse.Namespace,
    plan: list[Trial],
    statuses: dict[str, dict[str, Any]],
    *,
    started_at: str,
) -> None:
    counts: dict[str, int] = {}
    for row in statuses.values():
        status = str(row["status"])
        counts[status] = counts.get(status, 0) + 1
    ablation._write_json(
        args.state_path,
        {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "started_at": started_at,
            "reasoning_effort": args.reasoning_effort,
            "concurrency": args.concurrency,
            "context_injection_source": (
                str(args.context_file) if args.context_file is not None else ""
            ),
            "context_injection_sha256": args.context_injection_sha256,
            "runs_per_setting": args.runs,
            "tasks": args.tasks,
            "models": args.models,
            "slot_count": len(plan),
            "counts": counts,
            "trials": [statuses[trial.key] for trial in plan],
        },
    )


def _ablation_args(args: argparse.Namespace, trial: Trial) -> argparse.Namespace:
    trial_args = ablation.parse_args(
        [
            "--model",
            trial.model,
            "--reasoning-effort",
            args.reasoning_effort,
            "--tasks",
            trial.task,
            "--runs",
            str(args.runs),
            "--codeminer-root",
            str(args.codeminer_root),
            "--deepswe-root",
            str(args.deepswe_root),
            "--jobs-dir",
            str(_trial_jobs_dir(args, trial)),
            "--output-root",
            str(args.output_root),
        ]
    )
    trial_args.prompt_template_path = args.prompt_template_path
    trial_args.context_injection_source = args.context_file
    trial_args.context_injection_sha256 = args.context_injection_sha256
    return trial_args


def _prepare_context_template(args: argparse.Namespace) -> None:
    """Build Pier's Jinja prompt template from a task-independent context file."""

    args.prompt_template_path = None
    args.context_injection_sha256 = ""
    if args.context_file is None:
        return

    context_path = args.context_file.resolve()
    context = context_path.read_text(encoding="utf-8").strip()
    digest = hashlib.sha256(context.encode("utf-8")).hexdigest()
    template_dir = args.output_root / "_matrix_runs" / "prompt_templates"
    template_dir.mkdir(parents=True, exist_ok=True)
    template_path = template_dir / f"{context_path.stem}_{digest[:12]}.jinja2"
    rendered_source = (
        "{{ instruction }}\n\n"
        "---\n\n"
        "## Additional review context\n\n"
        f"{context}\n"
    )
    template_path.write_text(rendered_source, encoding="utf-8")
    args.context_file = context_path
    args.context_injection_sha256 = digest
    args.prompt_template_path = template_path.resolve()


def _initial_status(
    args: argparse.Namespace,
    trial: Trial,
) -> dict[str, Any]:
    metadata = _recorded_metadata(args, trial)
    status = "recorded" if metadata is not None else "pending"
    return {
        **asdict(trial),
        "job_id": trial.job_id,
        "key": trial.key,
        "status": status,
        "output_dir": str(_trial_dir(args, trial)),
        "returncode": (metadata or {}).get("returncode"),
        "reward": (metadata or {}).get("reward"),
        "f2p": (metadata or {}).get("f2p"),
        "p2p": (metadata or {}).get("p2p"),
        "partial": (metadata or {}).get("partial"),
    }


def _validate(args: argparse.Namespace) -> None:
    if args.runs < 1 or args.runs > 4:
        raise ValueError("--runs must be between 1 and 4")
    if args.concurrency < 1:
        raise ValueError("--concurrency must be at least 1")
    if len(set(args.models)) != len(args.models):
        raise ValueError("--models contains duplicates")
    if len(set(args.tasks)) != len(args.tasks):
        raise ValueError("--tasks contains duplicates")
    missing = [
        args.deepswe_root / "tasks" / task
        for task in args.tasks
        if not (args.deepswe_root / "tasks" / task).is_dir()
    ]
    if missing:
        paths = ", ".join(str(path) for path in missing)
        raise FileNotFoundError(f"DeepSWE task directories do not exist: {paths}")
    if args.context_file is not None and not args.context_file.is_file():
        raise FileNotFoundError(
            f"context injection file does not exist: {args.context_file}"
        )
    profile = args.codeminer_root / "deepsweguardian" / "task_venv_profile.sh"
    if not profile.is_file():
        raise FileNotFoundError(f"task virtualenv profile does not exist: {profile}")
    if not args.dry_run and shutil.which("pier") is None:
        raise FileNotFoundError("pier is not on PATH")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--tasks", nargs="+", default=list(DEFAULT_TASKS))
    parser.add_argument(
        "--reasoning-effort",
        default=DEFAULT_REASONING_EFFORT,
    )
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="Maximum number of Pier trials to run concurrently (default: 3).",
    )
    parser.add_argument(
        "--rerun-recorded",
        action="store_true",
        help="Archive and rerun slots that already have valid metadata.",
    )
    parser.add_argument(
        "--stop-on-launcher-error",
        action="store_true",
        help="Stop after a Python/Pier launcher exception instead of continuing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the matrix without launching Pier.",
    )
    parser.add_argument(
        "--show-commands",
        action="store_true",
        help="With --dry-run, print every pending Pier command.",
    )
    parser.add_argument(
        "--codeminer-root",
        type=Path,
        default=DEFAULT_CODEMINER_ROOT,
    )
    parser.add_argument(
        "--deepswe-root",
        type=Path,
        default=ablation.DEFAULT_DEEPSWE_ROOT,
    )
    parser.add_argument(
        "--jobs-dir",
        type=Path,
        default=ablation.DEFAULT_JOBS_DIR,
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
    )
    parser.add_argument(
        "--context-file",
        type=Path,
        default=None,
        help=(
            "Append this Markdown file to the original task instruction using "
            "Pier's Codex prompt template support."
        ),
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=None,
        help="Progress JSON path (default: <output-root>/_matrix_runs/...).",
    )
    args = parser.parse_args(argv)
    if args.state_path is None:
        args.state_path = (
            args.output_root / "_matrix_runs" / "solo_python_5x3x4_medium.json"
        )
    return args


def _run_plan(
    args: argparse.Namespace,
    plan: list[Trial],
    statuses: dict[str, dict[str, Any]],
    *,
    started_at: str,
) -> tuple[int, int]:
    """Run pending slots with bounded concurrency.

    Only the coordinator thread mutates ``statuses`` or writes the state file.
    Worker threads are restricted to one isolated Pier trial each.
    """

    scheduled: list[tuple[int, Trial]] = []
    for ordinal, trial in enumerate(plan, 1):
        status = statuses[trial.key]
        if status["status"] == "recorded" and not args.rerun_recorded:
            print(
                f"[{ordinal}/{len(plan)} skip] {trial.model} {trial.task} "
                f"{trial.job_id}"
            )
            continue
        scheduled.append((ordinal, trial))

    launcher_errors = 0
    run_count = 0
    next_trial = 0
    stop_scheduling = False
    running: dict[Future[dict[str, Any]], tuple[int, Trial]] = {}

    with ThreadPoolExecutor(
        max_workers=args.concurrency,
        thread_name_prefix="deepswe-trial",
    ) as executor:
        while next_trial < len(scheduled) or running:
            while (
                not stop_scheduling
                and next_trial < len(scheduled)
                and len(running) < args.concurrency
            ):
                ordinal, trial = scheduled[next_trial]
                next_trial += 1
                status = statuses[trial.key]
                archived = _archive_trial_dir(args, trial)
                if archived is not None:
                    status["archived_output_dir"] = str(archived)
                status["status"] = "running"
                status["started_at"] = datetime.now().isoformat(timespec="seconds")
                _write_state(args, plan, statuses, started_at=started_at)
                print(
                    f"[{ordinal}/{len(plan)} run] {trial.model} {trial.task} "
                    f"{trial.job_id}",
                    flush=True,
                )
                future = executor.submit(
                    ablation._run_trial,
                    _ablation_args(args, trial),
                    task=trial.task,
                    baseline="solo",
                    repeat_index=trial.repeat_index,
                )
                running[future] = (ordinal, trial)

            if not running:
                break

            completed, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in completed:
                _, trial = running.pop(future)
                status = statuses[trial.key]
                try:
                    row = future.result()
                except Exception as exc:
                    launcher_errors += 1
                    status["status"] = "launcher_error"
                    status["error"] = f"{type(exc).__name__}: {exc}"
                    status["finished_at"] = datetime.now().isoformat(timespec="seconds")
                    print(
                        f"[launcher-error] {trial.model} {trial.task} "
                        f"{trial.job_id}: {status['error']}",
                        file=sys.stderr,
                        flush=True,
                    )
                    if args.stop_on_launcher_error:
                        stop_scheduling = True
                else:
                    run_count += 1
                    status.update(
                        {
                            "status": (
                                "recorded"
                                if row.get("returncode") == 0
                                else "recorded_nonzero"
                            ),
                            "returncode": row.get("returncode"),
                            "reward": row.get("reward"),
                            "f2p": row.get("f2p"),
                            "p2p": row.get("p2p"),
                            "partial": row.get("partial"),
                            "finished_at": datetime.now().isoformat(timespec="seconds"),
                        }
                    )
                _write_state(args, plan, statuses, started_at=started_at)

    return run_count, launcher_errors


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    _validate(args)
    _prepare_context_template(args)
    plan = _build_plan(args)
    statuses = {trial.key: _initial_status(args, trial) for trial in plan}
    recorded = sum(row["status"] == "recorded" for row in statuses.values())
    pending = len(plan) if args.rerun_recorded else len(plan) - recorded

    print(
        f"[matrix] {len(args.models)} models x {len(args.tasks)} tasks x "
        f"{args.runs} trials = {len(plan)} slots"
    )
    print(
        f"[matrix] recorded={recorded} pending={pending} "
        f"reasoning_effort={args.reasoning_effort} concurrency={args.concurrency}"
    )

    if args.dry_run:
        for trial in plan:
            status = statuses[trial.key]["status"]
            action = "run" if args.rerun_recorded or status == "pending" else "skip"
            print(
                f"[{action}] {trial.model} {trial.task} {trial.job_id}: "
                f"{_trial_dir(args, trial)}"
            )
            if action == "run" and args.show_commands:
                trial_args = _ablation_args(args, trial)
                command = ablation._build_pier_command(
                    trial_args,
                    task=trial.task,
                    baseline="solo",
                    logs_dir=_trial_dir(args, trial) / "agent_logs",
                )
                print("  " + " ".join(command))
        return 0

    started_at = datetime.now().isoformat(timespec="seconds")
    _write_state(args, plan, statuses, started_at=started_at)
    matrix_started = time.monotonic()
    run_count, launcher_errors = _run_plan(
        args,
        plan,
        statuses,
        started_at=started_at,
    )

    elapsed = time.monotonic() - matrix_started
    final_counts: dict[str, int] = {}
    for row in statuses.values():
        status_name = str(row["status"])
        final_counts[status_name] = final_counts.get(status_name, 0) + 1
    print(
        f"[done] launched={run_count} launcher_errors={launcher_errors} "
        f"elapsed_hours={elapsed / 3600:.2f}"
    )
    print(f"[done] state: {args.state_path}")
    print(f"[done] statuses: {json.dumps(final_counts, sort_keys=True)}")
    return 1 if launcher_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
