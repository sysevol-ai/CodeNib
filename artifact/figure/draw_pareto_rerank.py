#!/usr/bin/env python
"""Dense and pointwise-reranker construction/latency operating points."""

import argparse
import colorsys
import glob
import json
import os
from pathlib import Path

import matplotlib
import matplotlib.colors as mc
import numpy as np
import pandas as pd

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from draw_profile import construction_time
from matplotlib import gridspec
from plot_style import (
    DOUBLE_COLUMN_WIDTH,
    MODEL_COLORS,
    MODEL_DISPLAY_LABELS,
    MODEL_LINESTYLES,
    MODEL_MARKERS,
    NEUTRAL_COLOR,
    aligned_panel_title,
    apply_paper_style,
    save_figure,
    style_axis,
)


def shade(color, factor):
    """Adjust lightness of a color. factor<1 darker, factor>1 lighter."""
    r, g, b = mc.to_rgb(color)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if factor < 1:
        l_new = max(0.0, l * factor)
    else:
        l_new = min(1.0, 1 - (1 - l) / factor)
    return colorsys.hls_to_rgb(h, l_new, s)


DEFAULT_OUTPUT_PREFIX = Path(__file__).resolve().parent / "pareto_rerank"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--query-dir", type=Path, required=True)
    parser.add_argument("--eval-dir", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, default=DEFAULT_OUTPUT_PREFIX)
    return parser.parse_args()


K_TARGET = "10"  # recall@k

# ── Dense single-stage baselines ──────────────────────────────────
DENSE_MODELS = {
    "Salesforce/SweRankEmbed-Small": "SweRank-Small",
    "Qwen/Qwen3-Embedding-0.6B": "Qwen3-Embed-0.6B",
    "jinaai/jina-code-embeddings-1.5b": "Jina-Code-1.5B",
    "Qwen/Qwen3-Embedding-4B": "Qwen3-Embed-4B",
    "fishmingyu/SweRankEmbed-Large": "SweRank-Large",
}
DENSE_PALETTE = MODEL_COLORS
COLOR_TO_MODEL = {color: model for model, color in DENSE_PALETTE.items()}

# ── Cross-encoder rerank pipelines (with K sweeps) ────────────────
# Encoding: COLOR = embedder (base color, no shading);
#           MARKER SHAPE = reranker size;
#           LINESTYLE = uniform dashed (avoids solid-over-dashed occlusion).
# Reranker → marker progression: 3→4→5 points (visually monotonic):
#   + Reranker-0.6B → triangle "^"
#   + Reranker-4B  → diamond  "D"
#   + Reranker-8B  → pentagon "p"
# (pipeline_label, color, marker, list of (K, eval_filename, query_filename))


def _ce_eval(embed_safe, rr_safe, cfg):
    return f"rerank_cross_encoder_codeminer_base_test_{embed_safe}__x__{rr_safe}__{cfg}.json"


def _ce_query(embed_safe, rr_safe, cfg):
    return (
        f"codeminer_base__dense_retrieve_plus_cross_encoder_rerank__"
        f"{embed_safe}__{embed_safe}__x__{rr_safe}__{cfg}.json"
    )


def _sweep(embed_safe, rr_safe, base_cfg):
    """K=30 uses base_cfg, K=50/100 append _k50/_k100."""
    return [
        (
            30,
            _ce_eval(embed_safe, rr_safe, base_cfg),
            _ce_query(embed_safe, rr_safe, base_cfg),
        ),
        (
            50,
            _ce_eval(embed_safe, rr_safe, base_cfg + "_k50"),
            _ce_query(embed_safe, rr_safe, base_cfg + "_k50"),
        ),
        (
            100,
            _ce_eval(embed_safe, rr_safe, base_cfg + "_k100"),
            _ce_query(embed_safe, rr_safe, base_cfg + "_k100"),
        ),
    ]


