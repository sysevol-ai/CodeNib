#!/usr/bin/env python3
"""Render paired agent-runtime token and localization guardrail results."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
from matplotlib.ticker import MultipleLocator
from plot_style import (
    BASELINE_COLOR,
    BASELINE_LIGHT,
    DOUBLE_COLUMN_WIDTH,
    NEUTRAL_COLOR,
    PANEL_FILL,
    PRIMARY_COLOR,
    PRIMARY_LIGHT,
    SINGLE_COLUMN_WIDTH,
    apply_paper_style,
    panel_title,
    save_figure,
    style_axis,
)

DEFAULT_OUTPUT_PREFIX = Path(__file__).resolve().parent / "agent_runtime"
DEFAULT_BREAKDOWN_OUTPUT_PREFIX = (
    Path(__file__).resolve().parent / "agent_runtime_breakdown"
)

MODEL_ORDER = (
    "Claude Haiku 4.5",
    "Qwen3.5-9B",
    "Qwen3.5-27B",
    "Gemma 4-12B",
    "Gemini 2.5 Flash",
)
LANGUAGE_ORDER = ("All", "C++_C", "Go", "Python", "Rust", "TypeScript_JavaScript")
LANGUAGE_LABELS = {
    "All": "All",
    "C++_C": "C/C++",
    "Go": "Go",
    "Python": "Python",
    "Rust": "Rust",
    "TypeScript_JavaScript": "TS/JS",
}
CATEGORY_ORDER = (
    "behavioral",
    "file_hint",
    "module_hint",
    "reasoning",
    "symbol_hint",
    "traversal",
)
CATEGORY_LABELS = {
    "behavioral": "Behavioral",
    "file_hint": "File hint",
    "module_hint": "Module hint",
    "reasoning": "Reasoning",
    "symbol_hint": "Symbol hint",
    "traversal": "Traversal",
}
ARM_IDS = {
    "grep": "grep_only",
    "eager": "preinj_eager",
    "compact": "preinj_eager_compact",
}
ARM_ORDER = ("eager", "compact")
ARM_LABELS = {
    "eager": "Eager",
    "compact": "Eager + compact",
}
ARM_COLORS = {"eager": BASELINE_COLOR, "compact": PRIMARY_COLOR}
ARM_FILLS = {"eager": BASELINE_LIGHT, "compact": PRIMARY_LIGHT}
ARM_HATCHES = {"eager": "///", "compact": "xx"}
ARM_LINESTYLES = {"eager": "--", "compact": "-"}
MODEL_MATRIX_LABELS = {
    "Claude Haiku 4.5": "Haiku",
    "Qwen3.5-9B": "9B",
    "Qwen3.5-27B": "27B",
    "Gemma 4-12B": "Gemma4-12B",
    "Gemini 2.5 Flash": "Gemini-Flash",
}
ALLOWED_RUNTIME_TOOLS = frozenset({"read", "grep", "glob", "bash"})

apply_paper_style()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--haiku-root",
        type=Path,
        action="append",
        dest="haiku_roots",
        required=True,
        help="Haiku result root; repeat for split result directories.",
    )
    parser.add_argument(
        "--qwen9b-root",
        type=Path,
        action="append",
        dest="qwen9b_roots",
        required=True,
        help="Qwen3.5-9B result root; repeat for split result directories.",
    )
    parser.add_argument(
        "--qwen27b-root",
        "--qwen-root",
        type=Path,
        action="append",
        dest="qwen27b_roots",
        required=True,
        help="Qwen3.5-27B result root; repeat for split result directories.",
    )
    parser.add_argument(
        "--gemma12b-root",
        type=Path,
        action="append",
        dest="gemma12b_roots",
        required=True,
        help="Gemma 4-12B result root; repeat for split result directories.",
    )
    parser.add_argument(
        "--gemini25-root",
        type=Path,
        action="append",
        dest="gemini25_roots",
        required=True,
        help="Gemini 2.5 Flash result root; repeat for split result directories.",
    )
    parser.add_argument("--output-prefix", type=Path, default=DEFAULT_OUTPUT_PREFIX)
    parser.add_argument(
        "--breakdown-output-prefix",
        type=Path,
        default=DEFAULT_BREAKDOWN_OUTPUT_PREFIX,
    )
    parser.add_argument("--expected-pairs", type=int, default=500)
    parser.add_argument(
        "--exclude-query-id",
        action="append",
        default=[],
        help="Query id to exclude after loading the complete paired matrix.",
    )
    parser.add_argument("--bootstrap-samples", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20260713)
    parser.add_argument("--recall-guardrail", type=float, default=-0.05)
    parser.add_argument(
        "--provenance-root",
        type=Path,
        help=(
            "Store input roots relative to this directory so the metrics JSON "
            "is portable across artifact extractions."
        ),
    )
    return parser.parse_args()


def _cell_paths(root: Path) -> Iterable[Path]:
    direct = root / "cells"
    if direct.is_dir():
        yield from direct.glob("*.json")
    yield from root.glob("**/cells/*.json")


def load_paired_cells(
    roots: Sequence[Path], model_label: str, expected_pairs: int
) -> dict[str, dict[str, dict[str, Any]]]:
    """Load a strict three-arm matrix keyed by synthesis query id."""

    arm_by_id = {subset_id: arm for arm, subset_id in ARM_IDS.items()}
    cells: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    seen_paths: set[Path] = set()
    for root in roots:
        for path in _cell_paths(root):
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            cell = json.loads(path.read_text(encoding="utf-8"))
            arm = arm_by_id.get(str(cell.get("subset_id") or ""))
            if arm is None:
                continue
            if not cell.get("success") or not cell.get("metrics_meaningful"):
                continue
            query_id = str(cell.get("query_id") or "")
            if not query_id:
                raise ValueError(f"{path}: missing query_id")
            if arm in cells[query_id]:
                raise ValueError(
                    f"duplicate {model_label} cell for query={query_id!r}, arm={arm!r}"
                )
            unexpected_tools = {
                str(call.get("skill_id") or "")
                for call in cell.get("tool_calls") or []
                if not call.get("error")
            } - ALLOWED_RUNTIME_TOOLS
            if unexpected_tools:
                raise ValueError(
                    f"{path}: executed unadvertised runtime tools "
                    f"{sorted(unexpected_tools)}"
                )
            cells[query_id][arm] = cell

    paired = {
        query_id: arms for query_id, arms in cells.items() if set(arms) == set(ARM_IDS)
    }
    if len(paired) != expected_pairs:
        incomplete = {
            query_id: sorted(set(ARM_IDS) - set(arms))
            for query_id, arms in cells.items()
            if set(arms) != set(ARM_IDS)
        }
        raise ValueError(
            f"{model_label}: expected {expected_pairs} complete pairs, found "
            f"{len(paired)}; incomplete={len(incomplete)}"
        )
    return paired


def summarize_input_provenance(
    roots: Sequence[Path],
    paired: Mapping[str, Mapping[str, Mapping[str, Any]]],
    provenance_root: Path | None = None,
) -> dict[str, Any]:
    """Fingerprint the exact successful cells consumed by the figure."""

    digest = hashlib.sha256()
    model_ids: set[str] = set()
    source_configs: set[str] = set()
    instance_ids: set[str] = set()
    for query_id in sorted(paired):
        for arm in sorted(paired[query_id]):
            cell = paired[query_id][arm]
            payload = json.dumps(
                cell,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
            digest.update(query_id.encode("utf-8"))
            digest.update(b"\0")
            digest.update(arm.encode("utf-8"))
            digest.update(b"\0")
            digest.update(payload)
            digest.update(b"\n")
            model_ids.add(str(cell.get("model") or ""))
            source_configs.add(str(cell.get("source_config") or ""))
            instance_ids.add(str(cell.get("instance_id") or ""))

    metadata = []
    resolved_provenance_root = provenance_root.resolve() if provenance_root else None
    for root in roots:
        resolved_root = root.resolve()
        if resolved_provenance_root:
            try:
                recorded_root = resolved_root.relative_to(resolved_provenance_root)
            except ValueError as exc:
                raise ValueError(
                    f"input root {resolved_root} is outside provenance root "
                    f"{resolved_provenance_root}"
                ) from exc
            root_value = recorded_root.as_posix()
        else:
            root_value = str(resolved_root)
        files = []
        for relative in (
            Path("protocol/manifest.json"),
            Path("protocol/query_identity.json"),
            Path("analysis/analysis_manifest.json"),
            Path("final_status.json"),
            Path("synthesis_summary.json"),
            Path("run.log"),
        ):
            path = resolved_root / relative
            if path.is_file():
                files.append(
                    {
                        "path": str(relative),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
        metadata.append({"root": root_value, "metadata_files": files})

    return {
        "roots": metadata,
        "root_path_contract": (
            "relative_to_provenance_root" if resolved_provenance_root else "absolute"
        ),
        "selected_cell_count": sum(len(arms) for arms in paired.values()),
        "selected_cells_sha256": digest.hexdigest(),
        "query_count": len(paired),
        "repository_snapshot_count": len(instance_ids),
        "model_ids": sorted(model_ids),
        "source_configs": sorted(source_configs),
    }


def _recall_at_5(cell: Mapping[str, Any]) -> float:
    answer_blocks = (cell.get("metrics") or {}).get("answer_blocks") or {}
    row = answer_blocks.get("5") or answer_blocks.get(5) or {}
    value = row.get("recall")
    if not isinstance(value, (int, float)):
        raise ValueError(f"cell {cell.get('cell_id')}: missing answer block Recall@5")
    return float(value)


def _keys_for_language(
    paired: Mapping[str, Mapping[str, Mapping[str, Any]]], language: str
) -> list[str]:
    if language == "All":
        return sorted(paired)
    return sorted(
        query_id
        for query_id, arms in paired.items()
        if arms["grep"].get("source_config") == language
    )


def _keys_for_category(
    paired: Mapping[str, Mapping[str, Mapping[str, Any]]], category: str
) -> list[str]:
    return sorted(
        query_id
        for query_id, arms in paired.items()
        if arms["grep"].get("category") == category
    )


def _cluster_bootstrap_arm(
    paired: Mapping[str, Mapping[str, Mapping[str, Any]]],
    keys: Sequence[str],
    arm: str,
    *,
    baseline_arm: str = "grep",
    samples: int,
    seed: int,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    by_instance: dict[str, list[str]] = defaultdict(list)
    for query_id in keys:
        instance_id = str(paired[query_id]["grep"].get("instance_id") or "")
        by_instance[instance_id].append(query_id)
    clusters = sorted(by_instance)
    if not clusters:
        raise ValueError("cannot bootstrap an empty stratum")

    baseline_sums = np.asarray(
        [
            sum(
                paired[key][baseline_arm]["total_tokens"]
                for key in by_instance[cluster]
            )
            for cluster in clusters
        ],
        dtype=float,
    )
    candidate_sums = np.asarray(
        [
            sum(paired[key][arm]["total_tokens"] for key in by_instance[cluster])
            for cluster in clusters
        ],
        dtype=float,
    )
    recall_delta_sums = np.asarray(
        [
            sum(
                _recall_at_5(paired[key][arm]) - _recall_at_5(paired[key][baseline_arm])
                for key in by_instance[cluster]
            )
            for cluster in clusters
        ],
        dtype=float,
    )
    query_counts = np.asarray(
        [len(by_instance[cluster]) for cluster in clusters], dtype=float
    )

    ratio_observed = 100.0 * candidate_sums.sum() / baseline_sums.sum()
    delta_observed = recall_delta_sums.sum() / query_counts.sum()
    ratio_indices = np.random.default_rng(seed).integers(
        0, len(clusters), size=(samples, len(clusters))
    )
    delta_indices = np.random.default_rng(seed + 1).integers(
        0, len(clusters), size=(samples, len(clusters))
    )
    ratio_bootstrap = (
        100.0
        * candidate_sums[ratio_indices].sum(axis=1)
        / baseline_sums[ratio_indices].sum(axis=1)
    )
    delta_bootstrap = recall_delta_sums[delta_indices].sum(axis=1) / query_counts[
        delta_indices
    ].sum(axis=1)
    ratio_low, ratio_high = np.percentile(ratio_bootstrap, [2.5, 97.5])
    delta_low, delta_high = np.percentile(delta_bootstrap, [2.5, 97.5])
    return (
        (float(ratio_observed), float(ratio_low), float(ratio_high)),
        (float(delta_observed), float(delta_low), float(delta_high)),
    )


def _summarize_strata(
    paired: Mapping[str, Mapping[str, Mapping[str, Any]]],
    strata: Sequence[str],
    key_selector: Callable[
        [Mapping[str, Mapping[str, Mapping[str, Any]]], str], list[str]
    ],
    *,
    bootstrap_samples: int,
    seed: int,
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for stratum_index, stratum in enumerate(strata):
        keys = key_selector(paired, stratum)
        stratum_row: dict[str, Any] = {
            "queries": len(keys),
            "repositories": len(
                {paired[key]["grep"].get("instance_id") for key in keys}
            ),
            "grep_mean_tokens": float(
                np.mean([paired[key]["grep"]["total_tokens"] for key in keys])
            ),
            "grep_block_recall_at_5": float(
                np.mean([_recall_at_5(paired[key]["grep"]) for key in keys])
            ),
            "arms": {},
        }
        for arm_index, arm in enumerate(ARM_ORDER):
            bootstrap_seed = seed + stratum_index * 101 + arm_index * 10_007
            ratio_result, delta_result = _cluster_bootstrap_arm(
                paired,
                keys,
                arm,
                samples=bootstrap_samples,
                seed=bootstrap_seed,
            )
            ratio, ratio_low, ratio_high = ratio_result
            delta, delta_low, delta_high = delta_result
            stratum_row["arms"][arm] = {
                "token_percent_of_grep": ratio,
                "token_percent_ci95": [ratio_low, ratio_high],
                "block_recall_at_5": float(
                    np.mean([_recall_at_5(paired[key][arm]) for key in keys])
                ),
                "block_recall_at_5_delta": delta,
                "block_recall_at_5_delta_ci95": [delta_low, delta_high],
            }
        summary[stratum] = stratum_row
    return summary


def summarize_model(
    paired: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    bootstrap_samples: int,
    seed: int,
) -> dict[str, Any]:
    return _summarize_strata(
        paired,
        LANGUAGE_ORDER,
        _keys_for_language,
        bootstrap_samples=bootstrap_samples,
        seed=seed,
    )


def summarize_categories(
    paired: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    bootstrap_samples: int,
    seed: int,
) -> dict[str, Any]:
    return _summarize_strata(
        paired,
        CATEGORY_ORDER,
        _keys_for_category,
        bootstrap_samples=bootstrap_samples,
        seed=seed,
    )


def pool_model_runs(
    paired_by_model: Mapping[str, Mapping[str, Mapping[str, Mapping[str, Any]]]],
) -> dict[str, Mapping[str, Mapping[str, Any]]]:
    """Combine model runs while retaining repository-level bootstrap clusters."""

    pooled = {}
    for model, paired in paired_by_model.items():
        for query_id, arms in paired.items():
            pooled[f"{model}\0{query_id}"] = arms
    return pooled


def _summarize_execution_cells(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    calls = [call for cell in cells for call in cell.get("tool_calls") or []]
    unadvertised_attempts = [
        call
        for call in calls
        if str(call.get("skill_id") or "") not in ALLOWED_RUNTIME_TOOLS
    ]
    return {
        "cells": len(cells),
        "cells_with_tool_errors": sum(
            any(call.get("error") for call in cell.get("tool_calls") or [])
            for cell in cells
        ),
        "tool_call_errors": sum(bool(call.get("error")) for call in calls),
        "unadvertised_tool_attempts": len(unadvertised_attempts),
        "unadvertised_tool_ids": sorted(
            {str(call.get("skill_id") or "") for call in unadvertised_attempts}
        ),
        "executed_unadvertised_tools": sum(
            not call.get("error") for call in unadvertised_attempts
        ),
        "format_failures": sum(bool(cell.get("format_failed")) for cell in cells),
        "max_turns_exhausted": sum(
            bool((cell.get("trace_summary") or {}).get("max_turns_exhausted"))
            for cell in cells
        ),
        "forced_final_answers": sum(
            bool((cell.get("trace_summary") or {}).get("forced_final_answer"))
            for cell in cells
        ),
    }


def summarize_execution_audit(
    paired: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    arm_order = ("grep", *ARM_ORDER)
    result = _summarize_execution_cells(
        [cell for arms in paired.values() for cell in arms.values()]
    )
    result["by_arm"] = {
        arm: _summarize_execution_cells([arms[arm] for arms in paired.values()])
        for arm in arm_order
    }
    return result


def build_plot_data(
    paired: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Preserve query and repository variation used by the paper figure."""

    query_ids = sorted(paired)
    query_level: dict[str, Any] = {}
    for arm in ARM_ORDER:
        query_level[arm] = {
            "token_percent_of_grep": [
                float(
                    100.0
                    * paired[query_id][arm]["total_tokens"]
                    / paired[query_id]["grep"]["total_tokens"]
                )
                for query_id in query_ids
            ],
            "block_recall_at_5_delta": [
                float(
                    _recall_at_5(paired[query_id][arm])
                    - _recall_at_5(paired[query_id]["grep"])
                )
                for query_id in query_ids
            ],
        }

    by_repository: dict[str, list[Mapping[str, Mapping[str, Any]]]] = defaultdict(list)
    for query_id in query_ids:
        cells = paired[query_id]
        repository = str(cells["grep"].get("instance_id") or "")
        by_repository[repository].append(cells)

    repository_level: dict[str, list[dict[str, Any]]] = {}
    for arm in ARM_ORDER:
        rows = []
        for repository in sorted(by_repository):
            cells = by_repository[repository]
            baseline_tokens = np.mean([cell["grep"]["total_tokens"] for cell in cells])
            arm_tokens = np.mean([cell[arm]["total_tokens"] for cell in cells])
            rows.append(
                {
                    "repository": repository,
                    "queries": len(cells),
                    "token_percent_of_grep": float(
                        100.0 * arm_tokens / baseline_tokens
                    ),
                    "block_recall_at_5_delta": float(
                        np.mean(
                            [
                                _recall_at_5(cell[arm]) - _recall_at_5(cell["grep"])
                                for cell in cells
                            ]
                        )
                    ),
                }
            )
        repository_level[arm] = rows

    return {
        "query_ids": query_ids,
        "query_level": query_level,
        "repository_level": repository_level,
    }


