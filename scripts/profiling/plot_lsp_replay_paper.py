#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Render a dense single-column figure from LSP replay reports."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import matplotlib.pyplot as plt
import numpy as np

LANGUAGE_ORDER = ("cpp", "go", "python", "rust", "typescript")
CAPABILITY_ORDER = ("definition", "references")
LANGUAGE_LABELS = {
    "cpp": "C/C++",
    "go": "Go",
    "python": "Python",
    "rust": "Rust",
    "typescript": "TS/JS",
}
CAPABILITY_LABELS = {"definition": "def", "references": "refs"}
STATIC_COLOR = "#008B8B"
LIVE_COLOR = "#DE5A3A"
PAIR_COLOR = "#999999"


def _capability(row: Mapping[str, Any]) -> str:
    request = row.get("request") or {}
    value = str(request.get("capability") or row.get("capability") or "")
    return value.removeprefix("textDocument/")


def _duration(row: Mapping[str, Any], field: str) -> float | None:
    value = (row.get(field) or {}).get("duration_ms")
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _median(values: Iterable[float]) -> float | None:
    data = list(values)
    return float(np.median(data)) if data else None


def _percentile(values: Iterable[float], q: float) -> float | None:
    data = list(values)
    return float(np.percentile(data, q)) if data else None


def _load_reports(report_dir: Path) -> list[dict[str, Any]]:
    reports = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(report_dir.glob("*.json"))
    ]
    if not reports:
        raise ValueError(f"no JSON reports found in {report_dir}")
    return reports


def _request_coverage(
    reports: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], tuple[int, int]]:
    grouped: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for report in reports:
        language = str((report.get("subject") or {}).get("language") or "")
        requests: dict[tuple[str, str], list[bool]] = defaultdict(list)
        for row in report.get("comparisons") or []:
            capability = _capability(row)
            request_id = str((row.get("request") or {}).get("request_id") or "")
            requests[(capability, request_id)].append(row.get("same_result") is True)
        for (capability, _), verdicts in requests.items():
            grouped[(language, capability)].append(bool(verdicts) and all(verdicts))
    return {key: (sum(verdicts), len(verdicts)) for key, verdicts in grouped.items()}