_SMALL = "Salesforce__SweRankEmbed-Small"
_Q06 = "Qwen__Qwen3-Embedding-0.6B"
_JINA = "jinaai__jina-code-embeddings-1.5b"
_Q4 = "Qwen__Qwen3-Embedding-4B"
_RR_06 = "Qwen__Qwen3-Reranker-0.6B"
_RR_4 = "Qwen__Qwen3-Reranker-4B"
_RR_8 = "Qwen__Qwen3-Reranker-8B"

_C_SMALL = DENSE_PALETTE["SweRank-Small"]
_C_Q06 = DENSE_PALETTE["Qwen3-Embed-0.6B"]
_C_JINA = DENSE_PALETTE["Jina-Code-1.5B"]
_C_Q4 = DENSE_PALETTE["Qwen3-Embed-4B"]

MARK_RR_06 = "^"  # triangle  (3 points) — Reranker-0.6B
MARK_RR_4 = "D"  # diamond   (4 points) — Reranker-4B
MARK_RR_8 = "p"  # pentagon  (5 points) — Reranker-8B

# Per-shape marker size — diamond's square footprint reads visually larger
# than triangle/pentagon at the same `markersize`, so it gets a smaller value.
MARK_SIZE = {MARK_RR_06: 4.4, MARK_RR_4: 3.8, MARK_RR_8: 3.8}

RERANK_PIPELINES = [
    # SweRank-Small embedder (older runs; only 0.6B and 4B rerankers available)
    (
        "Small + Reranker-0.6B",
        _C_SMALL,
        MARK_RR_06,
        [
            (
                30,
                _ce_eval(_SMALL, _RR_06, "ce_qwen3_0p6b"),
                _ce_query(_SMALL, _RR_06, "ce_qwen3_0p6b"),
            ),
            (
                50,
                _ce_eval(_SMALL, _RR_06, "ce_qwen3_swerank_small_k50"),
                _ce_query(_SMALL, _RR_06, "ce_qwen3_swerank_small_k50"),
            ),
            (
                100,
                _ce_eval(_SMALL, _RR_06, "ce_qwen3_swerank_small_k100"),
                _ce_query(_SMALL, _RR_06, "ce_qwen3_swerank_small_k100"),
            ),
        ],
    ),
    (
        "Small + Reranker-4B",
        _C_SMALL,
        MARK_RR_4,
        [
            (
                30,
                _ce_eval(_SMALL, _RR_4, "ce_qwen3_4b"),
                _ce_query(_SMALL, _RR_4, "ce_qwen3_4b"),
            ),
            (
                50,
                _ce_eval(_SMALL, _RR_4, "ce_qwen3_swerank_small_k50"),
                _ce_query(_SMALL, _RR_4, "ce_qwen3_swerank_small_k50"),
            ),
            (
                100,
                _ce_eval(_SMALL, _RR_4, "ce_qwen3_swerank_small_k100"),
                _ce_query(_SMALL, _RR_4, "ce_qwen3_swerank_small_k100"),
            ),
        ],
    ),
    (
        "Small + Reranker-8B",
        _C_SMALL,
        MARK_RR_8,
        [
            (
                30,
                _ce_eval(_SMALL, _RR_8, "ce_qwen3_swerank_small"),
                _ce_query(_SMALL, _RR_8, "ce_qwen3_swerank_small"),
            ),
            (
                50,
                _ce_eval(_SMALL, _RR_8, "ce_qwen3_swerank_small_k50"),
                _ce_query(_SMALL, _RR_8, "ce_qwen3_swerank_small_k50"),
            ),
            (
                100,
                _ce_eval(_SMALL, _RR_8, "ce_qwen3_swerank_small_k100"),
                _ce_query(_SMALL, _RR_8, "ce_qwen3_swerank_small_k100"),
            ),
        ],
    ),
    # Full-matrix sweep: 3 embedders × {4B, 8B} rerankers × K∈{30,50,100}
    (
        "Qwen3-0.6B + Reranker-4B",
        _C_Q06,
        MARK_RR_4,
        _sweep(_Q06, _RR_4, "ce_qwen3_full_matrix"),
    ),
    (
        "Qwen3-0.6B + Reranker-8B",
        _C_Q06,
        MARK_RR_8,
        _sweep(_Q06, _RR_8, "ce_qwen3_full_matrix"),
    ),
    (
        "Jina + Reranker-4B",
        _C_JINA,
        MARK_RR_4,
        _sweep(_JINA, _RR_4, "ce_qwen3_full_matrix"),
    ),
    (
        "Jina + Reranker-8B",
        _C_JINA,
        MARK_RR_8,
        _sweep(_JINA, _RR_8, "ce_qwen3_full_matrix"),
    ),
    (
        "Qwen3-4B + Reranker-4B",
        _C_Q4,
        MARK_RR_4,
        _sweep(_Q4, _RR_4, "ce_qwen3_full_matrix"),
    ),
    (
        "Qwen3-4B + Reranker-8B",
        _C_Q4,
        MARK_RR_8,
        _sweep(_Q4, _RR_8, "ce_qwen3_full_matrix"),
    ),
]