def _plot_runtime_policies(ax) -> None:
    """Show exactly which context survives the first successful read."""

    panel_title(ax, "a", "Runtime Protocol", pad=4.0)
    ax.set_xlim(0.0, 1.0)
    ax.set_ylim(-0.65, 3.25)
    ax.axis("off")

    split = 0.57
    ax.text(0.35, 2.86, "explore", fontsize=5.5, ha="center")
    ax.text(0.79, 2.86, "continue / commit", fontsize=5.5, ha="center")
    ax.axvline(split, ymin=0.14, ymax=0.83, color="#777777", linestyle=":", lw=0.8)
    ax.text(
        split,
        2.58,
        "first successful read",
        color="#555555",
        fontsize=5.1,
        ha="center",
        va="bottom",
    )

    rows = (
        (
            2.05,
            "Base",
            "query +\ntool history",
            "full history\ngrows",
            NEUTRAL_COLOR,
            "#EEEAE7",
            "..",
        ),
        (
            1.05,
            "Eager",
            "query + Top-10 L2\n+ tool history",
            "preload retained +\ntool history grows",
            ARM_COLORS["eager"],
            ARM_FILLS["eager"],
            ARM_HATCHES["eager"],
        ),
        (
            0.05,
            "Compact",
            "query + Top-10 L2\n+ tool history",
            "RESET\nquery + latest read\n+ $\\leq$600-char assessment",
            ARM_COLORS["compact"],
            ARM_FILLS["compact"],
            ARM_HATCHES["compact"],
        ),
    )
    for y, label, before, after, color, fill, hatch in rows:
        ax.text(
            0.12,
            y,
            label,
            color=color,
            fontsize=5.8,
            fontweight="bold",
            ha="right",
            va="center",
        )
        ax.add_patch(
            Rectangle(
                (0.15, y - 0.29),
                split - 0.17,
                0.58,
                facecolor=fill,
                edgecolor=color,
                linewidth=0.8,
                hatch=hatch,
            )
        )
        after_fill = PRIMARY_LIGHT if label == "Compact" else fill
        ax.add_patch(
            Rectangle(
                (split + 0.02, y - 0.29),
                0.40,
                0.58,
                facecolor=after_fill,
                edgecolor=color,
                linewidth=1.15 if label == "Compact" else 0.8,
                hatch=hatch,
            )
        )
        ax.text(0.35, y, before, fontsize=4.25, ha="center", va="center")
        ax.text(0.79, y, after, fontsize=4.05, ha="center", va="center")


