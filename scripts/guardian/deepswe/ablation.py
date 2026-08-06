#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Run DeepSWE Codex-vs-Guardian ablations.

Example:

    python -m scripts.guardian.deepswe.ablation \\
        --model gpt-5.6-terra \\
        --reasoning-effort medium \\
        --tasks igel-persist-feature-schema

The script runs two baselines for each task:

* ``solo``: Pier's regular Codex agent, no Guardian.
* ``guardian``: GuardianCodingAgent wrapping Codex with the Guardian sidecar.

Pass ``--guardian-no-budget-limit`` to measure Guardian's natural token cost.
This disables only the cycle token ceiling; turn and wall-clock limits remain.

Each requested baseline is run ``--runs`` times.  Results are stored in fixed
slots keyed by model setting:

    deepswe_outputs/<model>_<reasoning_effort>/<task_name>/<baseline_name>/job_1/
    ...
    deepswe_outputs/<model>_<reasoning_effort>/<task_name>/<baseline_name>/job_4/

Each job directory is self-contained:

    command.json          exact ``pier run`` argv
    metadata.json         normalized metrics/tokens/provenance for table scans
    pier_result.json      copied Pier aggregate result.json
    pier_stdout.txt       raw Pier stdout/stderr
    agent_logs/           Codex transcript, token log, bridge log
    agent_logs/guardian_episodes/
                         per-cycle Guardian logs and reports

Guardian per-cycle debug logs are under:

    agent_logs/guardian_episodes/<cycle>_<commit>/guardian.log

The table generator only counts ``job_1`` through ``job_4``.  Existing solo
slots are skipped by default; Guardian slots are refreshed when run again.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_CODENIB_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DEEPSWE_ROOT = Path("/home/xiangye/deep-swe")
DEFAULT_OUTPUT_ROOT = Path("/mnt/data/xiangye/deepswe_outputs")
DEFAULT_JOBS_DIR = DEFAULT_DEEPSWE_ROOT / "jobs"
DEEPSWE_HARNESS_RELATIVE_PATH = Path("scripts/guardian/deepswe/harness")


def _harness_path(codenib_root: Path) -> Path:
    """Return the dependency-isolated Pier harness in the CodeNib checkout."""

    return codenib_root / DEEPSWE_HARNESS_RELATIVE_PATH


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")