apply_paper_style()


def mean_query_time(qpath):
    with open(qpath) as f:
        d = json.load(f)
    durations = []
    for inst in d["instances"]:
        sections = {section["label"]: section["total"] for section in inst["sections"]}
        if "pipeline.query" in sections:
            durations.append(sections["pipeline.query"])
            continue
        required = ("retrieve.small", "rerank.cross_encoder")
        if all(label in sections for label in required):
            durations.append(sum(sections[label] for label in required))
            continue
        raise ValueError(f"No query-only timing in {qpath}: {inst['instance_id']}")
    return np.mean(durations)


def mean_recall(epath, level):
    with open(epath) as f:
        d = json.load(f)
    return np.mean([inst["metrics"][level][K_TARGET]["recall"] for inst in d])


def mean_build_time(hf_id, profile_dir):
    """Mean L2 build time for the callable index used by retrieval."""
    times = []
    for fp in sorted(glob.glob(os.path.join(profile_dir, "*.json"))):
        with open(fp) as handle:
            d = json.load(handle)
        if (
            d.get("embedding_model") != hf_id
            or d.get("profile_tag") != "codeminer_base_test"
        ):
            continue
        sec = {s["label"]: s["total"] for s in d.get("sections", [])}
        value = construction_time(sec, "l2")
        if np.isfinite(value):
            times.append(value)
    return float(np.mean(times)) if times else float("nan")


def load_pareto_data(profile_dir, query_dir, eval_dir):
    """Load the exact dense and reranker operating points used by the figure."""

    profile_dir = str(profile_dir)
    query_dir = str(query_dir)
    eval_dir = str(eval_dir)
    dense_rows = []
    for hf_id, label in DENSE_MODELS.items():
        safe = hf_id.replace("/", "__")
        qpath = os.path.join(
            query_dir, f"codeminer_base__embedding_baseline__{safe}__{safe}.json"
        )
        epath = os.path.join(eval_dir, f"eval_codeminer_base_test_{safe}.json")
        dense_rows.append(
            {
                "label": label,
                "color": DENSE_PALETTE[label],
                "query_time": mean_query_time(qpath),
                "build_time": mean_build_time(hf_id, profile_dir),
                "recall_files": mean_recall(epath, "files"),
                "recall_symbols": mean_recall(epath, "symbols"),
            }
        )
    dense_frame = pd.DataFrame(dense_rows)

    # Rerank pipelines reuse their base embedder's dense index.
    base_build = dict(zip(dense_frame["color"], dense_frame["build_time"], strict=True))
    rerank_rows = []
    for pipe_label, color, marker, sweeps in RERANK_PIPELINES:
        for candidate_k, eval_name, query_name in sweeps:
            rerank_rows.append(
                {
                    "pipeline": pipe_label,
                    "color": color,
                    "marker": marker,
                    "k": candidate_k,
                    "query_time": mean_query_time(os.path.join(query_dir, query_name)),
                    "build_time": base_build[color],
                    "recall_files": mean_recall(
                        os.path.join(eval_dir, eval_name), "files"
                    ),
                    "recall_symbols": mean_recall(
                        os.path.join(eval_dir, eval_name), "symbols"
                    ),
                }
            )
    rerank_frame = pd.DataFrame(rerank_rows).sort_values(["pipeline", "k"])
    return dense_frame, rerank_frame