def _plot_model_token_costs(ax, summary: Mapping[str, Any]) -> None:
    """Compare model-level trajectory cost against the paired baseline."""

    centers = np.arange(len(MODEL_ORDER) - 1, -1, -1, dtype=float)
    offsets = {"eager": 0.15, "compact": -0.15}

    for center in centers[::2]:
        ax.axhspan(center - 0.43, center + 0.43, color=PANEL_FILL, zorder=0)

    for arm in ARM_ORDER:
        rows = [summary[model]["All"]["arms"][arm] for model in MODEL_ORDER]
        token_cost = np.asarray(
            [row["token_percent_of_grep"] for row in rows], dtype=float
        )
        token_ci = np.asarray([row["token_percent_ci95"] for row in rows])
        y = centers + offsets[arm]
        ax.hlines(
            y,
            0.0,
            token_cost,
            color=ARM_COLORS[arm],
            linewidth=1.15,
            alpha=0.78,
            zorder=1,
        )
        ax.errorbar(
            token_cost,
            y,
            xerr=[token_cost - token_ci[:, 0], token_ci[:, 1] - token_cost],
            fmt=BREAKDOWN_MARKERS[arm],
            color=ARM_COLORS[arm],
            ecolor=ARM_COLORS[arm],
            markerfacecolor=ARM_FILLS[arm],
            markeredgecolor=ARM_COLORS[arm],
            markeredgewidth=0.65,
            markersize=4.2,
            elinewidth=0.8,
            capsize=1.5,
            capthick=0.7,
            zorder=3,
        )

    ax.axvline(100.0, color=NEUTRAL_COLOR, linestyle=":", linewidth=0.7)
    ax.text(
        100.0,
        0.99,
        "grep/read",
        transform=ax.get_xaxis_transform(),
        color=NEUTRAL_COLOR,
        fontsize=4.6,
        ha="right",
        va="top",
        bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.8, "pad": 0.2},
    )
    ax.set_xlim(0.0, 105.0)
    ax.xaxis.set_major_locator(MultipleLocator(25.0))
    ax.set_xlabel("Trajectory tokens (% grep/read) $\\downarrow$")
    ax.set_yticks(centers, labels=[MODEL_MATRIX_LABELS[model] for model in MODEL_ORDER])
    ax.set_ylim(-0.48, len(MODEL_ORDER) - 0.52)
    ax.tick_params(axis="y", length=0, pad=1.5)
    style_axis(ax, grid_axis="x", open_frame=True)
    panel_title(ax, "a", "Token Cost by Model", pad=3.0)


