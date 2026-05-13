# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
SWE-bench sampling utilities.

This module is designed for programmatic use in the CodeMiner infra. It exposes
configuration-driven APIs and avoids CLI side effects.
"""

from __future__ import annotations

import csv
import json
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from codeminer.dataset.swebench import SwebenchDataset
from codeminer.dataset.swebench_multilingual import SwebenchMultilingualDataset
from codeminer.log_utils import get_logger

logger = get_logger(__name__)

PYTHON_LANGUAGE = "Python"

DEFAULT_LANGUAGES = {"C++/C", "Rust", "TypeScript/JavaScript", "Go", PYTHON_LANGUAGE}
DEFAULT_MULTILINGUAL_DATASET = "SWE-bench/SWE-bench_Multilingual"
DEFAULT_LITE_DATASET = "princeton-nlp/SWE-bench_Verified"


@dataclass(frozen=True)
class SamplingConfig:
    languages: Set[str] = field(default_factory=lambda: set(DEFAULT_LANGUAGES))
    shallow_clone: bool = True
    min_instances: int = 3
    repos_per_language: int = 5
    instances_per_repo: int = 3
    dataset_split: str = "test"
    filter_instance: str = ".*"
    multilingual_dataset: str = DEFAULT_MULTILINGUAL_DATASET
    lite_dataset: str = DEFAULT_LITE_DATASET
    multilingual_csv_path: Optional[Path] = None
    cache_dir: Optional[Path] = None
    repo_cache_dir: Optional[Path] = None
    output_dir: Optional[Path] = None
    write_outputs: bool = True
    difficulty_model: str = "opus"
    gt_locator_work_dir: Optional[Path] = None
    total_instances: Optional[int] = None
    max_gt_code_blocks: int = 10


@dataclass
class SamplingResults:
    selected_repos: Dict[str, List[Dict[str, Any]]]
    selected_instances: List[Dict[str, Any]]
    repo_sizes: List[Dict[str, Any]]
    instance_difficulties: List[Dict[str, Any]]
    gt_filtered_instances: List[Dict[str, Any]]
    output_dir: Optional[Path]


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


def read_repo_language_map(csv_path: Path) -> Dict[str, str]:
    repo_to_language: Dict[str, str] = {}
    if not csv_path.exists():
        return repo_to_language
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            repo = row.get("repo") or row.get("Repository")
            lang = row.get("language") or row.get("Language")
            if repo and lang:
                repo_to_language[repo] = lang
    return repo_to_language


def select_indices_by_percentile(
    n: int,
    target_count: int,
    percentiles: Sequence[float],
    include_ends: bool = True,
) -> List[int]:
    """Select representative indices using percentiles with fallback logic."""
    if n <= target_count:
        return list(range(n))

    indices = {0, n - 1} if include_ends else set()
    for p in percentiles:
        indices.add(round(p * (n - 1)))

    indices = sorted(indices)

    if len(indices) < target_count:
        mid = n // 2
        all_indices = set(indices)
        offset = 1
        while len(all_indices) < target_count and offset < n:
            for c in (mid - offset, mid + offset):
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
    repos_by_group: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for repo_info in repo_sizes:
        repos_by_group[repo_info["language_group"]].append(repo_info)

    selected: Dict[str, List[Dict[str, Any]]] = {}
    for group, repos in repos_by_group.items():
        repos_sorted = sorted(repos, key=lambda x: x["file_count"])
        n = len(repos_sorted)

        indices = select_indices_by_percentile(n, target_count, [0.25, 0.5, 0.75])
        selected_repos = [repos_sorted[i] for i in indices]
        selected[group] = selected_repos

    return selected


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
                    "base_commit": instance.get("base_commit"),
                    "problem_statement": instance.get("problem_statement"),
                    "hints_text": instance.get("hints_text"),
                    "patch": instance.get("patch"),
                }
            )
    logger.info("Calculated difficulty for %d instances", len(difficulties))
    return difficulties


def _build_selected_entry(
    inst: Dict[str, Any], level: str, total: int
) -> Dict[str, Any]:
    entry = {
        "repo": inst["repo"],
        "language_group": inst["language_group"],
        "instance_id": inst["instance_id"],
        "base_commit": inst.get("base_commit"),
        "problem_statement": inst.get("problem_statement"),
        "hints_text": inst.get("hints_text"),
        "patch": inst.get("patch"),
        "changed_loc": inst["changed_loc"],
        "difficulty_level": level,
        "total_in_repo": total,
        "gt_symbols_modified": inst.get("gt_symbols_modified", []),
        "gt_symbols_deleted": inst.get("gt_symbols_deleted", []),
        "gt_target_files": inst.get("gt_target_files", []),
        "gt_code_blocks": inst.get("gt_code_blocks", []),
    }
    return entry


def _select_by_agent_difficulty(
    instances: List[Dict[str, Any]], target_count: int
) -> List[Dict[str, Any]]:
    """Pick diverse instances using agent-assigned difficulty levels."""
    by_level: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for inst in instances:
        by_level[inst.get("agent_difficulty", "medium")].append(inst)

    m = len(instances)
    selected: List[Dict[str, Any]] = []
    # One from each level for diversity.
    for level in ("low", "medium", "high"):
        if by_level[level] and len(selected) < target_count:
            inst = by_level[level].pop(0)
            selected.append(_build_selected_entry(inst, level, total=m))
    # Fill remaining slots from whatever is left.
    remaining = [inst for bucket in by_level.values() for inst in bucket]
    for inst in remaining:
        if len(selected) >= target_count:
            break
        level = inst.get("agent_difficulty", "medium")
        selected.append(_build_selected_entry(inst, level, total=m))
    return selected


def _compute_repo_quotas(
    instances_by_repo: Dict[str, List[Dict[str, Any]]],
    selected_repos: Dict[str, List[Dict[str, Any]]],
    base_per_repo: int,
    total_target: int,
) -> Dict[str, int]:
    """Compute per-repo instance quotas to hit a total target.

    Starts with ``base_per_repo`` per repo, then distributes any deficit
    round-robin across language groups from repos with surplus.
    """
    available = {repo: len(insts) for repo, insts in instances_by_repo.items()}
    quotas = {repo: min(base_per_repo, avail) for repo, avail in available.items()}

    total_base = sum(quotas.values())
    total_available = sum(available.values())

    if total_target > total_available:
        logger.warning(
            "total_instances=%d exceeds available instances (%d); "
            "using all available",
            total_target,
            total_available,
        )
        return available

    deficit = total_target - total_base
    if deficit <= 0:
        return quotas

    # Build round-robin order: cycle through language groups for even spread.
    repo_to_group: Dict[str, str] = {}
    for group, repos in selected_repos.items():
        for repo_info in repos:
            repo_to_group[repo_info["repo"]] = group

    groups = sorted(selected_repos.keys())
    repos_by_group: Dict[str, List[str]] = defaultdict(list)
    for repo, group in repo_to_group.items():
        if available[repo] > quotas[repo]:
            repos_by_group[group].append(repo)

    while deficit > 0:
        made_progress = False
        for group in groups:
            for repo in repos_by_group.get(group, []):
                if deficit <= 0:
                    break
                if quotas[repo] < available[repo]:
                    quotas[repo] += 1
                    deficit -= 1
                    made_progress = True
            if deficit <= 0:
                break
        if not made_progress:
            break

    return quotas


def select_representative_instances(
    instance_difficulties: List[Dict[str, Any]],
    selected_repos: Dict[str, List[Dict[str, Any]]],
    target_count: int = 3,
    total_instances: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Select representative instances within each selected repo.

    Uses agent-classified difficulty levels to pick diverse instances.
    When *total_instances* is set, adaptively allocates extra instances
    from data-rich repos to hit the target total.
    """
    selected_repo_names = {
        repo_info["repo"] for repos in selected_repos.values() for repo_info in repos
    }

    instances_by_repo: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for inst in instance_difficulties:
        if inst["repo"] in selected_repo_names:
            instances_by_repo[inst["repo"]].append(inst)

    if total_instances is not None:
        quotas = _compute_repo_quotas(
            instances_by_repo, selected_repos, target_count, total_instances
        )
    else:
        quotas = {repo: target_count for repo in instances_by_repo}

    selected_instances: List[Dict[str, Any]] = []

    for repo, instances in instances_by_repo.items():
        selected_instances.extend(
            _select_by_agent_difficulty(instances, quotas.get(repo, target_count))
        )

    return selected_instances


