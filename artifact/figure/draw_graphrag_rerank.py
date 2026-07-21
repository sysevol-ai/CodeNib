#!/usr/bin/env python
"""Plot the controlled GraphRAG embedding and reranker ablation.

All points come from the held-out, repository-disjoint test partition of one
evaluator protocol over 100 CodeMiner Base snapshots. The upper panel reports
GraphRAG's paired effect with repository-clustered intervals; the lower panel
shows the corresponding accuracy/latency trade-off.
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from plot_style import (
    BASELINE_COLOR,
    DOUBLE_COLUMN_WIDTH,
    MODEL_COLORS,
    MODEL_IDS,
    MODEL_LABELS,
    NEUTRAL_COLOR,
    PRIMARY_COLOR,
    TWO_PANEL_HEIGHT,
    apply_paper_style,
    panel_title,
    save_figure,
    style_axis,
)

DEFAULT_OUTPUT_PREFIX = Path(__file__).resolve().parent / "graphrag_vs_rerank"

EMBEDDINGS = {
    model: {"label": MODEL_LABELS[model], "color": MODEL_COLORS[MODEL_LABELS[model]]}
    for model in MODEL_IDS
}

ARMS = {
    "dense": {
        "label": "Dense",
        "marker": "o",
        "graph": False,
        "reranked": False,
    },
    "dense_graph_fusion_w0p5": {
        "label": "Dense + Graph",
        "marker": "X",
        "graph": True,
        "reranked": False,
    },
    "dense_cross_encoder": {
        "label": "Dense + Reranker",
        "marker": "^",
        "graph": False,
        "reranked": True,
    },
    "dense_graph_fusion_w0p5_cross_encoder": {
        "label": "Dense + Graph + Reranker",
        "marker": "D",
        "graph": True,
        "reranked": True,
    },
}

GRAPH_PAIRS = (
    ("dense", "dense_graph_fusion_w0p5", "No reranker", BASELINE_COLOR),
    (
        "dense_cross_encoder",
        "dense_graph_fusion_w0p5_cross_encoder",
        "+ Reranker",
        PRIMARY_COLOR,
    ),
)

apply_paper_style()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--result-dir",
        type=Path,
        required=True,
        help="Directory containing the five v4 matrix result files.",
    )
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=DEFAULT_OUTPUT_PREFIX,
        help="Output path without an extension.",
    )
    parser.add_argument(
        "--partition",
        choices=("test", "all"),
        default="test",
        help="Analyze the held-out test partition or all recorded snapshots.",
    )
    parser.add_argument(
        "--bootstrap-samples",
        type=int,
        default=20_000,
        help="Repository-clustered bootstrap samples for GraphRAG intervals.",
    )
    return parser.parse_args()


def _protocol(config: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in config.items()
        if key not in {"embedding_model", "embedding_dimension"}
    }


def load_matrix(result_dir: Path, partition: str = "test") -> list[dict[str, Any]]:
    paths = sorted(result_dir.glob("dense_graphrag_*_all.json"))
    if len(paths) != len(EMBEDDINGS):
        raise ValueError(
            f"Expected {len(EMBEDDINGS)} matrix files in {result_dir}, "
            f"found {len(paths)}"
        )

    by_model: dict[str, dict[str, Any]] = {}
    reference_ids: tuple[str, ...] | None = None
    reference_protocol: dict[str, Any] | None = None

    for path in paths:
        with path.open() as handle:
            data = json.load(handle)

        model = data["config"]["embedding_model"]
        if model not in EMBEDDINGS:
            raise ValueError(f"Unexpected embedding model {model!r} in {path}")
        if model in by_model:
            raise ValueError(f"Duplicate result for embedding model {model!r}")

        summary = data["summary"]
        instance_ids = tuple(data["instance_ids"])
        if summary["errors"] != 0 or summary["scored"] != len(instance_ids):
            raise ValueError(f"Incomplete result file: {path}")
        if len(instance_ids) != 100 or len(set(instance_ids)) != 100:
            raise ValueError(f"Expected 100 unique instances in {path}")

        protocol = _protocol(data["config"])
        if reference_ids is None:
            reference_ids = instance_ids
            reference_protocol = protocol
        elif instance_ids != reference_ids:
            raise ValueError(f"Instance order or identity differs in {path}")
        elif protocol != reference_protocol:
            raise ValueError(f"Evaluator protocol differs in {path}")

        if data["config"]["graph_weights"] != [0.5]:
            raise ValueError(f"Expected a single GraphRAG weight of 0.5 in {path}")
        if data["config"]["cross_encoder_model"] != "Qwen/Qwen3-Reranker-4B":
            raise ValueError(f"Unexpected cross encoder in {path}")
        if data["config"]["cross_encoder_candidate_k"] != 50:
            raise ValueError(f"Unexpected cross-encoder candidate budget in {path}")

        for arm in ARMS:
            if arm not in summary["recall"]:
                raise ValueError(f"Missing recall summary for {arm} in {path}")
            if f"arm.{arm}" not in summary["timing_ms"]:
                raise ValueError(f"Missing timing summary for {arm} in {path}")
            for result in data["results"]:
                if arm not in result["recall"]:
                    raise ValueError(f"Missing per-instance recall for {arm} in {path}")

        if partition != "all":
            data["results"] = [
                result
                for result in data["results"]
                if data["partitions"].get(result["instance_id"]) == partition
            ]
            data["instance_ids"] = [result["instance_id"] for result in data["results"]]
        if not data["results"]:
            raise ValueError(f"No results for partition {partition!r} in {path}")
        data["analysis_partition"] = partition

        data["path"] = path
        by_model[model] = data

    missing = set(EMBEDDINGS) - set(by_model)
    if missing:
        raise ValueError(f"Missing embedding results: {sorted(missing)}")
    return [by_model[model] for model in EMBEDDINGS]


def _success(data: dict[str, Any], arm: str) -> float:
    values = [float(result["recall"][arm]["files@10"]) for result in data["results"]]
    return 100.0 * float(np.mean(values))


def _latency(data: dict[str, Any], arm: str, statistic: str) -> float:
    values = np.asarray(
        [float(result["timing_ms"][f"arm.{arm}"]) for result in data["results"]]
    )
    if statistic == "median":
        return float(np.median(values))
    if statistic == "p90":
        return float(np.percentile(values, 90))
    raise ValueError(f"Unsupported latency statistic: {statistic!r}")


def _paired_effect(
    data: dict[str, Any],
    baseline: str,
    graph: str,
    samples: int,
    rng: np.random.Generator,
) -> tuple[float, float, float]:
    differences = np.asarray(
        [
            float(result["recall"][graph]["files@10"])
            - float(result["recall"][baseline]["files@10"])
            for result in data["results"]
        ]
    )
    clusters: dict[str, list[float]] = {}
    for result, difference in zip(data["results"], differences, strict=True):
        instance_id = str(result["instance_id"])
        repository, separator, issue_id = instance_id.rpartition("-")
        if not separator or not repository or not issue_id.isdigit():
            raise ValueError(f"Cannot derive repository cluster from {instance_id!r}")
        clusters.setdefault(repository, []).append(float(difference))
    if len(clusters) < 2:
        raise ValueError("Clustered bootstrap requires at least two repositories")

    cluster_values = [np.asarray(clusters[key]) for key in sorted(clusters)]
    indices = rng.integers(0, len(cluster_values), size=(samples, len(cluster_values)))
    bootstrap = 100.0 * np.asarray(
        [
            np.concatenate([cluster_values[index] for index in sample]).mean()
            for sample in indices
        ]
    )
    low, high = np.percentile(bootstrap, [2.5, 97.5])
    return 100.0 * float(differences.mean()), float(low), float(high)


def _pairwise_interaction_audit(
    matrix: list[dict[str, Any]],
    baseline: str,
    graph: str,
    samples: int,
    rng: np.random.Generator,
) -> dict[str, Any]:
    """Form simultaneous intervals for all cross-embedding graph contrasts."""
    instance_ids = [str(result["instance_id"]) for result in matrix[0]["results"]]
    effects = []
    labels = []
    for data in matrix:
        candidate_ids = [str(result["instance_id"]) for result in data["results"]]
        if candidate_ids != instance_ids:
            raise ValueError("Embedding results are not paired by instance")
        labels.append(EMBEDDINGS[data["config"]["embedding_model"]]["label"])
        effects.append(
            [
                float(result["recall"][graph]["files@10"])
                - float(result["recall"][baseline]["files@10"])
                for result in data["results"]
            ]
        )
    effects_array = np.asarray(effects)

    clusters: dict[str, list[int]] = {}
    for index, instance_id in enumerate(instance_ids):
        repository, separator, issue_id = instance_id.rpartition("-")
        if not separator or not repository or not issue_id.isdigit():
            raise ValueError(f"Cannot derive repository cluster from {instance_id!r}")
        clusters.setdefault(repository, []).append(index)
    cluster_indices = [np.asarray(clusters[key], dtype=int) for key in sorted(clusters)]

    pairs = list(itertools.combinations(range(len(matrix)), 2))
    point = 100.0 * np.asarray(
        [(effects_array[left] - effects_array[right]).mean() for left, right in pairs]
    )
    sampled_clusters = rng.integers(
        0, len(cluster_indices), size=(samples, len(cluster_indices))
    )
    bootstrap = np.asarray(
        [
            [
                100.0
                * (effects_array[left, sampled] - effects_array[right, sampled]).mean()
                for left, right in pairs
            ]
            for sampled in (
                np.concatenate([cluster_indices[index] for index in draw])
                for draw in sampled_clusters
            )
        ]
    )
    critical_value = float(
        np.percentile(np.max(np.abs(bootstrap - point), axis=1), 95.0)
    )

    contrasts = []
    for (left, right), estimate in zip(pairs, point, strict=True):
        low = float(estimate - critical_value)
        high = float(estimate + critical_value)
        contrasts.append(
            {
                "left": labels[left],
                "right": labels[right],
                "delta_difference_pp": float(estimate),
                "simultaneous_ci95": [low, high],
                "excludes_zero": low > 0.0 or high < 0.0,
            }
        )

    return {
        "method": "repository-clustered bootstrap max-absolute-deviation",
        "familywise_confidence": 0.95,
        "comparisons": len(contrasts),
        "critical_half_width_pp": critical_value,
        "any_interval_excludes_zero": any(
            contrast["excludes_zero"] for contrast in contrasts
        ),
        "contrasts": contrasts,
    }


def write_summary(
    path: Path,
    matrix: list[dict[str, Any]],
    partition: str,
    bootstrap_samples: int,
) -> None:
    rng = np.random.default_rng(20260713)
    interaction = {}
    for baseline, graph, label, _ in GRAPH_PAIRS:
        interaction[label] = _pairwise_interaction_audit(
            matrix, baseline, graph, bootstrap_samples, rng
        )
    summary = {
        "schema_version": 1,
        "analysis_partition": partition,
        "snapshots": len(matrix[0]["results"]),
        "repositories": len(
            {
                result["instance_id"].rpartition("-")[0]
                for result in matrix[0]["results"]
            }
        ),
        "bootstrap_samples": bootstrap_samples,
        "seed": 20260713,
        "cross_embedding_interaction": interaction,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2) + "\n")


def _plot_graph_effect(
    ax: plt.Axes,
    matrix: list[dict[str, Any]],
    bootstrap_samples: int,
    *,
    panel_label: str = "a",
) -> None:
    rng = np.random.default_rng(20260713)
    y = np.arange(len(matrix), dtype=float)
    offsets = (-0.15, 0.15)

    ax.axvline(0, color="#777777", linewidth=0.8, zorder=0)
    for pair_index, (baseline, graph, _label, color) in enumerate(GRAPH_PAIRS):
        for row_index, data in enumerate(matrix):
            effect, low, high = _paired_effect(
                data, baseline, graph, bootstrap_samples, rng
            )
            position = y[row_index] + offsets[pair_index]
            ax.errorbar(
                effect,
                position,
                xerr=[[effect - low], [high - effect]],
                fmt="o",
                color=color,
                markeredgecolor=NEUTRAL_COLOR,
                markeredgewidth=0.35,
                markersize=5.0,
                elinewidth=1.25,
                capsize=2.1,
                capthick=1.0,
                zorder=3,
            )

    ax.set_yticks(y)
    ax.set_yticklabels(
        [EMBEDDINGS[d["config"]["embedding_model"]]["label"] for d in matrix]
    )
    for label, data in zip(ax.get_yticklabels(), matrix, strict=True):
        label.set_color(EMBEDDINGS[data["config"]["embedding_model"]]["color"])
        label.set_fontweight("semibold")
    ax.invert_yaxis()
    ax.set_xlim(-11.5, 12.5)
    ax.set_xticks([-10, -5, 0, 5, 10])
    ax.set_xlabel(r"$\Delta$ File Success@10 (pp; paired 95% CI)")
    panel_title(ax, panel_label, "Graph effect")
    style_axis(ax, grid_axis="x", open_frame=True)


def _plot_tradeoff(
    ax: plt.Axes,
    matrix: list[dict[str, Any]],
    *,
    panel_label: str = "b",
    show_note: bool = True,
) -> None:
    for data in matrix:
        model = data["config"]["embedding_model"]
        color = EMBEDDINGS[model]["color"]

        for arm, arm_style in ARMS.items():
            median = _latency(data, arm, "median")
            success = _success(data, arm)
            ax.scatter(
                median,
                success,
                s=37 if arm_style["graph"] else 31,
                marker=arm_style["marker"],
                color=color,
                edgecolor=NEUTRAL_COLOR,
                linewidth=0.35,
                zorder=3,
            )

    ax.set_xscale("log")
    ax.set_xlim(10, 12_000)
    ax.set_ylim(64, 83.5)
    ax.set_yticks([65, 70, 75, 80])
    ax.set_xlabel("Median query latency (ms, log scale)")
    ax.set_ylabel("File Success@10 (%)")
    panel_title(ax, panel_label, "Accuracy vs. latency")
    style_axis(ax, open_frame=True)
    if show_note:
        ax.text(
            0.02,
            0.97,
            "Colors match panel (a)\nReranker: Qwen3-4B (pointwise)",
            transform=ax.transAxes,
            ha="left",
            va="top",
            fontsize=6.2,
            color="#666666",
        )


def print_table(matrix: list[dict[str, Any]]) -> None:
    print("embedding,arm,file_success@10,median_ms,p90_ms")
    for data in matrix:
        embedding = EMBEDDINGS[data["config"]["embedding_model"]]["label"]
        for arm, style in ARMS.items():
            print(
                f"{embedding},{style['label']},{_success(data, arm):.1f},"
                f"{_latency(data, arm, 'median'):.3f},"
                f"{_latency(data, arm, 'p90'):.3f}"
            )


def print_effects(matrix: list[dict[str, Any]], bootstrap_samples: int) -> None:
    rng = np.random.default_rng(20260713)
    print("embedding,comparison,delta_file_success@10,ci95_low,ci95_high")
    for baseline, graph, label, _ in GRAPH_PAIRS:
        for data in matrix:
            embedding = EMBEDDINGS[data["config"]["embedding_model"]]["label"]
            effect, low, high = _paired_effect(
                data, baseline, graph, bootstrap_samples, rng
            )
            print(f"{embedding},{label},{effect:.1f},{low:.1f},{high:.1f}")


def main() -> None:
    args = parse_args()
    if args.bootstrap_samples <= 0:
        raise ValueError("--bootstrap-samples must be positive")

    matrix = load_matrix(args.result_dir, args.partition)
    print(
        f"analysis_partition={args.partition},snapshots={len(matrix[0]['results'])},"
        "repositories="
        f"{len({result['instance_id'].rpartition('-')[0] for result in matrix[0]['results']})}"
    )
    print_table(matrix)
    print_effects(matrix, args.bootstrap_samples)
    summary_path = args.output_prefix.with_suffix(".json")
    write_summary(summary_path, matrix, args.partition, args.bootstrap_samples)
    print(f"Saved: {summary_path}")

    fig, (effect_ax, tradeoff_ax) = plt.subplots(
        1,
        2,
        figsize=(DOUBLE_COLUMN_WIDTH, TWO_PANEL_HEIGHT),
        gridspec_kw={"width_ratios": [1.0, 1.05], "wspace": 0.35},
    )
    _plot_graph_effect(effect_ax, matrix, args.bootstrap_samples)
    _plot_tradeoff(tradeoff_ax, matrix)

    effect_handles = [
        Line2D(
            [],
            [],
            color=color,
            marker="o",
            markeredgecolor=NEUTRAL_COLOR,
            markeredgewidth=0.35,
            label=f"Graph effect: {label.lower()}",
        )
        for _, _, label, color in GRAPH_PAIRS
    ]
    arm_handles = [
        Line2D(
            [],
            [],
            marker=style["marker"],
            linestyle="none",
            markersize=4.2,
            color=NEUTRAL_COLOR,
            markerfacecolor="white",
            markeredgecolor=NEUTRAL_COLOR,
            label=style["label"],
        )
        for style in ARMS.values()
    ]
    fig.legend(
        handles=effect_handles + arm_handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.995),
        ncol=3,
        frameon=False,
    )
    fig.subplots_adjust(left=0.15, right=0.995, top=0.69, bottom=0.20)
    for output in save_figure(fig, args.output_prefix):
        print(f"Saved: {output}")
    plt.close(fig)


if __name__ == "__main__":
    main()