def _plot_model_quality_effects(
    ax, summary: Mapping[str, Any], recall_guardrail: float
) -> None:
    """Show model-level localization effects against the admission rule."""

    centers = np.arange(len(MODEL_ORDER) - 1, -1, -1, dtype=float)
    offsets = {"eager": 0.15, "compact": -0.15}
    quality_bounds = [0.0, recall_guardrail]

    for center in centers[::2]:
        ax.axhspan(center - 0.43, center + 0.43, color=PANEL_FILL, zorder=0)

    for arm in ARM_ORDER:
        rows = [summary[model]["All"]["arms"][arm] for model in MODEL_ORDER]
        deltas = np.asarray(
            [row["block_recall_at_5_delta"] for row in rows], dtype=float
        )
        recall_ci = np.asarray(
            [row["block_recall_at_5_delta_ci95"] for row in rows], dtype=float
        )
        quality_bounds.extend(recall_ci.ravel().tolist())
        ax.errorbar(
            deltas,
            centers + offsets[arm],
            xerr=[deltas - recall_ci[:, 0], recall_ci[:, 1] - deltas],
            color=ARM_COLORS[arm],
            linestyle="none",
            marker=BREAKDOWN_MARKERS[arm],
            markerfacecolor=ARM_COLORS[arm],
            markeredgecolor=NEUTRAL_COLOR,
            markeredgewidth=0.3,
            markersize=3.8,
            elinewidth=0.8,
            capsize=1.5,
            capthick=0.7,
            zorder=3,
        )

    span = max(quality_bounds) - min(quality_bounds)
    margin = max(0.012, 0.16 * span)
    ax.set_xlim(min(quality_bounds) - margin, max(quality_bounds) + margin)
    ax.axvline(0.0, color=NEUTRAL_COLOR, linewidth=0.55)
    ax.axvline(
        recall_guardrail,
        color=BASELINE_COLOR,
        linestyle=":",
        linewidth=0.7,
    )
    ax.xaxis.set_major_locator(MultipleLocator(0.05))
    ax.set_xlabel("$\\Delta$ answer-block R@5 $\\uparrow$")
    ax.set_yticks([])
    ax.set_ylim(-0.48, len(MODEL_ORDER) - 0.52)
    style_axis(ax, grid_axis="x", open_frame=True)

    reference_transform = ax.get_xaxis_transform()
    ax.text(
        recall_guardrail,
        0.985,
        "admission gate",
        transform=reference_transform,
        color=BASELINE_COLOR,
        fontsize=4.35,
        ha="right",
        va="top",
        bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.8, "pad": 0.25},
    )
    ax.text(
        0.0,
        0.985,
        "no change",
        transform=reference_transform,
        color=NEUTRAL_COLOR,
        fontsize=4.35,
        ha="left",
        va="top",
        bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.8, "pad": 0.25},
    )
    ax.tick_params(axis="both", labelsize=5.2)
    ax.xaxis.label.set_size(5.6)
    panel_title(ax, "b", "Localization Quality by Model", pad=3.0)


