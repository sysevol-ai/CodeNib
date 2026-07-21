#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Render a compact double-column figure from LSP replay reports."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from plot_style import (
    DOUBLE_COLUMN_WIDTH,
    LIVE_COLOR,
    NEUTRAL_COLOR,
    STATIC_COLOR,
    TWO_PANEL_HEIGHT,
    apply_paper_style,
    panel_title,
    save_figure,
    style_axis,
)

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
PAIR_COLOR = "#9B9692"
DEFAULT_OUTPUT_PREFIX = Path(__file__).resolve().parent / "lsp_replay_100"

apply_paper_style()


@dataclass(frozen=True)
class RequestLatency:
    snapshot_index: int
    language: str
    capability: str
    request_id: str
    repetitions: int
    static_ms: float
    live_ms: float
    saving_ms: float
    speedup_ratio: float


@dataclass(frozen=True)
class ReplayPlotData:
    reports: Sequence[Mapping[str, Any]]
    coverage: dict[tuple[str, str], tuple[int, int]]
    requests: dict[tuple[str, str], list[RequestLatency]]
    groups: tuple[tuple[str, str], ...]
    matched_row_count: int
    static_all: list[float]
    live_all: list[float]
    saving_all: list[float]
    speedup_all: list[float]


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


def _request_latencies(
    reports: Sequence[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str], list[RequestLatency]], int]:
    grouped: dict[tuple[str, str], list[RequestLatency]] = defaultdict(list)
    matched_row_count = 0
    for snapshot_index, report in enumerate(reports):
        language = str((report.get("subject") or {}).get("language") or "")
        request_rows: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in report.get("comparisons") or []:
            capability = _capability(row)
            request_id = str((row.get("request") or {}).get("request_id") or "")
            if not request_id:
                raise ValueError(
                    f"snapshot {snapshot_index} contains a request without an id"
                )
            request_rows[(capability, request_id)].append(row)

        for (capability, request_id), rows in request_rows.items():
            if not rows or not all(row.get("same_result") is True for row in rows):
                continue
            static_values = [_duration(row, "static_call") for row in rows]
            live_values = [_duration(row, "reference_call") for row in rows]
            if any(value is None for value in static_values + live_values):
                raise ValueError(
                    f"matched request {request_id!r} has an invalid duration"
                )
            static_ms = float(np.median(static_values))
            live_ms = float(np.median(live_values))
            grouped[(language, capability)].append(
                RequestLatency(
                    snapshot_index=snapshot_index,
                    language=language,
                    capability=capability,
                    request_id=request_id,
                    repetitions=len(rows),
                    static_ms=static_ms,
                    live_ms=live_ms,
                    saving_ms=live_ms - static_ms,
                    speedup_ratio=live_ms / static_ms,
                )
            )
            matched_row_count += len(rows)
    return grouped, matched_row_count


def _snapshot_medians(
    requests: Sequence[RequestLatency],
) -> list[tuple[float, float]]:
    by_snapshot: dict[int, list[RequestLatency]] = defaultdict(list)
    for request in requests:
        by_snapshot[request.snapshot_index].append(request)
    return [
        (
            float(np.median([request.static_ms for request in values])),
            float(np.median([request.live_ms for request in values])),
        )
        for _, values in sorted(by_snapshot.items())
    ]


def prepare_replay_data(
    reports: Sequence[Mapping[str, Any]],
) -> ReplayPlotData:
    coverage = _request_coverage(reports)
    requests, matched_row_count = _request_latencies(reports)
    groups = tuple(
        (language, capability)
        for language in LANGUAGE_ORDER
        for capability in CAPABILITY_ORDER
    )
    all_matched = [request for values in requests.values() for request in values]
    coverage_matches = sum(matched for matched, _ in coverage.values())
    if len(all_matched) != coverage_matches:
        raise ValueError(
            "request coverage and latency summaries disagree: "
            f"{coverage_matches} matched versus {len(all_matched)} summarized"
        )
    return ReplayPlotData(
        reports=reports,
        coverage=coverage,
        requests=requests,
        groups=groups,
        matched_row_count=matched_row_count,
        static_all=[request.static_ms for request in all_matched],
        live_all=[request.live_ms for request in all_matched],
        saving_all=[request.saving_ms for request in all_matched],
        speedup_all=[request.speedup_ratio for request in all_matched],
    )