def print_summary(dense_frame, rerank_frame):
    print("\n── Dense baselines ──")
    print(
        dense_frame[["label", "query_time", "recall_files", "recall_symbols"]]
        .round(3)
        .to_string(index=False)
    )
    print("\n── Cross-encoder rerank K sweeps ──")
    print(
        rerank_frame[["pipeline", "k", "query_time", "recall_files", "recall_symbols"]]
        .round(3)
        .to_string(index=False)
    )


def pareto_front(points):
    """Indices in points where lower-x and higher-y are non-dominated."""
    sorted_pts = sorted(enumerate(points), key=lambda p: p[1][0])
    front, best = [], -np.inf
    for idx, (_x, y) in sorted_pts:
        if y > best:
            front.append(idx)
            best = y
    return front


# ── Plot helpers ──────────────────────────────────────────────────
DENSE_OFFSETS = {
    ("query_time", "recall_files"): {
        "SweRank-Small": (3, -6, "left"),
        "Qwen3-Embed-0.6B": (0, 10, "center"),
        "Jina-Code-1.5B": (-5, -4, "right"),
        "Qwen3-Embed-4B": (-5, 8, "right"),
        "SweRank-Large": (5, -20, "center"),
    },
    ("query_time", "recall_symbols"): {
        "SweRank-Small": (4, -5, "left"),
        "Jina-Code-1.5B": (-5, -9, "right"),
        "Qwen3-Embed-0.6B": (10, -2, "left"),
        "Qwen3-Embed-4B": (0, 10, "center"),
        "SweRank-Large": (-5, 5, "right"),
    },
    ("build_time", "recall_files"): {
        "SweRank-Small": (0, -12, "center"),
        "Qwen3-Embed-0.6B": (4, -11, "left"),
        "Jina-Code-1.5B": (-5, -12, "right"),
        "Qwen3-Embed-4B": (0, -14, "center"),
        "SweRank-Large": (-2, 4, "right"),
    },
}


def draw_rerank(ax, rerank_frame, xcol, ycol, connect=True, only_k=None):
    """Plot rerank pipeline curves (or markers only if connect=False).
    only_k: optional list of K values; if set, only those K markers are drawn.
    """
    for pipe_label, color, marker, _ in RERANK_PIPELINES:
        sub = rerank_frame[rerank_frame["pipeline"] == pipe_label]
        if only_k is not None:
            sub = sub[sub["k"].isin(only_k)]
        sub = sub.sort_values(xcol)
        model = COLOR_TO_MODEL[color]
        ax.plot(
            sub[xcol],
            sub[ycol],
            color=color,
            linewidth=0.9 if connect else 0,
            alpha=0.85,
            linestyle=MODEL_LINESTYLES[model] if connect else "None",
            marker=marker,
            markersize=MARK_SIZE[marker],
            markerfacecolor=color,
            markeredgecolor=NEUTRAL_COLOR,
            markeredgewidth=0.3,
            zorder=3,
            label=pipe_label,
        )


SHORT_LABEL = {
    "SweRank-Small": "SR-Small",
    "Qwen3-Embed-0.6B": "Qwen3-0.6B",
    "Jina-Code-1.5B": "Jina-1.5B",
    "Qwen3-Embed-4B": "Qwen3-4B",
    "SweRank-Large": "SR-Large",
}

DISPLAY_LABEL = {
    "SweRank-Small": MODEL_DISPLAY_LABELS["SweRank-Small"],
    "SweRank-Large": MODEL_DISPLAY_LABELS["SweRank-Large"],
}


