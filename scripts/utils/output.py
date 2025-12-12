"""
Output and Visualization Module

Provides data saving (CSV/JSON) and visualization/plotting functionality.
"""

import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

# Add project root to path for codeminer imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from codeminer.log_utils import get_logger

logger = get_logger(__name__)


# =============================================================================
# Data Saving
# =============================================================================


def save_csv(data: List[Dict[str, Any]], path: str, fieldnames: List[str]):
    """Save data to CSV file."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)
    logger.info(f"Saved to: {path}")


def save_json(data: Any, path: str):
    """Save data to JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved to: {path}")


# =============================================================================
# Visualization Helper Functions
# =============================================================================


def _slugify(s: str) -> str:
    """Convert string to filename-safe slug."""
    return (
        s.replace("/", "__")
        .replace(" ", "_")
        .replace(":", "_")
        .replace("|", "_")
        .replace("\\", "_")
    )


def _setup_plot_style() -> None:
    """Set unified 'professional' plotting style."""
    for style in ("seaborn-v0_8-whitegrid", "seaborn-whitegrid", "ggplot"):
        try:
            plt.style.use(style)
            break
        except Exception:
            continue

    plt.rcParams.update(
        {
            "figure.dpi": 140,
            "savefig.dpi": 200,
            "axes.titlesize": 12,
            "axes.labelsize": 10,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
            "legend.fontsize": 9,
            "font.family": "DejaVu Sans",
        }
    )


# =============================================================================
# Visualization: Repository Sizes
# =============================================================================


def plot_repo_sizes(
    repo_sizes: List[Dict[str, Any]],
    selected_repos: Dict[str, List[Dict[str, Any]]],
    output_dir: Path,
) -> str:
    """
    Generate a single chart containing repo sizes for all language groups
    (2x2 or dynamic grid layout):
    - Each subplot sorted by file_count from small to large
    - Selected repos marked in red
    """
    _setup_plot_style()

    plots_dir = output_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    repos_by_group: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in repo_sizes:
        repos_by_group[r["language_group"]].append(r)

    selected_by_group = {
        group: {r["repo"] for r in repos} for group, repos in selected_repos.items()
    }

    groups = sorted(repos_by_group.keys())
    n_groups = len(groups)

    if n_groups == 0:
        return ""

    # Calculate grid layout: 2 columns
    n_cols = min(2, n_groups)
    n_rows = (n_groups + n_cols - 1) // n_cols

    # Calculate height needed for each subplot (based on repo count)
    max_repos = max(len(repos_by_group[g]) for g in groups) if groups else 1
    subplot_height = max(3.5, min(0.4 * max_repos + 1.5, 12))

    fig, axes = plt.subplots(
        n_rows, n_cols, figsize=(7 * n_cols, subplot_height * n_rows), squeeze=False
    )

    for idx, group in enumerate(groups):
        row, col = idx // n_cols, idx % n_cols
        ax = axes[row][col]

        repos = repos_by_group[group]
        repos_sorted = sorted(repos, key=lambda x: x["file_count"])
        selected_set = selected_by_group.get(group, set())

        y_labels = [r["repo"].split("/")[-1] for r in repos_sorted]
        y_pos = list(range(len(repos_sorted)))
        values = [r["file_count"] for r in repos_sorted]
        colors = [
            "#D62728" if r["repo"] in selected_set else "#4C78A8" for r in repos_sorted
        ]

        ax.barh(
            y_pos, values, color=colors, alpha=0.95, edgecolor="white", linewidth=0.6
        )
        ax.set_yticks(y_pos)
        ax.set_yticklabels(y_labels, fontsize=8)
        ax.invert_yaxis()
        ax.set_xlabel("File count", fontsize=9)
        ax.set_title(f"{group}", fontsize=11, fontweight="bold")

        # Annotate selected repos
        for i, r in enumerate(repos_sorted):
            if r["repo"] in selected_set:
                x = values[i] * 1.02 if values[i] > 0 else 0.1
                ax.text(x, i, "★", va="center", ha="left", color="#D62728", fontsize=9)

    # Hide extra subplots
    for idx in range(n_groups, n_rows * n_cols):
        row, col = idx // n_cols, idx % n_cols
        axes[row][col].set_visible(False)

    # Add global legend
    fig.legend(
        handles=[
            Patch(facecolor="#4C78A8", label="Not selected"),
            Patch(facecolor="#D62728", label="Selected"),
        ],
        loc="upper right",
        frameon=True,
        fontsize=9,
    )

    fig.suptitle(
        "Repository Sizes by Language (sorted by file count)",
        fontsize=13,
        fontweight="bold",
        y=1.01,
    )
    fig.tight_layout()

    out_path = plots_dir / "repo_sizes.png"
    fig.savefig(out_path, bbox_inches="tight", dpi=150)
    plt.close(fig)

    return str(out_path)


# =============================================================================
# Visualization: Instance Difficulty
# =============================================================================