def _setting_slug(model: str, reasoning_effort: str) -> str:
    return f"{_slug(model)}_{_slug(reasoning_effort)}"


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_json(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _extract_metrics(result: dict[str, Any] | None) -> dict[str, float | None]:
    empty = {
        "reward": None,
        "f2p": None,
        "f2p_passed": None,
        "f2p_total": None,
        "p2p": None,
        "p2p_passed": None,
        "p2p_total": None,
        "partial": None,
    }
    if not result:
        return empty
    evals = (result.get("stats") or {}).get("evals") or {}
    for eval_row in evals.values():
        metrics = eval_row.get("metrics") or []
        if metrics:
            return {k: metrics[0].get(k) for k in empty}
    return empty


def _guardian_run_status(logs_dir: Path) -> dict[str, Any] | None:
    """Combine run-wide health and usage with the final cycle's state."""

    rows = [
        (path, status)
        for path in (logs_dir / "guardian_episodes").glob("*/status.json")
        if (status := _load_json(path)) is not None
    ]
    if rows and all(
        isinstance(status.get("cycle_index"), int) for _path, status in rows
    ):
        rows.sort(key=lambda row: int(row[1]["cycle_index"]))
    else:
        rows.sort(key=lambda row: (row[0].stat().st_mtime_ns, row[0].parent.name))
    statuses = [status for _path, status in rows]
    if not statuses:
        return None

    summary = dict(statuses[-1])
    token_fields = ("prompt", "cached_input", "completion", "total")
    summary["llm_tokens"] = {
        field: sum(
            int((status.get("llm_tokens") or {}).get(field, 0) or 0)
            for status in statuses
        )
        for field in token_fields
    }
    summary["cycle_count"] = sum(
        bool(status.get("review_performed", True)) for status in statuses
    )
    summary["exit_reasons"] = [
        str(status.get("exit_reason") or "") for status in statuses
    ]
    summary["degraded"] = any(bool(status.get("degraded")) for status in statuses)

    health = {str(status.get("analysis_status") or "") for status in statuses}
    if "failed" in health:
        summary["analysis_status"] = "failed"
    elif health - {"complete", "degraded"}:
        summary["analysis_status"] = "incomplete"
    elif summary["degraded"] or "degraded" in health:
        summary["analysis_status"] = "degraded"
    else:
        summary["analysis_status"] = "complete"
    return summary


def _codex_tokens_from_log(log_path: Path) -> dict[str, int] | None:
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    seen = False
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return None

    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = event.get("usage")
        if event.get("type") != "turn.completed" or not isinstance(usage, dict):
            continue
        seen = True
        for key in totals:
            value = usage.get(key)
            if isinstance(value, int):
                totals[key] += value
    return totals if seen else None


def _find_result_path(stdout: str, jobs_dir: Path, started_at: float) -> Path | None:
    match = re.search(r"Results written to (.+/result\.json)", stdout)
    if match:
        path = Path(match.group(1).strip())
        if path.exists():
            return path
    candidates = [
        p for p in jobs_dir.glob("*/result.json") if p.stat().st_mtime >= started_at - 2
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _common_mounts(args: argparse.Namespace, logs_dir: Path) -> list[dict[str, str]]:
    return [
        {"type": "bind", "source": str(logs_dir), "target": "/logs/agent"},
        {
            "type": "bind",
            "source": str(_harness_path(args.codenib_root) / "task_venv_profile.sh"),
            "target": "/etc/profile.d/zz-deepswe-task-venv.sh",
        },
    ]


def _guardian_mounts(
    args: argparse.Namespace,
    logs_dir: Path,
) -> list[dict[str, object]]:
    mounts: list[dict[str, object]] = list(_common_mounts(args, logs_dir))
    for name in ("responses", "latest"):
        mounts.append(
            {
                "type": "bind",
                "source": str(logs_dir / "guardian_exchange" / name),
                "target": f"/logs/agent/guardian_exchange/{name}",
                "read_only": True,
            }
        )
    mounts.append(
        {
            "type": "bind",
            "source": str(logs_dir / "guardian_episodes"),
            "target": "/logs/agent/guardian_episodes",
            "read_only": True,
        }
    )
    return mounts


def _prepare_host_output_dirs(
    *,
    base_dir: Path,
    logs_dir: Path,
) -> None:
    """Create mount targets on the host so container-created files are removable.

    Pier/Docker can write mounted files as a container UID that maps to host
    ``nobody``.  Deletion is controlled by write permission on the parent
    directory, so keep the parent directories host-owned and writable.
    """
    dirs = [
        base_dir,
        logs_dir,
        logs_dir / "guardian_episodes",
        logs_dir / "guardian_exchange",
        logs_dir / "guardian_exchange" / "responses",
        logs_dir / "guardian_exchange" / "latest",
    ]
    for path in dirs:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o777)


def _repair_output_permissions(path: Path) -> None:
    """Make an output tree manageable by the current host user.

    Container-created files may be owned by a mapped UID such as ``nobody``.
    We avoid requiring sudo here; broad user/group/other write+execute on the
    output tree is enough for the owner of ancestor dirs to clean it up.
    """
    if not path.exists():
        return
    for child in path.rglob("*"):
        try:
            if child.is_dir():
                child.chmod(0o777)
            else:
                child.chmod(0o666)
        except OSError:
            pass
    try:
        path.chmod(0o777)
    except OSError:
        pass


def _build_pier_command(
    args: argparse.Namespace,
    *,
    task: str,
    baseline: str,
    logs_dir: Path,
) -> list[str]:
    task_path = args.deepswe_root / "tasks" / task
    cmd = [
        "pier",
        "run",
        "-p",
        str(task_path),
        "--model",
        args.model,
        "--ae",
        "CODEX_FORCE_AUTH_JSON=1",
        "--ak",
        f"reasoning_effort={args.reasoning_effort}",
        "--jobs-dir",
        str(args.jobs_dir),
        "-y",
    ]
    env_pythonpath = os.environ.get("PYTHONPATH", "")
    if baseline == "guardian":
        cmd.extend(
            [
                "--agent-import-path",
                "scripts.guardian.deepswe.harness.agent:GuardianCodingAgent",
                "--ak",
                "solver=codex",
                "--ak",
                f"guardian_model={args.guardian_model}",
                "--ak",
                f"guardian_explorer_count={args.guardian_explorer_count}",
                "--ak",
                f"guardian_max_findings={args.guardian_max_findings}",
                "--ak",
                f"guardian_max_cycles={args.guardian_max_cycles}",
                "--ak",
                f"guardian_rollout_timeout={args.guardian_rollout_timeout}",
                "--ak",
                "guardian_host_exchange_dir=" f"{logs_dir / 'guardian_exchange'}",
                "--mounts-json",
                json.dumps(_guardian_mounts(args, logs_dir)),
            ]
        )
        if str(args.codenib_root) not in env_pythonpath.split(os.pathsep):
            os.environ["PYTHONPATH"] = (
                str(args.codenib_root)
                if not env_pythonpath
                else f"{args.codenib_root}{os.pathsep}{env_pythonpath}"
            )
    else:
        cmd.extend(
            [
                "--agent",
                "codex",
                "--mounts-json",
                json.dumps(_common_mounts(args, logs_dir)),
            ]
        )
        prompt_template_path = getattr(args, "prompt_template_path", None)
        if prompt_template_path is not None:
            cmd.extend(
                [
                    "--ak",
                    f"prompt_template_path={prompt_template_path}",
                ]
            )
    return cmd


def _run_trial(
    args: argparse.Namespace,
    *,
    task: str,
    baseline: str,
    repeat_index: int,
) -> dict[str, Any]:
    job_id = f"job_{repeat_index}"
    setting = _setting_slug(args.model, args.reasoning_effort)
    base_dir = args.output_root / setting / _slug(task) / baseline / job_id
    if baseline == "guardian" and base_dir.exists():
        _repair_output_permissions(base_dir)
        shutil.rmtree(base_dir)
    logs_dir = base_dir / "agent_logs"
    _prepare_host_output_dirs(
        base_dir=base_dir,
        logs_dir=logs_dir,
    )

    cmd = _build_pier_command(
        args,
        task=task,
        baseline=baseline,
        logs_dir=logs_dir,
    )
    _write_json(
        base_dir / "command.json",
        {
            "cwd": str(args.codenib_root),
            "argv": cmd,
        },
    )
    stdout_path = base_dir / "pier_stdout.txt"
    started = time.time()
    print(f"[run] {task} {baseline} #{repeat_index}: {base_dir}", flush=True)
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=args.codenib_root,
        check=False,
    )
    _repair_output_permissions(base_dir)
    stdout_path.write_text(proc.stdout, encoding="utf-8")
    result_path = _find_result_path(proc.stdout, args.jobs_dir, started)
    local_result_path = base_dir / "pier_result.json"
    if result_path:
        shutil.copy2(result_path, local_result_path)
    result = _load_json(result_path) if result_path else None
    metrics = _extract_metrics(result)

    codex_tokens = _load_json(logs_dir / "codex_tokens.json") or _codex_tokens_from_log(
        logs_dir / "codex.txt"
    )
    guardian_status = _guardian_run_status(logs_dir) if baseline == "guardian" else None

    row: dict[str, Any] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "task": task,
        "baseline": baseline,
        "job_id": job_id,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "context_injection_source": str(
            getattr(args, "context_injection_source", "") or ""
        ),
        "context_injection_sha256": str(
            getattr(args, "context_injection_sha256", "") or ""
        ),
        "guardian_model": args.guardian_model if baseline == "guardian" else "",
        "guardian_explorer_count": (
            args.guardian_explorer_count if baseline == "guardian" else None
        ),
        "guardian_max_findings": (
            args.guardian_max_findings if baseline == "guardian" else None
        ),
        "guardian_max_cycles": (
            args.guardian_max_cycles if baseline == "guardian" else None
        ),
        "guardian_rollout_timeout": (
            args.guardian_rollout_timeout if baseline == "guardian" else None
        ),
        "repeat_index": repeat_index,
        "returncode": proc.returncode,
        "output_dir": str(base_dir),
        "pier_result_json": str(local_result_path) if result_path else "",
        "source_result_json": str(result_path) if result_path else "",
        "result_json": str(local_result_path) if result_path else "",
        "main_input_tokens": (codex_tokens or {}).get("input_tokens"),
        "main_cached_input_tokens": (codex_tokens or {}).get("cached_input_tokens"),
        "main_output_tokens": (codex_tokens or {}).get("output_tokens"),
        "main_reasoning_output_tokens": (codex_tokens or {}).get(
            "reasoning_output_tokens"
        ),
        "guardian_llm_backend": (guardian_status or {}).get("llm_backend"),
        "guardian_findings": (guardian_status or {}).get("findings"),
        "guardian_backlog": (guardian_status or {}).get("backlog"),
        "guardian_high_confidence_backlog": (guardian_status or {}).get(
            "high_confidence_backlog"
        ),
        "guardian_degraded": (guardian_status or {}).get("degraded"),
        "guardian_analysis_status": (guardian_status or {}).get("analysis_status"),
        "guardian_cycle_count": (guardian_status or {}).get("cycle_count"),
        "guardian_exit_reasons": (guardian_status or {}).get("exit_reasons"),
        "guardian_terminal": (guardian_status or {}).get("terminal"),
        "guardian_termination_reason": (guardian_status or {}).get(
            "termination_reason"
        ),
        "guardian_prompt_tokens": ((guardian_status or {}).get("llm_tokens") or {}).get(
            "prompt"
        ),
        "guardian_cached_input_tokens": (
            (guardian_status or {}).get("llm_tokens") or {}
        ).get("cached_input"),
        "guardian_completion_tokens": (
            (guardian_status or {}).get("llm_tokens") or {}
        ).get("completion"),
        "guardian_total_tokens": ((guardian_status or {}).get("llm_tokens") or {}).get(
            "total"
        ),
    }
    row.update(metrics)
    _write_json(base_dir / "metadata.json", row)
    return row


