#!/usr/bin/env python
"""Plot dense-query latency distributions by embedding model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Rectangle
from plot_style import (
    MODEL_COLORS,
    MODEL_HATCHES,
    MODEL_IDS,
    MODEL_LABELS,
    MODEL_ORDER,
    MODEL_PARAMETER_LABELS,
    NEUTRAL_COLOR,
    SINGLE_COLUMN_WIDTH,
    apply_paper_style,
    save_figure,
    style_axis,
)
from scipy.stats import gaussian_kde

DEFAULT_OUTPUT_PREFIX = Path(__file__).resolve().parent / "profile_query_time"

AXIS_LABELS = {
    "SweRank-Small": f"SR-Small\n{MODEL_PARAMETER_LABELS['SweRank-Small']} · 768d",
    "Qwen3-Embed-0.6B": "Qwen-0.6B\n1024d",
    "Jina-Code-1.5B": "Jina-1.5B\n1536d",
    "Qwen3-Embed-4B": "Qwen-4B\n2560d",
    "SweRank-Large": f"SR-Large\n{MODEL_PARAMETER_LABELS['SweRank-Large']} · 3584d",
}

apply_paper_style()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query-dir", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, default=DEFAULT_OUTPUT_PREFIX)
    return parser.parse_args()


def load_profiles(query_dir: Path) -> pd.DataFrame:
    records = []
    for path in sorted(query_dir.glob("codeminer_base__embedding_baseline__*.json")):
        with path.open() as handle:
            data = json.load(handle)
        model = data.get("embedding_model")
        if model not in MODEL_IDS:
            continue
        for instance in data.get("instances", []):
            sections = {
                section["label"]: section["total"]
                for section in instance.get("sections", [])
            }
            records.append(
                {
                    "instance_id": instance["instance_id"],
                    "model": MODEL_LABELS[model],
                    "query_ms": 1000 * sections.get("pipeline.query", np.nan),
                }
            )
    frame = pd.DataFrame(records)
    if frame.empty:
        raise ValueError(f"No dense-baseline query profiles found in {query_dir}")
    print(
        f"Loaded {len(frame)} runs "
        f"({frame['instance_id'].nunique()} instances x {frame['model'].nunique()} models)"
    )
    return frame


def draw_half_violin(
    ax, position: int, values: np.ndarray, color: str, hatch: str
) -> None:
    values = values[np.isfinite(values) & (values > 0)]
    if not len(values):
        return
    log_values = np.log10(values)
    if len(np.unique(log_values)) > 1:
        kde = gaussian_kde(log_values, bw_method=0.3)
        grid = np.linspace(log_values.min() - 0.25, log_values.max() + 0.25, 200)
        density = kde(grid)
        density = density / density.max() * 0.32
        ax.fill_betweenx(
            10**grid,
            position,
            position + density,
            alpha=0.22,
            color=color,
            linewidth=0,
        )
        ax.plot(
            position + density,
            10**grid,
            color=color,
            linewidth=0.85,
            alpha=0.9,
        )

    p05, q25, median, q75, p95 = np.percentile(values, [5, 25, 50, 75, 95])
    summary_x = position - 0.09
    ax.vlines(summary_x, p05, p95, color=color, linewidth=0.9, zorder=3)
    ax.hlines(
        [p05, p95], summary_x - 0.035, summary_x + 0.035, color=color, linewidth=0.9
    )
    ax.add_patch(
        Rectangle(
            (position - 0.16, q25),
            0.14,
            q75 - q25,
            facecolor=color,
            edgecolor=NEUTRAL_COLOR,
            linewidth=0.45,
            alpha=0.42,
            hatch=hatch,
            zorder=3,
        )
    )
    ax.hlines(
        median,
        position - 0.16,
        position - 0.02,
        color=color,
        linewidth=1.4,
        zorder=4,
    )


def render(frame: pd.DataFrame, output_prefix: Path) -> None:
    fig, query_ax = plt.subplots(figsize=(SINGLE_COLUMN_WIDTH, 2.25))
    for position, model in enumerate(MODEL_ORDER):
        values = frame.loc[frame["model"] == model, "query_ms"].to_numpy()
        draw_half_violin(
            query_ax,
            position,
            values,
            MODEL_COLORS[model],
            MODEL_HATCHES[model],
        )
    query_ax.set_yscale("log")
    query_ax.set_xlim(-0.35, len(MODEL_ORDER) - 0.45)
    query_ax.set_xticks(range(len(MODEL_ORDER)))
    query_ax.set_xticklabels([AXIS_LABELS[model] for model in MODEL_ORDER])
    query_ax.set_ylabel("Query time (ms)")
    style_axis(query_ax, grid_axis="y", minor_grid=True, open_frame=True)
    query_ax.tick_params(axis="x", labelsize=5.8)
    query_ax.text(
        0.01,
        0.985,
        "hatched box = IQR; whiskers = P5–P95",
        transform=query_ax.transAxes,
        ha="left",
        va="top",
        fontsize=5.2,
        color="#555555",
    )

    fig.subplots_adjust(left=0.16, right=0.99, top=0.96, bottom=0.24)
    for output in save_figure(fig, output_prefix):
        print(f"Saved: {output}")
    plt.close(fig)


def print_summary(frame: pd.DataFrame) -> None:
    print("\nQuery latency (ms):")
    summary = (
        frame.groupby("model")["query_ms"]
        .agg(["count", "mean", "median", "std", "min", "max"])
        .reindex(MODEL_ORDER)
        .round(1)
    )
    print(summary.to_string())


def main() -> None:
    args = parse_args()
    frame = load_profiles(args.query_dir)
    print_summary(frame)
    render(frame, args.output_prefix)


if __name__ == "__main__":
    main()
