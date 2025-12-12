#!/usr/bin/env python3
"""
SWE-bench Data Sampling Script

Supports sampling from:
- SWE-bench Multilingual (Rust/C/C++/Go/Java/JS/TS/PHP/Ruby)
- SWE-bench Lite (Python)

Features:
1. Select representative repos and instances from SWE-bench datasets
2. Sample within specified language groups
3. Select 5 repos per language group representing different "repository sizes"
4. Select 3 instances per repo covering different "patch difficulty (changed LOC)"

Repository size metric: number of non-hidden files at latest commit (excluding .git)
Difficulty metric: changed_loc = added_lines + deleted_lines

Uses percentile approximation to cover different scale/difficulty ranges,
with fallback logic for small samples.

Usage:
    # Default: shallow clone, sample Rust/C/C++/JS/TS/Python
    python scripts/swebench_sample.py

    # Use deep clone
    python scripts/swebench_sample.py --deep

    # Specify languages
    python scripts/swebench_sample.py --languages Go Java

Output:
    Final results:
    - plots/repo_sizes.png: repo size distribution for all languages (single chart)
    - plots/instance_difficulty_<lang>.png: instance difficulty distribution per language
    - selected_instances.csv: same as above, CSV format
"""

import argparse
import csv
import sys
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

# Add project root to path for codeminer imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from codeminer.env.process_swebench_data import load_filter_swebench_dataset_explicit
from codeminer.env.utils import get_cache_dir
from codeminer.log_utils import get_logger

logger = get_logger(__name__)

# Import from utils module
from utils import (
    PYTHON_LANGUAGE,
    calculate_repo_sizes,
    filter_repos_by_instance_count,
    group_instances_by_repo,
    plot_instance_difficulties,
    plot_repo_sizes,
    save_csv,
    save_json,
)

# =============================================================================
# Global Configuration
# =============================================================================

DEFAULT_CACHE_DIR = get_cache_dir()

# Default target languages (the three categories we support)
DEFAULT_LANGUAGES = {"Rust", "C", "C++", "JavaScript/TypeScript", "Python"}


# =============================================================================
# Percentile Selection Algorithm
# =============================================================================


@lru_cache(maxsize=1)
def read_multilingual_csv(csv_path: str) -> Tuple[Set[str], Dict[str, str]]:
    """Read swebench_multilingual.csv and return (valid_languages, repo_to_language)."""
    valid_languages: Set[str] = set()
    repo_to_language: Dict[str, str] = {}
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lang = row.get("Language")
            repo = row.get("Repository")
            if lang:
                valid_languages.add(lang)
            if repo and lang:
                repo_to_language[repo] = lang
    return valid_languages, repo_to_language


def select_indices_by_percentile(
    n: int,
    target_count: int,
    percentiles: List[float],
    include_ends: bool = True,
) -> List[int]:
    """
    Select representative indices using percentiles.
    Calculate representative positions based on given percentiles,
    with fallback logic for small samples.
    When include_ends=True, forces inclusion of min and max values.
    """
    if n <= target_count:
        return list(range(n))

    indices = {0, n - 1} if include_ends else set()
    for p in percentiles:
        indices.add(round(p * (n - 1)))

    indices = sorted(indices)

    # Fallback: if count is insufficient, expand from center outward
    if len(indices) < target_count:
        mid = n // 2
        all_indices = set(indices)
        offset = 1
        while len(all_indices) < target_count and offset < n:
            for c in [mid - offset, mid + offset]:
                if 0 <= c < n and c not in all_indices:
                    all_indices.add(c)
                    if len(all_indices) >= target_count:
                        break
            offset += 1
        indices = sorted(all_indices)

    return indices[:target_count]


def select_representative_repos(
    repo_sizes: List[Dict[str, Any]],
    target_count: int = 5,
) -> Dict[str, List[Dict[str, Any]]]:
    """Select representative repos by file_count within each language group."""
    repos_by_group = defaultdict(list)
    for repo_info in repo_sizes:
        repos_by_group[repo_info["language_group"]].append(repo_info)

    selected = {}
    for group, repos in repos_by_group.items():
        repos_sorted = sorted(repos, key=lambda x: x["file_count"])
        n = len(repos_sorted)

        indices = select_indices_by_percentile(n, target_count, [0.25, 0.5, 0.75])
        selected_repos = [repos_sorted[i] for i in indices]
        selected[group] = selected_repos

    return selected