def group_instances_by_repo(
    multilingual_dataset: Optional[Iterable[Dict[str, Any]]],
    lite_dataset: Optional[Iterable[Dict[str, Any]]],
    repo_to_language: Dict[str, str],
    target_languages: Set[str],
) -> Dict[str, Dict[str, Any]]:
    repo_info: Dict[str, Dict[str, Any]] = {}

    if multilingual_dataset:
        for instance in multilingual_dataset:
            repo = instance.get("repo")
            if not repo:
                continue
            language_group = repo_to_language.get(repo)
            if not language_group or language_group not in target_languages:
                continue
            info = repo_info.setdefault(
                repo, {"language_group": language_group, "instances": []}
            )
            info["instances"].append(instance)

    if lite_dataset and PYTHON_LANGUAGE in target_languages:
        for instance in lite_dataset:
            repo = instance.get("repo")
            if not repo:
                continue
            info = repo_info.setdefault(
                repo, {"language_group": PYTHON_LANGUAGE, "instances": []}
            )
            info["instances"].append(instance)

    return repo_info


def filter_repos_by_instance_count(
    repo_info: Dict[str, Dict[str, Any]],
    min_instances: int = 3,
) -> Dict[str, Dict[str, Any]]:
    return {
        repo: info
        for repo, info in repo_info.items()
        if len(info["instances"]) >= min_instances
    }


