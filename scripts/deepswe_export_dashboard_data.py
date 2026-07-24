#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Export fixed-slot DeepSWE Guardian outputs for the static dashboard."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from deepswe_guardian_table import (
    COUNTED_JOB_IDS,
    DEFAULT_OUTPUT_ROOT,
    _load_rows,
    _metric_mean,
    _metric_sum,
    _with_costs,
)


DEFAULT_DASHBOARD_DIR = Path("deepswe_dashboard")


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _latest_episode_dir(row: dict[str, Any]) -> str:
    output_dir = Path(str(row.get("output_dir") or ""))
    episodes = sorted((output_dir / "agent_logs" / "guardian_episodes").glob("*"))
    if not episodes:
        return ""
    return str(episodes[-1])


def _trial_row(row: dict[str, Any]) -> dict[str, Any]:
    output_dir = str(row.get("output_dir") or "")
    latest_episode = _latest_episode_dir(row)
    return {
        "task": row.get("task"),
        "baseline": row.get("baseline"),
        "job_id": row.get("job_id"),
        "model": row.get("model"),
        "reasoning_effort": row.get("reasoning_effort"),
        "reward": row.get("reward"),
        "f2p": row.get("f2p"),
        "f2p_passed": row.get("f2p_passed"),
        "f2p_total": row.get("f2p_total"),
        "p2p": row.get("p2p"),
        "p2p_passed": row.get("p2p_passed"),
        "p2p_total": row.get("p2p_total"),
        "partial": row.get("partial"),
        "returncode": row.get("returncode"),
        "main_input_tokens": row.get("main_input_tokens"),
        "main_cached_input_tokens": row.get("main_cached_input_tokens"),
        "main_output_tokens": row.get("main_output_tokens"),
        "main_reasoning_output_tokens": row.get("main_reasoning_output_tokens"),
        "guardian_findings": row.get("guardian_findings"),
        "guardian_llm_backend": row.get("guardian_llm_backend"),
        "guardian_prompt_tokens": row.get("guardian_prompt_tokens"),
        "guardian_completion_tokens": row.get("guardian_completion_tokens"),
        "main_cost_usd": row.get("main_cost_usd"),
        "guardian_cost_usd": row.get("guardian_cost_usd"),
        "total_cost_usd": row.get("total_cost_usd"),
        "created_at": row.get("created_at"),
        "output_dir": output_dir,
        "pier_result_json": row.get("pier_result_json") or row.get("result_json"),
        "metadata_path": row.get("metadata_path"),
        "codex_log": f"{output_dir}/agent_logs/codex.txt" if output_dir else "",
        "codex_bridge_log": (
            f"{output_dir}/agent_logs/codex_bridge.log"
            if row.get("baseline") == "guardian" and output_dir
            else ""
        ),
        "latest_guardian_episode": latest_episode,
        "latest_guardian_findings": (
            f"{latest_episode}/findings.md" if latest_episode else ""
        ),
        "latest_guardian_status": (
            f"{latest_episode}/status.json" if latest_episode else ""
        ),
        "latest_guardian_log": f"{latest_episode}/guardian.log" if latest_episode else "",
    }


def _group_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("task") or ""),
        str(row.get("baseline") or ""),
        str(row.get("model") or ""),
        str(row.get("reasoning_effort") or ""),
    )


def _summary_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("returncode") == 0:
            groups.setdefault(_group_key(row), []).append(row)

    result: list[dict[str, Any]] = []
    for (task, baseline, model, effort), group in sorted(groups.items()):
        rewards = [_num(r.get("reward")) for r in group]
        rewards = [r for r in rewards if r is not None]
        result.append(
            {
                "task": task,
                "baseline": baseline,
                "model": model,
                "reasoning_effort": effort,
                "label": f"{baseline} / {model} / {effort}",
                "n_trials": len(group),
                "pass_rate": _metric_mean(group, "reward"),
                "avg_reward": _metric_mean(group, "reward"),
                "avg_f2p": _metric_mean(group, "f2p"),
                "avg_p2p": _metric_mean(group, "p2p"),
                "avg_partial": _metric_mean(group, "partial"),
                "avg_main_input_tokens": _metric_mean(group, "main_input_tokens"),
                "avg_main_output_tokens": _metric_mean(group, "main_output_tokens"),
                "avg_guardian_prompt_tokens": _metric_mean(
                    group, "guardian_prompt_tokens"
                ),
                "avg_guardian_completion_tokens": _metric_mean(
                    group, "guardian_completion_tokens"
                ),
                "avg_total_cost_usd": _metric_mean(group, "total_cost_usd"),
                "sum_total_cost_usd": _metric_sum(group, "total_cost_usd"),
                "std_reward": (
                    statistics.pstdev(rewards) if len(rewards) > 1 else 0.0
                ),
                "latest_output_dir": max(
                    group, key=lambda r: str(r.get("created_at") or "")
                ).get("output_dir"),
            }
        )
    return result


def _task_rows(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in summaries:
        grouped.setdefault(str(row["task"]), []).append(row)
    result = []
    for task, rows in sorted(grouped.items()):
        solo = next((r for r in rows if r["baseline"] == "solo"), None)
        guardian = next((r for r in rows if r["baseline"] == "guardian"), None)
        result.append(
            {
                "task": task,
                "solo_pass_rate": (solo or {}).get("pass_rate"),
                "guardian_pass_rate": (guardian or {}).get("pass_rate"),
                "delta_pass_rate": (
                    (guardian or {}).get("pass_rate", 0)
                    - (solo or {}).get("pass_rate", 0)
                    if solo and guardian
                    else None
                ),
                "solo_trials": (solo or {}).get("n_trials", 0),
                "guardian_trials": (guardian or {}).get("n_trials", 0),
                "guardian_avg_cost_usd": (guardian or {}).get("avg_total_cost_usd"),
            }
        )
    return result


def export_dashboard_data(args: argparse.Namespace) -> dict[str, Any]:
    rows = [_with_costs(r, args) for r in _load_rows(args.output_root)]
    trials = [_trial_row(r) for r in rows]
    summaries = _summary_rows(rows)
    tasks = _task_rows(summaries)
    return {
        "schema_version": 1,
        "output_root": str(args.output_root),
        "counted_job_ids": sorted(COUNTED_JOB_IDS),
        "summary": summaries,
        "tasks": tasks,
        "trials": trials,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--dashboard-dir", type=Path, default=DEFAULT_DASHBOARD_DIR)
    parser.add_argument("--input-usd-per-mtok", type=float)
    parser.add_argument("--cached-input-usd-per-mtok", type=float)
    parser.add_argument("--output-usd-per-mtok", type=float)
    parser.add_argument("--output-excludes-reasoning", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data = export_dashboard_data(args)
    args.dashboard_dir.mkdir(parents=True, exist_ok=True)
    path = args.dashboard_dir / "data.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[done] wrote {path}")
    print(f"[done] trials={len(data['trials'])} summary_rows={len(data['summary'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