BREAKDOWN_MARKERS = {"eager": "o", "compact": "s"}


def _stratified_metric_limits(
    strata: Sequence[tuple[Mapping[str, Any], Sequence[str]]],
    *,
    metric: str,
    interval: str,
    reference: float,
    recall_guardrail: float,
    include_reference: bool = True,
) -> tuple[float, float]:
    limits = [reference] if include_reference else []
    if metric == "block_recall_at_5_delta":
        limits.append(recall_guardrail)
    for summaries, groups in strata:
        for group in groups:
            for arm in ARM_ORDER:
                low, high = summaries[group]["arms"][arm][interval]
                limits.extend((float(low), float(high)))
    span = max(limits) - min(limits)
    floor = 0.02 if metric == "block_recall_at_5_delta" else 4.0
    margin = max(floor, 0.08 * span)
    return min(limits) - margin, max(limits) + margin


def _plot_pooled_stratified_forest(
    ax,
    summaries: Mapping[str, Any],
    groups: Sequence[str],
    group_labels: Mapping[str, str],
    *,
    label: str,
    title: str,
    metric: str,
    interval: str,
    reference: float,
    recall_guardrail: float,
    xlim: tuple[float, float],
    show_ylabels: bool,
    show_xlabel: bool,
) -> None:
    offsets = {"eager": 0.13, "compact": -0.13}
    centers = np.arange(len(groups) - 1, -1, -1, dtype=float)

    for group_index, group in enumerate(groups):
        center = centers[group_index]
        if group_index % 2 == 0:
            ax.axhspan(center - 0.44, center + 0.44, color=PANEL_FILL, zorder=0)
        for arm in ARM_ORDER:
            row = summaries[group]["arms"][arm]
            value = float(row[metric])
            low, high = (float(bound) for bound in row[interval])
            ax.errorbar(
                value,
                center + offsets[arm],
                xerr=[[value - low], [high - value]],
                fmt=BREAKDOWN_MARKERS[arm],
                markersize=3.7,
                color=ARM_COLORS[arm],
                markeredgecolor=NEUTRAL_COLOR,
                markeredgewidth=0.3,
                elinewidth=0.8,
                capsize=1.5,
                capthick=0.7,
                zorder=3,
            )

    labels = [group_labels[group] for group in groups] if show_ylabels else []
    ax.set_yticks(centers, labels=labels)
    ax.set_xlim(*xlim)
    ax.set_ylim(-0.48, len(groups) - 0.52)
    if show_ylabels:
        ax.text(
            1.055,
            1.015,
            "n",
            transform=ax.transAxes,
            ha="center",
            va="bottom",
            fontsize=5.0,
            fontweight="bold",
            color="#555555",
            clip_on=False,
        )
        for group_index, group in enumerate(groups):
            ax.text(
                1.035,
                centers[group_index],
                f"{summaries[group]['queries']}",
                transform=ax.get_yaxis_transform(),
                ha="left",
                va="center",
                fontsize=5.0,
                color="#555555",
                zorder=4,
                clip_on=False,
            )
    if xlim[0] <= reference <= xlim[1]:
        ax.axvline(
            reference,
            color="#666666",
            linestyle="-" if reference == 0.0 else "--",
            linewidth=0.75,
        )
    if metric == "block_recall_at_5_delta":
        ax.axvline(
            recall_guardrail,
            color=BASELINE_COLOR,
            linestyle=":",
            linewidth=0.8,
        )
        xlabel = r"$\Delta$ answer-block R@5"
        ax.xaxis.set_major_locator(MultipleLocator(0.1))
    else:
        xlabel = "Trajectory tokens (% of grep/read)"
        ax.xaxis.set_major_locator(MultipleLocator(10.0))
    if show_xlabel:
        ax.set_xlabel(xlabel)
    panel_title(ax, label, title, pad=4.0)
    ax.title.set_fontsize(7.0)
    style_axis(ax, grid_axis="x", open_frame=True)
    ax.tick_params(axis="both", labelsize=5.8)
    ax.xaxis.label.set_size(6.4)
    if not show_xlabel:
        ax.tick_params(axis="x", labelbottom=False)
    if not show_ylabels:
        ax.tick_params(axis="y", length=0)


