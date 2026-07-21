#!/usr/bin/env python3
"""Render static-LSP serving and lifecycle costs as one compact paper figure."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from draw_lsp_replay import _load_reports, draw_replay_panels
from draw_materialized_trace import (
    _draw_amortization,
    _draw_phase_costs,
    _validate_summary,
)
from matplotlib.lines import Line2D
from plot_style import (
    DOUBLE_COLUMN_WIDTH,
    LIVE_COLOR,
    NEUTRAL_COLOR,
    STATIC_COLOR,
    apply_paper_style,
    save_figure,
)

DEFAULT_OUTPUT_PREFIX = Path(__file__).resolve().parent / "serving_lifecycle_suite"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", required=True, type=Path)
    parser.add_argument("--lifecycle-summary", required=True, type=Path)
    parser.add_argument("--output-prefix", type=Path, default=DEFAULT_OUTPUT_PREFIX)
    return parser.parse_args()


def _lsp_legend_handles() -> list[Line2D]:
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


def render(args: argparse.Namespace) -> None:
    reports = _load_reports(args.reports_dir)
    summary = json.loads(args.lifecycle_summary.read_text(encoding="utf-8"))
    _validate_summary(summary)

    apply_paper_style()
    figure = plt.figure(figsize=(DOUBLE_COLUMN_WIDTH, 3.68))
    outer = figure.add_gridspec(2, 1, height_ratios=(1.12, 1.0), hspace=0.60)
    replay_grid = outer[0].subgridspec(1, 2, width_ratios=(1.25, 1.0), wspace=0.39)
    lifecycle_grid = outer[1].subgridspec(1, 2, width_ratios=(1.34, 1.0), wspace=0.34)
    replay_axis = figure.add_subplot(replay_grid[0, 0])
    ecdf_axis = figure.add_subplot(replay_grid[0, 1])
    phase_axis = figure.add_subplot(lifecycle_grid[0, 0])
    reuse_axis = figure.add_subplot(lifecycle_grid[0, 1])

    draw_replay_panels(
        figure,
        replay_axis,
        ecdf_axis,
        reports,
        panel_labels=("a", "b"),
    )
    _draw_phase_costs(phase_axis, summary["snapshot_rows"], panel_label="c")
    _draw_amortization(
        reuse_axis,
        summary,
        panel_label="d",
        legend_loc="lower left",
    )
    ecdf_axis.legend(
        handles=_lsp_legend_handles(),
        loc="lower right",
        fontsize=5.8,
        handlelength=1.6,
    )

    figure.subplots_adjust(left=0.12, right=0.985, top=0.965, bottom=0.115)
    for output in save_figure(figure, args.output_prefix):
        print(f"Saved: {output}")
    plt.close(figure)


def main() -> None:
    render(parse_args())


if __name__ == "__main__":
    main()
