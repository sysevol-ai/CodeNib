#!/usr/bin/env python
"""Plot file- and symbol-level retrieval accuracy across embeddings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from plot_style import (
    DOUBLE_COLUMN_WIDTH,
    MODEL_COLORS,
    MODEL_IDS,
    MODEL_LABELS,
    MODEL_LINESTYLES,
    MODEL_MARKERS,
    MODEL_ORDER,
    NEUTRAL_COLOR,
    TWO_PANEL_HEIGHT,
    apply_paper_style,
    embedding_legend_handles,
    panel_title,
    save_figure,
    style_axis,
)

DEFAULT_OUTPUT_PREFIX = Path(__file__).resolve().parent / "eval_recall_at_k"
K_VALUES = (1, 3, 5, 10, 15, 20)

apply_paper_style()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-dir", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, default=DEFAULT_OUTPUT_PREFIX)
    return parser.parse_args()


def load_results(eval_dir: Path) -> dict[str, dict[str, list[float]]]:
    results = {}
    for model in MODEL_IDS:
        safe = model.replace("/", "__")
        path = eval_dir / f"eval_codeminer_base_test_{safe}.json"
        with path.open() as handle:
            rows = json.load(handle)
        if not rows or rows[0]["embedding_model"] != model:
            raise ValueError(f"Unexpected or empty evaluation file: {path}")

        label = MODEL_LABELS[model]
        results[label] = {
            level: [
                float(
                    np.mean([row["metrics"][level][str(k)]["recall"] for row in rows])
                )
                for k in K_VALUES
            ]
            for level in ("files", "symbols")
        }
        print(f"Loaded {label}: {len(rows)} instances")
    return results


def render(results: dict[str, dict[str, list[float]]], output_prefix: Path) -> None:
    fig, axes = plt.subplots(
        1,
        2,
        figsize=(DOUBLE_COLUMN_WIDTH, TWO_PANEL_HEIGHT),
        sharey=True,
        gridspec_kw={"wspace": 0.24},
    )

    for ax, level, label, title in (
        (axes[0], "files", "a", "File-level retrieval"),
        (axes[1], "symbols", "b", "Symbol-level retrieval"),
    ):
        for model in MODEL_ORDER:
            ax.plot(
                K_VALUES,
                results[model][level],
                color=MODEL_COLORS[model],
                linestyle=MODEL_LINESTYLES[model],
                marker=MODEL_MARKERS[model],
                markersize=3.5,
                markeredgecolor=NEUTRAL_COLOR,
                markeredgewidth=0.3,
                linewidth=1.1,
            )
        ax.set_xlim(0.3, 20.7)
        ax.set_ylim(0.1, 1.0)
        ax.set_xticks(K_VALUES)
        ax.set_xlabel("Retrieval cutoff $k$")
        panel_title(ax, label, title)
        style_axis(ax, grid_axis="y", open_frame=True)

    axes[0].set_ylabel("Recall@k")
    fig.legend(
        handles=embedding_legend_handles(),
        loc="upper center",
        bbox_to_anchor=(0.5, 0.995),
        ncol=5,
        frameon=False,
        handletextpad=0.3,
        columnspacing=0.8,
    )
    fig.subplots_adjust(left=0.075, right=0.995, top=0.76, bottom=0.20)
    for output in save_figure(fig, output_prefix):
        print(f"Saved: {output}")
    plt.close(fig)


def print_summary(results: dict[str, dict[str, list[float]]]) -> None:
    print("\nlevel,model," + ",".join(f"recall@{k}" for k in K_VALUES))
    for level in ("files", "symbols"):
        for model in MODEL_ORDER:
            values = ",".join(f"{value:.3f}" for value in results[model][level])
            print(f"{level},{model},{values}")


def main() -> None:
    args = parse_args()
    results = load_results(args.eval_dir)
    print_summary(results)
    render(results, args.output_prefix)


if __name__ == "__main__":
    main()
