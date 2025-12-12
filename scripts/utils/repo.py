"""
Repository Processing Module

Provides repository grouping, filtering, and size calculation functionality.
"""

import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Set

# Add project root to path for codeminer imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from codeminer.env.utils import clone_repo
from codeminer.log_utils import get_logger

logger = get_logger(__name__)

# =============================================================================
# Constants
# =============================================================================

# Language group mapping: categorize raw language labels
LANGUAGE_GROUP_MAPPING = {
    "Rust": "Rust",
    "C": "C/C++",
    "C++": "C/C++",
    "JavaScript/TypeScript": "JS/TS",
    "JavaScript": "JS/TS",
    "TypeScript": "JS/TS",
    "Go": "Go",
    "Java": "Java",
    "PHP": "PHP",
    "Ruby": "Ruby",
    "Python": "Python",
}

# Python uses SWE-bench Lite dataset separately
PYTHON_LANGUAGE = "Python"


# =============================================================================
# Repository Grouping and Filtering
# =============================================================================


def group_instances_by_repo(
    multilingual_dataset: Any,
    lite_dataset: Any,
    repo_to_language: Dict[str, str],
    target_languages: Set[str],
) -> Dict[str, Dict[str, Any]]:
    """Group instances by repo, keeping only repos in target languages.

    Supports processing both SWE-bench Multilingual and SWE-bench Lite (Python) datasets.
    """
    repo_info = defaultdict(
        lambda: {"instances": [], "language_group": None, "language_raw": None}
    )

    # Process Multilingual dataset (non-Python languages)
    if multilingual_dataset is not None:
        for instance in multilingual_dataset:
            repo = instance["repo"]
            language_raw = repo_to_language.get(repo)

            if language_raw is None:
                logger.warning(f"Language info not found for repo {repo}, skipping")
                continue

            # Check if in target languages
            if language_raw not in target_languages:
                continue

            language_group = LANGUAGE_GROUP_MAPPING.get(language_raw, language_raw)
            repo_info[repo]["language_group"] = language_group
            repo_info[repo]["language_raw"] = language_raw
            repo_info[repo]["instances"].append(instance)

    # Process SWE-bench Lite dataset (Python)
    if lite_dataset is not None and PYTHON_LANGUAGE in target_languages:
        for instance in lite_dataset:
            repo = instance["repo"]
            repo_info[repo]["language_group"] = "Python"
            repo_info[repo]["language_raw"] = "Python"
            repo_info[repo]["instances"].append(instance)

    # Calculate num_instances
    for info in repo_info.values():
        info["num_instances"] = len(info["instances"])

    logger.info(f"Total {len(repo_info)} repos in target language groups")
    return dict(repo_info)


def filter_repos_by_instance_count(
    repo_info: Dict[str, Dict[str, Any]],
    min_instances: int = 3,
) -> Dict[str, Dict[str, Any]]:
    """Filter out repos with insufficient instance count."""
    filtered = {
        repo: info
        for repo, info in repo_info.items()
        if info["num_instances"] >= min_instances
    }
    removed_count = len(repo_info) - len(filtered)
    remaining = len(filtered)
    logger.info(
        f"Filtered out {removed_count} repos with < {min_instances} instances, "
        f"{remaining} remaining"
    )
    return filtered


# =============================================================================
# Repository Size Calculation
# =============================================================================


def count_files_in_repo(repo_path: str) -> int:
    """
    Count non-hidden regular files in repository
    (excluding .git and other hidden directories).
    """
    file_count = 0
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if not f.startswith("."):
                file_path = os.path.join(root, f)
                if os.path.isfile(file_path):
                    file_count += 1
    return file_count


def calculate_repo_sizes(
    repo_info: Dict[str, Dict[str, Any]],
    cache_dir: str,
    shallow: bool = True,
) -> List[Dict[str, Any]]:
    """Calculate sizes (file count) for all repos."""
    repo_sizes = []

    for repo, info in repo_info.items():
        # Clone repository
        try:
            repo_path = clone_repo(repo, cache_dir, shallow=shallow)
        except RuntimeError as e:
            logger.error(f"Failed to clone {repo}: {e}")
            continue

        # Count files
        file_count = count_files_in_repo(repo_path)

        repo_sizes.append(
            {
                "repo": repo,
                "language_group": info["language_group"],
                "file_count": file_count,
                "num_instances": info["num_instances"],
            }
        )

    return repo_sizes
