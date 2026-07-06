#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Render small-run feedback summaries for agent-runner result directories."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, List, Optional, Sequence

from codeminer.eval.agent_runner.feedback_summary import (
    FeedbackSummary,
    load_feedback_summary,
)


def render_report(result_name: str, summary: FeedbackSummary) -> List[str]:
    """Render a compact Markdown report for one feedback summary."""

    lines = [
        f"## {result_name}",
        "",
        f"- cells: {summary.cell_count}",
        f"- baseline: {summary.baseline or 'n/a'}",
        f"- metric cutoff: @{summary.k}",
        "",
        "| arm | n | success | meaningful | fmt_fail | files | answer_blocks | "
        "tokens | cost | turns | ctx_tokens | ctx_sources | failures |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | "
        "---: | --- | --- |",
    ]
    for arm in summary.arms:
        lines.append(
            "| "
            + " | ".join(
                [
                    arm.arm,
                    str(arm.n),
                    _pct(arm.success_rate),
                    _pct(arm.metrics_meaningful_rate),
                    _pct(arm.format_fail_rate),
                    _num(arm.files_recall_at_k),
                    _num(arm.answer_blocks_recall_at_k),
                    _num(arm.total_tokens_mean, ndigits=0),
                    _num(arm.cost_usd_mean, ndigits=4),
                    _num(arm.total_turns_mean),
                    _num(arm.context_tokens_mean, ndigits=0),
                    _counts(arm.context_sources),
                    _counts(arm.failure_groups),
                ]
            )
            + " |"
        )

    if summary.deltas:
        lines.extend(
            [
                "",
                "| arm | baseline | d_success | d_files | d_answer_blocks | "
                "d_tokens | d_cost | d_turns | d_ctx_tokens |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for delta in summary.deltas:
            lines.append(
                "| "
                + " | ".join(
                    [
                        delta.arm,
                        delta.baseline,
                        _signed_pct(delta.success_rate_delta),
                        _signed(delta.files_recall_delta),
                        _signed(delta.answer_blocks_recall_delta),
                        _signed(delta.total_tokens_delta, ndigits=0),
                        _signed(delta.cost_usd_delta, ndigits=4),
                        _signed(delta.total_turns_delta),
                        _signed(delta.context_tokens_delta, ndigits=0),
                    ]
                )
                + " |"
            )
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize small agent feedback result directories.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("result_dirs", nargs="+", type=Path)
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--arm-field", default="subset_id")
    parser.add_argument("--output-json", type=Path, default=None)
    args = parser.parse_args(argv)

    payload: List[dict[str, Any]] = []
    report = ["# Agent feedback summary", ""]
    for result_dir in args.result_dirs:
        summary = load_feedback_summary(
            result_dir,
            baseline=args.baseline,
            k=args.k,
            arm_field=args.arm_field,
        )
        payload.append(
            {
                "result_name": result_dir.name,
                "result_dir": str(result_dir),
                "summary": summary.to_dict(),
            }
        )
        report.extend(render_report(result_dir.name, summary))
        report.append("")

    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps({"results": payload}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print("\n".join(report).rstrip())
    return 0


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _signed_pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:+.0f}pp"


def _num(value: Optional[float], *, ndigits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{ndigits}f}"


def _signed(value: Optional[float], *, ndigits: int = 3) -> str:
    return "n/a" if value is None else f"{value:+.{ndigits}f}"


def _counts(counts: dict[str, int]) -> str:
    if not counts:
        return "-"
    return ", ".join(f"{key}:{value}" for key, value in sorted(counts.items()))


if __name__ == "__main__":
    raise SystemExit(main())
