# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CSV and dashboard reports for externally produced DeepSWE trials."""

from __future__ import annotations

import csv
import math
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

from .metrics import add_costs, metric_mean, metric_sum
from .results import COUNTED_JOB_IDS, load_rows


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        return f"{value:.6g}"
    return str(value)


def write_summary(rows: Sequence[Mapping[str, Any]], summary_csv: Path) -> None:
    """Write aggregate trial metrics grouped by task, arm, model, and effort."""

    groups: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        key = (
            str(row.get("task") or ""),
            str(row.get("baseline") or ""),
            str(row.get("model") or ""),
            str(row.get("reasoning_effort") or ""),
        )
        groups.setdefault(key, []).append(row)

    fields = [
        "task",
        "baseline",
        "model",
        "reasoning_effort",
        "n_recorded_trials",
        "n_trials",
        "n_execution_failures",
        "avg_reward",
        "avg_f2p",
        "avg_p2p",
        "avg_partial",
        "avg_f2p_passed",
        "avg_f2p_total",
        "avg_p2p_passed",
        "avg_p2p_total",
        "pass_rate",
        "avg_main_input_tokens",
        "avg_main_cached_input_tokens",
        "avg_main_output_tokens",
        "avg_main_reasoning_output_tokens",
        "avg_guardian_prompt_tokens",
        "avg_guardian_cached_input_tokens",
        "avg_guardian_completion_tokens",
        "avg_main_cost_usd",
        "avg_guardian_cost_usd",
        "avg_total_cost_usd",
        "sum_total_cost_usd",
        "latest_output_dir",
        "latest_result_json",
    ]
    summary_csv.parent.mkdir(parents=True, exist_ok=True)
    with summary_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for (task, baseline, model, effort), group in sorted(groups.items()):
            valid = [row for row in group if row.get("returncode") == 0]
            latest = max(group, key=lambda row: str(row.get("created_at") or ""))
            output = {
                "task": task,
                "baseline": baseline,
                "model": model,
                "reasoning_effort": effort,
                "n_recorded_trials": len(group),
                "n_trials": len(valid),
                "n_execution_failures": len(group) - len(valid),
                "avg_reward": metric_mean(valid, "reward"),
                "avg_f2p": metric_mean(valid, "f2p"),
                "avg_p2p": metric_mean(valid, "p2p"),
                "avg_partial": metric_mean(valid, "partial"),
                "avg_f2p_passed": metric_mean(valid, "f2p_passed"),
                "avg_f2p_total": metric_mean(valid, "f2p_total"),
                "avg_p2p_passed": metric_mean(valid, "p2p_passed"),
                "avg_p2p_total": metric_mean(valid, "p2p_total"),
                "pass_rate": metric_mean(valid, "reward"),
                "avg_main_input_tokens": metric_mean(valid, "main_input_tokens"),
                "avg_main_cached_input_tokens": metric_mean(
                    valid, "main_cached_input_tokens"
                ),
                "avg_main_output_tokens": metric_mean(valid, "main_output_tokens"),
                "avg_main_reasoning_output_tokens": metric_mean(
                    valid, "main_reasoning_output_tokens"
                ),
                "avg_guardian_prompt_tokens": metric_mean(
                    valid, "guardian_prompt_tokens"
                ),
                "avg_guardian_cached_input_tokens": metric_mean(
                    valid, "guardian_cached_input_tokens"
                ),
                "avg_guardian_completion_tokens": metric_mean(
                    valid, "guardian_completion_tokens"
                ),
                "avg_main_cost_usd": metric_mean(valid, "main_cost_usd"),
                "avg_guardian_cost_usd": metric_mean(valid, "guardian_cost_usd"),
                "avg_total_cost_usd": metric_mean(valid, "total_cost_usd"),
                "sum_total_cost_usd": metric_sum(valid, "total_cost_usd"),
                "latest_output_dir": latest.get("output_dir") or "",
                "latest_result_json": latest.get("result_json") or "",
            }
            writer.writerow({key: _format_cell(output.get(key)) for key in fields})


def _latest_episode_dir(row: Mapping[str, Any]) -> str:
    output_dir = Path(str(row.get("output_dir") or ""))
    episodes = sorted((output_dir / "agent_logs" / "guardian_episodes").glob("*"))
    return str(episodes[-1]) if episodes else ""


