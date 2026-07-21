#!/usr/bin/env python
"""Plot L0/L2 construction and dense-query latency profiles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from draw_query_profile import AXIS_LABELS, draw_half_violin
from draw_query_profile import load_profiles as load_query_profiles
from draw_query_profile import print_summary as print_query_summary
from plot_style import (
    DOUBLE_COLUMN_WIDTH,
    MODEL_COLORS,
    MODEL_DISPLAY_LABELS,
    MODEL_IDS,
    MODEL_LABELS,
    MODEL_LINESTYLES,
    MODEL_MARKERS,
    MODEL_ORDER,
    NEUTRAL_COLOR,
    aligned_panel_title,
    apply_paper_style,
    embedding_legend_handles,
    save_figure,
    style_axis,
)

DEFAULT_OUTPUT_PREFIX = Path(__file__).resolve().parent / "profile_l0_vs_l2"
PROFILE_SUITE_HEIGHT = 1.62

apply_paper_style()


def construction_time(sections: Mapping[str, float], level: str) -> float:
    """Normalize legacy split timers to the current combined build timer."""

    combined = f"vector_store_add_documents_{level}"
    if combined in sections:
        return sections[combined]
    embedding = sections.get(f"embedding_encode_{level}", np.nan)
    if not np.isfinite(embedding):
        return np.nan
    return embedding + sections.get(f"faiss_index_add_{level}", 0.0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--stats-file", type=Path, required=True)
    parser.add_argument("--query-dir", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, default=DEFAULT_OUTPUT_PREFIX)
    return parser.parse_args()


def load_profiles(data_dir: Path, stats_file: Path) -> pd.DataFrame:
    records = []
    for path in sorted(data_dir.glob("*.json")):
        with path.open() as handle:
            data = json.load(handle)
        model = data.get("embedding_model")
        if model not in MODEL_IDS or data.get("profile_tag") != "codeminer_base_test":
            continue
        sections = {
            section["label"]: section["total"] for section in data.get("sections", [])
        }
        records.append(
            {
                "instance_id": data.get("instance_id", ""),
                "model": MODEL_LABELS[model],
                "time_l0": construction_time(sections, "l0"),
                "time_l2": construction_time(sections, "l2"),
            }
        )

    with stats_file.open() as handle:
        stats = json.load(handle)
    loc_by_instance = {
        row["instance_id"]: (row.get("chunk_stats") or {}).get("total_loc")
        for row in stats
    }

    frame = pd.DataFrame(records)
    if frame.empty:
        raise ValueError(f"No matching profiles found in {data_dir}")
    frame["total_loc_k"] = frame["instance_id"].map(loc_by_instance) / 1000
    print(
        f"Loaded {len(frame)} runs "
        f"({frame['instance_id'].nunique()} instances x {frame['model'].nunique()} models)"
    )
    return frame


def scatter_with_fit(ax, x: np.ndarray, y: np.ndarray, model: str) -> None:
    mask = np.isfinite(x) & np.isfinite(y)
    x_values = x[mask]
    y_values = y[mask]
    ax.scatter(
        x_values,
        y_values,
        s=10,
        alpha=0.48,
        color=MODEL_COLORS[model],
        marker=MODEL_MARKERS[model],
        edgecolors=NEUTRAL_COLOR,
        linewidths=0.2,
        rasterized=True,
    )
    if len(x_values) <= 2:
        return
    slope, intercept = np.polyfit(x_values, y_values, 1)
    start = max(x_values.min(), -intercept / slope) if slope > 0 else x_values.min()
    fit_x = np.linspace(start, x_values.max(), 100)
    fit_y = slope * fit_x + intercept
    valid = fit_y >= 0
    ax.plot(
        fit_x[valid],
        fit_y[valid],
        color=MODEL_COLORS[model],
        linestyle=MODEL_LINESTYLES[model],
        linewidth=1.1,
        alpha=0.95,
    )


def render(frame: pd.DataFrame, query_frame: pd.DataFrame, output_prefix: Path) -> None:
    fig, axes = plt.subplots(
        1,
        3,
        figsize=(DOUBLE_COLUMN_WIDTH, PROFILE_SUITE_HEIGHT),
        gridspec_kw={"width_ratios": [1.0, 1.0, 0.92], "wspace": 0.30},
    )

    for model in MODEL_ORDER:
        subset = frame[frame["model"] == model]
        scatter_with_fit(
            axes[0],
            subset["total_loc_k"].to_numpy(),
            subset["time_l0"].to_numpy(),
            model,
        )
        scatter_with_fit(
            axes[1],
            subset["total_loc_k"].to_numpy(),
            subset["time_l2"].to_numpy(),
            model,
        )

    for ax, panel, title in (
        (axes[0], "a", "L0 file-level embedding"),
        (axes[1], "b", "L2 callable-level embedding"),
    ):
        ax.set_xlabel(r"Total LOC ($\times 10^3$)")
        ax.set_ylabel("Build time (s)")
        aligned_panel_title(ax, panel, title)
        style_axis(ax, open_frame=True)
        ax.set_xlim(left=0)
        ax.set_ylim(bottom=0)

    query_ax = axes[2]
    for position, model in enumerate(MODEL_ORDER):
        values = query_frame.loc[query_frame["model"] == model, "query_ms"].to_numpy()
        draw_half_violin(
            query_ax,
            position,
            values,
            MODEL_COLORS[model],
            "",
        )
    query_ax.set_yscale("log")
    query_ax.set_xlim(-0.35, len(MODEL_ORDER) - 0.45)
    query_ax.set_xticks(range(len(MODEL_ORDER)))
    query_ax.set_xticklabels([AXIS_LABELS[model] for model in MODEL_ORDER])
    query_ax.set_ylabel("Query time (ms)")
    query_ax.tick_params(axis="x", labelsize=5.1)
    aligned_panel_title(query_ax, "c", "Warm query latency")
    style_axis(query_ax, grid_axis="y", minor_grid=True, open_frame=True)
    query_ax.text(
        0.01,
        0.985,
        "box = IQR; whiskers = P5–P95",
        transform=query_ax.transAxes,
        ha="left",
        va="top",
        fontsize=4.7,
        color="#555555",
    )

    fig.legend(
        handles=embedding_legend_handles(
            marker_size=3.8, display_labels=MODEL_DISPLAY_LABELS
        ),
        loc="upper center",
        bbox_to_anchor=(0.5, 0.995),
        ncol=5,
        frameon=False,
    )
    fig.subplots_adjust(left=0.065, right=0.995, top=0.76, bottom=0.20)
    for output in save_figure(fig, output_prefix):
        print(f"Saved: {output}")
    plt.close(fig)


def print_summary(frame: pd.DataFrame) -> None:
    for level in ("l0", "l2"):
        print(f"\n{level.upper()} time (s):")
        summary = (
            frame.groupby("model")[f"time_{level}"]
            .agg(["count", "mean", "median", "std", "min", "max"])
            .reindex(MODEL_ORDER)
            .round(1)
        )
        print(summary.to_string())


def main() -> None:
    args = parse_args()
    frame = load_profiles(args.data_dir, args.stats_file)
    query_frame = load_query_profiles(args.query_dir)
    print_summary(frame)
    print_query_summary(query_frame)
    render(frame, query_frame, args.output_prefix)


if __name__ == "__main__":
    main()
