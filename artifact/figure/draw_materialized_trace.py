#!/usr/bin/env python3

"""Draw the integrated materialize--serve lifecycle experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import NullFormatter
from plot_style import (
    BASELINE_COLOR,
    DOUBLE_COLUMN_WIDTH,
    GRID_COLOR,
    MUTED_COLOR,
    NEUTRAL_COLOR,
    PASTEL_BLUE,
    PASTEL_GREEN,
    PASTEL_MAUVE,
    PASTEL_ORANGE,
    PASTEL_TERRACOTTA,
    PRIMARY_COLOR,
    apply_paper_style,
    panel_title,
    save_figure,
    style_axis,
)

PHASES = (
    ("BM25", "bm25", PASTEL_BLUE),
    ("Graph", "symbol_graph", PASTEL_GREEN),
    ("Vector", "vector", PASTEL_ORANGE),
    ("Build total", "materialization", PRIMARY_COLOR),
    ("Hydrate", "load", PASTEL_MAUVE),
    ("Serve", "service", PASTEL_TERRACOTTA),
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument(
        "--output-prefix", type=Path, default=Path("materialized_trace")
    )
    args = parser.parse_args(argv)

    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    _validate_summary(summary)
    figure = draw_materialized_trace(summary)
    outputs = save_figure(figure, args.output_prefix)
    plt.close(figure)
    print("\n".join(str(path) for path in outputs))
    return 0


def draw_materialized_trace(summary: Mapping[str, Any]):
    apply_paper_style()
    figure, (phase_axis, curve_axis) = plt.subplots(
        1,
        2,
        figsize=(DOUBLE_COLUMN_WIDTH, 2.35),
        gridspec_kw={"width_ratios": (1.42, 1.0), "wspace": 0.30},
    )
    _draw_phase_costs(phase_axis, summary["snapshot_rows"])
    _draw_amortization(curve_axis, summary)
    return figure


def _draw_phase_costs(
    axis,
    rows: Sequence[Mapping[str, Any]],
    *,
    panel_label: str = "a",
) -> None:
    offsets = np.asarray([_snapshot_offset(row) for row in rows], dtype=float)
    for phase_index, (_label, key, color) in enumerate(PHASES):
        values = np.asarray([_phase_value(row, key) for row in rows], dtype=float)
        axis.scatter(
            phase_index + offsets,
            values,
            s=9,
            marker="o",
            facecolor=color,
            edgecolor="white",
            linewidth=0.32,
            alpha=1.0,
            zorder=3,
        )
        q25, median, q75 = np.quantile(values, (0.25, 0.5, 0.75))
        axis.vlines(
            phase_index,
            q25,
            q75,
            color=NEUTRAL_COLOR,
            linewidth=2.2,
            zorder=4,
        )
        axis.plot(
            [phase_index - 0.13, phase_index + 0.13],
            [median, median],
            color=NEUTRAL_COLOR,
            linewidth=1.05,
            zorder=5,
        )

    axis.set_yscale("log")
    axis.set_ylabel("Wall time (s, log scale)")
    axis.set_xticks(range(len(PHASES)), [phase[0] for phase in PHASES])
    axis.tick_params(axis="x", rotation=24)
    axis.set_xlim(-0.48, len(PHASES) - 0.52)
    panel_title(axis, panel_label, "Measured stage and view costs")
    style_axis(axis, grid_axis="y", minor_grid=True, open_frame=True)


def _draw_amortization(
    axis,
    summary: Mapping[str, Any],
    *,
    panel_label: str = "b",
    legend_loc: str = "upper right",
) -> None:
    curve = summary["amortization_curve"]
    consumers = np.asarray([row["consumer_count"] for row in curve], dtype=float)
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
    legend_handles = []
    for key, label, color, marker, linestyle in models:
        stats = [
            row["deployment_models"][key]["effective_seconds_per_consumer"]
            for row in curve
        ]
        medians = np.asarray([item["median"] for item in stats], dtype=float)
        lows = np.asarray([item["ci95_low"] for item in stats], dtype=float)
        highs = np.asarray([item["ci95_high"] for item in stats], dtype=float)
        handle = axis.errorbar(
            consumers,
            medians,
            yerr=np.vstack((medians - lows, highs - medians)),
            color=color,
            marker=marker,
            markerfacecolor=color,
            markeredgecolor="white",
            markeredgewidth=0.4,
            linestyle=linestyle,
            linewidth=1.15,
            markersize=3.8,
            capsize=1.7,
            elinewidth=0.65,
            label=label,
            zorder=3,
        )
        legend_handles.append(handle)

    service_floor = np.median(
        [
            row["service_seconds"] / row["source_queries"]
            for row in summary["snapshot_rows"]
        ]
    )
    service_handle = axis.axhline(
        service_floor,
        color=MUTED_COLOR,
        linewidth=0.8,
        linestyle=":",
        label="Measured service/session",
        zorder=2,
    )
    axis.axvline(20, color=GRID_COLOR, linewidth=0.8, linestyle=(0, (2, 2)))
    axis.text(
        20,
        0.36,
        "trace mix: q=20",
        transform=axis.get_xaxis_transform(),
        ha="left",
        va="center",
        fontsize=5.7,
        color=MUTED_COLOR,
        rotation=90,
    )
    axis.set_xscale("log")
    axis.set_yscale("log")
    axis.set_xlabel("Source-query sessions / snapshot")
    axis.set_ylabel("Effective time / session (s)")
    axis.set_xticks(consumers, [str(int(value)) for value in consumers])
    axis.get_xaxis().set_minor_formatter(NullFormatter())
    panel_title(axis, panel_label, "Projected cost under reuse")
    style_axis(axis, grid_axis="both", minor_grid=True, open_frame=True)
    axis.legend(
        handles=[*legend_handles, service_handle],
        loc=legend_loc,
        fontsize=5.8,
        handlelength=1.7,
    )


def _snapshot_offset(row: Mapping[str, Any]) -> float:
    """Place each snapshot reproducibly without encoding row or value order."""
    identity = str(row["instance_id"]).encode("utf-8")
    rank = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big")
    return -0.20 + 0.40 * rank / (2**64 - 1)


def _phase_value(row: Mapping[str, Any], key: str) -> float:
    if key in {"bm25", "symbol_graph", "vector"}:
        value = row["per_index_seconds"][key]
    elif key == "materialization":
        value = row["materialization_seconds"]
    elif key == "load":
        value = row["load_seconds"]
    elif key == "service":
        value = row["service_seconds"]
    else:
        raise ValueError(f"unknown phase {key!r}")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"phase {key!r} must be finite and positive")
    return number


def _validate_summary(summary: Mapping[str, Any]) -> None:
    protocol = summary.get("protocol") or {}
    if summary.get("schema_version") != 2:
        raise ValueError("expected materialized-trace summary schema 2")
    if protocol.get("snapshot_count") != 25:
        raise ValueError("paper figure requires all 25 repository snapshots")
    rows = summary.get("snapshot_rows")
    if not isinstance(rows, list) or len(rows) != 25:
        raise ValueError("summary does not contain 25 snapshot rows")
    curve = summary.get("amortization_curve")
    if not isinstance(curve, list) or not curve:
        raise ValueError("summary has no amortization curve")
    required_models = {
        "artifact_shared_process_isolated",
        "shared_resident_runtime",
    }
    for point in curve:
        if set((point.get("deployment_models") or {})) != required_models:
            raise ValueError("amortization point has incomplete deployment models")


if __name__ == "__main__":
    raise SystemExit(main())