def draw_dense(ax, dense_frame, xcol, ycol, with_labels=True):
    """Plot dense-baseline points with optional direct labels."""
    offsets = DENSE_OFFSETS.get((xcol, ycol), {})
    for _, r in dense_frame.iterrows():
        ax.scatter(
            r[xcol],
            r[ycol],
            s=36,
            color=r["color"],
            marker=MODEL_MARKERS[r["label"]],
            edgecolors=NEUTRAL_COLOR,
            linewidth=0.3,
            zorder=4,
            label=r["label"],
        )
        if with_labels and r["label"] in offsets:
            dx, dy, ha = offsets[r["label"]]
            ax.annotate(
                SHORT_LABEL[r["label"]],
                (r[xcol], r[ycol]),
                textcoords="offset points",
                xytext=(dx, dy),
                ha=ha,
                fontsize=7.0,
                color="#222222",
                fontweight="semibold",
            )


def draw_pareto(ax, dense_frame, rerank_frame, xcol, ycol, only_k=None):
    """Plot the global Pareto frontier dotted line (over visible markers only)."""
    rerank_pts = rerank_frame
    if only_k is not None:
        rerank_pts = rerank_pts[rerank_pts["k"].isin(only_k)]
    pts = [(r[xcol], r[ycol]) for _, r in dense_frame.iterrows()] + [
        (r[xcol], r[ycol]) for _, r in rerank_pts.iterrows()
    ]
    pf = sorted(pareto_front(pts), key=lambda i: pts[i][0])
    ax.plot(
        [pts[i][0] for i in pf],
        [pts[i][1] for i in pf],
        color="#444",
        linewidth=0.85,
        linestyle=":",
        alpha=0.7,
        zorder=1,
    )


def setup_panel(
    ax,
    dense_frame,
    rerank_frame,
    xcol,
    ycol,
    panel,
    title,
    xlabel,
    *,
    connect_rerank=True,
    only_k=None,
    label_dense=True,
):
    """Plot rerank + dense + Pareto on a main log-x panel."""
    draw_rerank(
        ax,
        rerank_frame,
        xcol,
        ycol,
        connect=connect_rerank,
        only_k=only_k,
    )
    draw_dense(ax, dense_frame, xcol, ycol, with_labels=label_dense)
    draw_pareto(ax, dense_frame, rerank_frame, xcol, ycol, only_k=only_k)
    ax.set_xscale("log")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(f"Recall@{K_TARGET}")
    aligned_panel_title(ax, panel, title, pad=3.5)
    style_axis(ax, open_frame=True)
    yvals = list(dense_frame[ycol]) + list(rerank_frame[ycol])
    yr = max(yvals) - min(yvals)
    ax.set_ylim(min(yvals) - 0.13 * yr, max(yvals) + 0.13 * yr)
    xvals = list(dense_frame[xcol]) + list(rerank_frame[xcol])
    ax.set_xlim(min(xvals) * 0.42, max(xvals) * 1.5)


def _shape(marker, label):
    return plt.Line2D(
        [0],
        [0],
        color=NEUTRAL_COLOR,
        markerfacecolor="#777777",
        markeredgecolor="white",
        markeredgewidth=0.6,
        marker=marker,
        markersize=MARK_SIZE[marker] + 0.4,
        linewidth=1.0,
        linestyle="--",
        label=label,
    )


reranker_handles = [
    _shape(MARK_RR_06, "+ Reranker-0.6B"),
    _shape(MARK_RR_4, "+ Reranker-4B"),
    _shape(MARK_RR_8, "+ Reranker-8B"),
]
misc_handles = [
    plt.Line2D(
        [0],
        [0],
        color=NEUTRAL_COLOR,
        linewidth=0.9,
        linestyle=":",
        alpha=0.7,
        label="Pareto frontier",
    ),
]


def _embedder(color, model, label):
    return plt.Line2D(
        [0],
        [0],
        color=color,
        linestyle=MODEL_LINESTYLES[model],
        marker=MODEL_MARKERS[model],
        markerfacecolor=color,
        markeredgecolor=NEUTRAL_COLOR,
        markeredgewidth=0.3,
        markersize=4.2,
        label=label,
    )


embedder_handles = [
    _embedder(
        DENSE_PALETTE[label],
        label,
        DISPLAY_LABEL.get(label, label),
    )
    for label in DENSE_MODELS.values()
]
legend_handles = [
    embedder_handles[0],
    reranker_handles[0],
    embedder_handles[1],
    reranker_handles[1],
    embedder_handles[2],
    reranker_handles[2],
    embedder_handles[3],
    misc_handles[0],
    embedder_handles[4],
]


