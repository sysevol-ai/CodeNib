#!/usr/bin/env python3
"""Draw the implementation-aligned single-column agent runtime schematic."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
from matplotlib.path import Path as MplPath
from plot_style import (
    BASELINE_COLOR,
    BASELINE_LIGHT,
    GRID_COLOR,
    MUTED_COLOR,
    NEUTRAL_COLOR,
    PANEL_FILL,
    PASTEL_BLUE,
    PASTEL_GREEN,
    PRIMARY_COLOR,
    PRIMARY_LIGHT,
    SINGLE_COLUMN_WIDTH,
    apply_paper_style,
    save_figure,
)

DEFAULT_OUTPUT_PREFIX = Path(__file__).resolve().parent / "agent_runtime_schematic"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=DEFAULT_OUTPUT_PREFIX,
        help="Output path without an extension.",
    )
    return parser.parse_args()


def box(
    ax,
    x: float,
    y: float,
    width: float,
    height: float,
    title: str,
    detail: str = "",
    *,
    facecolor: str = "white",
    edgecolor: str = NEUTRAL_COLOR,
    title_color: str = NEUTRAL_COLOR,
    title_size: float = 6.75,
    detail_size: float = 5.6,
    linestyle: str = "-",
    linewidth: float = 0.75,
    zorder: int = 2,
) -> None:
    patch = FancyBboxPatch(
        (x, y),
        width,
        height,
        boxstyle="round,pad=0.006,rounding_size=0.012",
        facecolor=facecolor,
        edgecolor=edgecolor,
        linewidth=linewidth,
        linestyle=linestyle,
        zorder=zorder,
    )
    ax.add_patch(patch)
    if detail:
        ax.text(
            x + width / 2,
            y + height * 0.66,
            title,
            ha="center",
            va="center",
            fontsize=title_size,
            fontweight="semibold",
            color=title_color,
            zorder=zorder + 1,
        )
        ax.text(
            x + width / 2,
            y + height * 0.29,
            detail,
            ha="center",
            va="center",
            fontsize=detail_size,
            color=MUTED_COLOR,
            linespacing=1.05,
            zorder=zorder + 1,
        )
    else:
        ax.text(
            x + width / 2,
            y + height / 2,
            title,
            ha="center",
            va="center",
            fontsize=title_size,
            fontweight="semibold",
            color=title_color,
            zorder=zorder + 1,
        )


def arrow(
    ax,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    color: str = NEUTRAL_COLOR,
    label: str | None = None,
    label_xy: tuple[float, float] | None = None,
    connectionstyle: str = "arc3",
    linestyle: str = "-",
    linewidth: float = 0.85,
    mutation_scale: float = 7.5,
    zorder: int = 5,
) -> None:
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=mutation_scale,
            linewidth=linewidth,
            linestyle=linestyle,
            color=color,
            connectionstyle=connectionstyle,
            shrinkA=1.5,
            shrinkB=1.5,
            zorder=zorder,
        )
    )
    if label and label_xy:
        ax.text(
            *label_xy,
            label,
            ha="center",
            va="center",
            fontsize=5.1,
            color=color,
            bbox={"facecolor": "white", "edgecolor": "none", "pad": 0.35},
            zorder=zorder + 1,
        )


def routed_arrow(
    ax,
    points: tuple[tuple[float, float], ...],
    *,
    color: str = NEUTRAL_COLOR,
    label: str | None = None,
    label_xy: tuple[float, float] | None = None,
    linestyle: str = "-",
    linewidth: float = 0.85,
    zorder: int = 5,
) -> None:
    path = MplPath(points, [MplPath.MOVETO] + [MplPath.LINETO] * (len(points) - 1))
    ax.add_patch(
        FancyArrowPatch(
            path=path,
            arrowstyle="-|>",
            mutation_scale=7.5,
            linewidth=linewidth,
            linestyle=linestyle,
            color=color,
            shrinkA=1.5,
            shrinkB=1.5,
            zorder=zorder,
        )
    )
    if label and label_xy:
        ax.text(
            *label_xy,
            label,
            ha="center",
            va="center",
            fontsize=5.1,
            color=color,
            bbox={"facecolor": "white", "edgecolor": "none", "pad": 0.35},
            zorder=zorder + 1,
        )


def lane_label(ax, y: float, text: str) -> None:
    ax.text(
        0.02,
        y,
        text,
        ha="left",
        va="center",
        fontsize=5.65,
        fontweight="semibold",
        color=MUTED_COLOR,
    )
    ax.plot([0.19, 0.98], [y, y], color=GRID_COLOR, linewidth=0.55, zorder=0)


def context_policy_box(ax) -> None:
    x, y, width, height = 0.025, 0.055, 0.955, 0.185
    patch = FancyBboxPatch(
        (x, y),
        width,
        height,
        boxstyle="round,pad=0.006,rounding_size=0.012",
        facecolor="#FBF4EC",
        edgecolor=BASELINE_LIGHT,
        linewidth=0.75,
        zorder=2,
    )
    ax.add_patch(patch)
    ax.text(
        x + 0.012,
        y + height - 0.027,
        "Context policy",
        ha="left",
        va="center",
        fontsize=6.2,
        fontweight="semibold",
        color=BASELINE_COLOR,
        zorder=3,
    )
    ax.text(
        x + width - 0.012,
        y + height - 0.027,
        "$C_{10}$ = ranked top-10 $L_2$ units shared by eager arms",
        ha="right",
        va="center",
        fontsize=4.95,
        color=MUTED_COLOR,
        zorder=3,
    )

    columns = (
        (
            "Grep/read",
            "$H_0=[S,Q]$\nappend-only",
            NEUTRAL_COLOR,
        ),
        (
            "Eager",
            "$H_0=[S,Q,C_{10}]$\nbefore turn 1",
            PRIMARY_COLOR,
        ),
        (
            "Eager + compact",
            "same $H_0$\nfirst successful read\n"
            r"$\Rightarrow$ replace $H_t$ once"
            "\n"
            r"$[S,Q,\,\mathrm{latest\ read},\,\mathrm{assessment}]$",
            BASELINE_COLOR,
        ),
    )
    content_y = y + 0.048
    column_width = (width - 0.024) / 3
    for index, (label, detail, color) in enumerate(columns):
        left = x + 0.012 + index * column_width
        if index:
            ax.plot(
                [left, left],
                [y + 0.015, y + height - 0.055],
                color="#E3D7CB",
                linewidth=0.4,
                zorder=2,
            )
        ax.text(
            left + column_width / 2,
            y + 0.104,
            label,
            ha="center",
            va="center",
            fontsize=5.5,
            fontweight="semibold",
            color=color,
            zorder=3,
        )
        ax.text(
            left + column_width / 2,
            content_y,
            detail,
            ha="center",
            va="center",
            fontsize=4.9,
            color=NEUTRAL_COLOR,
            linespacing=1.05,
            zorder=3,
        )


def draw_schematic(output_prefix: Path) -> None:
    apply_paper_style()
    figure, axis = plt.subplots(figsize=(SINGLE_COLUMN_WIDTH, 3.12))
    axis.set_xlim(0, 1)
    axis.set_ylim(0, 1)
    axis.axis("off")

    lane_label(axis, 0.955, "SESSION SETUP")
    box(
        axis,
        0.02,
        0.835,
        0.29,
        0.075,
        "Issue + harness",
        "",
        facecolor="#F1F4F6",
        edgecolor=PASTEL_BLUE,
        title_size=6.1,
    )
    box(
        axis,
        0.02,
        0.745,
        0.29,
        0.075,
        "Manifest $M_c$",
        "",
        facecolor="#F4F0F7",
        edgecolor=PRIMARY_LIGHT,
        title_size=6.1,
    )
    box(
        axis,
        0.42,
        0.75,
        0.56,
        0.165,
        "Active tool surface",
        "hydrate required views | bind skill executors\n"
        "preflight resources; expose schemas + warnings",
        facecolor="#F1F5F2",
        edgecolor=PASTEL_GREEN,
        detail_size=5.25,
    )
    arrow(axis, (0.31, 0.873), (0.42, 0.873), color=PASTEL_BLUE)
    arrow(axis, (0.31, 0.782), (0.42, 0.782), color=PRIMARY_COLOR)

    lane_label(axis, 0.695, "BOUNDED AGENT LOOP")
    box(
        axis,
        0.025,
        0.485,
        0.235,
        0.155,
        "History $H_t$",
        "initial $H_0$\n+ observations",
        facecolor="#F1F5F2",
        edgecolor=PASTEL_GREEN,
    )
    box(
        axis,
        0.355,
        0.505,
        0.205,
        0.115,
        "LLM",
        "tool call | answer",
        facecolor="#F4F0F7",
        edgecolor=PRIMARY_COLOR,
        title_color=PRIMARY_COLOR,
    )

    dispatch = FancyBboxPatch(
        (0.65, 0.455),
        0.33,
        0.205,
        boxstyle="round,pad=0.006,rounding_size=0.012",
        facecolor=PANEL_FILL,
        edgecolor=NEUTRAL_COLOR,
        linewidth=0.75,
        zorder=2,
    )
    axis.add_patch(dispatch)
    axis.text(
        0.815,
        0.625,
        "Tool dispatch",
        ha="center",
        va="center",
        fontsize=6.4,
        fontweight="semibold",
        color=NEUTRAL_COLOR,
        zorder=3,
    )
    box(
        axis,
        0.665,
        0.475,
        0.145,
        0.105,
        "Index skills",
        "search | graph\nstatic LSP",
        facecolor="#F4F0F7",
        edgecolor=PRIMARY_LIGHT,
        title_size=5.9,
        detail_size=5.05,
        linewidth=0.6,
        zorder=3,
    )
    box(
        axis,
        0.82,
        0.475,
        0.145,
        0.105,
        "Repo tools",
        "read | grep\nglob | shell",
        facecolor="#FBF4EC",
        edgecolor=BASELINE_LIGHT,
        title_size=5.9,
        detail_size=5.05,
        linewidth=0.6,
        zorder=3,
    )

    box(
        axis,
        0.65,
        0.335,
        0.14,
        0.09,
        "Observation",
        "tool result",
        facecolor="white",
        edgecolor=PASTEL_BLUE,
        title_size=5.4,
        detail_size=4.65,
    )
    arrow(axis, (0.26, 0.565), (0.355, 0.565), color=PRIMARY_COLOR)
    arrow(
        axis,
        (0.56, 0.565),
        (0.65, 0.565),
        color=PRIMARY_COLOR,
        label="tool call",
        label_xy=(0.605, 0.59),
    )
    arrow(
        axis,
        (0.815, 0.455),
        (0.72, 0.425),
        color=NEUTRAL_COLOR,
        connectionstyle="arc3,rad=-0.06",
    )
    routed_arrow(
        axis,
        ((0.65, 0.38), (0.61, 0.31), (0.17, 0.31), (0.17, 0.485)),
        color=PRIMARY_COLOR,
        label="append",
        label_xy=(0.38, 0.31),
    )
    arrow(
        axis,
        (0.815, 0.75),
        (0.815, 0.66),
        color=MUTED_COLOR,
        label="executors",
        label_xy=(0.858, 0.72),
        connectionstyle="arc3,rad=0.0",
        linestyle="--",
        linewidth=0.7,
    )
    arrow(
        axis,
        (0.56, 0.75),
        (0.24, 0.64),
        color=MUTED_COLOR,
        label="prompt",
        label_xy=(0.49, 0.71),
        connectionstyle="arc3,rad=0.06",
        linestyle="--",
        linewidth=0.7,
    )

    lane_label(axis, 0.285, "CONTEXT POLICIES")
    context_policy_box(axis)
    box(
        axis,
        0.84,
        0.335,
        0.14,
        0.09,
        "Trace + usage",
        "tokens | calls",
        facecolor="#F1F4F6",
        edgecolor=PASTEL_BLUE,
        title_size=5.4,
        detail_size=4.65,
    )
    box(
        axis,
        0.355,
        0.335,
        0.205,
        0.09,
        "AgentResult",
        "answer + usage/trace",
        facecolor="#F4F0F7",
        edgecolor=PRIMARY_COLOR,
        title_color=PRIMARY_COLOR,
        title_size=5.7,
        detail_size=4.65,
    )
    arrow(
        axis,
        (0.79, 0.38),
        (0.84, 0.38),
        color=MUTED_COLOR,
        connectionstyle="arc3,rad=0.0",
        linestyle=":",
        linewidth=0.75,
    )
    arrow(
        axis,
        (0.90, 0.455),
        (0.91, 0.425),
        color=MUTED_COLOR,
        connectionstyle="arc3,rad=0.0",
        linestyle=":",
        linewidth=0.75,
    )
    arrow(
        axis,
        (0.458, 0.505),
        (0.458, 0.425),
        color=PRIMARY_COLOR,
        label="answer",
        label_xy=(0.505, 0.465),
        connectionstyle="arc3,rad=0.0",
    )

    figure.subplots_adjust(left=0.005, right=0.995, top=0.995, bottom=0.005)
    for output in save_figure(figure, output_prefix, formats=("png", "pdf", "svg")):
        print(f"Saved: {output}")
    plt.close(figure)


def main() -> None:
    args = parse_args()
    draw_schematic(args.output_prefix)


if __name__ == "__main__":
    main()
