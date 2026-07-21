#!/usr/bin/env python
"""Plot controlled FAISS index-family accuracy and latency trade-offs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from plot_style import (
    BASELINE_MID,
    DOUBLE_COLUMN_WIDTH,
    NEUTRAL_COLOR,
    PRIMARY_COLOR,
    apply_paper_style,
    panel_title,
    save_figure,
    style_axis,
)

DEFAULT_OUTPUT_PREFIX = Path(__file__).resolve().parent / "faiss_index_ablation"

FAMILY_COLORS = {
    "Flat": NEUTRAL_COLOR,
    "IVF-Flat": BASELINE_MID,
    "HNSW-Flat": PRIMARY_COLOR,
}
FAMILY_MARKERS = {"Flat": "D", "IVF-Flat": "o", "HNSW-Flat": "s"}
FAMILY_LABELS = {"Flat": "Flat", "IVF-Flat": "IVF", "HNSW-Flat": "HNSW"}
BOOTSTRAP_SAMPLES = 20_000
BOOTSTRAP_SEED = 20260713

apply_paper_style()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, default=DEFAULT_OUTPUT_PREFIX)
    parser.add_argument("--accuracy-guardrail", type=float, default=0.95)
    parser.add_argument("--bootstrap-samples", type=int, default=BOOTSTRAP_SAMPLES)
    return parser.parse_args()


def load_results(path: Path) -> dict:
    with path.open() as handle:
        results = json.load(handle)
    successful = [
        instance for instance in results.get("instances", []) if "methods" in instance
    ]
    if not successful:
        raise ValueError(f"No successful index-family results found in {path}")
    results["instances"] = successful
    return results


def method_sort_key(item: tuple[str, dict]) -> float:
    name, row = item
    parameters = row["parameters"]
    if row["family"] == "IVF-Flat":
        return float(parameters["probe_fraction"])
    if row["family"] == "HNSW-Flat":
        return float(parameters["ef_search"])
    return 0.0


def methods_by_family(summary: dict, family: str) -> list[tuple[str, dict]]:
    rows = [
        (name, row)
        for name, row in summary["methods"].items()
        if row["family"] == family
    ]
    return sorted(rows, key=method_sort_key)


def method_parameter_label(method_name: str, row: dict) -> str:
    if row["family"] == "IVF-Flat":
        return f"{100 * row['parameters']['probe_fraction']:.0f}%"
    if row["family"] == "HNSW-Flat":
        return f"ef={row['parameters']['ef_search']}"
    return method_name


def select_guardrailed_method(
    summary: dict, family: str, accuracy_guardrail: float
) -> tuple[str, dict]:
    candidates = [
        item
        for item in methods_by_family(summary, family)
        if item[1]["mean_ann_recall_at_k"] >= accuracy_guardrail
    ]
    if not candidates:
        raise ValueError(
            f"No {family} configuration reaches Recall@10 >= {accuracy_guardrail:.2f}"
        )
    return min(candidates, key=lambda item: item[1]["mean_search_ms"])


def binned_medians(
    vector_counts: np.ndarray, speedups: np.ndarray, bins: int = 6
) -> tuple[np.ndarray, np.ndarray]:
    edges = np.geomspace(vector_counts.min(), vector_counts.max() * 1.0001, bins + 1)
    x_values = []
    y_values = []
    for left, right in zip(edges[:-1], edges[1:], strict=True):
        mask = (vector_counts >= left) & (vector_counts < right)
        if np.any(mask):
            x_values.append(float(np.median(vector_counts[mask])))
            y_values.append(float(np.median(speedups[mask])))
    return np.asarray(x_values), np.asarray(y_values)


def clustered_frontier_intervals(
    results: dict,
    *,
    bootstrap_samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, tuple[float, float, float, float]]:
    """Bootstrap mean latency/recall by source-repository cluster."""
    if bootstrap_samples <= 0:
        raise ValueError("bootstrap_samples must be positive")
    instances = results["instances"]
    repositories = sorted({row["repo"] for row in instances})
    groups = [
        np.asarray(
            [index for index, row in enumerate(instances) if row["repo"] == repo],
            dtype=int,
        )
        for repo in repositories
    ]
    rng = np.random.default_rng(seed)
    draws = rng.multinomial(
        len(repositories),
        np.full(len(repositories), 1.0 / len(repositories)),
        size=bootstrap_samples,
    )
    cluster_sizes = np.asarray([len(group) for group in groups], dtype=float)
    denominators = draws @ cluster_sizes

    intervals = {}
    for method_name in results["summary"]["methods"]:
        latency = np.asarray(
            [row["methods"][method_name]["search"]["mean_ms"] for row in instances]
        )
        recall = np.asarray(
            [row["methods"][method_name]["ann_recall_at_k"] for row in instances]
        )
        latency_sums = np.asarray([latency[group].sum() for group in groups])
        recall_sums = np.asarray([recall[group].sum() for group in groups])
        latency_means = (draws @ latency_sums) / denominators
        recall_means = (draws @ recall_sums) / denominators
        latency_low, latency_high = np.quantile(latency_means, (0.025, 0.975))
        recall_low, recall_high = np.quantile(recall_means, (0.025, 0.975))
        intervals[method_name] = (
            float(latency_low),
            float(latency_high),
            float(recall_low),
            float(recall_high),
        )
    return intervals


def plot_accuracy_frontier(
    ax,
    results: dict,
    accuracy_guardrail: float,
    *,
    panel_label: str = "a",
    compact: bool = False,
    bootstrap_samples: int = BOOTSTRAP_SAMPLES,
) -> None:
    summary = results["summary"]
    intervals = clustered_frontier_intervals(
        results,
        bootstrap_samples=bootstrap_samples,
    )
    for family in ("IVF-Flat", "HNSW-Flat"):
        rows = methods_by_family(summary, family)
        latency = [row["mean_search_ms"] for _, row in rows]
        accuracy = [row["mean_ann_recall_at_k"] for _, row in rows]
        ax.plot(
            latency,
            accuracy,
            color=FAMILY_COLORS[family],
            marker=FAMILY_MARKERS[family],
            markersize=4.6,
            markeredgecolor=NEUTRAL_COLOR,
            markeredgewidth=0.3,
            label=FAMILY_LABELS[family],
            zorder=3,
        )
        for method_name, row in rows:
            latency_low, latency_high, recall_low, recall_high = intervals[method_name]
            ax.errorbar(
                row["mean_search_ms"],
                row["mean_ann_recall_at_k"],
                xerr=[
                    [row["mean_search_ms"] - latency_low],
                    [latency_high - row["mean_search_ms"]],
                ],
                yerr=[
                    [row["mean_ann_recall_at_k"] - recall_low],
                    [recall_high - row["mean_ann_recall_at_k"]],
                ],
                fmt="none",
                ecolor=FAMILY_COLORS[family],
                elinewidth=0.65,
                capsize=1.2,
                capthick=0.6,
                alpha=0.58,
                zorder=2,
            )

        _selected_name, selected = select_guardrailed_method(
            summary, family, accuracy_guardrail
        )
        ax.scatter(
            selected["mean_search_ms"],
            selected["mean_ann_recall_at_k"],
            s=58,
            marker=FAMILY_MARKERS[family],
            facecolors="none",
            edgecolors=FAMILY_COLORS[family],
            linewidths=1.05,
            zorder=4,
        )

        if compact and family == "HNSW-Flat":
            labels = (rows[0], rows[-1])
        elif compact:
            labels = tuple(
                item
                for item in rows
                if item[1]["parameters"]["probe_fraction"] in {0.01, 0.25, 1.0}
            )
        else:
            labels = rows if family == "HNSW-Flat" else rows[::2]
        for label_index, (method_name, row) in enumerate(labels):
            if family == "HNSW-Flat":
                if compact and label_index == len(labels) - 1:
                    offset = (-3, 4)
                    horizontal_alignment = "right"
                else:
                    offset = (3, -10)
                    horizontal_alignment = "left"
            elif row["parameters"]["probe_fraction"] == 0.25:
                offset = (4, -2) if compact else (3, -12)
                horizontal_alignment = "left"
            else:
                offset = (3, 4)
                horizontal_alignment = "left"
            ax.annotate(
                method_parameter_label(method_name, row),
                (row["mean_search_ms"], row["mean_ann_recall_at_k"]),
                xytext=offset,
                textcoords="offset points",
                ha=horizontal_alignment,
                fontsize=6.3,
                color=FAMILY_COLORS[family],
            )

    flat = summary["methods"]["flat"]
    flat_latency_low, flat_latency_high, _, _ = intervals["flat"]
    ax.errorbar(
        flat["mean_search_ms"],
        flat["mean_ann_recall_at_k"],
        xerr=[
            [flat["mean_search_ms"] - flat_latency_low],
            [flat_latency_high - flat["mean_search_ms"]],
        ],
        fmt="none",
        ecolor=FAMILY_COLORS["Flat"],
        elinewidth=0.65,
        capsize=1.2,
        capthick=0.6,
        alpha=0.58,
        zorder=2,
    )
    ax.scatter(
        flat["mean_search_ms"],
        flat["mean_ann_recall_at_k"],
        s=31,
        marker=FAMILY_MARKERS["Flat"],
        color=FAMILY_COLORS["Flat"],
        edgecolor=NEUTRAL_COLOR,
        linewidth=0.35,
        label="Flat (exact)",
        zorder=4,
    )
    ax.annotate(
        "exact",
        (flat["mean_search_ms"], flat["mean_ann_recall_at_k"]),
        xytext=(0, -18) if compact else (-4, -11),
        textcoords="offset points",
        ha="center" if compact else "right",
        fontsize=6.3,
        color=FAMILY_COLORS["Flat"],
    )
    ax.axhline(
        accuracy_guardrail,
        color="#888888",
        linestyle="--",
        linewidth=0.8,
        zorder=1,
    )
    ax.text(
        0.03 if compact else 0.97,
        accuracy_guardrail - 0.012,
        f"{accuracy_guardrail:.2f} guardrail",
        transform=ax.get_yaxis_transform(),
        ha="left" if compact else "right",
        va="top",
        fontsize=6.4,
        color="#666666",
    )
    ax.text(
        0.97,
        0.04,
        "100 snapshots x 100 repeats",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=5.8,
        color="#666666",
    )
    ax.set_xscale("log")
    ax.set_ylim(0.63, 1.018)
    ax.set_xlabel("Mean search latency (ms/query)")
    ax.set_ylabel("Neighbor Recall@10 vs. Flat")
    panel_title(ax, panel_label, "ANN accuracy-latency frontier")
    style_axis(ax, minor_grid=True, open_frame=True)


def plot_scale_speedup(
    ax,
    results: dict,
    accuracy_guardrail: float,
    *,
    panel_label: str = "b",
) -> None:
    summary = results["summary"]
    instances = results["instances"]
    vector_counts = np.asarray([row["n_vectors"] for row in instances], dtype=float)

    for family in ("IVF-Flat", "HNSW-Flat"):
        method_name, method = select_guardrailed_method(
            summary, family, accuracy_guardrail
        )
        flat_latency = np.asarray(
            [row["methods"]["flat"]["search"]["mean_ms"] for row in instances]
        )
        method_latency = np.asarray(
            [row["methods"][method_name]["search"]["mean_ms"] for row in instances]
        )
        speedups = flat_latency / method_latency
        color = FAMILY_COLORS[family]
        marker = FAMILY_MARKERS[family]
        label = (
            f"{FAMILY_LABELS[family]} "
            f"({method_parameter_label(method_name, method)})"
        )
        ax.scatter(
            vector_counts,
            speedups,
            s=11,
            marker=marker,
            facecolors="none",
            edgecolors=color,
            linewidths=0.55,
            alpha=0.48,
            rasterized=True,
            zorder=2,
        )
        x_median, y_median = binned_medians(vector_counts, speedups)
        ax.plot(
            x_median,
            y_median,
            color=color,
            marker=marker,
            markersize=4.4,
            markeredgecolor=NEUTRAL_COLOR,
            markeredgewidth=0.3,
            linewidth=1.25,
            label=label,
            zorder=3,
        )

    ax.axhline(1.0, color="#777777", linestyle="--", linewidth=0.8)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Repository index size (vectors)")
    ax.set_ylabel("Search speedup over Flat")
    panel_title(ax, panel_label, "Guardrailed speedup by scale")
    style_axis(ax, minor_grid=True, open_frame=True)


def print_summary(results: dict, accuracy_guardrail: float) -> None:
    summary = results["summary"]
    flat = summary["methods"]["flat"]
    print(
        f"Flat: {flat['mean_search_ms']:.4f} ms, "
        f"task Recall@10={flat['mean_task_recall']:.3f}, "
        f"build={flat['mean_build_ms']:.1f} ms, "
        f"size={flat['mean_index_mb']:.2f} MiB"
    )
    for family in ("IVF-Flat", "HNSW-Flat"):
        method_name, row = select_guardrailed_method(
            summary, family, accuracy_guardrail
        )
        ratio = flat["mean_search_ms"] / row["mean_search_ms"]
        print(
            f"{method_name}: {row['mean_search_ms']:.4f} ms ({ratio:.1f}x), "
            f"neighbor Recall@10={row['mean_ann_recall_at_k']:.3f}, "
            f"task Recall@10={row['mean_task_recall']:.3f}, "
            f"build={row['mean_build_ms']:.1f} ms, "
            f"size={row['mean_index_mb']:.2f} MiB"
        )


def render(
    results: dict,
    output_prefix: Path,
    accuracy_guardrail: float,
    bootstrap_samples: int,
) -> None:
    fig, axes = plt.subplots(
        1,
        2,
        figsize=(DOUBLE_COLUMN_WIDTH, 2.36),
        gridspec_kw={"wspace": 0.34, "width_ratios": [1.03, 0.97]},
    )
    plot_accuracy_frontier(
        axes[0],
        results,
        accuracy_guardrail,
        bootstrap_samples=bootstrap_samples,
    )
    plot_scale_speedup(axes[1], results, accuracy_guardrail)
    handles = []
    for family in ("IVF-Flat", "HNSW-Flat"):
        method_name, method = select_guardrailed_method(
            results["summary"], family, accuracy_guardrail
        )
        handles.append(
            Line2D(
                [],
                [],
                color=FAMILY_COLORS[family],
                marker=FAMILY_MARKERS[family],
                markeredgecolor=NEUTRAL_COLOR,
                markeredgewidth=0.3,
                label=(
                    f"{FAMILY_LABELS[family]} "
                    f"({method_parameter_label(method_name, method)} selected)"
                ),
            )
        )
    handles.append(
        Line2D(
            [],
            [],
            color=FAMILY_COLORS["Flat"],
            marker=FAMILY_MARKERS["Flat"],
            linestyle="none",
            label="Flat (exact)",
        )
    )
    fig.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.995),
        ncol=3,
        frameon=False,
    )
    fig.subplots_adjust(left=0.075, right=0.995, top=0.76, bottom=0.20)
    for output in save_figure(fig, output_prefix):
        print(f"Saved: {output}")
    plt.close(fig)


def main() -> None:
    args = parse_args()
    results = load_results(args.results)
    print_summary(results, args.accuracy_guardrail)
    render(
        results,
        args.output_prefix,
        args.accuracy_guardrail,
        args.bootstrap_samples,
    )


if __name__ == "__main__":
    main()