def _existing_jobs(
    output_root: Path,
    *,
    task: str,
    baseline: str,
    model: str,
    reasoning_effort: str,
) -> list[dict[str, Any]]:
    base = output_root / _setting_slug(model, reasoning_effort) / _slug(task) / baseline
    rows: list[dict[str, Any]] = []
    for path in sorted(base.glob("job_*/metadata.json")):
        row = _load_json(path)
        if not row:
            continue
        if (
            row.get("model") == model
            and row.get("reasoning_effort") == reasoning_effort
            and str(row.get("job_id") or "").removeprefix("job_").isdigit()
        ):
            rows.append(row)
    return rows


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning-effort", required=True)
    parser.add_argument("--tasks", nargs="+", required=True, help="DeepSWE task names")
    parser.add_argument("--runs", type=int, default=4)
    parser.add_argument(
        "--baselines",
        nargs="+",
        choices=("solo", "guardian"),
        default=("solo", "guardian"),
        help="Select which experiment arms to run",
    )
    parser.add_argument("--guardian-model", default=None)
    parser.add_argument(
        "--guardian-explorer-count",
        type=int,
        default=2,
        help="Number of independent local-specification explorers",
    )
    parser.add_argument(
        "--guardian-max-findings",
        type=int,
        default=5,
        help="Maximum evidence-admitted findings delivered per review",
    )
    parser.add_argument(
        "--guardian-max-cycles",
        type=int,
        default=3,
        help="Maximum completed Guardian reviews before terminal handoff",
    )
    parser.add_argument(
        "--guardian-rollout-timeout",
        type=float,
        default=600.0,
        help="Timeout in seconds for each explorer or aggregator rollout",
    )
    parser.add_argument("--codenib-root", type=Path, default=DEFAULT_CODENIB_ROOT)
    parser.add_argument("--deepswe-root", type=Path, default=DEFAULT_DEEPSWE_ROOT)
    parser.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS_DIR)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--force-solo",
        action="store_true",
        help="Run solo even when this table already has solo data for the setting.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned Pier commands without running them.",
    )
    args = parser.parse_args(argv)
    if args.runs < 1 or args.runs > 4:
        raise ValueError(
            "--runs must be between 1 and 4; analysis only counts job_1..job_4"
        )
    if args.guardian_max_cycles < 1:
        raise ValueError("--guardian-max-cycles must be positive")
    if args.guardian_model is None:
        args.guardian_model = f"codex:{args.model}"
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    for task in args.tasks:
        for task_path in [args.deepswe_root / "tasks" / task]:
            if not task_path.exists():
                raise FileNotFoundError(f"task does not exist: {task_path}")

        plan: list[tuple[str, int]] = []
        if "solo" in args.baselines:
            solo_existing = {
                int(str(row.get("job_id", "")).removeprefix("job_"))
                for row in _existing_jobs(
                    args.output_root,
                    task=task,
                    baseline="solo",
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                )
                if str(row.get("job_id", "")).removeprefix("job_").isdigit()
            }
            for idx in range(1, args.runs + 1):
                if idx in solo_existing and not args.force_solo:
                    existing_dir = (
                        args.output_root
                        / _setting_slug(args.model, args.reasoning_effort)
                        / _slug(task)
                        / "solo"
                        / f"job_{idx}"
                    )
                    print(
                        f"[skip] {task} solo job_{idx}: existing trial in "
                        f"{existing_dir}"
                    )
                else:
                    plan.append(("solo", idx))
        if "guardian" in args.baselines:
            plan.extend(("guardian", i + 1) for i in range(args.runs))

        if args.dry_run:
            for baseline, idx in plan:
                job_id = f"job_{idx}"
                base_dir = (
                    args.output_root
                    / _setting_slug(args.model, args.reasoning_effort)
                    / _slug(task)
                    / baseline
                    / job_id
                )
                logs_dir = base_dir / "agent_logs"
                cmd = _build_pier_command(
                    args,
                    task=task,
                    baseline=baseline,
                    logs_dir=logs_dir,
                )
                print(" ".join(subprocess.list2cmdline([part]) for part in cmd))
            continue

        for baseline, idx in plan:
            _run_trial(
                args,
                task=task,
                baseline=baseline,
                repeat_index=idx,
            )

    if not args.dry_run:
        print(f"[done] output root: {args.output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