def _load_gt_cache(cache_path: Path) -> Dict[str, Dict[str, Any]]:
    """Load gt_locator cache from disk."""
    if not cache_path.exists():
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load gt_locator cache: %s", exc)
        return {}


def _save_gt_cache(cache: Dict[str, Dict[str, Any]], cache_path: Path) -> None:
    """Save gt_locator cache to disk."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    logger.info("Saved %d gt_locator results to cache", len(cache))


def run_gt_locator_filter(
    instance_difficulties: List[Dict[str, Any]],
    selected_repos: Dict[str, List[Dict[str, Any]]],
    *,
    work_dir: Path,
    cache_path: Path,
    max_gt_code_blocks: int = 10,
) -> List[Dict[str, Any]]:
    """Run gt_locator on instances from selected repos; exclude those with added symbols.

    Args:
        instance_difficulties: Full list of instance dicts (with agent_difficulty).
        selected_repos: Dict from select_representative_repos (language_group -> list).
        work_dir: Directory for GTLocator repo operations (clone, checkout).
        cache_path: Path to gt_locator_cache.json.
        max_gt_code_blocks: Maximum number of GT code blocks allowed per instance.
            Instances exceeding this limit are excluded. Default: 10.

    Returns:
        Filtered list with gt_locator fields added. Instances that have any
        ``symbols_added``, a gt_locator error, or too many code blocks are excluded.
    """
    from codeminer.dataset.gt_locate import GTLocator

    selected_repo_names = {
        repo_info["repo"] for repos in selected_repos.values() for repo_info in repos
    }

    candidates = [
        inst for inst in instance_difficulties if inst["repo"] in selected_repo_names
    ]
    logger.info(
        "gt_locator: %d instances from %d selected repos",
        len(candidates),
        len(selected_repo_names),
    )

    cache = _load_gt_cache(cache_path)
    logger.info("gt_locator cache: %d entries loaded", len(cache))

    uncached = [inst for inst in candidates if inst["instance_id"] not in cache]
    logger.info(
        "gt_locator: %d cached, %d to analyze",
        len(candidates) - len(uncached),
        len(uncached),
    )

    if uncached:
        locator = GTLocator(work_dir=str(work_dir))

        # Group by repo to minimise checkout thrashing.
        by_repo: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for inst in uncached:
            by_repo[inst["repo"]].append(inst)

        for repo, repo_instances in by_repo.items():
            logger.info(
                "gt_locator: analyzing %d instances for %s",
                len(repo_instances),
                repo,
            )
            for inst in repo_instances:
                iid = inst["instance_id"]
                try:
                    result = locator.analyze_instance(inst)
                    cache[iid] = {
                        "symbols_modified": result["symbols_modified"],
                        "symbols_added": result["symbols_added"],
                        "symbols_deleted": result["symbols_deleted"],
                        "target_files": result["target_files"],
                        "code_blocks": result["code_blocks"],
                        "error": result["error"],
                    }
                except Exception as e:
                    logger.error("gt_locator error for %s: %s", iid, e, exc_info=True)
                    cache[iid] = {
                        "symbols_modified": [],
                        "symbols_added": [],
                        "symbols_deleted": [],
                        "target_files": [],
                        "code_blocks": [],
                        "error": str(e),
                    }

        _save_gt_cache(cache, cache_path)

    # Enrich instances and filter.
    filtered: List[Dict[str, Any]] = []
    excluded_added = 0
    excluded_error = 0
    excluded_empty_blocks = 0
    excluded_too_many_blocks = 0
    for inst in candidates:
        iid = inst["instance_id"]
        gt = cache.get(iid, {})

        if gt.get("symbols_added"):
            excluded_added += 1
            logger.debug(
                "Excluding %s: has %d added symbols", iid, len(gt["symbols_added"])
            )
            continue

        if gt.get("error"):
            excluded_error += 1
            logger.debug("Excluding %s: gt_locator error: %s", iid, gt["error"])
            continue

        n_blocks = len(gt.get("code_blocks", []))
        if n_blocks == 0:
            excluded_empty_blocks += 1
            logger.debug("Excluding %s: empty gt_code_blocks", iid)
            continue

        if n_blocks > max_gt_code_blocks:
            excluded_too_many_blocks += 1
            logger.debug(
                "Excluding %s: %d gt_code_blocks exceeds max %d",
                iid,
                n_blocks,
                max_gt_code_blocks,
            )
            continue

        enriched = dict(inst)
        enriched["gt_symbols_modified"] = gt.get("symbols_modified", [])
        enriched["gt_symbols_deleted"] = gt.get("symbols_deleted", [])
        enriched["gt_target_files"] = gt.get("target_files", [])
        enriched["gt_code_blocks"] = gt.get("code_blocks", [])
        filtered.append(enriched)

    logger.info(
        "gt_locator filter: %d passed, %d excluded (added), %d excluded (error), "
        "%d excluded (empty blocks), %d excluded (>%d code_blocks), from %d candidates",
        len(filtered),
        excluded_added,
        excluded_error,
        excluded_empty_blocks,
        excluded_too_many_blocks,
        max_gt_code_blocks,
        len(candidates),
    )
    return filtered


def _is_hidden_path(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def _clone_repo(repo: str, repo_dir: Path, shallow: bool) -> None:
    if repo_dir.exists():
        return
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    clone_cmd = ["git", "clone"]
    if shallow:
        clone_cmd.extend(["--depth", "1", "--no-tags"])
    clone_cmd.extend([f"https://github.com/{repo}.git", str(repo_dir)])
    logger.info("Cloning %s", repo)
    subprocess.run(
        clone_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def _count_repo_files(repo_dir: Path) -> int:
    count = 0
    for path in repo_dir.rglob("*"):
        if not path.is_file():
            continue
        if _is_hidden_path(path):
            continue
        count += 1
    return count


def calculate_repo_sizes(
    repo_info: Dict[str, Dict[str, Any]],
    cache_dir: Path,
    shallow: bool = True,
) -> List[Dict[str, Any]]:
    repo_sizes = []
    for repo, info in repo_info.items():
        repo_dir = cache_dir / repo.replace("/", "_")
        _clone_repo(repo, repo_dir, shallow=shallow)
        file_count = _count_repo_files(repo_dir)
        repo_sizes.append(
            {
                "repo": repo,
                "language_group": info["language_group"],
                "file_count": file_count,
            }
        )
    return repo_sizes


def save_json(data: Any, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def save_csv(
    data: Sequence[Dict[str, Any]], path: str, fieldnames: Sequence[str]
) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in data:
            writer.writerow(row)


def _resolve_cache_dir(cache_dir: Optional[Path]) -> Path:
    return cache_dir if cache_dir is not None else Path.home() / ".codeminer"


def load_multilingual_dataset(
    dataset_name: str,
    split: str,
    filter_instance: str,
) -> Iterable[Dict[str, Any]]:
    dataset = SwebenchMultilingualDataset(
        dataset=dataset_name,
        split=split,
        filter_instance=filter_instance,
    )
    return dataset.load()


def load_lite_dataset(
    dataset_name: str,
    split: str,
    filter_instance: str,
) -> Iterable[Dict[str, Any]]:
    dataset = SwebenchDataset(
        dataset=dataset_name,
        split=split,
        filter_instance=filter_instance,
    )
    return dataset.load()


def run_sampling(config: SamplingConfig) -> SamplingResults:
    cache_dir = _resolve_cache_dir(config.cache_dir)
    repo_cache_dir = config.repo_cache_dir or cache_dir

    target_languages = set(config.languages)
    non_python_languages = target_languages - {PYTHON_LANGUAGE}

    repo_to_language: Dict[str, str] = {}
    multilingual_dataset = None
    lite_dataset = None
    if non_python_languages:
        logger.info("Loading multilingual dataset: %s", config.multilingual_dataset)
        multilingual_dataset = load_multilingual_dataset(
            config.multilingual_dataset, config.dataset_split, config.filter_instance
        )
        if config.multilingual_csv_path is None:
            local_csv = Path(__file__).resolve().parent / "data"
            local_csv = local_csv / "swebench_multilingual_repos.csv"
            repo_to_language = read_repo_language_map(local_csv)
            if not repo_to_language:
                raise FileNotFoundError(
                    "Missing multilingual repo map. Provide multilingual_csv_path or "
                    "add swebench_multilingual_repos.csv under dataset/collect/data."
                )
            valid_languages = set(repo_to_language.values())
        else:
            csv_path = config.multilingual_csv_path
            valid_languages, repo_to_language = read_multilingual_csv(str(csv_path))

        invalid_languages = non_python_languages - valid_languages
        if invalid_languages:
            raise ValueError(
                f"Invalid languages: {', '.join(sorted(invalid_languages))}. "
                f"Available: {', '.join(sorted(valid_languages))}"
            )

    if PYTHON_LANGUAGE in target_languages:
        logger.info("Loading Lite dataset: %s", config.lite_dataset)
        lite_dataset = load_lite_dataset(
            config.lite_dataset, config.dataset_split, config.filter_instance
        )

    repo_info = group_instances_by_repo(
        multilingual_dataset, lite_dataset, repo_to_language, target_languages
    )
    repo_info = filter_repos_by_instance_count(
        repo_info, min_instances=config.min_instances
    )

    repo_sizes = calculate_repo_sizes(
        repo_info, repo_cache_dir, shallow=config.shallow_clone
    )
    selected_repos = select_representative_repos(
        repo_sizes, target_count=config.repos_per_language
    )

    instance_difficulties = calculate_instance_difficulties(repo_info)

    from .difficulty_classifier import AgentDifficultyClassifier

    _output_dir = config.output_dir or (cache_dir / "swebench_sampling")
    classifier = AgentDifficultyClassifier(
        model=config.difficulty_model,
        cache_path=_output_dir / "difficulty_cache.json",
    )
    try:
        instance_difficulties = classifier.classify_batch(instance_difficulties)
    finally:
        classifier.close()

    gt_work_dir = config.gt_locator_work_dir or repo_cache_dir
    gt_filtered = run_gt_locator_filter(
        instance_difficulties,
        selected_repos,
        work_dir=gt_work_dir,
        cache_path=_output_dir / "gt_locator_cache.json",
        max_gt_code_blocks=config.max_gt_code_blocks,
    )

    selected_instances = select_representative_instances(
        gt_filtered,
        selected_repos,
        target_count=config.instances_per_repo,
        total_instances=config.total_instances,
    )

    output_dir = None
    if config.write_outputs:
        output_dir = config.output_dir or (cache_dir / "swebench_sampling")
        output_dir.mkdir(parents=True, exist_ok=True)

        save_json(selected_repos, str(output_dir / "selected_repos.json"))
        save_json(selected_instances, str(output_dir / "selected_instances.json"))
        save_csv(
            [
                {
                    "language_group": row["language_group"],
                    "repo": row["repo"],
                    "instance_id": row["instance_id"],
                    "changed_loc": row["changed_loc"],
                    "difficulty_level": row["difficulty_level"],
                    "total_in_repo": row["total_in_repo"],
                    "gt_symbols_count": len(row.get("gt_symbols_modified", []))
                    + len(row.get("gt_symbols_deleted", [])),
                    "gt_has_ground_truth": bool(row.get("gt_code_blocks")),
                }
                for row in selected_instances
            ],
            str(output_dir / "selected_instances.csv"),
            [
                "language_group",
                "repo",
                "instance_id",
                "changed_loc",
                "difficulty_level",
                "total_in_repo",
                "gt_symbols_count",
                "gt_has_ground_truth",
            ],
        )

    return SamplingResults(
        selected_repos=selected_repos,
        selected_instances=selected_instances,
        repo_sizes=repo_sizes,
        instance_difficulties=instance_difficulties,
        gt_filtered_instances=gt_filtered,
        output_dir=output_dir,
    )
