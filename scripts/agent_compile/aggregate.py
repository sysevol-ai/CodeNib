#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Aggregate sweep cells into a Phase 0 kill-switch report.

Reads ``cells/*.json`` written by ``run_sweep.py`` and produces:

* ``phase0_report.md`` — markdown table per subset (tokens, turns,
  files@k, cap-hit rate, cost) and the kill-switch verdict
* ``phase0_metrics.json`` — same numbers, machine-readable

Aggregation:

* Per (subset, instance), cost-like quantities (tokens, wall time, cost)
  are taken as the **min across reps** — this matches @MihirJagtap's
  noise-handling ritual in #131 (provider hiccups / GC inflate, never
  deflate, so the min is the closest estimate to the true lower bound).
* Accuracy metrics (``files@k`` / ``symbols@k``) are taken as the **mean
  across reps** — accuracy noise is two-sided, so the mean is the
  natural estimator.
* Per (subset), final numbers are the **mean across instances**.

This script is Phase-0-scoped. Phase 2's aggregator (bootstrap CIs,
ANOVA, invocation/success-rate histograms, scenario slicing) lives in
the same directory but is a separate entry point.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Kill-switch thresholds — also pinned in docs/agent_compile_design.md.
# Keep these in sync with the ADR.
KILL_TOKEN_CEILING = 30_000
KILL_FILES_AT_5_FLOOR = 0.55


@dataclass
class SubsetMetrics:
    subset_id: str
    instance_count: int = 0
    rep_count: int = 0
    failed_instances: int = 0
    mean_prompt_tokens: float = 0.0
    mean_completion_tokens: float = 0.0
    mean_total_tokens: float = 0.0
    mean_total_duration_ms: float = 0.0
    mean_total_turns: float = 0.0
    cap_hit_rate: float = 0.0
    mean_cost_usd: Optional[float] = None
    files_at_k: Dict[int, float] = field(default_factory=dict)
    symbols_at_k: Dict[int, float] = field(default_factory=dict)


def _load_cells(cells_dir: Path) -> List[Dict[str, Any]]:
    cells: List[Dict[str, Any]] = []
    for path in sorted(cells_dir.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as f:
                cells.append(json.load(f))
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"aggregate: WARN unreadable cell {path}: {exc}",
                file=sys.stderr,
            )
    return cells


def _group_by_subset_instance(
    cells: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Group cells by ``subset_id`` then ``instance_id`` for rep aggregation."""
    out: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for c in cells:
        sid = c.get("subset_id")
        iid = c.get("instance_id")
        if not sid or not iid:
            continue
        out[sid][iid].append(c)
    return out


def _safe_min(values: Sequence[Optional[float]]) -> Optional[float]:
    xs = [v for v in values if v is not None]
    return min(xs) if xs else None


def _safe_mean(values: Sequence[Optional[float]]) -> Optional[float]:
    xs = [v for v in values if v is not None]
    return statistics.fmean(xs) if xs else None


def _mean_or_zero(value: Optional[float]) -> float:
    return float(value) if value is not None else 0.0


def _at_k_from_cell(cell: Dict[str, Any], scope: str, k: int) -> Optional[float]:
    metrics = cell.get("metrics") or {}
    bucket = metrics.get(scope) or {}
    # The eval driver may use int or str keys depending on json roundtrip.
    if k in bucket:
        stats = bucket[k]
    elif str(k) in bucket:
        stats = bucket[str(k)]
    else:
        return None
    if isinstance(stats, dict):
        return stats.get("accuracy")
    return stats


def aggregate(
    cells: Sequence[Dict[str, Any]],
    max_turns: int,
    metrics_k: Sequence[int] = (1, 3, 5, 10),
) -> Dict[str, SubsetMetrics]:
    """Roll up cells into per-subset metrics."""
    grouped = _group_by_subset_instance(cells)
    out: Dict[str, SubsetMetrics] = {}

    for subset_id, by_inst in grouped.items():
        sm = SubsetMetrics(subset_id=subset_id)

        instance_tokens_prompt: List[float] = []
        instance_tokens_completion: List[float] = []
        instance_tokens_total: List[float] = []
        instance_duration: List[float] = []
        instance_turns: List[float] = []
        instance_cost: List[Optional[float]] = []
        instance_cap_hit: List[int] = []
        instance_files_at_k: Dict[int, List[float]] = {k: [] for k in metrics_k}
        instance_symbols_at_k: Dict[int, List[float]] = {k: [] for k in metrics_k}

        for _instance_id, reps in by_inst.items():
            failed = [r for r in reps if not r.get("success")]
            if failed and len(failed) == len(reps):
                sm.failed_instances += 1
                continue

            sm.rep_count += len(reps)

            # Cost-like: min across reps.
            instance_tokens_prompt.append(
                _mean_or_zero(_safe_min([r.get("prompt_tokens") for r in reps]))
            )
            instance_tokens_completion.append(
                _mean_or_zero(_safe_min([r.get("completion_tokens") for r in reps]))
            )
            instance_tokens_total.append(
                _mean_or_zero(_safe_min([r.get("total_tokens") for r in reps]))
            )
            instance_duration.append(
                _mean_or_zero(_safe_min([r.get("total_duration_ms") for r in reps]))
            )
            instance_turns.append(
                _mean_or_zero(_safe_min([r.get("total_turns") for r in reps]))
            )
            instance_cost.append(_safe_min([r.get("cost_usd") for r in reps]))

            # Cap hit: any rep that ran to the cap counts.
            cap_hit_any = any((r.get("total_turns") or 0) >= max_turns for r in reps)
            instance_cap_hit.append(1 if cap_hit_any else 0)

            # Accuracy: mean across reps.
            for k in metrics_k:
                files_vals = [_at_k_from_cell(r, "files", k) for r in reps]
                symbols_vals = [_at_k_from_cell(r, "symbols", k) for r in reps]
                fmean = _safe_mean(files_vals)
                smean = _safe_mean(symbols_vals)
                if fmean is not None:
                    instance_files_at_k[k].append(fmean)
                if smean is not None:
                    instance_symbols_at_k[k].append(smean)

        sm.instance_count = len(by_inst) - sm.failed_instances
        if sm.instance_count == 0:
            out[subset_id] = sm
            continue

        sm.mean_prompt_tokens = statistics.fmean(instance_tokens_prompt)
        sm.mean_completion_tokens = statistics.fmean(instance_tokens_completion)
        sm.mean_total_tokens = statistics.fmean(instance_tokens_total)
        sm.mean_total_duration_ms = statistics.fmean(instance_duration)
        sm.mean_total_turns = statistics.fmean(instance_turns)
        sm.cap_hit_rate = (
            sum(instance_cap_hit) / len(instance_cap_hit) if instance_cap_hit else 0.0
        )
        cost_values = [c for c in instance_cost if c is not None]
        sm.mean_cost_usd = statistics.fmean(cost_values) if cost_values else None
        for k in metrics_k:
            if instance_files_at_k[k]:
                sm.files_at_k[k] = statistics.fmean(instance_files_at_k[k])
            if instance_symbols_at_k[k]:
                sm.symbols_at_k[k] = statistics.fmean(instance_symbols_at_k[k])

        out[subset_id] = sm

    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _kill_switch_verdict(metrics: Dict[str, SubsetMetrics]) -> Dict[str, Any]:
    """Return a verdict object based on the A6 numbers, if present."""
    a6 = metrics.get("A6")
    if a6 is None:
        return {
            "verdict": "inconclusive",
            "reason": "A6 missing from cells; cannot evaluate kill-switch",
        }
    tokens_ok = a6.mean_total_tokens < KILL_TOKEN_CEILING
    files_ok = a6.files_at_k.get(5, 0.0) >= KILL_FILES_AT_5_FLOOR
    closes = tokens_ok and files_ok
    return {
        "verdict": "close-rfc" if closes else "proceed",
        "tokens_ok": tokens_ok,
        "files_at_5_ok": files_ok,
        "a6_tokens": a6.mean_total_tokens,
        "a6_files_at_5": a6.files_at_k.get(5, 0.0),
        "token_ceiling": KILL_TOKEN_CEILING,
        "files_at_5_floor": KILL_FILES_AT_5_FLOOR,
        "note": (
            "Both A6 cost and A6 accuracy must clear the ADR thresholds "
            "to close the RFC. Proceed otherwise."
        ),
    }


def _render_markdown(
    metrics: Dict[str, SubsetMetrics],
    verdict: Dict[str, Any],
    metrics_k: Sequence[int],
) -> str:
    lines: List[str] = []
    lines.append("# Phase 0 kill-switch report")
    lines.append("")
    lines.append(f"Subsets observed: {', '.join(sorted(metrics.keys())) or '(none)'}")
    lines.append("")
    lines.append("## Per-subset metrics")
    lines.append("")
    header = (
        [
            "subset",
            "instances",
            "mean_total_tokens",
            "mean_turns",
            "cap_hit_rate",
            "mean_cost_usd",
        ]
        + [f"files@{k}" for k in metrics_k]
        + [f"symbols@{k}" for k in metrics_k]
    )
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for subset_id in sorted(metrics.keys()):
        sm = metrics[subset_id]
        row = [
            subset_id,
            str(sm.instance_count),
            f"{sm.mean_total_tokens:.0f}",
            f"{sm.mean_total_turns:.1f}",
            f"{sm.cap_hit_rate:.2%}",
            f"${sm.mean_cost_usd:.4f}" if sm.mean_cost_usd is not None else "n/a",
        ]
        for k in metrics_k:
            row.append(f"{sm.files_at_k.get(k, 0.0):.3f}")
        for k in metrics_k:
            row.append(f"{sm.symbols_at_k.get(k, 0.0):.3f}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("## Kill-switch verdict")
    lines.append("")
    lines.append(f"* Verdict: **{verdict['verdict']}**")
    if "a6_tokens" in verdict:
        lines.append(
            f"* A6 mean total tokens: {verdict['a6_tokens']:.0f} "
            f"(threshold < {verdict['token_ceiling']}, "
            f"{'✓' if verdict['tokens_ok'] else '✗'})"
        )
        lines.append(
            f"* A6 files@5: {verdict['a6_files_at_5']:.3f} "
            f"(threshold ≥ {verdict['files_at_5_floor']:.2f}, "
            f"{'✓' if verdict['files_at_5_ok'] else '✗'})"
        )
    lines.append(f"* Note: {verdict.get('note', '')}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate sweep cells into a Phase 0 kill-switch report.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--cells-dir",
        required=True,
        type=Path,
        help="Directory of cell JSONs produced by run_sweep.py.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Where to write phase0_report.md and phase0_metrics.json.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=20,
        help="Per-run turn cap (locked at 20 per the design ADR).",
    )
    parser.add_argument(
        "--metrics-k",
        type=int,
        nargs="+",
        default=[1, 3, 5, 10],
        help="K values to report files@k / symbols@k against.",
    )
    args = parser.parse_args(argv)

    cells = _load_cells(args.cells_dir)
    if not cells:
        print(f"aggregate: no cells under {args.cells_dir}", file=sys.stderr)
        return 2

    metrics = aggregate(cells, max_turns=args.max_turns, metrics_k=args.metrics_k)
    verdict = _kill_switch_verdict(metrics)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    json_payload = {
        "metrics": {
            sid: {
                "subset_id": sm.subset_id,
                "instance_count": sm.instance_count,
                "rep_count": sm.rep_count,
                "failed_instances": sm.failed_instances,
                "mean_prompt_tokens": sm.mean_prompt_tokens,
                "mean_completion_tokens": sm.mean_completion_tokens,
                "mean_total_tokens": sm.mean_total_tokens,
                "mean_total_duration_ms": sm.mean_total_duration_ms,
                "mean_total_turns": sm.mean_total_turns,
                "cap_hit_rate": sm.cap_hit_rate,
                "mean_cost_usd": sm.mean_cost_usd,
                "files_at_k": {str(k): v for k, v in sm.files_at_k.items()},
                "symbols_at_k": {str(k): v for k, v in sm.symbols_at_k.items()},
            }
            for sid, sm in metrics.items()
        },
        "verdict": verdict,
        "cell_count": len(cells),
    }
    with (args.output_dir / "phase0_metrics.json").open("w", encoding="utf-8") as f:
        json.dump(json_payload, f, indent=2, ensure_ascii=False)

    md = _render_markdown(metrics, verdict, args.metrics_k)
    (args.output_dir / "phase0_report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