def trial_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one metadata row for the static dashboard."""

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
        "guardian_uncertain_specifications": row.get(
            "guardian_uncertain_specifications", row.get("guardian_backlog")
        ),
        "guardian_degraded": row.get("guardian_degraded"),
        "guardian_analysis_status": row.get("guardian_analysis_status"),
        "guardian_cycle_count": row.get("guardian_cycle_count"),
        "guardian_exit_reasons": row.get("guardian_exit_reasons"),
        "guardian_llm_backend": row.get("guardian_llm_backend"),
        "guardian_prompt_tokens": row.get("guardian_prompt_tokens"),
        "guardian_cached_input_tokens": row.get("guardian_cached_input_tokens"),
        "guardian_completion_tokens": row.get("guardian_completion_tokens"),
        "guardian_total_tokens": row.get("guardian_total_tokens"),
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
        "latest_guardian_log": (
            f"{latest_episode}/guardian.log" if latest_episode else ""
        ),
    }


def _group_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("task") or ""),
        str(row.get("baseline") or ""),
        str(row.get("model") or ""),
        str(row.get("reasoning_effort") or ""),
    )


def summary_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate valid trials while retaining every recorded slot in counts."""

    groups: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(_group_key(row), []).append(row)

    result: list[dict[str, Any]] = []
    for (task, baseline, model, effort), group in sorted(groups.items()):
        valid = [row for row in group if row.get("returncode") == 0]
        rewards = [
            float(value)
            for row in valid
            if isinstance((value := row.get("reward")), (int, float))
        ]
        result.append(
            {
                "task": task,
                "baseline": baseline,
                "model": model,
                "reasoning_effort": effort,
                "label": f"{baseline} / {model} / {effort}",
                "n_recorded_trials": len(group),
                "n_trials": len(valid),
                "n_execution_failures": len(group) - len(valid),
                "pass_rate": metric_mean(valid, "reward"),
                "avg_reward": metric_mean(valid, "reward"),
                "avg_f2p": metric_mean(valid, "f2p"),
                "avg_p2p": metric_mean(valid, "p2p"),
                "avg_partial": metric_mean(valid, "partial"),
                "avg_main_input_tokens": metric_mean(valid, "main_input_tokens"),
                "avg_main_output_tokens": metric_mean(valid, "main_output_tokens"),
                "avg_guardian_prompt_tokens": metric_mean(
                    valid, "guardian_prompt_tokens"
                ),
                "avg_guardian_cached_input_tokens": metric_mean(
                    valid, "guardian_cached_input_tokens"
                ),
                "avg_guardian_completion_tokens": metric_mean(
                    valid, "guardian_completion_tokens"
                ),
                "avg_total_cost_usd": metric_mean(valid, "total_cost_usd"),
                "sum_total_cost_usd": metric_sum(valid, "total_cost_usd"),
                "std_reward": (
                    statistics.pstdev(rewards)
                    if len(rewards) > 1
                    else 0.0 if rewards else None
                ),
                "latest_output_dir": max(
                    group, key=lambda row: str(row.get("created_at") or "")
                ).get("output_dir"),
            }
        )
    return result


def task_rows(summaries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pair solo and Guardian summaries for each task/model setting."""

    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for row in summaries:
        key = (
            str(row["task"]),
            str(row["model"]),
            str(row["reasoning_effort"]),
        )
        grouped.setdefault(key, []).append(row)
    result = []
    for (task, model, effort), rows in sorted(grouped.items()):
        solo = next((row for row in rows if row["baseline"] == "solo"), None)
        guardian = next((row for row in rows if row["baseline"] == "guardian"), None)
        solo_pass_rate = (solo or {}).get("pass_rate")
        guardian_pass_rate = (guardian or {}).get("pass_rate")
        result.append(
            {
                "task": task,
                "model": model,
                "reasoning_effort": effort,
                "solo_pass_rate": solo_pass_rate,
                "guardian_pass_rate": guardian_pass_rate,
                "solo_avg_f2p": (solo or {}).get("avg_f2p"),
                "guardian_avg_f2p": (guardian or {}).get("avg_f2p"),
                "solo_avg_p2p": (solo or {}).get("avg_p2p"),
                "guardian_avg_p2p": (guardian or {}).get("avg_p2p"),
                "solo_avg_partial": (solo or {}).get("avg_partial"),
                "guardian_avg_partial": (guardian or {}).get("avg_partial"),
                "delta_pass_rate": (
                    guardian_pass_rate - solo_pass_rate
                    if isinstance(solo_pass_rate, (int, float))
                    and isinstance(guardian_pass_rate, (int, float))
                    else None
                ),
                "solo_trials": (solo or {}).get("n_trials", 0),
                "guardian_trials": (guardian or {}).get("n_trials", 0),
                "solo_recorded_trials": (solo or {}).get("n_recorded_trials", 0),
                "guardian_recorded_trials": (guardian or {}).get(
                    "n_recorded_trials", 0
                ),
                "guardian_avg_cost_usd": (guardian or {}).get("avg_total_cost_usd"),
            }
        )
    return result


def export_dashboard_data(
    output_root: Path,
    *,
    input_usd_per_mtok: float | None = None,
    cached_input_usd_per_mtok: float | None = None,
    output_usd_per_mtok: float | None = None,
    output_includes_reasoning: bool = True,
) -> dict[str, Any]:
    """Build the complete serializable payload consumed by the dashboard."""

    rows = [
        add_costs(
            row,
            input_usd_per_mtok=input_usd_per_mtok,
            cached_input_usd_per_mtok=cached_input_usd_per_mtok,
            output_usd_per_mtok=output_usd_per_mtok,
            output_includes_reasoning=output_includes_reasoning,
        )
        for row in load_rows(output_root)
    ]
    summaries = summary_rows(rows)
    return {
        "schema_version": 1,
        "output_root": str(output_root),
        "counted_job_ids": sorted(COUNTED_JOB_IDS),
        "summary": summaries,
        "tasks": task_rows(summaries),
        "trials": [trial_row(row) for row in rows],
    }