def _breakdown_legend_handles() -> list[Line2D]:
    return [
        Line2D(
            [],
            [],
            color=ARM_COLORS[arm],
            linestyle=ARM_LINESTYLES[arm],
            linewidth=0.9,
            marker=BREAKDOWN_MARKERS[arm],
            markeredgecolor=NEUTRAL_COLOR,
            markeredgewidth=0.3,
            markersize=4.3,
            label=ARM_LABELS[arm],
        )
        for arm in ARM_ORDER
    ]


def _plot_compact_subgroup_metric(
    ax,
    rows: Sequence[tuple[Mapping[str, Any], str, str]],
    positions: Sequence[float],
    *,
    metric: str,
    interval: str,
    xlim: tuple[float, float],
    recall_guardrail: float,
    show_ylabels: bool,
) -> None:
    offsets = {"eager": 0.12, "compact": -0.12}
    for row_index, ((summaries, group, _), center) in enumerate(
        zip(rows, positions, strict=True)
    ):
        if row_index % 2 == 0:
            ax.axhspan(center - 0.42, center + 0.42, color=PANEL_FILL, zorder=0)
        for arm in ARM_ORDER:
            row = summaries[group]["arms"][arm]
            value = float(row[metric])
            low, high = (float(bound) for bound in row[interval])
            ax.errorbar(
                value,
                center + offsets[arm],
                xerr=[[value - low], [high - value]],
                fmt=BREAKDOWN_MARKERS[arm],
                markersize=2.8,
                color=ARM_COLORS[arm],
                markeredgecolor=NEUTRAL_COLOR,
                markeredgewidth=0.25,
                elinewidth=0.65,
                capsize=1.1,
                capthick=0.6,
                zorder=3,
            )

    ax.set_xlim(*xlim)
    ax.set_ylim(-1.45, 11.45)
    ax.axhline(5.5, color="#B7B1AC", linewidth=0.55)
    if metric == "block_recall_at_5_delta":
        ax.axvline(0.0, color="#666666", linewidth=0.7)
        ax.axvline(
            recall_guardrail,
            color=BASELINE_COLOR,
            linestyle=":",
            linewidth=0.8,
        )
        ax.set_xlabel(r"$\Delta$ answer-block R@5")
        ax.xaxis.set_major_locator(MultipleLocator(0.1))
    else:
        ax.set_xlabel("Trajectory tokens (% baseline)")
        ax.xaxis.set_major_locator(MultipleLocator(10.0))

    if show_ylabels:
        ticks = [11.0, *positions[:5], 5.0, *positions[5:]]
        labels = [
            "LANGUAGE",
            *[
                f"{label} ({summaries[group]['queries']})"
                for summaries, group, label in rows[:5]
            ],
            "QUERY TYPE",
            *[
                f"{label} ({summaries[group]['queries']})"
                for summaries, group, label in rows[5:]
            ],
        ]
        ax.set_yticks(ticks, labels=labels)
        ax.tick_params(axis="y", length=0, pad=1.5)
        ylabels = ax.get_yticklabels()
        ylabels[0].set_fontweight("bold")
        ylabels[6].set_fontweight("bold")
        ylabels[0].set_color("#555555")
        ylabels[6].set_color("#555555")
    else:
        ax.set_yticks([])

    style_axis(ax, grid_axis="x", open_frame=True)
    ax.tick_params(axis="both", labelsize=4.8)
    ax.xaxis.label.set_size(5.8)


def _plot_compact_subgroups(
    token_ax,
    quality_ax,
    pooled_summary: Mapping[str, Any],
    pooled_category_summary: Mapping[str, Any],
    recall_guardrail: float,
) -> None:
    language_groups = LANGUAGE_ORDER[1:]
    rows = [
        (pooled_summary, group, LANGUAGE_LABELS[group]) for group in language_groups
    ] + [
        (pooled_category_summary, group, CATEGORY_LABELS[group])
        for group in CATEGORY_ORDER
    ]
    positions = [10.0, 9.0, 8.0, 7.0, 6.0, 4.0, 3.0, 2.0, 1.0, 0.0, -1.0]
    token_xlim = _stratified_metric_limits(
        (
            (pooled_summary, language_groups),
            (pooled_category_summary, CATEGORY_ORDER),
        ),
        metric="token_percent_of_grep",
        interval="token_percent_ci95",
        reference=100.0,
        recall_guardrail=recall_guardrail,
        include_reference=False,
    )
    quality_xlim = _stratified_metric_limits(
        (
            (pooled_summary, language_groups),
            (pooled_category_summary, CATEGORY_ORDER),
        ),
        metric="block_recall_at_5_delta",
        interval="block_recall_at_5_delta_ci95",
        reference=0.0,
        recall_guardrail=recall_guardrail,
    )
    _plot_compact_subgroup_metric(
        token_ax,
        rows,
        positions,
        metric="token_percent_of_grep",
        interval="token_percent_ci95",
        xlim=token_xlim,
        recall_guardrail=recall_guardrail,
        show_ylabels=True,
    )
    _plot_compact_subgroup_metric(
        quality_ax,
        rows,
        positions,
        metric="block_recall_at_5_delta",
        interval="block_recall_at_5_delta_ci95",
        xlim=quality_xlim,
        recall_guardrail=recall_guardrail,
        show_ylabels=False,
    )
    panel_title(token_ax, "c", "Workload Effects", pad=3.0)
    quality_ax.set_title("Recall change", loc="center", pad=3.0, fontsize=6.5)