def render(dense_frame, rerank_frame, output_prefix):
    """Render the three-panel retrieval operating-point figure."""

    figure = plt.figure(figsize=(DOUBLE_COLUMN_WIDTH, 2.38))
    outer = gridspec.GridSpec(
        1,
        3,
        figure=figure,
        width_ratios=[1.0, 1.0, 1.45],
        left=0.055,
        right=0.995,
        bottom=0.19,
        top=0.70,
        wspace=0.28,
    )
    axis_build = figure.add_subplot(outer[0])
    axis_file = figure.add_subplot(outer[1])
    axis_symbol = figure.add_subplot(outer[2])

    # Candidate K does not change build cost, so panel (a) shows K=100 only.
    setup_panel(
        axis_build,
        dense_frame,
        rerank_frame,
        "build_time",
        "recall_files",
        "a",
        "File-level · build time",
        "Mean L2 build time/repo (s, log)",
        connect_rerank=False,
        only_k=[100],
        label_dense=False,
    )
    axis_build.set_ylim(0.62, axis_build.get_ylim()[1])
    setup_panel(
        axis_file,
        dense_frame,
        rerank_frame,
        "query_time",
        "recall_files",
        "b",
        "File-level · query time",
        "Mean query time (s, log)",
        label_dense=False,
    )
    setup_panel(
        axis_symbol,
        dense_frame,
        rerank_frame,
        "query_time",
        "recall_symbols",
        "c",
        "Symbol-level · query time",
        "Mean query time (s, log)",
        label_dense=False,
    )

    zoom_x = (3.3, 5.9)
    zoom_y = (0.60, 0.70)
    zoom = axis_symbol.inset_axes([0.59, 0.085, 0.39, 0.15])
    for _, row in rerank_frame.iterrows():
        if (
            zoom_x[0] <= row["query_time"] <= zoom_x[1]
            and zoom_y[0] <= row["recall_symbols"] <= zoom_y[1]
        ):
            zoom.plot(
                row["query_time"],
                row["recall_symbols"],
                marker=row["marker"],
                markersize=MARK_SIZE[row["marker"]],
                markerfacecolor=row["color"],
                markeredgecolor=NEUTRAL_COLOR,
                markeredgewidth=0.3,
                linestyle="None",
                zorder=3,
            )
    zoom.set_xlim(*zoom_x)
    zoom.set_ylim(*zoom_y)
    zoom.tick_params(labelsize=6.2, length=1.5)
    zoom.tick_params(axis="x", labelsize=5.0, direction="out", pad=1)
    zoom.grid(True, which="major", alpha=0.3)
    zoom.grid(False, which="minor")
    zoom.text(
        1.0,
        1.04,
        "Zoom (linear x)",
        transform=zoom.transAxes,
        ha="right",
        va="bottom",
        fontsize=4.2,
        color="#444444",
        fontweight="semibold",
        clip_on=False,
    )
    for spine in zoom.spines.values():
        spine.set_edgecolor("#444")
    indicator = axis_symbol.indicate_inset_zoom(
        zoom, edgecolor="#999999", linewidth=1.0, alpha=0.85
    )
    for connector in indicator.connectors or ():
        connector.set_visible(False)

    figure.legend(
        handles=legend_handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.985),
        ncol=5,
        fontsize=5.8,
        frameon=False,
        handletextpad=0.35,
        columnspacing=0.9,
        labelspacing=0.38,
    )
    for output in save_figure(figure, output_prefix):
        print(f"Saved: {output}")
    plt.close(figure)


def main():
    args = parse_args()
    dense_frame, rerank_frame = load_pareto_data(
        args.profile_dir, args.query_dir, args.eval_dir
    )
    print_summary(dense_frame, rerank_frame)
    render(dense_frame, rerank_frame, args.output_prefix)


if __name__ == "__main__":
    main()
