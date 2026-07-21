#!/usr/bin/env python3
"""Render the paper's single-column LSP replay-latency panel."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from draw_lsp_replay import (
    _load_reports,
    draw_replay_latency,
    lsp_legend_handles,
    prepare_replay_data,
)
from plot_style import SINGLE_COLUMN_WIDTH, apply_paper_style, save_figure

DEFAULT_OUTPUT_PREFIX = Path(__file__).resolve().parent / "lsp_replay_latency"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", required=True, type=Path)
    parser.add_argument("--output-prefix", type=Path, default=DEFAULT_OUTPUT_PREFIX)
    parser.add_argument("--panel-label", default="a")
    return parser.parse_args()


def render(args: argparse.Namespace) -> None:
    data = prepare_replay_data(_load_reports(args.reports_dir))
    apply_paper_style()
    # A shallow landscape canvas matches this single horizontal comparison:
    # keep labels at their native paper size while reducing unused vertical
    # weight in the one-column manuscript placement.
    figure, axis = plt.subplots(figsize=(SINGLE_COLUMN_WIDTH, 1.82))
    draw_replay_latency(axis, data, panel_label=args.panel_label, title=None)
    figure.legend(
        handles=lsp_legend_handles(),
        loc="upper center",
        bbox_to_anchor=(0.57, 0.995),
        ncol=2,
        frameon=False,
        columnspacing=0.7,
        handletextpad=0.25,
    )
    figure.subplots_adjust(left=0.30, right=0.86, top=0.84, bottom=0.19)
    for output in save_figure(figure, args.output_prefix):
        print(f"Saved: {output}")
    plt.close(figure)


def main() -> None:
    render(parse_args())


if __name__ == "__main__":
    main()