def render_main_figure(
    summary: Mapping[str, Any],
    pooled_summary: Mapping[str, Any],
    pooled_category_summary: Mapping[str, Any],
    output_prefix: Path,
    recall_guardrail: float,
) -> None:
    fig = plt.figure(figsize=(DOUBLE_COLUMN_WIDTH, 2.48))
    grid = fig.add_gridspec(
        1,
        3,
        width_ratios=(1.16, 1.10, 2.20),
        left=0.055,
        right=0.995,
        top=0.80,
        bottom=0.19,
        wspace=0.30,
    )
    token_ax = fig.add_subplot(grid[0])
    quality_ax = fig.add_subplot(grid[1])
    subgroup_grid = grid[2].subgridspec(1, 2, width_ratios=(1.08, 1.0), wspace=0.12)
    subgroup_token_ax = fig.add_subplot(subgroup_grid[0])
    subgroup_quality_ax = fig.add_subplot(subgroup_grid[1])

    _plot_model_token_costs(token_ax, summary)
    _plot_model_quality_effects(quality_ax, summary, recall_guardrail)
    _plot_compact_subgroups(
        subgroup_token_ax,
        subgroup_quality_ax,
        pooled_summary,
        pooled_category_summary,
        recall_guardrail,
    )

    fig.legend(
        handles=_breakdown_legend_handles(),
        loc="upper center",
        bbox_to_anchor=(0.5, 0.997),
        ncol=2,
        frameon=False,
        fontsize=5.9,
    )

    for output in save_figure(fig, output_prefix):
        print(f"Saved: {output}")
    plt.close(fig)


def render_breakdown_figure(
    pooled_summary: Mapping[str, Any],
    pooled_category_summary: Mapping[str, Any],
    output_prefix: Path,
    recall_guardrail: float,
) -> None:
    fig, axes = plt.subplots(
        2,
        2,
        figsize=(SINGLE_COLUMN_WIDTH, 2.55),
        sharex="col",
    )
    language_groups = LANGUAGE_ORDER[1:]
    token_xlim = _stratified_metric_limits(
        (
            (pooled_summary, language_groups),
            (pooled_category_summary, CATEGORY_ORDER),
        ),
        metric="token_percent_of_grep",
        interval="token_percent_ci95",
        reference=100.0,
        recall_guardrail=recall_guardrail,
        include_reference=False,
    )
    quality_xlim = _stratified_metric_limits(
        (
            (pooled_summary, language_groups),
            (pooled_category_summary, CATEGORY_ORDER),
        ),
        metric="block_recall_at_5_delta",
        interval="block_recall_at_5_delta_ci95",
        reference=0.0,
        recall_guardrail=recall_guardrail,
    )
    panels = (
        (
            axes[0, 0],
            pooled_summary,
            language_groups,
            LANGUAGE_LABELS,
            "a",
            "Language: Tokens",
            "token_percent_of_grep",
            "token_percent_ci95",
            100.0,
            token_xlim,
            True,
            False,
        ),
        (
            axes[0, 1],
            pooled_summary,
            language_groups,
            LANGUAGE_LABELS,
            "b",
            "Language: Recall",
            "block_recall_at_5_delta",
            "block_recall_at_5_delta_ci95",
            0.0,
            quality_xlim,
            False,
            False,
        ),
        (
            axes[1, 0],
            pooled_category_summary,
            CATEGORY_ORDER,
            CATEGORY_LABELS,
            "c",
            "Query Type: Tokens",
            "token_percent_of_grep",
            "token_percent_ci95",
            100.0,
            token_xlim,
            True,
            True,
        ),
        (
            axes[1, 1],
            pooled_category_summary,
            CATEGORY_ORDER,
            CATEGORY_LABELS,
            "d",
            "Query Type: Recall",
            "block_recall_at_5_delta",
            "block_recall_at_5_delta_ci95",
            0.0,
            quality_xlim,
            False,
            True,
        ),
    )
    for (
        ax,
        summaries,
        groups,
        labels,
        panel,
        title,
        metric,
        interval,
        reference,
        xlim,
        show_ylabels,
        show_xlabel,
    ) in panels:
        _plot_pooled_stratified_forest(
            ax,
            summaries,
            groups,
            labels,
            label=panel,
            title=title,
            metric=metric,
            interval=interval,
            reference=reference,
            recall_guardrail=recall_guardrail,
            xlim=xlim,
            show_ylabels=show_ylabels,
            show_xlabel=show_xlabel,
        )

    fig.legend(
        handles=_breakdown_legend_handles(),
        loc="upper center",
        bbox_to_anchor=(0.5, 0.998),
        ncol=2,
        frameon=False,
        handlelength=1.0,
        columnspacing=1.0,
        fontsize=5.9,
    )
    fig.subplots_adjust(
        left=0.18,
        right=0.985,
        top=0.88,
        bottom=0.15,
        wspace=0.16,
        hspace=0.30,
    )
    for output in save_figure(fig, output_prefix):
        print(f"Saved: {output}")
    plt.close(fig)


def print_summary(
    summary: Mapping[str, Any], policy_contrasts: Mapping[str, Any]
) -> None:
    for model in MODEL_ORDER:
        pooled = summary[model]["All"]
        print(
            f"{model}: n={pooled['queries']}, repos={pooled['repositories']}, "
            f"grep={pooled['grep_mean_tokens'] / 1000:.1f}k tokens/query, "
            f"block-R@5={pooled['grep_block_recall_at_5']:.3f}"
        )
        for arm in ARM_ORDER:
            row = pooled["arms"][arm]
            ratio = row["token_percent_of_grep"]
            ratio_ci = row["token_percent_ci95"]
            recall = row["block_recall_at_5_delta"]
            recall_ci = row["block_recall_at_5_delta_ci95"]
            print(
                f"  {arm}: tokens={ratio:.1f}% "
                f"[{ratio_ci[0]:.1f}, {ratio_ci[1]:.1f}], "
                f"block-R@5={row['block_recall_at_5']:.3f}, "
                f"delta-block-R@5={recall:+.3f} "
                f"[{recall_ci[0]:+.3f}, {recall_ci[1]:+.3f}]"
            )
        contrast = policy_contrasts[model]["compact_vs_eager"]
        print(
            "  compact/eager: "
            f"tokens={contrast['token_percent_of_eager']:.1f}% "
            f"[{contrast['token_percent_ci95'][0]:.1f}, "
            f"{contrast['token_percent_ci95'][1]:.1f}], "
            f"delta-block-R@5={contrast['block_recall_at_5_delta']:+.3f} "
            f"[{contrast['block_recall_at_5_delta_ci95'][0]:+.3f}, "
            f"{contrast['block_recall_at_5_delta_ci95'][1]:+.3f}]"
        )


