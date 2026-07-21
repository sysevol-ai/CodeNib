#!/usr/bin/env python3
"""Render guarded incremental maintenance and lifecycle accounting."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from draw_materialized_trace import PHASES, _phase_value
from draw_materialized_trace import _validate_summary as validate_lifecycle_summary
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
from matplotlib.ticker import NullFormatter
from plot_style import (
    BASELINE_COLOR,
    DOUBLE_COLUMN_WIDTH,
    GRAPH_COLOR,
    GRID_COLOR,
    MUTED_COLOR,
    NEUTRAL_COLOR,
    PRIMARY_COLOR,
    apply_paper_style,
    panel_title,
    save_figure,
    style_axis,
)
from scipy.stats import gaussian_kde

DEFAULT_OUTPUT_PREFIX = Path(__file__).resolve().parent / "maintenance_lifecycle"
LANGUAGES = ("go", "python", "rust", "ts")
LANGUAGE_LABELS = {
    "go": "Go",
    "python": "Python",
    "rust": "Rust",
    "ts": "TS/JS",
}
VIEW_STYLES = {
    "symbol": {"label": "Symbol", "color": PRIMARY_COLOR, "marker": "s"},
    "file": {"label": "File", "color": GRAPH_COLOR, "marker": "D"},
    "delta": {"label": "Vector", "color": BASELINE_COLOR, "marker": "o"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-results", required=True, type=Path)
    parser.add_argument("--vector-results", required=True, type=Path)
    parser.add_argument("--incremental-summary", required=True, type=Path)
    parser.add_argument("--lifecycle-summary", required=True, type=Path)
    parser.add_argument("--output-prefix", type=Path, default=DEFAULT_OUTPUT_PREFIX)
    return parser.parse_args()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: expected an object")
        rows.append(row)
    return rows


def _positive_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return number


def _transition_identity(row: Mapping[str, Any]) -> tuple[str, int]:
    instance_id = str(row.get("instance_id") or "")
    step = row.get("step")
    if not instance_id or not isinstance(step, int) or step < 1:
        raise ValueError("incremental row is missing instance_id or step")
    return instance_id, step


def _normalize_language(row: Mapping[str, Any]) -> str:
    language = str(row.get("language") or "").lower()
    if language not in LANGUAGES:
        raise ValueError(f"unsupported incremental language: {language!r}")
    return language


def _extract_graph_points(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    points = []
    seen = set()
    for row in rows:
        identity = _transition_identity(row)
        if identity in seen:
            raise ValueError(f"duplicate graph transition: {identity}")
        seen.add(identity)
        if int(row.get("delta_files") or 0) == 0:
            continue
        symbol_speedup = _positive_number(
            row.get("speedup_vs_fully_rebuild"),
            f"graph speedup for {identity}",
        )
        full_time = _positive_number(
            (row.get("fully-rebuild") or {}).get("t_s"),
            f"graph rebuild time for {identity}",
        )
        file_time = _positive_number(
            (row.get("file-level-patch") or {}).get("amortized_t_s"),
            f"graph file-patch time for {identity}",
        )
        language = _normalize_language(row)
        points.extend(
            (
                {
                    "view": "graph",
                    "arm": "symbol",
                    "language": language,
                    "identity": identity,
                    "speedup": symbol_speedup,
                    "admitted": (
                        row.get("equivalence_guarded_speedup_vs_fully_rebuild")
                        is not None
                    ),
                },
                {
                    "view": "graph",
                    "arm": "file",
                    "language": language,
                    "identity": identity,
                    "speedup": full_time / file_time,
                    "admitted": (
                        row.get(
                            "file_level_equivalence_guarded_speedup_vs_fully_rebuild"
                        )
                        is not None
                    ),
                },
            )
        )
    return points


def _extract_vector_points(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    points = []
    seen = set()
    for row in rows:
        identity = _transition_identity(row)
        if identity in seen:
            raise ValueError(f"duplicate vector transition: {identity}")
        seen.add(identity)
        if int(row.get("delta_files_test_excluded") or 0) == 0:
            continue
        full_time = _positive_number(
            (row.get("fully-rebuild") or {}).get("t_s"),
            f"vector rebuild time for {identity}",
        )
        update_time = _positive_number(
            (row.get("incremental") or {}).get("t_s"),
            f"vector update time for {identity}",
        )
        points.append(
            {
                "view": "vector",
                "arm": "delta",
                "language": _normalize_language(row),
                "identity": identity,
                "speedup": full_time / update_time,
                "admitted": row.get("analysis_admitted") is True,
            }
        )
    return points


def _validate_incremental_inputs(
    summary: Mapping[str, Any],
    graph_rows: Sequence[Mapping[str, Any]],
    vector_rows: Sequence[Mapping[str, Any]],
    points: Sequence[Mapping[str, Any]],
) -> None:
    if summary.get("schema_version") != 2:
        raise ValueError("expected incremental-maintenance summary schema 2")
    for view, rows, protocol_key in (
        ("graph", graph_rows, "protocol_version"),
        ("vector", vector_rows, "protocol_version"),
    ):
        view_summary = summary.get(view) or {}
        completeness = view_summary.get("completeness") or {}
        if completeness.get("complete") is not True:
            raise ValueError(f"{view} result set is incomplete")
        if int(completeness.get("unique_transition_rows") or 0) != len(rows):
            raise ValueError(f"{view} row count disagrees with the summary")
        protocols = {row.get(protocol_key) for row in rows}
        if protocols != set(view_summary.get("protocol_versions") or ()):
            raise ValueError(f"{view} protocol set disagrees with the summary")

    for view in ("graph", "vector"):
        by_language = summary[view]["by_language"]
        for language in LANGUAGES:
            observed = [
                point
                for point in points
                if point["view"] == view and point["language"] == language
            ]
            expected = int(by_language[language]["source_changing_rows"])
            expected_points = expected * (2 if view == "graph" else 1)
            if len(observed) != expected_points:
                raise ValueError(
                    f"{view}/{language} has {len(observed)} points, "
                    f"expected {expected_points}"
                )
            if view == "graph":
                for arm, summary_key in (("symbol", "symbol"), ("file", "file")):
                    arm_points = [point for point in observed if point["arm"] == arm]
                    actual_admitted = sum(point["admitted"] for point in arm_points)
                    expected_admitted = int(
                        by_language[language][summary_key]["admitted"]
                    )
                    if actual_admitted != expected_admitted:
                        raise ValueError(
                            f"{view}/{language}/{arm} admission count disagrees "
                            "with the summary"
                        )
            else:
                expected_admitted = int(
                    by_language[language]["incremental"]["admitted"]
                )
                actual_admitted = sum(point["admitted"] for point in observed)
                if actual_admitted != expected_admitted:
                    raise ValueError(
                        f"{view}/{language} admission count disagrees with the summary"
                    )


def _identity_offset(identity: tuple[str, int], amplitude: float = 0.13) -> float:
    payload = f"{identity[0]}:{identity[1]}".encode("utf-8")
    rank = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    return -amplitude + 2 * amplitude * rank / (2**64 - 1)


def _draw_incremental_speedup(axis, points: Sequence[Mapping[str, Any]]) -> None:
    row_specs = [
        ("graph", "go", 8.0),
        ("graph", "python", 7.1),
        ("graph", "rust", 6.2),
        ("graph", "ts", 5.3),
        ("vector", "go", 3.7),
        ("vector", "python", 2.8),
        ("vector", "rust", 1.9),
        ("vector", "ts", 1.0),
    ]
    labels = []
    positions = []
    for view, language, y in row_specs:
        group = [
            point
            for point in points
            if point["view"] == view and point["language"] == language
        ]
        arms = ("symbol", "file") if view == "graph" else ("delta",)
        arm_offsets = {"symbol": 0.13, "file": -0.13, "delta": 0.0}
        for arm in arms:
            style = VIEW_STYLES[arm]
            arm_group = [point for point in group if point["arm"] == arm]
            values = np.asarray([point["speedup"] for point in arm_group], dtype=float)
            q25, median, q75 = np.quantile(values, (0.25, 0.5, 0.75))
            arm_y = y + arm_offsets[arm]
            axis.hlines(
                arm_y,
                q25,
                q75,
                color=style["color"],
                linewidth=1.7,
                zorder=2,
            )
            axis.plot(
                [median, median],
                [arm_y - 0.10, arm_y + 0.10],
                color=NEUTRAL_COLOR,
                linewidth=0.85,
                zorder=3,
            )
            for point in arm_group:
                admitted = bool(point["admitted"])
                axis.scatter(
                    point["speedup"],
                    arm_y + _identity_offset(point["identity"], amplitude=0.055),
                    s=14,
                    marker=style["marker"],
                    facecolor=style["color"] if admitted else "white",
                    edgecolor="white" if admitted else style["color"],
                    linewidth=0.45 if admitted else 0.8,
                    alpha=0.82 if admitted else 1.0,
                    zorder=4,
                )
        labels.append(
            f"{'Graph' if view == 'graph' else 'Vector'} · "
            f"{LANGUAGE_LABELS[language]}"
        )
        positions.append(y)

    axis.axvline(1.0, color=MUTED_COLOR, linewidth=0.75, linestyle=":", zorder=1)
    axis.axhline(4.5, color=GRID_COLOR, linewidth=0.7, zorder=1)
    axis.set_xscale("log")
    axis.set_xlim(0.25, 100)
    axis.set_ylim(0.55, 8.45)
    axis.set_yticks(positions, labels)
    axis.set_xlabel("Observed speedup vs. rebuild (×, log)")
    panel_title(axis, "a", "Guarded incremental maintenance")
    style_axis(axis, grid_axis="x", minor_grid=True, open_frame=True)

    def admission_label(arm: str) -> str:
        arm_points = [point for point in points if point["arm"] == arm]
        admitted = sum(point["admitted"] for point in arm_points)
        return f"{VIEW_STYLES[arm]['label']} {admitted}/{len(arm_points)}"

    arm_legend = axis.legend(
        handles=[
            Line2D(
                [],
                [],
                marker=VIEW_STYLES["symbol"]["marker"],
                color=VIEW_STYLES["symbol"]["color"],
                linestyle="none",
                markersize=3.8,
                label=admission_label("symbol"),
            ),
            Line2D(
                [],
                [],
                marker=VIEW_STYLES["file"]["marker"],
                color=VIEW_STYLES["file"]["color"],
                linestyle="none",
                markersize=3.6,
                label=admission_label("file"),
            ),
            Line2D(
                [],
                [],
                marker=VIEW_STYLES["delta"]["marker"],
                color=VIEW_STYLES["delta"]["color"],
                linestyle="none",
                markersize=3.8,
                label=admission_label("delta"),
            ),
        ],
        loc="center left",
        bbox_to_anchor=(0.01, 0.465),
        ncols=3,
        fontsize=4.8,
        handlelength=0.8,
        columnspacing=0.55,
        borderaxespad=0.0,
    )
    axis.add_artist(arm_legend)
    axis.legend(
        handles=[
            Line2D(
                [],
                [],
                marker="o",
                linestyle="none",
                markerfacecolor=NEUTRAL_COLOR,
                markeredgecolor=NEUTRAL_COLOR,
                markersize=3.8,
                label="Admitted",
            ),
            Line2D(
                [],
                [],
                marker="o",
                linestyle="none",
                markerfacecolor="white",
                markeredgecolor=NEUTRAL_COLOR,
                markersize=3.8,
                label="Unadmitted",
            ),
        ],
        loc="center left",
        bbox_to_anchor=(0.01, 0.405),
        ncols=2,
        fontsize=4.8,
        handlelength=0.8,
        columnspacing=0.55,
        borderaxespad=0.0,
    )


def _format_seconds(value: float) -> str:
    if value < 1:
        return f"{value:.2f}"
    if value < 10:
        return f"{value:.2f}"
    return f"{value:.1f}"


def _draw_horizontal_half_violin(
    axis,
    position: float,
    values: np.ndarray,
    color: str,
) -> float:
    """Draw a log-domain half violin with IQR and P5--P95 summaries."""

    values = values[np.isfinite(values) & (values > 0)]
    if not len(values):
        raise ValueError("lifecycle distribution has no positive observations")
    log_values = np.log10(values)
    if len(np.unique(log_values)) > 1:
        kde = gaussian_kde(log_values, bw_method=0.3)
        grid = np.linspace(log_values.min() - 0.18, log_values.max() + 0.18, 200)
        density = kde(grid)
        density = density / density.max() * 0.27
        axis.fill_between(
            10**grid,
            position,
            position + density,
            color=color,
            alpha=0.22,
            linewidth=0,
            zorder=2,
        )
        axis.plot(
            10**grid,
            position + density,
            color=color,
            linewidth=0.8,
            alpha=0.9,
            zorder=3,
        )

    p05, q25, median, q75, p95 = np.percentile(values, [5, 25, 50, 75, 95])
    summary_y = position - 0.10
    axis.hlines(summary_y, p05, p95, color=color, linewidth=0.85, zorder=4)
    axis.vlines(
        [p05, p95],
        summary_y - 0.035,
        summary_y + 0.035,
        color=color,
        linewidth=0.85,
        zorder=4,
    )
    axis.add_patch(
        Rectangle(
            (q25, position - 0.17),
            q75 - q25,
            0.14,
            facecolor=color,
            edgecolor=NEUTRAL_COLOR,
            linewidth=0.4,
            alpha=0.42,
            zorder=4,
        )
    )
    axis.vlines(
        median,
        position - 0.17,
        position - 0.03,
        color=color,
        linewidth=1.25,
        zorder=5,
    )
    return float(median)


def _draw_lifecycle_terms(axis, rows: Sequence[Mapping[str, Any]]) -> None:
    y_positions = np.arange(len(PHASES) - 1, -1, -1, dtype=float)
    for y, (_label, key, color) in zip(y_positions, PHASES, strict=True):
        values = np.asarray([_phase_value(row, key) for row in rows], dtype=float)
        median = _draw_horizontal_half_violin(axis, y, values, color)
        axis.annotate(
            _format_seconds(median),
            (median, y),
            xytext=(2.2, 2.5),
            textcoords="offset points",
            ha="left",
            va="bottom",
            fontsize=4.8,
            color=NEUTRAL_COLOR,
            bbox={"facecolor": "white", "edgecolor": "none", "pad": 0.15},
            zorder=6,
        )

    axis.axhline(2.5, color=GRID_COLOR, linewidth=0.7, zorder=1)
    axis.set_xscale("log")
    axis.set_xlim(0.025, 6000)
    axis.set_ylim(-0.42, 5.42)
    axis.set_yticks(
        y_positions,
        ["Hydrate views" if phase[1] == "load" else phase[0] for phase in PHASES],
    )
    axis.set_xlabel("Measured wall time (s, log)")
    panel_title(axis, "b", "Lifecycle stage costs")
    style_axis(axis, grid_axis="x", minor_grid=True, open_frame=True)


def _draw_reuse_curve(axis, summary: Mapping[str, Any]) -> None:
    curve = summary["amortization_curve"]
    consumers = np.asarray([row["consumer_count"] for row in curve], dtype=float)
    trace_matches = np.flatnonzero(consumers == 20)
    if len(trace_matches) != 1:
        raise ValueError("lifecycle projection requires exactly one q=20 point")
    trace_index = int(trace_matches[0])
    models = (
        (
            "artifact_shared_process_isolated",
            "Process-isolated",
            PRIMARY_COLOR,
            "s",
            "-",
        ),
        (
            "shared_resident_runtime",
            "Shared resident",
            BASELINE_COLOR,
            "o",
            "--",
        ),
    )
    for key, label, color, marker, linestyle in models:
        stats = [
            row["deployment_models"][key]["effective_seconds_per_consumer"]
            for row in curve
        ]
        medians = np.asarray([item["median"] for item in stats], dtype=float)
        lows = np.asarray([item["ci95_low"] for item in stats], dtype=float)
        highs = np.asarray([item["ci95_high"] for item in stats], dtype=float)
        axis.fill_between(
            consumers,
            lows,
            highs,
            color=color,
            alpha=0.12,
            linewidth=0,
            zorder=1,
        )
        axis.plot(
            consumers,
            medians,
            color=color,
            linestyle=linestyle,
            linewidth=1.0,
            label=label,
            zorder=3,
        )
        axis.errorbar(
            [consumers[trace_index]],
            [medians[trace_index]],
            yerr=[
                [medians[trace_index] - lows[trace_index]],
                [highs[trace_index] - medians[trace_index]],
            ],
            color=color,
            marker=marker,
            markerfacecolor=color,
            markeredgecolor="white",
            markeredgewidth=0.4,
            linestyle="none",
            markersize=4.2,
            capsize=1.4,
            elinewidth=0.65,
            zorder=4,
        )

    service_floor = np.median(
        [
            row["service_seconds"] / row["source_queries"]
            for row in summary["snapshot_rows"]
        ]
    )
    axis.axhline(
        service_floor,
        color=MUTED_COLOR,
        linewidth=0.75,
        linestyle=":",
        label="Warm serve/session",
        zorder=2,
    )
    axis.axvline(20, color=GRID_COLOR, linewidth=0.8, linestyle=(0, (2, 2)))
    axis.text(
        20,
        0.06,
        "trace scale q=20",
        transform=axis.get_xaxis_transform(),
        ha="left",
        va="bottom",
        fontsize=4.8,
        color=MUTED_COLOR,
        rotation=90,
    )
    axis.set_xscale("log")
    axis.set_yscale("log")
    axis.set_xlabel("Sessions / snapshot")
    axis.set_ylabel("Effective time / session (s)")
    axis.set_xticks(consumers, [str(int(value)) for value in consumers])
    axis.get_xaxis().set_minor_formatter(NullFormatter())
    panel_title(axis, "c", "Projected cost under runtime reuse")
    style_axis(axis, grid_axis="both", minor_grid=True, open_frame=True)
    handles, labels = axis.get_legend_handles_labels()
    order = [
        labels.index(label)
        for label in ("Process-isolated", "Shared resident", "Warm serve/session")
    ]
    axis.legend(
        [handles[index] for index in order],
        [labels[index] for index in order],
        loc="upper right",
        fontsize=5.0,
        handlelength=1.35,
        borderaxespad=0.25,
    )


def render(args: argparse.Namespace) -> None:
    graph_rows = _load_jsonl(args.graph_results)
    vector_rows = _load_jsonl(args.vector_results)
    incremental_summary = json.loads(
        args.incremental_summary.read_text(encoding="utf-8")
    )
    lifecycle_summary = json.loads(args.lifecycle_summary.read_text(encoding="utf-8"))
    validate_lifecycle_summary(lifecycle_summary)
    points = [*_extract_graph_points(graph_rows), *_extract_vector_points(vector_rows)]
    _validate_incremental_inputs(
        incremental_summary,
        graph_rows,
        vector_rows,
        points,
    )

    apply_paper_style()
    figure, axes = plt.subplots(
        1,
        3,
        figsize=(DOUBLE_COLUMN_WIDTH, 2.18),
        gridspec_kw={"width_ratios": (1.35, 1.0, 1.0), "wspace": 0.43},
    )
    _draw_incremental_speedup(axes[0], points)
    _draw_lifecycle_terms(axes[1], lifecycle_summary["snapshot_rows"])
    _draw_reuse_curve(axes[2], lifecycle_summary)
    figure.subplots_adjust(left=0.105, right=0.99, top=0.91, bottom=0.20)
    for output in save_figure(figure, args.output_prefix):
        print(f"Saved: {output}")
    plt.close(figure)


def main() -> None:
    render(parse_args())


if __name__ == "__main__":
    main()