def plot_instance_difficulties(
    instance_difficulties: List[Dict[str, Any]],
    selected_repos: Dict[str, List[Dict[str, Any]]],
    selected_instances: List[Dict[str, Any]],
    output_dir: Path,
) -> List[str]:
    """
    Generate one chart per language containing instance difficulty distribution
    for all selected repos in that language:
    - One subplot per repo
    - x: rank (index after sorting by changed_loc from small to large)
    - y: changed_loc
    - Selected instances marked in red and annotated
    """
    _setup_plot_style()

    plots_dir = output_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    # Organize selected repos by language group
    repos_by_lang: Dict[str, List[str]] = defaultdict(list)
    for group, repos in selected_repos.items():
        for r in repos:
            repos_by_lang[group].append(r["repo"])

    # Build instance data
    selected_inst_map: Dict[str, Dict[str, str]] = defaultdict(dict)
    for s in selected_instances:
        selected_inst_map[s["repo"]][s["instance_id"]] = s.get("difficulty_level", "")

    insts_by_repo: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for inst in instance_difficulties:
        insts_by_repo[inst["repo"]].append(inst)

    saved_paths: List[str] = []

    for lang_group in sorted(repos_by_lang.keys()):
        repos_in_lang = repos_by_lang[lang_group]
        n_repos = len(repos_in_lang)

        if n_repos == 0:
            continue

        # Calculate subplot layout
        n_cols = min(3, n_repos)
        n_rows = math.ceil(n_repos / n_cols)

        fig, axes = plt.subplots(
            n_rows, n_cols, figsize=(5 * n_cols, 3.5 * n_rows), squeeze=False
        )

        for idx, repo in enumerate(sorted(repos_in_lang)):
            row, col = idx // n_cols, idx % n_cols
            ax = axes[row][col]

            insts = insts_by_repo.get(repo, [])
            insts_sorted = sorted(insts, key=lambda x: x["changed_loc"])
            m = len(insts_sorted)

            if m == 0:
                ax.set_visible(False)
                continue

            xs = list(range(1, m + 1))
            ys = [i["changed_loc"] for i in insts_sorted]

            # Full distribution: gray scatter points
            ax.scatter(xs, ys, s=20, color="#7F7F7F", alpha=0.7, linewidths=0)

            # Selected points: red + annotation
            selected_map = selected_inst_map.get(repo, {})
            if selected_map:
                sel_xs, sel_ys, sel_labels = [], [], []
                for i, inst in enumerate(insts_sorted, start=1):
                    iid = inst["instance_id"]
                    if iid in selected_map:
                        sel_xs.append(i)
                        sel_ys.append(inst["changed_loc"])
                        level = selected_map[iid]
                        # Simplify label: only show issue number and difficulty
                        short_id = iid.split("-")[-1] if "-" in iid else iid
                        sel_labels.append(
                            f"{short_id} ({level})" if level else short_id
                        )

                ax.scatter(
                    sel_xs,
                    sel_ys,
                    s=50,
                    color="#D62728",
                    alpha=0.95,
                    edgecolors="white",
                    linewidths=0.8,
                    zorder=3,
                )

                for x, y, label in zip(sel_xs, sel_ys, sel_labels, strict=True):
                    ax.annotate(
                        label,
                        (x, y),
                        textcoords="offset points",
                        xytext=(5, 4),
                        ha="left",
                        fontsize=7,
                        color="#D62728",
                        bbox=dict(
                            boxstyle="round,pad=0.2",
                            fc="white",
                            ec="#D62728",
                            alpha=0.8,
                            lw=0.6,
                        ),
                    )

            repo_short = repo.split("/")[-1]
            ax.set_title(f"{repo_short} (n={m})", fontsize=9, fontweight="bold")
            ax.set_xlabel("Rank", fontsize=8)
            ax.set_ylabel("changed_loc", fontsize=8)
            ax.tick_params(axis="both", labelsize=7)

        # Hide extra subplots
        for idx in range(n_repos, n_rows * n_cols):
            row, col = idx // n_cols, idx % n_cols
            axes[row][col].set_visible(False)

        # Add global legend and title
        fig.legend(
            handles=[
                Patch(facecolor="#7F7F7F", label="All instances"),
                Patch(facecolor="#D62728", label="Selected"),
            ],
            loc="upper right",
            frameon=True,
            fontsize=8,
        )
        fig.suptitle(
            f"Instance Difficulty Distribution: {lang_group}",
            fontsize=12,
            fontweight="bold",
            y=1.02,
        )
        fig.tight_layout()

        out_path = plots_dir / f"instance_difficulty_{_slugify(lang_group)}.png"
        fig.savefig(out_path, bbox_inches="tight", dpi=150)
        plt.close(fig)
        saved_paths.append(str(out_path))

    return saved_paths


# =============================================================================
# Summary Printing
# =============================================================================


def print_summary(
    selected_repos: Dict[str, List[Dict[str, Any]]],
    selected_instances: List[Dict[str, Any]],
):
    """Print sampling results summary."""
    logger.info("\n" + "=" * 60)
    logger.info("Sampling Results Summary")
    logger.info("=" * 60)

    for group in sorted(selected_repos.keys()):
        repos = selected_repos[group]
        logger.info(f"\n[{group}] Selected {len(repos)} repos:")
        for repo in repos:
            repo_name = repo["repo"]
            file_count = repo["file_count"]
            num_instances = repo["num_instances"]
            logger.info(
                f"  - {repo_name} (files: {file_count}, instances: {num_instances})"
            )

    logger.info(f"\nTotal {len(selected_instances)} instances selected:")

    by_group = defaultdict(list)
    for inst in selected_instances:
        by_group[inst["language_group"]].append(inst)

    for group in sorted(by_group.keys()):
        insts = by_group[group]
        logger.info(f"\n[{group}] {len(insts)} instances:")
        for inst in insts:
            inst_id = inst["instance_id"]
            changed_loc = inst["changed_loc"]
            difficulty = inst["difficulty_level"]
            logger.info(
                f"  - {inst_id} (changed_loc: {changed_loc}, difficulty: {difficulty})"
            )

    logger.info("\n" + "=" * 60)