def summarize_operating_point_selection(
    summary: Mapping[str, Any], recall_guardrail: float
) -> dict[str, Any]:
    """Record the deterministic reporting rule used for model-level points."""
    models = {}
    for model in MODEL_ORDER:
        arms = summary[model]["All"]["arms"]
        admitted = [
            arm
            for arm in ARM_ORDER
            if arms[arm]["block_recall_at_5_delta_ci95"][0] >= recall_guardrail
        ]
        if not admitted:
            raise ValueError(
                f"{model}: no context-delivery arm clears the recall guardrail"
            )
        selected = min(
            admitted,
            key=lambda arm: (
                arms[arm]["token_percent_of_grep"],
                ARM_ORDER.index(arm),
            ),
        )
        row = arms[selected]
        models[model] = {
            "admitted_arms": admitted,
            "selected_arm": selected,
            "selected_token_percent_of_grep": row["token_percent_of_grep"],
            "selected_block_recall_at_5_delta": row["block_recall_at_5_delta"],
            "selected_block_recall_at_5_delta_ci95": row[
                "block_recall_at_5_delta_ci95"
            ],
        }
    return {
        "scope": "per model",
        "candidate_arms": list(ARM_ORDER),
        "rule": (
            "select the minimum-token context-delivery arm whose lower "
            "snapshot-clustered 95% interval bound for paired block "
            "Recall@5 change is at least the quality margin"
        ),
        "quality_metric": "paired change in committed-answer block Recall@5",
        "quality_interval": "snapshot-clustered percentile 95%",
        "quality_margin": recall_guardrail,
        "models": models,
    }


def main() -> None:
    args = parse_args()
    excluded_query_ids = set(args.exclude_query_id)
    roots = {
        "Claude Haiku 4.5": tuple(args.haiku_roots),
        "Qwen3.5-9B": tuple(args.qwen9b_roots),
        "Qwen3.5-27B": tuple(args.qwen27b_roots),
        "Gemma 4-12B": tuple(args.gemma12b_roots),
        "Gemini 2.5 Flash": tuple(args.gemini25_roots),
    }
    summary = {}
    category_summary = {}
    execution_audit = {}
    input_provenance = {}
    policy_contrasts = {}
    plot_data = {}
    paired_by_model = {}
    for model_index, model in enumerate(MODEL_ORDER):
        paired = load_paired_cells(roots[model], model, args.expected_pairs)
        missing_exclusions = excluded_query_ids - set(paired)
        if missing_exclusions:
            raise ValueError(
                f"{model}: excluded query ids are absent from paired data: "
                f"{sorted(missing_exclusions)}"
            )
        paired = {
            query_id: arms
            for query_id, arms in paired.items()
            if query_id not in excluded_query_ids
        }
        paired_by_model[model] = paired
        input_provenance[model] = summarize_input_provenance(
            roots[model], paired, args.provenance_root
        )
        summary[model] = summarize_model(
            paired,
            bootstrap_samples=args.bootstrap_samples,
            seed=args.seed + model_index * 1_000_003,
        )
        category_summary[model] = summarize_categories(
            paired,
            bootstrap_samples=args.bootstrap_samples,
            seed=args.seed + model_index * 1_000_003 + 500_009,
        )
        execution_audit[model] = summarize_execution_audit(paired)
        plot_data[model] = build_plot_data(paired)
        ratio_result, delta_result = _cluster_bootstrap_arm(
            paired,
            sorted(paired),
            "compact",
            baseline_arm="eager",
            samples=args.bootstrap_samples,
            seed=args.seed + model_index * 1_000_003 + 900_001,
        )
        ratio, ratio_low, ratio_high = ratio_result
        delta, delta_low, delta_high = delta_result
        policy_contrasts[model] = {
            "compact_vs_eager": {
                "token_percent_of_eager": ratio,
                "token_percent_ci95": [ratio_low, ratio_high],
                "block_recall_at_5_delta": delta,
                "block_recall_at_5_delta_ci95": [delta_low, delta_high],
            }
        }

    operating_point_selection = summarize_operating_point_selection(
        summary, args.recall_guardrail
    )

    pooled_runs = pool_model_runs(paired_by_model)
    pooled_summary = summarize_model(
        pooled_runs,
        bootstrap_samples=args.bootstrap_samples,
        seed=args.seed + 7_000_021,
    )
    pooled_category_summary = summarize_categories(
        pooled_runs,
        bootstrap_samples=args.bootstrap_samples,
        seed=args.seed + 7_500_031,
    )
    first_model = MODEL_ORDER[0]
    for group in LANGUAGE_ORDER:
        pooled_summary[group]["model_query_observations"] = pooled_summary[group][
            "queries"
        ]
        pooled_summary[group]["queries"] = summary[first_model][group]["queries"]
    for group in CATEGORY_ORDER:
        pooled_category_summary[group]["model_query_observations"] = (
            pooled_category_summary[group]["queries"]
        )
        pooled_category_summary[group]["queries"] = category_summary[first_model][
            group
        ]["queries"]

    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    metrics_path = args.output_prefix.with_suffix(".json")
    metrics_path.write_text(
        json.dumps(
            {
                "schema_version": 11,
                "source_paired_query_count": args.expected_pairs,
                "paired_query_count": len(paired_by_model[MODEL_ORDER[0]]),
                "excluded_query_ids": sorted(excluded_query_ids),
                "bootstrap": {
                    "unit": "repository snapshot",
                    "samples": args.bootstrap_samples,
                    "seed": args.seed,
                    "interval": "percentile 95%",
                },
                "recall_guardrail": args.recall_guardrail,
                "operating_point_selection": operating_point_selection,
                "models": summary,
                "policy_contrasts": policy_contrasts,
                "query_categories": category_summary,
                "pooled_strata": {
                    "models": len(MODEL_ORDER),
                    "languages": pooled_summary,
                    "query_categories": pooled_category_summary,
                },
                "execution_audit": execution_audit,
                "input_provenance": input_provenance,
                "distributions": plot_data,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Saved: {metrics_path}")
    print_summary(summary, policy_contrasts)
    render_main_figure(
        summary,
        pooled_summary,
        pooled_category_summary,
        args.output_prefix,
        args.recall_guardrail,
    )
    render_breakdown_figure(
        pooled_summary,
        pooled_category_summary,
        args.breakdown_output_prefix,
        args.recall_guardrail,
    )


if __name__ == "__main__":
    main()