def draw_replay_latency(
    axis,
    data: ReplayPlotData,
    *,
    panel_label: str = "a",
    title: str | None = "Replay latency",
) -> None:
    pooled_medians = []
    for index, (language, capability) in enumerate(data.groups):
        y = len(data.groups) - 1 - index
        values = data.requests.get((language, capability), [])
        static = _median(request.static_ms for request in values)
        live = _median(request.live_ms for request in values)
        speedup = _median(request.speedup_ratio for request in values)
        if static is None or live is None or speedup is None:
            continue
        pooled_medians.extend((static, live))
        pairs = _snapshot_medians(values)
        offsets = np.linspace(-0.18, 0.18, len(pairs)) if pairs else []
        for offset, (static_pair, live_pair) in zip(offsets, pairs, strict=True):
            axis.plot(
                [static_pair, live_pair],
                [y + offset, y + offset],
                color=PAIR_COLOR,
                alpha=0.18,
                linewidth=0.45,
                zorder=1,
            )
            axis.scatter(
                static_pair,
                y + offset,
                s=6,
                color=STATIC_COLOR,
                alpha=0.28,
                linewidths=0,
                zorder=2,
            )
            axis.scatter(
                live_pair,
                y + offset,
                s=7,
                marker="^",
                color=LIVE_COLOR,
                alpha=0.28,
                linewidths=0,
                zorder=2,
            )
        axis.plot([static, live], [y, y], color=NEUTRAL_COLOR, linewidth=1.05, zorder=3)
        axis.scatter(
            static,
            y,
            s=25,
            color=STATIC_COLOR,
            edgecolor=NEUTRAL_COLOR,
            linewidth=0.3,
            zorder=4,
        )
        axis.scatter(
            live,
            y,
            s=34,
            marker="^",
            color=LIVE_COLOR,
            edgecolor=NEUTRAL_COLOR,
            linewidth=0.3,
            zorder=4,
        )
        axis.text(
            1.01,
            y,
            f"{speedup:.1f}x",
            transform=axis.get_yaxis_transform(),
            ha="left",
            va="center",
            fontsize=6.8,
            fontweight="bold",
        )

    labels = []
    for language, capability in data.groups:
        matched, total = data.coverage.get((language, capability), (0, 0))
        rate = matched / total if total else 0
        labels.append(
            f"{LANGUAGE_LABELS[language]} {CAPABILITY_LABELS[capability]}  {rate:.0%}"
        )
    axis.set_yticks(range(len(data.groups)))
    axis.set_yticklabels(reversed(labels))
    axis.set_xscale("log")
    lower = max(0.03, min(pooled_medians) / 2)
    upper = max(pooled_medians) * 2.5
    axis.set_xlim(lower, upper)
    axis.set_ylim(-0.55, len(data.groups) - 0.45)
    style_axis(axis, grid_axis="x", minor_grid=True, open_frame=True)
    axis.set_xlabel("Warm request latency (ms, log scale)")
    if title:
        panel_title(axis, panel_label, title)
    axis.text(
        1.015,
        1.01,
        "median\nlive/static",
        transform=axis.transAxes,
        ha="left",
        va="bottom",
        fontsize=6.5,
        color="#555555",
    )


