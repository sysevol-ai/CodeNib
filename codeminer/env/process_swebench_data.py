import argparse
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union

import datasets
from datasets import Features, Value

from ..log_utils import get_logger
from .utils import checkout_commit, clone_repo, get_cache_dir

logger = get_logger(__name__)


# =============================================================================
# Dataset Loading Functions
# =============================================================================


def load_filter_swebench_dataset(
    args: argparse.Namespace,
) -> datasets.arrow_dataset.Dataset:
    ret = load_filter_swebench_dataset_explicit(
        dataset=args.dataset, filter_instance=args.filter_instance, split=args.split
    )
    # Cannot has both idx_list and idx_range
    assert not (
        hasattr(args, "idx_list") and hasattr(args, "idx_range")
    ), "Cannot has both idx_list and idx_range in arguments"
    if hasattr(args, "idx_list"):
        if args.filter_instance != ".*":
            logger.info(
                (
                    "Running idx_list on a filtered (non-full) dataset."
                    "Please make sure this is expected."
                )
            )
        return ret.select(args.idx_list)
    elif hasattr(args, "idx_range"):
        if args.filter_instance != ".*":
            logger.info(
                (
                    "Running idx_range on a filtered (non-full) dataset."
                    "Please make sure this is expected."
                )
            )
        start_idx = args.idx_range[0]
        end_idx = args.idx_range[1]
        assert start_idx < end_idx, "start_idx should be smaller than end_idx"
        return ret.select(range(start_idx, end_idx))
    else:
        return ret


def load_filter_swebench_dataset_explicit(
    dataset: str, filter_instance: str, split: str
) -> datasets.arrow_dataset.Dataset:
    """
    Load and cache SWE-bench series datasets.

    Supported datasets include:
    - princeton-nlp/SWE-bench_Lite
    - princeton-nlp/SWE-bench_Verified
    - SWE-bench/SWE-bench_Multilingual
    - And other datasets compatible with SWE-bench format

    Args:
        dataset: HuggingFace dataset name
        filter_instance: Regular expression for filtering instance_id (".*" means no filtering)
        split: Dataset split (e.g., "test")

    Returns:
        Filtered Dataset object
    """
    cache_dir = get_cache_dir()
    dataset_file = f'{dataset.replace("/", "__")}_{split}.json'
    dataset_path = f"{cache_dir}/{dataset_file}"

    if not os.path.exists(dataset_path):
        # First load: download from HuggingFace and cache to local JSON
        ds = datasets.load_dataset(dataset, split=split)
        logger.info(f"Loaded {len(ds)} instances from {dataset} dataset, split {split}")
        ds.to_json(dataset_path)
    else:
        # Load from local cache
        logger.info(f"Dataset already exists at {dataset_path}")
        data_files = {split: dataset_path}

        # Try using explicit schema (for SWE-bench Lite/Verified)
        # If fails, fall back to auto-inference (for Multilingual, etc.)
        features = _get_swebench_features(dataset)

        if features is not None:
            try:
                ds = datasets.load_dataset(
                    "json", data_files=data_files, split=split, features=features
                )
            except Exception:
                # Schema mismatch, fall back to auto-inference
                logger.info("Explicit schema failed, falling back to auto-inference")
                ds = datasets.load_dataset("json", data_files=data_files, split=split)
        else:
            # Unknown dataset type, use auto-inference
            ds = datasets.load_dataset("json", data_files=data_files, split=split)

        logger.info(f"Loaded {len(ds)} instances from cached dataset at {dataset_path}")

    return ds.filter(
        input_columns=["instance_id"],
        function=lambda x: bool(re.match(filter_instance, x)),
    )


def _get_swebench_features(dataset: str) -> Optional[Features]:
    """
    Return corresponding Features schema based on dataset name.

    Args:
        dataset: Dataset name

    Returns:
        Features object, or None for unknown datasets (use auto-inference)
    """
    # Base schema for SWE-bench Lite/Verified
    if "lite" in dataset.lower() or "verified" in dataset.lower():
        base_features = {
            "repo": Value("string"),
            "instance_id": Value("string"),
            "base_commit": Value("string"),
            "patch": Value("string"),
            "test_patch": Value("string"),
            "problem_statement": Value("string"),
            "hints_text": Value("string"),
            "created_at": Value("string"),
            "version": Value("string"),
            "FAIL_TO_PASS": Value("string"),
            "PASS_TO_PASS": Value("string"),
            "environment_setup_commit": Value("string"),
        }

        # SWE-bench Verified has additional difficulty field
        if "verified" in dataset.lower():
            base_features["difficulty"] = Value("string")

        return Features(base_features)

    # SWE-bench Multilingual and other datasets: return None to use auto-inference
    # This allows supporting different schemas (e.g., FAIL_TO_PASS is list instead of string)
    return None


def process_swebench_instance(
    dataset_row: Dict[str, Any], cache_dir: Union[Path, str] = "~/.codeminer"
) -> str:
    """
    Process a dataset instance by:
    1. Downloading the repository if not exists
    2. Checking out the specific commit

    Args:
        dataset_row: A row from the SWE-bench dataset containing repo_name,
            instance_id, and base_commit
        cache_dir: Directory to store repositories

    Returns:
        Path to the checked out repository
    """
    repo_name = dataset_row["repo"]
    base_commit = dataset_row["base_commit"]

    # Normalize cache_dir
    if isinstance(cache_dir, str):
        cache_dir = os.path.expanduser(cache_dir)
    cache_dir = str(Path(cache_dir).absolute())
    os.makedirs(cache_dir, exist_ok=True)

    # Clone repository (uses shared helper function)
    repo_path = clone_repo(repo_name, cache_dir, shallow=False)

    # Checkout to the specific commit
    checkout_commit(repo_path, base_commit, fetch=True)

    return repo_path