def _measurement_rows(
    reports: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for report in reports:
        language = str((report.get("subject") or {}).get("language") or "")
        for row in report.get("comparisons") or []:
            if row.get("same_result") is True:
                grouped[(language, _capability(row))].append(row)
    return grouped


def _snapshot_medians(
    reports: Sequence[Mapping[str, Any]], language: str, capability: str
) -> list[tuple[float, float]]:
    pairs = []
    for report in reports:
        if str((report.get("subject") or {}).get("language")) != language:
            continue
        rows = [
            row
            for row in report.get("comparisons") or []
            if row.get("same_result") is True and _capability(row) == capability
        ]
        static = _median(
            value
            for row in rows
            if (value := _duration(row, "static_call")) is not None
        )
        live = _median(
            value
            for row in rows
            if (value := _duration(row, "reference_call")) is not None
        )
        if static is not None and live is not None:
            pairs.append((static, live))
    return pairs


def render_figure(reports: Sequence[Mapping[str, Any]], output_prefix: Path) -> None:
    coverage = _request_coverage(reports)
    rows = _measurement_rows(reports)
    groups = [
        (language, capability)
        for language in LANGUAGE_ORDER
        for capability in CAPABILITY_ORDER
    ]
    all_equivalent = [row for values in rows.values() for row in values]
    static_all = [
        value
        for row in all_equivalent
        if (value := _duration(row, "static_call")) is not None
    ]
    live_all = [
        value
        for row in all_equivalent
        if (value := _duration(row, "reference_call")) is not None
    ]
    speedup_all = [
        float(row["speedup_ratio"])
        for row in all_equivalent
        if isinstance(row.get("speedup_ratio"), (int, float))
    ]

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 7.5,
            "axes.titlesize": 9,
            "axes.labelsize": 8,
            "xtick.labelsize": 7,
            "ytick.labelsize": 7,
            "legend.fontsize": 7,
            "axes.linewidth": 0.8,
        }
    )
    fig = plt.figure(figsize=(3.5, 5.2), constrained_layout=False)
    grid = fig.add_gridspec(2, 1, height_ratios=(2.25, 1.0), hspace=0.42)
    ax = fig.add_subplot(grid[0])

    pooled_medians = []
    for index, (language, capability) in enumerate(groups):
        y = len(groups) - 1 - index
        values = rows.get((language, capability), [])
        static = _median(
            value
            for row in values
            if (value := _duration(row, "static_call")) is not None
        )
        live = _median(
            value
            for row in values
            if (value := _duration(row, "reference_call")) is not None
        )
        speedup = _median(
            float(row["speedup_ratio"])
            for row in values
            if isinstance(row.get("speedup_ratio"), (int, float))
        )
        if static is None or live is None or speedup is None:
            continue
        pooled_medians.extend((static, live))
        pairs = _snapshot_medians(reports, language, capability)
        offsets = np.linspace(-0.18, 0.18, len(pairs)) if pairs else []
        for offset, (static_pair, live_pair) in zip(offsets, pairs, strict=True):
            ax.plot(
                [static_pair, live_pair],
                [y + offset, y + offset],
                color=PAIR_COLOR,
                alpha=0.18,
                linewidth=0.45,
                zorder=1,
            )
            ax.scatter(
                static_pair,
                y + offset,
                s=6,
                color=STATIC_COLOR,
                alpha=0.28,
                linewidths=0,
                zorder=2,
            )
            ax.scatter(
                live_pair,
                y + offset,
                s=7,
                marker="^",
                color=LIVE_COLOR,
                alpha=0.28,
                linewidths=0,
                zorder=2,
            )
        ax.plot([static, live], [y, y], color="#666666", linewidth=1.2, zorder=3)
        ax.scatter(static, y, s=28, color=STATIC_COLOR, edgecolor="white", zorder=4)
        ax.scatter(
            live,
            y,
            s=34,
            marker="^",
            color=LIVE_COLOR,
            edgecolor="white",
            zorder=4,
        )
        ax.text(
            1.01,
            y,
            f"{speedup:.1f}x",
            transform=ax.get_yaxis_transform(),
            ha="left",
            va="center",
            fontsize=6.8,
            fontweight="bold",
        )

    labels = []
    for language, capability in groups:
        equivalent, total = coverage.get((language, capability), (0, 0))
        rate = equivalent / total if total else 0
        labels.append(
            f"{LANGUAGE_LABELS[language]} {CAPABILITY_LABELS[capability]}  {rate:.0%}"
        )
    ax.set_yticks(range(len(groups)))
    ax.set_yticklabels(reversed(labels))
    ax.set_xscale("log")
    lower = max(0.03, min(pooled_medians) / 2)
    upper = max(pooled_medians) * 2.5
    ax.set_xlim(lower, upper)
    ax.set_ylim(-0.55, len(groups) - 0.45)
    ax.grid(axis="x", which="both", color="#dddddd", linewidth=0.55)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_xlabel("Warm request latency (ms, log scale)")
    ax.set_title(
        "(a) Equivalent latency and request coverage",
        loc="left",
        y=1.14,
        pad=0,
    )
    ax.legend(
        handles=[
            plt.Line2D(
                [],
                [],
                marker="o",
                color="none",
                markerfacecolor=STATIC_COLOR,
                markeredgecolor="none",
                label="CodeNib static",
            ),
            plt.Line2D(
                [],
                [],
                marker="^",
                color="none",
                markerfacecolor=LIVE_COLOR,
                markeredgecolor="none",
                label="Live JSON-RPC",
            ),
        ],
        loc="lower center",
        bbox_to_anchor=(0.5, 1.01),
        ncol=2,
        frameon=False,
        handletextpad=0.35,
        columnspacing=0.9,
    )
    ax.text(
        1.015,
        1.01,
        "median\nspeedup",
        transform=ax.transAxes,
        ha="left",
        va="bottom",
        fontsize=6.5,
        color="#555555",
    )

    ecdf = fig.add_subplot(grid[1])
    for values, color, label in (
        (static_all, STATIC_COLOR, "CodeNib static"),
        (live_all, LIVE_COLOR, "Live JSON-RPC"),
    ):
        ordered = np.sort(values)
        fractions = np.arange(1, len(ordered) + 1) / len(ordered)
        ecdf.step(
            ordered, fractions, where="post", color=color, linewidth=1.6, label=label
        )
    ecdf.set_xscale("log")
    ecdf.set_xlim(
        max(0.03, min(static_all + live_all) / 1.5),
        _percentile(static_all + live_all, 99.5) * 1.4,
    )
    ecdf.set_ylim(0, 1.02)
    ecdf.set_yticks((0, 0.25, 0.5, 0.75, 1.0))
    ecdf.grid(color="#dddddd", linewidth=0.55)
    ecdf.spines[["top", "right"]].set_visible(False)
    ecdf.set_xlabel("Warm request latency (ms, log scale)")
    ecdf.set_ylabel("Fraction of rows")
    ecdf.set_title("(b) ECDF over equivalent measured rows", loc="left")
    ecdf.legend(loc="lower right", frameon=False)
    ecdf.text(
        0.02,
        0.96,
        (
            f"Static p50 {_median(static_all):.2f} ms\n"
            f"Live p50 {_median(live_all):.2f} ms"
        ),
        transform=ecdf.transAxes,
        ha="left",
        va="top",
        bbox={"facecolor": "white", "edgecolor": "#bbbbbb", "pad": 2.0},
    )

    equivalent_requests = sum(value[0] for value in coverage.values())
    total_requests = sum(value[1] for value in coverage.values())
    fig.text(
        0.5,
        0.012,
        (
            f"{len(reports)} snapshots  |  {equivalent_requests}/{total_requests} "
            f"requests equivalent  |  p50 {_median(speedup_all):.2f}x"
        ),
        ha="center",
        va="bottom",
        fontsize=7.3,
        fontweight="bold",
    )
    fig.subplots_adjust(left=0.25, right=0.90, top=0.91, bottom=0.10)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_prefix.with_suffix(".pdf"), bbox_inches="tight")
    fig.savefig(output_prefix.with_suffix(".png"), dpi=300, bbox_inches="tight")
    plt.close(fig)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", required=True, type=Path)
    parser.add_argument("--output-prefix", required=True, type=Path)
    args = parser.parse_args(argv)
    reports = _load_reports(args.reports_dir)
    render_figure(reports, args.output_prefix)
    print(f"rendered {len(reports)} reports to {args.output_prefix}.{{pdf,png}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