def draw_matched_ecdf(
    axis,
    data: ReplayPlotData,
    *,
    panel_label: str = "b",
) -> None:
    for values, color, linestyle, label in (
        (data.static_all, STATIC_COLOR, "-", "CodeMiner static"),
        (data.live_all, LIVE_COLOR, "--", "Live JSON-RPC"),
    ):
        ordered = np.sort(values)
        fractions = np.arange(1, len(ordered) + 1) / len(ordered)
        axis.step(
            ordered,
            fractions,
            where="post",
            color=color,
            linestyle=linestyle,
            linewidth=1.4,
            label=label,
        )
    axis.set_xscale("log")
    axis.set_xlim(
        max(0.03, min(data.static_all + data.live_all) / 1.5),
        _percentile(data.static_all + data.live_all, 99.5) * 1.4,
    )
    axis.set_ylim(0, 1.02)
    axis.set_yticks((0, 0.25, 0.5, 0.75, 1.0))
    style_axis(axis, open_frame=True)
    axis.set_xlabel("Warm request latency (ms, log scale)")
    axis.set_ylabel("Fraction of matched requests")
    panel_title(axis, panel_label, "Matched-request ECDF")
    axis.text(
        0.02,
        0.96,
        (
            f"Static p50 {_median(data.static_all):.2f} ms\n"
            f"Live p50 {_median(data.live_all):.2f} ms"
        ),
        transform=axis.transAxes,
        ha="left",
        va="top",
        bbox={"facecolor": "white", "edgecolor": "#C8C3BF", "pad": 1.6},
    )


def lsp_legend_handles() -> list[Line2D]:
    return [
        Line2D(
            [],
            [],
            color=STATIC_COLOR,
            marker="o",
            markeredgecolor=NEUTRAL_COLOR,
            markeredgewidth=0.3,
            label="CodeMiner static",
        ),
        Line2D(
            [],
            [],
            color=LIVE_COLOR,
            linestyle="--",
            marker="^",
            markeredgecolor=NEUTRAL_COLOR,
            markeredgewidth=0.3,
            label="Live JSON-RPC",
        ),
    ]


def draw_replay_panels(
    fig,
    replay_axis,
    ecdf_axis,
    reports: Sequence[Mapping[str, Any]],
    *,
    panel_labels: tuple[str, str] = ("a", "b"),
    show_summary: bool = False,
    show_legend: bool = False,
) -> None:
    data = prepare_replay_data(reports)
    draw_replay_latency(replay_axis, data, panel_label=panel_labels[0])
    draw_matched_ecdf(ecdf_axis, data, panel_label=panel_labels[1])

    matched_requests = sum(value[0] for value in data.coverage.values())
    total_requests = sum(value[1] for value in data.coverage.values())
    summary_artist = fig.text(
        0.5,
        0.025,
        (
            f"{len(data.reports)} snapshots  |  {matched_requests}/{total_requests} "
            "requests matched  |  median live/static ratio "
            f"{_median(data.speedup_all):.2f}x"
        ),
        ha="center",
        va="bottom",
        fontsize=6.2,
        fontweight="semibold",
    )
    summary_artist.set_visible(show_summary)
    legend = fig.legend(
        handles=lsp_legend_handles(),
        loc="upper center",
        bbox_to_anchor=(0.5, 0.995),
        ncol=2,
        frameon=False,
    )
    legend.set_visible(show_legend)


def render_figure(reports: Sequence[Mapping[str, Any]], output_prefix: Path) -> None:
    fig = plt.figure(
        figsize=(DOUBLE_COLUMN_WIDTH, TWO_PANEL_HEIGHT), constrained_layout=False
    )
    grid = fig.add_gridspec(1, 2, width_ratios=(1.25, 1.0), wspace=0.38)
    replay_axis = fig.add_subplot(grid[0, 0])
    ecdf_axis = fig.add_subplot(grid[0, 1])
    draw_replay_panels(
        fig,
        replay_axis,
        ecdf_axis,
        reports,
        show_summary=True,
        show_legend=True,
    )
    fig.subplots_adjust(left=0.12, right=0.985, top=0.76, bottom=0.24)
    save_figure(fig, output_prefix)
    plt.close(fig)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", required=True, type=Path)
    parser.add_argument("--output-prefix", type=Path, default=DEFAULT_OUTPUT_PREFIX)
    args = parser.parse_args(argv)
    reports = _load_reports(args.reports_dir)
    render_figure(reports, args.output_prefix)
    print(f"rendered {len(reports)} reports to {args.output_prefix}.{{pdf,png}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
