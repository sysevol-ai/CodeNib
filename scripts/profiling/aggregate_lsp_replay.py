#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Aggregate LSP replay reports without treating repetitions as requests."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codeminer.eval.agent_runner.lsp_replay_benchmark import (  # noqa: E402
    summarize_lsp_replay_benchmark,
)


def _percentile(values: Iterable[float], q: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = q * (len(ordered) - 1)
    lo, hi = math.floor(rank), math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def aggregate_lsp_replay_reports(
    reports: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    comparisons: list[dict[str, Any]] = []
    request_rows: dict[tuple[int, str, str], list[Mapping[str, Any]]] = defaultdict(
        list
    )
    for report_index, report in enumerate(reports):
        for comparison in report.get("comparisons") or []:
            row = dict(comparison)
            comparisons.append(row)
            request = row.get("request") or {}
            capability = str(request.get("capability") or "unknown")
            request_id = str(request.get("request_id") or "")
            request_rows[(report_index, capability, request_id)].append(row)

    request_summary: dict[str, dict[str, int | float]] = {}
    by_capability: dict[str, list[list[Mapping[str, Any]]]] = defaultdict(list)
    for (_, capability, _), rows in request_rows.items():
        by_capability[capability].append(rows)
    by_capability["overall"] = list(request_rows.values())
    for capability, groups in sorted(by_capability.items()):
        equivalent = sum(
            1
            for rows in groups
            if rows and all(row.get("same_result") is True for row in rows)
        )
        errors = sum(
            1
            for rows in groups
            if any(
                row.get("verdict")
                in {"static_error", "reference_error", "fallback_required", "unknown"}
                for row in rows
            )
        )
        total = len(groups)
        request_summary[capability] = {
            "requests": total,
            "equivalent_requests": equivalent,
            "mismatch_requests": total - equivalent - errors,
            "error_or_fallback_requests": errors,
            "equivalence_rate": equivalent / total if total else 0.0,
        }

    setup = [report.get("setup") or {} for report in reports]
    setup_summary = {}
    for field in ("graph_load_ms", "static_provider_init_ms", "live_start_ms"):
        values = [
            float(row[field])
            for row in setup
            if isinstance(row.get(field), (int, float))
        ]
        setup_summary[field] = {
            "count": len(values),
            "p50": _percentile(values, 0.50),
            "p95": _percentile(values, 0.95),
        }

    measurement_summary = summarize_lsp_replay_benchmark({"comparisons": comparisons})
    return {
        "schema_version": 1,
        "snapshot_count": len(reports),
        "request_summary": request_summary,
        "setup": setup_summary,
        "measurements": measurement_summary,
    }


def render_markdown(summary: Mapping[str, Any]) -> str:
    lines = [
        "# LSP replay aggregate",
        "",
        (
            f"Snapshots: {summary['snapshot_count']}. Request equivalence is "
            "counted once per request; latency distributions retain measured "
            "repetitions."
        ),
        "",
        "| capability | equivalent/requests | mismatch | error/fallback "
        "| equivalence | static p50 ms | live p50 ms | speedup p50 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    requests = summary["request_summary"]
    measurements = summary["measurements"]
    for capability in ("overall", "definition", "references"):
        req = requests.get(capability) or {}
        measured = (
            measurements.get("overall")
            if capability == "overall"
            else (measurements.get("by_capability") or {}).get(capability)
        ) or {}
        lines.append(
            "| {cap} | {eq}/{total} | {mismatch} | {errors} | {rate:.1%} "
            "| {static} | {live} | {speedup} |".format(
                cap=capability,
                eq=req.get("equivalent_requests", 0),
                total=req.get("requests", 0),
                mismatch=req.get("mismatch_requests", 0),
                errors=req.get("error_or_fallback_requests", 0),
                rate=req.get("equivalence_rate", 0.0),
                static=_fmt_nested(measured, "static_ms", "p50"),
                live=_fmt_nested(measured, "live_ms", "p50"),
                speedup=_fmt_nested(measured, "speedup_ratio", "p50"),
            )
        )
    lines.extend(["", "## Setup", ""])
    lines.append("| phase | p50 ms | p95 ms |")
    lines.append("|---|---:|---:|")
    for field in ("graph_load_ms", "static_provider_init_ms", "live_start_ms"):
        values = summary["setup"][field]
        lines.append(
            f"| {field} | {_fmt(values.get('p50'))} | {_fmt(values.get('p95'))} |"
        )
    lines.append("")
    return "\n".join(lines)


def _fmt_nested(payload: Mapping[str, Any], group: str, field: str) -> str:
    return _fmt((payload.get(group) or {}).get(field))


def _fmt(value: Any) -> str:
    return f"{float(value):.2f}" if isinstance(value, (int, float)) else "n/a"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    args = parser.parse_args(argv)
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in args.reports]
    summary = aggregate_lsp_replay_reports(reports)
    markdown = render_markdown(summary)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
    if args.output_markdown:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
