#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Generate an aggregate table from DeepSWE Guardian ablation outputs.

Only fixed slots ``job_1`` through ``job_4`` are counted.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT_ROOT = Path("/mnt/data/xiangye/deepswe_outputs")
DEFAULT_SUMMARY_CSV = DEFAULT_OUTPUT_ROOT / "guardian_ablation_summary.csv"
COUNTED_JOB_IDS = {f"job_{idx}" for idx in range(1, 5)}

DEFAULT_PRICES_USD_PER_MTOK = {
    "gpt-5.6-terra": {
        "input": 2.50,
        "cached_input": 0.25,
        "output": 15.00,
    },
}


@dataclass(frozen=True)
class Pricing:
    input_usd_per_mtok: float | None
    cached_input_usd_per_mtok: float | None
    output_usd_per_mtok: float | None
    output_includes_reasoning: bool = True


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _load_rows(output_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(output_root.glob("*/*/job_*/metadata.json")):
        job_id = path.parent.name
        if job_id not in COUNTED_JOB_IDS:
            continue
        row = _load_json(path)
        if not row:
            print(f"[warn] ignoring unreadable metadata: {path}", file=sys.stderr)
            continue
        if str(row.get("job_id") or job_id) not in COUNTED_JOB_IDS:
            continue
        row["job_id"] = job_id
        row.setdefault("metadata_path", str(path))
        rows.append(row)
    return rows


def _pricing_for_model(model: str, args: argparse.Namespace) -> Pricing:
    defaults = DEFAULT_PRICES_USD_PER_MTOK.get(model, {})
    return Pricing(
        input_usd_per_mtok=args.input_usd_per_mtok
        if args.input_usd_per_mtok is not None
        else defaults.get("input"),
        cached_input_usd_per_mtok=args.cached_input_usd_per_mtok
        if args.cached_input_usd_per_mtok is not None
        else defaults.get("cached_input"),
        output_usd_per_mtok=args.output_usd_per_mtok
        if args.output_usd_per_mtok is not None
        else defaults.get("output"),
        output_includes_reasoning=not args.output_excludes_reasoning,
    )


def _cost_from_main_tokens(row: dict[str, Any], pricing: Pricing) -> float | None:
    if (
        pricing.input_usd_per_mtok is None
        or pricing.cached_input_usd_per_mtok is None
        or pricing.output_usd_per_mtok is None
    ):
        return None
    if row.get("main_input_tokens") is None:
        return None
    input_tokens = float(row.get("main_input_tokens") or 0)
    cached_tokens = float(row.get("main_cached_input_tokens") or 0)
    output_tokens = float(row.get("main_output_tokens") or 0)
    if not pricing.output_includes_reasoning:
        output_tokens += float(row.get("main_reasoning_output_tokens") or 0)
    uncached_tokens = max(0.0, input_tokens - cached_tokens)
    return (
        uncached_tokens / 1_000_000 * pricing.input_usd_per_mtok
        + cached_tokens / 1_000_000 * pricing.cached_input_usd_per_mtok
        + output_tokens / 1_000_000 * pricing.output_usd_per_mtok
    )


def _cost_from_guardian_tokens(row: dict[str, Any], pricing: Pricing) -> float | None:
    if pricing.input_usd_per_mtok is None or pricing.output_usd_per_mtok is None:
        return None
    if row.get("guardian_prompt_tokens") is None:
        return None
    prompt = float(row.get("guardian_prompt_tokens") or 0)
    completion = float(row.get("guardian_completion_tokens") or 0)
    return (
        prompt / 1_000_000 * pricing.input_usd_per_mtok
        + completion / 1_000_000 * pricing.output_usd_per_mtok
    )


def _with_costs(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    row = dict(row)
    pricing = _pricing_for_model(str(row.get("model") or ""), args)
    main_cost = _cost_from_main_tokens(row, pricing)
    guardian_cost = _cost_from_guardian_tokens(row, pricing)
    row["main_cost_usd"] = main_cost
    row["guardian_cost_usd"] = guardian_cost
    row["total_cost_usd"] = (
        (main_cost or 0) + (guardian_cost or 0)
        if main_cost is not None or guardian_cost is not None
        else None
    )
    return row


def _metric_mean(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
    if not vals:
        return None
    return float(statistics.fmean(vals))


def _metric_sum(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
    if not vals:
        return None
    return float(sum(vals))


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        return f"{value:.6g}"
    return str(value)


def write_summary(rows: list[dict[str, Any]], summary_csv: Path) -> None:
    rows = [r for r in rows if r.get("returncode") == 0]
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
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
        "n_trials",
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
        "avg_guardian_completion_tokens",
        "avg_main_cost_usd",
        "avg_guardian_cost_usd",
        "avg_total_cost_usd",
        "sum_total_cost_usd",
        "latest_output_dir",
        "latest_result_json",
    ]
    summary_csv.parent.mkdir(parents=True, exist_ok=True)
    with summary_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for (task, baseline, model, effort), group in sorted(groups.items()):
            latest = max(group, key=lambda r: str(r.get("created_at") or ""))
            out = {
                "task": task,
                "baseline": baseline,
                "model": model,
                "reasoning_effort": effort,
                "n_trials": len(group),
                "avg_reward": _metric_mean(group, "reward"),
                "avg_f2p": _metric_mean(group, "f2p"),
                "avg_p2p": _metric_mean(group, "p2p"),
                "avg_partial": _metric_mean(group, "partial"),
                "avg_f2p_passed": _metric_mean(group, "f2p_passed"),
                "avg_f2p_total": _metric_mean(group, "f2p_total"),
                "avg_p2p_passed": _metric_mean(group, "p2p_passed"),
                "avg_p2p_total": _metric_mean(group, "p2p_total"),
                "pass_rate": _metric_mean(group, "reward"),
                "avg_main_input_tokens": _metric_mean(group, "main_input_tokens"),
                "avg_main_cached_input_tokens": _metric_mean(
                    group, "main_cached_input_tokens"
                ),
                "avg_main_output_tokens": _metric_mean(group, "main_output_tokens"),
                "avg_main_reasoning_output_tokens": _metric_mean(
                    group, "main_reasoning_output_tokens"
                ),
                "avg_guardian_prompt_tokens": _metric_mean(
                    group, "guardian_prompt_tokens"
                ),
                "avg_guardian_completion_tokens": _metric_mean(
                    group, "guardian_completion_tokens"
                ),
                "avg_main_cost_usd": _metric_mean(group, "main_cost_usd"),
                "avg_guardian_cost_usd": _metric_mean(group, "guardian_cost_usd"),
                "avg_total_cost_usd": _metric_mean(group, "total_cost_usd"),
                "sum_total_cost_usd": _metric_sum(group, "total_cost_usd"),
                "latest_output_dir": latest.get("output_dir") or "",
                "latest_result_json": latest.get("result_json") or "",
            }
            writer.writerow({k: _format_cell(out.get(k)) for k in fields})


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--summary-csv", type=Path, default=DEFAULT_SUMMARY_CSV)
    parser.add_argument("--input-usd-per-mtok", type=float)
    parser.add_argument("--cached-input-usd-per-mtok", type=float)
    parser.add_argument("--output-usd-per-mtok", type=float)
    parser.add_argument(
        "--output-excludes-reasoning",
        action="store_true",
        help="Add reasoning_output_tokens to output_tokens for main Codex cost.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    rows = [_with_costs(r, args) for r in _load_rows(args.output_root)]
    write_summary(rows, args.summary_csv)
    print(f"[done] scanned {len(rows)} trial(s)")
    print(f"[done] summary: {args.summary_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
