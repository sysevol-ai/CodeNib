# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Analysis policy for the CodeMiner Base LSP agent ablation."""

from __future__ import annotations

import random
import statistics
from typing import Any, Iterable, Mapping, Optional, Sequence

from .lsp_agent_study import (
    CODEMINER_LSP_ARM,
    DEFAULT_ARMS,
    LIVE_LSP_ARM,
    NATIVE_LSP_SKILLS,
)


def summarize_lsp_agent_study(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize adoption and cost without treating model wall time as LSP latency."""

    by_arm: dict[str, Any] = {}
    for arm in DEFAULT_ARMS:
        rows = [row for row in cells if row.get("arm") == arm]
        if not rows:
            continue
        by_arm[arm] = {
            "cell_count": len(rows),
            "lsp_adoption_count": sum(
                int(row.get("lsp_call_count") or 0) > 0 for row in rows
            ),
            "lsp_call_count": sum(int(row.get("lsp_call_count") or 0) for row in rows),
            "answer_block_recall_at_5_mean": _mean(
                _metric(row, "answer_blocks", 5, "recall") for row in rows
            ),
            "file_recall_at_5_mean": _mean(
                _metric(row, "files", 5, "recall") for row in rows
            ),
            "turns_p50": _median(row.get("total_turns") for row in rows),
            "tokens_p50": _median(
                (row.get("usage") or {}).get("total_tokens") for row in rows
            ),
            "agent_wall_ms_p50": _median(row.get("total_duration_ms") for row in rows),
            "lsp_tool_ms_p50": _median(
                call.get("duration_ms")
                for row in rows
                for call in row.get("tool_calls") or []
                if call.get("skill_id") in NATIVE_LSP_SKILLS
            ),
        }

    pairs: dict[tuple[str, int], dict[str, Mapping[str, Any]]] = {}
    for row in cells:
        key = (str(row.get("instance_id")), int(row.get("rep") or 0))
        pairs.setdefault(key, {})[str(row.get("arm"))] = row
    primary_pairs = [
        pair
        for pair in pairs.values()
        if CODEMINER_LSP_ARM in pair and LIVE_LSP_ARM in pair
    ]
    return {
        "cell_count": len(cells),
        "primary_pair_count": len(primary_pairs),
        "primary_pair_schema_match_count": sum(
            _pair_config_matches(pair[CODEMINER_LSP_ARM], pair[LIVE_LSP_ARM])
            for pair in primary_pairs
        ),
        "frozen_live_trace_request_count": sum(
            len(pair[LIVE_LSP_ARM].get("lsp_requests") or []) for pair in primary_pairs
        ),
        "by_arm": by_arm,
    }


def analyze_lsp_agent_noninferiority(
    cells: Sequence[Mapping[str, Any]],
    *,
    role: str = "confirmatory",
    metric_scope: str = "answer_blocks",
    metric_k: int = 5,
    metric_field: str = "recall",
    margin: float = 0.05,
    expected_pair_count: Optional[int] = None,
    bootstrap_samples: int = 5000,
    seed: int = 0,
) -> dict[str, Any]:
    """Run a repository/instance/repetition hierarchical paired bootstrap."""

    if not 0 <= margin < 1:
        raise ValueError("margin must be in [0, 1)")
    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be positive")

    selected = [row for row in cells if str(row.get("role")) == role]
    grouped: dict[tuple[str, int], dict[str, Mapping[str, Any]]] = {}
    for row in selected:
        arm = str(row.get("arm") or "")
        if arm not in {CODEMINER_LSP_ARM, LIVE_LSP_ARM}:
            continue
        key = (str(row.get("instance_id") or ""), int(row.get("rep") or 0))
        if arm in grouped.setdefault(key, {}):
            raise ValueError(f"duplicate cell for pair {key!r}, arm {arm!r}")
        grouped[key][arm] = row

    complete = [
        pair
        for pair in grouped.values()
        if CODEMINER_LSP_ARM in pair and LIVE_LSP_ARM in pair
    ]
    paired_rows = []
    for pair in complete:
        static_row = pair[CODEMINER_LSP_ARM]
        live_row = pair[LIVE_LSP_ARM]
        static_value = _metric(static_row, metric_scope, metric_k, metric_field)
        live_value = _metric(live_row, metric_scope, metric_k, metric_field)
        if not isinstance(static_value, (int, float)) or not isinstance(
            live_value, (int, float)
        ):
            continue
        paired_rows.append(
            {
                "repo": str(static_row.get("repo") or ""),
                "instance_id": str(static_row.get("instance_id") or ""),
                "rep": int(static_row.get("rep") or 0),
                "delta": float(static_value) - float(live_value),
                "schema_match": _pair_config_matches(static_row, live_row),
            }
        )

    planned = expected_pair_count if expected_pair_count is not None else len(grouped)
    schema_match_count = sum(row["schema_match"] for row in paired_rows)
    analysis_ready = bool(paired_rows) and len(paired_rows) == planned
    analysis_ready = analysis_ready and schema_match_count == len(paired_rows)
    deltas = [row["delta"] for row in paired_rows]
    point = statistics.fmean(deltas) if deltas else None
    lower = upper = None
    if paired_rows:
        samples = _hierarchical_delta_bootstrap(
            paired_rows,
            samples=bootstrap_samples,
            seed=seed,
        )
        lower = _percentile(samples, 0.025)
        upper = _percentile(samples, 0.975)

    return {
        "role": role,
        "estimand": f"{CODEMINER_LSP_ARM} - {LIVE_LSP_ARM}",
        "metric": f"{metric_scope}.{metric_field}@{metric_k}",
        "noninferiority_margin": margin,
        "planned_pair_count": planned,
        "observed_pair_count": len(grouped),
        "complete_metric_pair_count": len(paired_rows),
        "schema_match_pair_count": schema_match_count,
        "repository_count": len({row["repo"] for row in paired_rows}),
        "point_delta": point,
        "ci95": {"lower": lower, "upper": upper},
        "analysis_ready": analysis_ready,
        "noninferior": bool(analysis_ready and lower is not None and lower > -margin),
        "bootstrap": {
            "samples": bootstrap_samples,
            "seed": seed,
            "levels": ["repository", "instance", "repetition"],
        },
    }


def _pair_config_matches(
    static_row: Mapping[str, Any], live_row: Mapping[str, Any]
) -> bool:
    return bool(
        static_row.get("tool_schema_sha256") == live_row.get("tool_schema_sha256")
        and static_row.get("system_prompt_sha256")
        == live_row.get("system_prompt_sha256")
        and static_row.get("model_config_sha256") == live_row.get("model_config_sha256")
        and static_row.get("prompt_cache_enabled")
        == live_row.get("prompt_cache_enabled")
    )


def _hierarchical_delta_bootstrap(
    rows: Sequence[Mapping[str, Any]],
    *,
    samples: int,
    seed: int,
) -> list[float]:
    by_repo: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        repo = str(row["repo"])
        instance_id = str(row["instance_id"])
        by_repo.setdefault(repo, {}).setdefault(instance_id, []).append(
            float(row["delta"])
        )
    repos = sorted(by_repo)
    if not repos:
        return []

    rng = random.Random(seed)
    results = []
    for _ in range(samples):
        sampled_deltas = []
        for repo in rng.choices(repos, k=len(repos)):
            instances = sorted(by_repo[repo])
            for instance_id in rng.choices(instances, k=len(instances)):
                reps = by_repo[repo][instance_id]
                sampled_deltas.extend(rng.choices(reps, k=len(reps)))
        results.append(statistics.fmean(sampled_deltas))
    return results


def _percentile(values: Sequence[float], quantile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _metric(row: Mapping[str, Any], scope: str, k: int, field: str) -> Any:
    metrics = row.get("metrics") or {}
    per_scope = metrics.get(scope) or {}
    return (per_scope.get(k) or per_scope.get(str(k)) or {}).get(field)


def _mean(values: Iterable[Any]) -> Optional[float]:
    numeric = [float(value) for value in values if isinstance(value, (int, float))]
    return statistics.fmean(numeric) if numeric else None


def _median(values: Iterable[Any]) -> Optional[float]:
    numeric = [float(value) for value in values if isinstance(value, (int, float))]
    return statistics.median(numeric) if numeric else None


__all__ = [
    "analyze_lsp_agent_noninferiority",
    "summarize_lsp_agent_study",
]
