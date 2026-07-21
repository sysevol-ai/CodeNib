#!/usr/bin/env python3
"""Render the compact retrieval-subsystem ablation suite used by the paper."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from draw_faiss_index_ablation import (
    FAMILY_COLORS,
    FAMILY_LABELS,
    FAMILY_MARKERS,
    load_results,
    method_parameter_label,
    plot_accuracy_frontier,
    plot_scale_speedup,
    select_guardrailed_method,
)
from draw_graphrag_rerank import EMBEDDINGS, GRAPH_PAIRS, _paired_effect, load_matrix
from matplotlib.lines import Line2D
from plot_style import (
    BASELINE_COLOR,
    DOUBLE_COLUMN_WIDTH,
    MODEL_PARAMETER_LABELS,
    NEUTRAL_COLOR,
    PRIMARY_COLOR,
    apply_paper_style,
    panel_title,
    save_figure,
    style_axis,
)

DEFAULT_OUTPUT_PREFIX = Path(__file__).resolve().parent / "retrieval_ablation_suite"
GRAPH_MODEL_AXIS_LABELS = {
    "SweRank-Small": f"SR-Small\n{MODEL_PARAMETER_LABELS['SweRank-Small']}",
    "Qwen3-Embed-0.6B": "Qwen\n0.6B",
    "Jina-Code-1.5B": "Jina\n1.5B",
    "Qwen3-Embed-4B": "Qwen\n4B",
    "SweRank-Large": f"SR-Large\n{MODEL_PARAMETER_LABELS['SweRank-Large']}",
}
GRAPH_SERIES_STYLES = (
    ("Before rerank", BASELINE_COLOR, "o", -0.11),
    ("After rerank", PRIMARY_COLOR, "s", 0.11),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-result-dir", required=True, type=Path)
    parser.add_argument("--graph-selection-results", type=Path)
    parser.add_argument("--faiss-results", required=True, type=Path)
    parser.add_argument("--output-prefix", type=Path, default=DEFAULT_OUTPUT_PREFIX)
    parser.add_argument("--partition", choices=("test", "all"), default="test")
    parser.add_argument("--bootstrap-samples", type=int, default=20_000)
    parser.add_argument("--accuracy-guardrail", type=float, default=0.95)
    return parser.parse_args()


def _weight_label(weight: float) -> str:
    return f"{weight:g}".replace("-", "m").replace(".", "p")


def select_graph_weight(selection: dict[str, Any]) -> float:
    """Reproduce the development-set graph-weight selection rule."""
    if selection.get("partition") != "dev":
        raise ValueError("graph selection results must use the dev partition")
    config = selection.get("config", {})
    weights = [float(value) for value in config.get("graph_weights", [])]
    if not weights:
        raise ValueError("graph selection results contain no graph weights")
    recalls = selection.get("summary", {}).get("recall", {})

    def score(weight: float) -> tuple[int, int, int]:
        method = f"dense_graph_fusion_w{_weight_label(weight)}"
        metrics = recalls.get(method)
        if not isinstance(metrics, dict):
            raise ValueError(f"graph selection results are missing {method}")
        return tuple(int(metrics[f"files@{cutoff}"]["hits"]) for cutoff in (10, 5, 1))

    scores = {weight: score(weight) for weight in weights}
    best_score = max(scores.values())
    winners = [weight for weight, value in scores.items() if value == best_score]
    if len(winners) != 1:
        raise ValueError(f"graph selection rule is ambiguous: winners={winners}")
    return winners[0]


def validate_graph_selection(path: Path, expected_weight: float = 0.5) -> None:
    selection = json.loads(path.read_text(encoding="utf-8"))
    selected = select_graph_weight(selection)
    if not np.isclose(selected, expected_weight):
        raise ValueError(
            f"development sweep selected graph weight {selected:g}, "
            f"expected {expected_weight:g}"
        )


def _faiss_legend_handles(results: dict, guardrail: float) -> list[Line2D]:
    handles = []
    for family in ("IVF-Flat", "HNSW-Flat"):
        method_name, method = select_guardrailed_method(
            results["summary"], family, guardrail
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
                    f"({method_parameter_label(method_name, method)})"
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
            label="Flat",
        )
    )
    return handles


def _plot_graph_effect_compact(
    axis,
    matrix: list[dict],
    bootstrap_samples: int,
) -> None:
    """Show paired graph effects with embedding identity on the x axis."""
    rng = np.random.default_rng(20260713)
    bounds = []
    positions = np.arange(len(matrix), dtype=float)
    for pair_index, (baseline, graph, _label, _color) in enumerate(GRAPH_PAIRS):
        series_label, color, marker, offset = GRAPH_SERIES_STYLES[pair_index]
        for model_index, data in enumerate(matrix):
            effect, low, high = _paired_effect(
                data, baseline, graph, bootstrap_samples, rng
            )
            bounds.extend((low, high))
            axis.errorbar(
                positions[model_index] + offset,
                effect,
                yerr=[[effect - low], [high - effect]],
                fmt=marker,
                color=color,
                markeredgecolor=NEUTRAL_COLOR,
                markeredgewidth=0.35,
                markersize=4.2,
                elinewidth=1.0,
                capsize=1.8,
                capthick=0.85,
                label=series_label if model_index == 0 else None,
                zorder=3,
            )

    axis.axhline(0, color="#777777", linewidth=0.8, zorder=0)
    lower = min(-10.0, np.floor(min(bounds) / 5.0) * 5.0)
    upper = max(25.0, np.ceil(max(bounds) / 5.0) * 5.0)
    axis.set_xlim(-0.45, len(matrix) - 0.55)
    axis.set_ylim(lower, upper)
    axis.set_xticks(
        positions,
        [
            GRAPH_MODEL_AXIS_LABELS[
                EMBEDDINGS[data["config"]["embedding_model"]]["label"]
            ]
            for data in matrix
        ],
    )
    axis.tick_params(axis="x", labelsize=4.9, pad=1.5)
    axis.set_ylabel(r"$\Delta$ File Success@10 (pp)")
    panel_title(axis, "a", "Graph expansion effect")
    style_axis(axis, grid_axis="y", open_frame=True)
    axis.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, 0.995),
        ncols=2,
        fontsize=4.7,
        handlelength=1.0,
        columnspacing=0.7,
        borderaxespad=0.2,
    )


def render(args: argparse.Namespace) -> None:
    if args.bootstrap_samples <= 0:
        raise ValueError("--bootstrap-samples must be positive")
    if args.graph_selection_results is not None:
        validate_graph_selection(args.graph_selection_results)
    matrix = load_matrix(args.graph_result_dir, args.partition)
    faiss = load_results(args.faiss_results)

    apply_paper_style()
    figure, (graph_effect, faiss_frontier, faiss_scale) = plt.subplots(
        1,
        3,
        figsize=(DOUBLE_COLUMN_WIDTH, 2.22),
        gridspec_kw={"width_ratios": (0.82, 1.05, 1.28), "wspace": 0.30},
    )

    _plot_graph_effect_compact(
        graph_effect,
        matrix,
        args.bootstrap_samples,
    )
    plot_accuracy_frontier(
        faiss_frontier,
        faiss,
        args.accuracy_guardrail,
        panel_label="b",
        compact=True,
        bootstrap_samples=args.bootstrap_samples,
    )
    plot_scale_speedup(
        faiss_scale,
        faiss,
        args.accuracy_guardrail,
        panel_label="c",
    )

    faiss_scale.legend(
        handles=_faiss_legend_handles(faiss, args.accuracy_guardrail),
        loc="upper left",
        fontsize=5.6,
        handlelength=1.35,
    )
    figure.subplots_adjust(left=0.07, right=0.995, top=0.86, bottom=0.20)
    for output in save_figure(figure, args.output_prefix):
        print(f"Saved: {output}")
    plt.close(figure)


def main() -> None:
    render(parse_args())


if __name__ == "__main__":
    main()
