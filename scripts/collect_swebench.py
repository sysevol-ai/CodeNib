#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Collect representative SWE-bench instances and save sampling artifacts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Optional, Sequence

from codeminer.dataset.collect.swebench_sample import SamplingConfig, run_sampling
from codeminer.log_utils import get_logger

logger = get_logger(__name__)


def _parse_languages(values: Optional[Sequence[str]]) -> Optional[set[str]]:
    if not values:
        return None
    languages: List[str] = []
    for val in values:
        languages.extend([v for v in val.split(",") if v])
    return {lang.strip() for lang in languages if lang.strip()}


def _dump_json(data: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect representative SWE-bench instances."
    )
    parser.add_argument(
        "--languages",
        nargs="*",
        default=None,
        help="Languages to include (space-separated or comma-separated).",
    )
    parser.add_argument(
        "--multilingual-csv-path",
        type=str,
        default=None,
        help="Path to swebench_multilingual.csv when sampling non-Python languages.",
    )
    parser.add_argument("--split", type=str, default="test", help="Dataset split.")
    parser.add_argument(
        "--filter-instance",
        type=str,
        default=".*",
        help="Regex filter for instance IDs.",
    )
    parser.add_argument("--repos-per-language", type=int, default=5)
    parser.add_argument("--instances-per-repo", type=int, default=4)
    parser.add_argument("--min-instances", type=int, default=3)
    parser.add_argument("--shallow-clone", action="store_true", default=True)
    parser.add_argument(
        "--no-shallow-clone", action="store_false", dest="shallow_clone"
    )
    parser.add_argument("--cache-dir", type=str, default=None)
    parser.add_argument("--repo-cache-dir", type=str, default=None)
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument(
        "--print-sample",
        type=int,
        default=5,
        help="Print the first N sampled instances to stdout.",
    )
    parser.add_argument(
        "--save-sampled",
        type=str,
        default=None,
        help="Optional path to save sampled instances as JSON.",
    )
    parser.add_argument(
        "--difficulty-model",
        type=str,
        default="opus",
        help="Model for agent difficulty classification (default: opus).",
    )
    parser.add_argument(
        "--total-instances",
        type=int,
        default=100,
        help="Target total number of instances to sample (default: 100). "
        "Adaptively allocates extra instances from data-rich repos when "
        "some repos have fewer than --instances-per-repo qualifying instances.",
    )
    parser.add_argument(
        "--max-gt-code-blocks",
        type=int,
        default=10,
        help="Maximum number of GT code blocks per instance (default: 10). "
        "Instances exceeding this limit are excluded during sampling.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    languages = _parse_languages(args.languages)
    config = SamplingConfig(
        languages=languages or SamplingConfig().languages,
        shallow_clone=args.shallow_clone,
        min_instances=args.min_instances,
        repos_per_language=args.repos_per_language,
        instances_per_repo=args.instances_per_repo,
        dataset_split=args.split,
        filter_instance=args.filter_instance,
        multilingual_csv_path=(
            Path(args.multilingual_csv_path) if args.multilingual_csv_path else None
        ),
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
        repo_cache_dir=Path(args.repo_cache_dir) if args.repo_cache_dir else None,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        difficulty_model=args.difficulty_model,
        total_instances=args.total_instances,
        max_gt_code_blocks=args.max_gt_code_blocks,
    )

    results = run_sampling(config)
    selected_instances = results.selected_instances
    if args.print_sample:
        logger.info("Sampled %d instances. Preview:", len(selected_instances))
        print(json.dumps(selected_instances[: args.print_sample], indent=2))

    if args.save_sampled:
        _dump_json(selected_instances, Path(args.save_sampled))


if __name__ == "__main__":
    main()
