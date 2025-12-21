#!/usr/bin/env python3
"""
Analyze SWE-bench patches to extract symbol-level changes.

This script extracts ground truth (GT) localization information from SWE-bench patches,
identifying which symbols (functions, methods, classes) were modified, added, or deleted.
Supports SWE-bench Verified, Lite, and Multilingual datasets.

Usage Examples:
    # Process all instances in the test split using Verified dataset (default)
    python scripts/swebench_gt_locate.py

    # Process multiple datasets with selected instances and upload to HuggingFace Hub
    python scripts/swebench_gt_locate.py --dataset lite multilingual \
        --filter-file scripts/sampling_output/selected_instances.csv \
        --push-to-hub


Output Format:
    Each entry in the output JSON array contains:
    {
        "instance_id": "astropy__astropy-13398",
        "repo": "astropy/astropy",
        "base_commit": "6500928dc0e57be8f06d1162eacc3ba5e2eff692",
        "target_files":     ["astropy/coordinates/builtin_frames/itrs.py", ...],
        "symbols_modified": ["file.py:Foo.bar()@10:50", "file.py:Baz()@60:80"],
        "symbols_added":    ["file.py:NewFunc()"],
        "symbols_deleted":  ["file.py:OldFunc()@100:120"],
        "error": null
    }

    Symbol Naming (with line range @start:end for modified/deleted):
    - Top-level functions: "module/file.py:function_name()@start:end"
    - Classes: "module/file.py:ClassName@start:end"
    - Class methods: "module/file.py:ClassName.method_name()@start:end"

HuggingFace Hub:
    When --push-to-hub is specified, results are uploaded to:
    stzoozz/codeminer_swebench

    Load with: datasets.load_dataset("stzoozz/codeminer_swebench", split="test")
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import datasets
from datasets import Features, Value

sys.path.insert(0, str(Path(__file__).parent.parent))

from codeminer.code_chunking import create_chunker  # noqa: E402
from codeminer.log_utils import get_logger  # noqa: E402

logger = get_logger(__name__)

DATASET_NAMES = {
    "verified": "princeton-nlp/SWE-bench_Verified",
    "lite": "princeton-nlp/SWE-bench_Lite",
    "multilingual": "SWE-bench/SWE-bench_Multilingual",
}

# Supported file extensions and their corresponding languages
SUPPORTED_EXTENSIONS: Dict[str, str] = {
    ".py": "python",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".rs": "rust",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}

# Extensions we recognize but don't support yet (for logging)
UNSUPPORTED_EXTENSIONS: Set[str] = {".java", ".go"}


class GTLocator:
    """Ground truth locator that analyzes patches to extract symbol-level changes."""

    def __init__(self, work_dir: Optional[str] = None):
        """
        Initialize the ground truth locator.

        Args:
            work_dir: Working directory for cloning repos (default: ~/.codeminer/tmp)
        """
        if work_dir is None:
            cache_dir = str(Path.home()) + "/.codeminer"
            os.makedirs(cache_dir, exist_ok=True)
            self.work_dir = os.path.join(cache_dir, "tmp")
            os.makedirs(self.work_dir, exist_ok=True)
            self.is_temp_dir = True
        else:
            self.work_dir = work_dir
            self.is_temp_dir = False

        # Cache chunkers by language
        self._chunkers: Dict[str, Any] = {}
        logger.info(f"Initialized GTLocator with work_dir: {self.work_dir}")

    def _get_chunker(self, language: str) -> Any:
        """Get or create a chunker for the specified language."""
        if language not in self._chunkers:
            self._chunkers[language] = create_chunker(language)
        return self._chunkers[language]

    def _detect_language(self, file_path: str) -> Optional[str]:
        """Detect language from file extension."""
        ext = os.path.splitext(file_path)[1].lower()
        return SUPPORTED_EXTENSIONS.get(ext)

    def get_target_files(self, patch_content: str) -> List[str]:
        """
        Extract target file paths from a unified diff patch.

        Args:
            patch_content: Content of the patch in unified diff format

        Returns:
            List of file paths affected by the patch
        """
        target_files = []
        file_pattern = re.compile(r"^(?:\+\+\+|---) [ab]/(.+)$", re.MULTILINE)

        for match in file_pattern.finditer(patch_content):
            file_path = match.group(1)
            if file_path != "/dev/null" and file_path not in target_files:
                target_files.append(file_path)

        logger.debug(f"Extracted {len(target_files)} target files from patch")
        return target_files

    def get_changed_line_ranges(
        self, patch_content: str
    ) -> Dict[str, List[Tuple[int, int]]]:
        """
        Extract the line ranges that were changed in each file from the patch.

        Args:
            patch_content: Content of the patch in unified diff format

        Returns:
            Dict mapping file paths to list of (start_line, end_line) tuples
            (new file coords, 0-based, inclusive)
        """
        changed_ranges = defaultdict(list)
        current_file = None
        old_file = None

        for line in patch_content.split("\n"):
            if line.startswith("--- a/"):
                old_file = line[6:]
                if old_file == "/dev/null":
                    old_file = None
                continue

            if line.startswith("+++ b/"):
                current_file = line[6:]
                if current_file == "/dev/null":
                    current_file = old_file
                continue

            hunk_match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if hunk_match and current_file:
                # Unified diff line numbers are 1-based; convert to 0-based
                # for chunk overlap checks.
                new_start = int(hunk_match.group(3)) - 1
                new_count = int(hunk_match.group(4)) if hunk_match.group(4) else 1
                if new_count > 0:
                    changed_ranges[current_file].append(
                        (new_start, new_start + new_count - 1)
                    )

        logger.debug(f"Extracted changed line ranges for {len(changed_ranges)} files")
        return dict(changed_ranges)

    def extract_symbols_from_file(
        self, file_path: str, language: str, relative_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Extract all symbols from a file with their chunks.

        Args:
            file_path: Absolute path to the source file
            language: Programming language of the file
            relative_path: Relative path for node_id generation

        Returns:
            Dictionary mapping node_id (file:symbol format) to CodeChunk objects
        """
        if not os.path.exists(file_path):
            logger.debug(f"File does not exist: {file_path}")
            return {}

        try:
            chunker = self._get_chunker(language)
            chunks = chunker.chunk_file(file_path, relative_path)
            symbols = {}

            for chunk in chunks:
                if chunk.chunk_type in ("function", "method", "class"):
                    symbols[chunk.node_id] = chunk

            logger.debug(f"Extracted {len(symbols)} symbols from {file_path}")
            return symbols
        except Exception as e:
            logger.warning(f"Failed to extract symbols from {file_path}: {e}")
            return {}

    def clone_repo(self, repo_url: str, target_dir: str) -> bool:
        """Clone a git repository if it doesn't exist."""
        try:
            if os.path.exists(target_dir):
                logger.info(f"Repository already exists at {target_dir}")
                return True

            logger.info(f"Cloning {repo_url} to {target_dir}")
            subprocess.run(
                ["git", "clone", repo_url, target_dir],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            logger.info(f"Successfully cloned repository to {target_dir}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to clone {repo_url}: {e}")
            logger.error(f"STDERR: {e.stderr.decode('utf-8')}")
            return False

    def checkout_commit(self, repo_dir: str, commit_hash: str) -> bool:
        """Checkout a specific commit in a git repository."""
        git_dir = os.path.join(repo_dir, ".git")
        if not os.path.exists(git_dir):
            logger.error(f"Not a git repository: {repo_dir}")
            return False

        try:
            logger.debug("Resetting repository to clean state")
            subprocess.run(
                ["git", "reset", "--hard"],
                cwd=repo_dir,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            subprocess.run(
                ["git", "clean", "-fd"],
                cwd=repo_dir,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            logger.info("Fetching updates from remote repository")
            subprocess.run(
                ["git", "fetch", "--all"],
                cwd=repo_dir,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            logger.info(f"Checking out commit {commit_hash}")
            subprocess.run(
                ["git", "checkout", "-f", commit_hash],
                cwd=repo_dir,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to checkout commit {commit_hash}: {e}")
            logger.error(f"STDERR: {e.stderr.decode('utf-8')}")
            return False

    def apply_patch(self, repo_dir: str, patch_content: str) -> bool:
        """Apply a patch to a repository."""
        try:
            logger.info("Applying patch")
            process = subprocess.run(
                ["git", "apply", "-"],
                cwd=repo_dir,
                input=patch_content,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if process.returncode != 0:
                logger.error(f"Failed to apply patch: {process.stderr}")
                return False

            return True
        except subprocess.TimeoutExpired:
            logger.error("Timeout while applying patch")
            return False
        except Exception as e:
            logger.error(f"Error applying patch: {e}")
            return False

    def compare_symbols(
        self,
        symbols_before: Dict[str, Any],
        symbols_after: Dict[str, Any],
        changed_ranges: Dict[str, List[Tuple[int, int]]],
    ) -> Tuple[List[str], List[str], List[str]]:
        """
        Compare symbols before and after patch to identify changes.

        Args:
            symbols_before: Dictionary mapping symbol names to CodeChunk objects before patch
            symbols_after: Dictionary mapping symbol names to CodeChunk objects after patch
            changed_ranges: Dictionary mapping file paths to list of changed line ranges

        Returns:
            Tuple of (symbols_modified, symbols_added, symbols_deleted)
        """
        logger.debug(
            f"Comparing symbols: {len(symbols_before)} before, {len(symbols_after)} after"
        )

        before_set = set(symbols_before.keys())
        after_set = set(symbols_after.keys())

        # Symbols that were deleted
        deleted_names = sorted(list(before_set - after_set))
        symbols_deleted = [
            # CodeChunk lines are 0-based; output as 1-based inclusive ranges.
            f"{name}@{symbols_before[name].start_line + 1}:{symbols_before[name].end_line + 1}"
            for name in deleted_names
        ]
        logger.debug(f"Found {len(symbols_deleted)} deleted symbols")

        # Symbols that were added
        symbols_added = sorted(list(after_set - before_set))
        logger.debug(f"Found {len(symbols_added)} added symbols")

        # Symbols that exist in both - check if they were modified
        common = before_set & after_set
        symbols_modified: List[str] = []

        for symbol_name in sorted(common):
            file_path, _, _ = symbol_name.partition(":")
            if file_path not in changed_ranges:
                continue

            chunk_before = symbols_before[symbol_name]
            chunk_after = symbols_after[symbol_name]

            length_before = chunk_before.end_line - chunk_before.start_line + 1
            length_after = chunk_after.end_line - chunk_after.start_line + 1

            is_modified = False
            if length_before != length_after:
                is_modified = True
            else:
                has_overlap = any(
                    change_end >= chunk_after.start_line
                    and change_start <= chunk_after.end_line
                    for change_start, change_end in changed_ranges[file_path]
                )
                if has_overlap and chunk_before.content != chunk_after.content:
                    is_modified = True

            if is_modified:
                symbols_modified.append(
                    # CodeChunk lines are 0-based; output as 1-based inclusive ranges.
                    f"{symbol_name}@{chunk_before.start_line + 1}:{chunk_before.end_line + 1}"
                )

        logger.debug(f"Found {len(symbols_modified)} modified symbols")
        return symbols_modified, symbols_added, symbols_deleted

    def analyze_instance(self, instance: Dict) -> Dict:
        """
        Analyze a single SWE-bench instance.

        Args:
            instance: Dictionary containing instance data

        Returns:
            Dictionary with analysis results
        """
        instance_id = instance["instance_id"]
        repo = instance["repo"]
        base_commit = instance["base_commit"]
        patch = instance["patch"]

        logger.info(f"Analyzing instance: {instance_id}")

        result = {
            "instance_id": instance_id,
            "repo": repo,
            "base_commit": base_commit,
            "target_files": [],
            "symbols_modified": [],
            "symbols_added": [],
            "symbols_deleted": [],
            "error": None,
        }

        # Setup repository
        repo_dir_name = repo.replace("/", "_")
        repo_dir = os.path.join(self.work_dir, repo_dir_name)
        repo_url = f"https://github.com/{repo}.git"
        logger.info(f"Repository directory: {repo_dir}")

        # Clone and checkout
        if not self.clone_repo(repo_url, repo_dir):
            result["error"] = "Failed to clone repository"
            return result

        if not self.checkout_commit(repo_dir, base_commit):
            result["error"] = "Failed to checkout base commit"
            return result

        # Get target files from patch
        logger.debug(f"Extracting target files for {instance_id}")
        target_files = self.get_target_files(patch)
        result["target_files"] = target_files

        if not target_files:
            logger.warning(f"No target files found in patch for {instance_id}")
            result["error"] = "No target files found in patch"
            return result

        # Group files by language
        files_by_language: Dict[str, List[str]] = defaultdict(list)
        unsupported_files: List[str] = []

        for file_path in target_files:
            lang = self._detect_language(file_path)
            if lang:
                files_by_language[lang].append(file_path)
            else:
                ext = os.path.splitext(file_path)[1].lower()
                if ext in UNSUPPORTED_EXTENSIONS:
                    logger.warning(f"Skipping unsupported file: {file_path} ({ext})")
                unsupported_files.append(file_path)

        if not files_by_language:
            logger.info(f"No supported files in {instance_id}")
            result["error"] = "No supported language files found"
            return result

        supported_count = sum(len(files) for files in files_by_language.values())
        logger.info(
            f"Found {supported_count} supported files, {len(unsupported_files)} unsupported"
        )

        # Extract symbols before patch
        logger.info("Extracting symbols BEFORE patch")
        symbols_before = {}
        for lang, files in files_by_language.items():
            for file_path in files:
                full_path = os.path.join(repo_dir, file_path)
                if os.path.exists(full_path):
                    file_symbols = self.extract_symbols_from_file(
                        full_path, lang, file_path
                    )
                    symbols_before.update(file_symbols)
        logger.info(f"Extracted {len(symbols_before)} symbols before patch")

        # Get changed line ranges
        logger.debug("Getting changed line ranges from patch")
        changed_ranges = self.get_changed_line_ranges(patch)

        # Apply patch
        if not self.apply_patch(repo_dir, patch):
            result["error"] = "Failed to apply patch"
            return result

        # Extract symbols after patch
        logger.info("Extracting symbols AFTER patch")
        symbols_after = {}
        for lang, files in files_by_language.items():
            for file_path in files:
                full_path = os.path.join(repo_dir, file_path)
                if os.path.exists(full_path):
                    file_symbols = self.extract_symbols_from_file(
                        full_path, lang, file_path
                    )
                    symbols_after.update(file_symbols)
        logger.info(f"Extracted {len(symbols_after)} symbols after patch")

        # Compare symbols to identify changes
        logger.info("Comparing symbols to identify changes")
        symbols_modified, symbols_added, symbols_deleted = self.compare_symbols(
            symbols_before, symbols_after, changed_ranges
        )

        result["symbols_modified"] = symbols_modified
        result["symbols_added"] = symbols_added
        result["symbols_deleted"] = symbols_deleted

        logger.info(
            f"Completed analysis for {instance_id}: "
            f"{len(symbols_modified)} modified, "
            f"{len(symbols_added)} added, "
            f"{len(symbols_deleted)} deleted"
        )

        return result

    def cleanup(self):
        """Clean up working directory if it's a temporary directory."""
        if self.is_temp_dir and self.work_dir and os.path.exists(self.work_dir):
            logger.info(f"Cleaning up temporary work directory: {self.work_dir}")
            shutil.rmtree(self.work_dir, ignore_errors=True)
        else:
            logger.info(f"Keeping repositories in work directory: {self.work_dir}")


def load_instance_ids_from_file(file_path: str) -> Set[str]:
    """
    Load instance IDs from a CSV or JSON file.

    Args:
        file_path: Path to CSV or JSON file

    Returns:
        Set of instance IDs
    """
    instance_ids: Set[str] = set()
    file_path = os.path.expanduser(file_path)

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Filter file not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".csv":
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if "instance_id" in row:
                    instance_ids.add(row["instance_id"])
        logger.info(f"Loaded {len(instance_ids)} instance IDs from CSV: {file_path}")

    elif ext == ".json":
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "instance_id" in item:
                    instance_ids.add(item["instance_id"])
                elif isinstance(item, str):
                    instance_ids.add(item)
        logger.info(f"Loaded {len(instance_ids)} instance IDs from JSON: {file_path}")

    else:
        raise ValueError(f"Unsupported filter file format: {ext}")

    return instance_ids


def load_swebench(
    split: str = "test",
    filter_pattern: Optional[str] = None,
    filter_ids: Optional[Set[str]] = None,
    limit: Optional[int] = None,
    dataset_types: Optional[List[str]] = None,
) -> datasets.Dataset:
    """
    Load the SWE-bench dataset with optional filtering and limiting.

    Args:
        split: Dataset split to load (default: "test")
        filter_pattern: Regex pattern to filter instance IDs
        filter_ids: Set of specific instance IDs to include
        limit: Maximum number of instances to return
        dataset_types: List of dataset types to load and merge

    Returns:
        Dataset object
    """
    if dataset_types is None:
        dataset_types = ["verified"]

    # Validate dataset types
    for dt in dataset_types:
        if dt.lower() not in DATASET_NAMES:
            raise ValueError(
                f"Invalid dataset_type: {dt}. Must be one of {list(DATASET_NAMES.keys())}"
            )

    logger.info(f"Loading SWE-bench datasets: {dataset_types} (split: {split})")

    cache_dir = str(Path.home()) + "/.codeminer"
    os.makedirs(cache_dir, exist_ok=True)

    all_instances: Dict[str, Dict] = {}  # Deduplicate by instance_id

    for dataset_type in dataset_types:
        dataset_name = DATASET_NAMES[dataset_type.lower()]
        dataset_file = f'{dataset_name.replace("/", "__")}_{split}.json'
        dataset_path = f"{cache_dir}/{dataset_file}"

        if not os.path.exists(dataset_path):
            logger.info(f"Downloading {dataset_name}...")
            ds = datasets.load_dataset(dataset_name, split=split)
            logger.info(f"Loaded {len(ds)} instances from {dataset_name}")
            ds.to_json(dataset_path)
        else:
            logger.info(f"Loading cached dataset from {dataset_path}")
            # Define features based on dataset type
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
            # Some datasets have additional fields
            if dataset_type.lower() in ("verified", "lite"):
                base_features["difficulty"] = Value("string")

            ft = Features(base_features)
            try:
                ds = datasets.load_dataset(
                    "json", data_files={split: dataset_path}, split=split, features=ft
                )
            except Exception:
                # Fallback without strict features
                ds = datasets.load_dataset(
                    "json", data_files={split: dataset_path}, split=split
                )

        # Add instances, deduplicating by instance_id
        for instance in ds:
            iid = instance["instance_id"]
            if iid not in all_instances:
                # Normalize instance: serialize complex types to strings
                inst_dict = {}
                for k, v in instance.items():
                    if v is None:
                        inst_dict[k] = None
                    elif hasattr(v, "isoformat"):  # datetime object
                        inst_dict[k] = v.isoformat()
                    elif isinstance(v, (list, dict)):
                        inst_dict[k] = json.dumps(v)
                    elif isinstance(v, bytes):
                        inst_dict[k] = v.decode("utf-8", errors="replace")
                    else:
                        inst_dict[k] = str(v) if not isinstance(v, str) else v
                all_instances[iid] = inst_dict

        logger.info(
            f"After {dataset_type}: {len(all_instances)} unique instances total"
        )

    # Convert back to dataset
    instances_list = list(all_instances.values())
    ds = datasets.Dataset.from_list(instances_list)

    # Apply filter_ids if specified
    if filter_ids:
        logger.info(f"Filtering to {len(filter_ids)} specific instance IDs")
        ds = ds.filter(lambda x: x["instance_id"] in filter_ids)
        logger.info(f"Filtered to {len(ds)} instances")

    # Apply regex filter if specified
    if filter_pattern and filter_pattern != ".*":
        logger.info(f"Filtering instances with pattern: {filter_pattern}")
        ds = ds.filter(lambda x: bool(re.match(filter_pattern, x["instance_id"])))
        logger.info(f"Filtered to {len(ds)} instances")

    # Apply limit if specified
    if limit is not None:
        logger.info(f"Limiting to {limit} instances")
        ds = ds.select(range(min(limit, len(ds))))

    logger.info(f"Final dataset size: {len(ds)} instances")
    return ds


def main():
    parser = argparse.ArgumentParser(
        description="Analyze SWE-bench patches to extract symbol-level changes"
    )
    parser.add_argument(
        "--dataset",
        type=str,
        nargs="+",
        default=["verified"],
        choices=["verified", "lite", "multilingual"],
        help="Dataset type(s) to use (default: verified)",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="test",
        help="Dataset split to analyze (default: test)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON file path (default: ~/.codeminer/swebench_gt.json)",
    )
    parser.add_argument(
        "--work-dir",
        type=str,
        default=None,
        help="Working directory for cloning repos (default: ~/.codeminer/tmp)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of instances to process (default: all)",
    )
    parser.add_argument(
        "--filter",
        type=str,
        default=None,
        help="Regex filter for instance IDs",
    )
    parser.add_argument(
        "--filter-file",
        type=str,
        default=None,
        help="Path to CSV or JSON file containing instance IDs to process",
    )
    parser.add_argument(
        "--keep-repos",
        action="store_true",
        help="Keep cloned repositories after analysis (default: cleanup)",
    )
    parser.add_argument(
        "--push-to-hub",
        action="store_true",
        help="Push results to HuggingFace Hub",
    )

    args = parser.parse_args()

    # Set default output path if not specified
    if args.output is None:
        cache_dir = str(Path.home()) + "/.codeminer"
        os.makedirs(cache_dir, exist_ok=True)
        dataset_suffix = "_".join(args.dataset)
        args.output = os.path.join(cache_dir, f"swebench_{dataset_suffix}_gt.json")

    # Load instance IDs from filter file if specified
    filter_ids = None
    if args.filter_file:
        filter_ids = load_instance_ids_from_file(args.filter_file)

    # Load dataset with filtering and limiting
    dataset = load_swebench(
        split=args.split,
        filter_pattern=args.filter,
        filter_ids=filter_ids,
        limit=args.limit,
        dataset_types=args.dataset,
    )

    logger.info(f"Processing {len(dataset)} instances")

    # Initialize locator
    locator = GTLocator(work_dir=args.work_dir)

    # Process instances
    results = []
    skipped_count = 0
    for i, instance in enumerate(dataset):
        logger.info(f"Processing instance {i + 1}/{len(dataset)}")
        try:
            result = locator.analyze_instance(instance)
            # Skip instances that only have symbols_added (no modified or deleted)
            if (
                not result.get("symbols_modified")
                and not result.get("symbols_deleted")
                and not result.get("error")
            ):
                skipped_count += 1
                logger.info(f"Skipping {result['instance_id']}: only has symbols_added")
                continue
            results.append(result)
        except Exception as e:
            logger.error(
                f"Error processing {instance['instance_id']}: {e}", exc_info=True
            )
            results.append({"instance_id": instance["instance_id"], "error": str(e)})

    # Save results to local JSON
    output_path = args.output
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Saving results to {output_path}")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    # Print summary
    success_count = sum(1 for r in results if not r.get("error"))
    error_count = len(results) - success_count

    logger.info(f"\n{'=' * 60}")
    logger.info("Analysis complete!")
    logger.info(f"Total processed: {len(dataset)}")
    logger.info(f"Skipped (added-only): {skipped_count}")
    logger.info(f"Output instances: {len(results)}")
    logger.info(f"Successful: {success_count}")
    logger.info(f"Errors: {error_count}")
    logger.info(f"Results saved to: {output_path}")
    logger.info(f"Repositories cached in: {locator.work_dir}")
    logger.info(f"{'=' * 60}")

    # Push to HuggingFace Hub if requested
    if args.push_to_hub:
        repo_id = f"stzoozz/codeminer_swebench"
        logger.info(f"Pushing results to HuggingFace Hub: {repo_id}")

        ds = datasets.Dataset.from_list(results)
        ds_dict = datasets.DatasetDict({args.split: ds})
        ds_dict.push_to_hub(repo_id, private=False)

        logger.info(f"Successfully pushed to https://huggingface.co/datasets/{repo_id}")

    # Cleanup
    if not args.keep_repos:
        locator.cleanup()
    else:
        logger.info(f"Keeping all repositories in: {locator.work_dir}")


if __name__ == "__main__":
    main()