# =============================================================================
# Patch Difficulty Calculation
# =============================================================================


def parse_patch_changed_loc(patch: str) -> int:
    """Parse changed_loc from patch content (added lines + deleted lines)."""
    added, deleted = 0, 0
    for line in patch.split("\n"):
        if line.startswith(("---", "+++", "@@", "diff ")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return added + deleted


def calculate_instance_difficulties(
    repo_info: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Calculate difficulty (changed_loc) for all instances."""
    difficulties = []
    for repo, info in repo_info.items():
        for instance in info["instances"]:
            difficulties.append(
                {
                    "repo": repo,
                    "language_group": info["language_group"],
                    "instance_id": instance["instance_id"],
                    "changed_loc": parse_patch_changed_loc(instance.get("patch", "")),
                }
            )
    logger.info(f"Calculated difficulty for {len(difficulties)} instances")
    return difficulties


# =============================================================================
# Instance Selection
# =============================================================================


def select_representative_instances(
    instance_difficulties: List[Dict[str, Any]],
    selected_repos: Dict[str, List[Dict[str, Any]]],
    target_count: int = 3,
) -> List[Dict[str, Any]]:
    """Select representative instances by changed_loc within each selected repo."""
    selected_repo_names = {
        repo_info["repo"] for repos in selected_repos.values() for repo_info in repos
    }

    instances_by_repo = defaultdict(list)
    for inst in instance_difficulties:
        if inst["repo"] in selected_repo_names:
            instances_by_repo[inst["repo"]].append(inst)

    selected_instances = []

    for _repo, instances in instances_by_repo.items():
        instances_sorted = sorted(instances, key=lambda x: x["changed_loc"])
        m = len(instances_sorted)

        if m <= target_count:
            indices = list(range(m))
            levels = ["low", "medium", "high"][:m] if m <= 3 else ["medium"] * m
        else:
            # Instance selection: use 25%, 50%, 75% percentiles + fallback
            # (avoid duplicate indices)
            indices = select_indices_by_percentile(
                m, target_count, [0.25, 0.5, 0.75], include_ends=False
            )
            levels = ["low", "medium", "high"]

        for i, idx in enumerate(indices):
            inst = instances_sorted[idx]
            level = levels[i] if i < len(levels) else "medium"

            selected_instances.append(
                {
                    "repo": inst["repo"],
                    "language_group": inst["language_group"],
                    "instance_id": inst["instance_id"],
                    "changed_loc": inst["changed_loc"],
                    "difficulty_level": level,
                    "rank_in_repo": idx + 1,
                    "total_in_repo": m,
                }
            )

    return selected_instances


# =============================================================================
# Main Workflow
# =============================================================================


def parse_args():
    """Parse command line arguments."""
    project_root = Path(__file__).parent.parent
    csv_path = project_root / "codeminer" / "env" / "swebench_multilingual.csv"

    valid_languages, _ = read_multilingual_csv(str(csv_path))

    # Python uses SWE-bench Lite, also a valid language option
    all_valid_languages = valid_languages | {PYTHON_LANGUAGE}

    parser = argparse.ArgumentParser(
        description="SWE-bench data sampling script (supports Multilingual + Lite)"
    )

    parser.add_argument(
        "--deep",
        action="store_true",
        help="Use deep clone (default is shallow clone)",
    )

    parser.add_argument(
        "--languages",
        nargs="+",
        default=list(DEFAULT_LANGUAGES),
        metavar="LANG",
        help=f"List of languages to sample (default: {', '.join(sorted(DEFAULT_LANGUAGES))})",
    )

    args = parser.parse_args()

    # Validate language arguments (Python is also a valid option)
    invalid_languages = set(args.languages) - all_valid_languages
    if invalid_languages:
        parser.error(
            f"Invalid languages: {', '.join(invalid_languages)}\n"
            f"Available languages: {', '.join(sorted(all_valid_languages))}"
        )

    return args


def main():
    """Main function: execute the complete sampling workflow."""
    args = parse_args()

    # Path configuration
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    csv_path = project_root / "codeminer" / "env" / "swebench_multilingual.csv"
    output_dir = script_dir / "sampling_output"
    output_dir.mkdir(parents=True, exist_ok=True)

    target_languages = set(args.languages)
    shallow = not args.deep

    logger.info("=" * 60)
    logger.info("SWE-bench Data Sampling")
    logger.info("=" * 60)
    logger.info(f"Clone mode: {'shallow' if shallow else 'deep'}")
    logger.info(f"Target languages: {', '.join(sorted(target_languages))}")

    # Step 1: Load data (inline implementation)
    logger.info("\n[Step 1] Loading data...")

    multilingual_dataset = None
    lite_dataset = None
    repo_to_language = {}

    non_python_languages = target_languages - {PYTHON_LANGUAGE}
    if non_python_languages:
        langs_str = ", ".join(sorted(non_python_languages))
        logger.info(f"Loading Multilingual dataset (languages: {langs_str})")
        _, repo_to_language = read_multilingual_csv(str(csv_path))
        logger.info(
            f"Loaded language mapping for {len(repo_to_language)} repos from CSV"
        )
        # Inline: load_swebench_multilingual
        dataset_name = "SWE-bench/SWE-bench_Multilingual"
        logger.info(f"Loading dataset: {dataset_name}")
        multilingual_dataset = load_filter_swebench_dataset_explicit(
            dataset=dataset_name, filter_instance=".*", split="test"
        )
        logger.info(f"Loaded {len(multilingual_dataset)} instances")

    if PYTHON_LANGUAGE in target_languages:
        logger.info("Loading SWE-bench Lite dataset (Python)")
        # Inline: load_swebench_lite
        dataset_name = "princeton-nlp/SWE-bench_Lite"
        logger.info(f"Loading dataset: {dataset_name}")
        lite_dataset = load_filter_swebench_dataset_explicit(
            dataset=dataset_name, filter_instance=".*", split="test"
        )
        logger.info(f"Loaded {len(lite_dataset)} Python instances")

    # Step 2: Group by language and filter
    logger.info("\n[Step 2] Grouping by language and filtering...")
    repo_info = group_instances_by_repo(
        multilingual_dataset, lite_dataset, repo_to_language, target_languages
    )
    repo_info = filter_repos_by_instance_count(repo_info, min_instances=3)

    # Step 3: Calculate repo sizes
    logger.info("\n[Step 3] Calculating repo sizes...")
    repo_sizes = calculate_repo_sizes(repo_info, DEFAULT_CACHE_DIR, shallow=shallow)

    # Step 4: Select representative repos
    logger.info("\n[Step 4] Selecting representative repos...")
    selected_repos = select_representative_repos(repo_sizes, target_count=5)

    # Step 4.1: repo size visualization (all languages in one chart)
    logger.info("\n[Step 4.1] Generating repo size visualization...")
    repo_plot_path = plot_repo_sizes(repo_sizes, selected_repos, output_dir)
    if repo_plot_path:
        logger.info(f"Saved chart: {repo_plot_path}")

    # Step 5: Calculate instance difficulties
    logger.info("\n[Step 5] Calculating instance difficulties...")
    instance_difficulties = calculate_instance_difficulties(repo_info)

    # Step 6: Select representative instances
    logger.info("\n[Step 6] Selecting representative instances...")
    selected_instances = select_representative_instances(
        instance_difficulties, selected_repos, target_count=3
    )

    # Step 6.1: instance difficulty visualization (one chart per language)
    logger.info("\n[Step 6.1] Generating instance difficulty visualization...")
    inst_plot_paths = plot_instance_difficulties(
        instance_difficulties, selected_repos, selected_instances, output_dir
    )
    for p in inst_plot_paths:
        logger.info(f"Saved chart: {p}")

    # Step 7: Save final results
    logger.info("\n[Step 7] Saving results...")
    save_json(selected_repos, str(output_dir / "selected_repos.json"))
    save_json(selected_instances, str(output_dir / "selected_instances.json"))
    save_csv(
        selected_instances,
        str(output_dir / "selected_instances.csv"),
        [
            "language_group",
            "repo",
            "instance_id",
            "changed_loc",
            "difficulty_level",
            "rank_in_repo",
            "total_in_repo",
        ],
    )

    logger.info(f"\n[DONE] All results saved to: {output_dir}")


if __name__ == "__main__":
    main()
