#!/usr/bin/env python3
"""
Analyze SWE-bench patches to extract symbol-level changes.

This module extracts ground truth (GT) localization information from SWE-bench patches,
identifying which symbols (functions, methods, classes) were modified, added,
or deleted.
Supports both SWE-bench Verified and Lite datasets.

Usage Examples:
    # Process all instances in the test split using Verified dataset (default)
    python codeminer/dataset/gt_locate.py

    # Process all instances using Lite dataset
    python codeminer/dataset/gt_locate.py --dataset lite

    # Process first 10 instances of repo "django/django", output into local file
    python codeminer/dataset/gt_locate.py --dataset verified \
        --filter "django__django-.*" --limit 10 --output results/test_gt.json \
        --keep-repos

Output Format:
    Each entry in the output JSON array contains:
    {
        "instance_id": "astropy__astropy-13398",
        "repo": "astropy/astropy",
        "base_commit": "6500928dc0e57be8f06d1162eacc3ba5e2eff692",
        "target_files":     ["astropy/coordinates/builtin_frames/itrs.py", ...],
        "symbols_modified": ["astropy/coordinates/builtin_frames/itrs.py:ITRS", ...],
        "symbols_added":    [
            "astropy/coordinates/builtin_frames/"
            "itrs_observed_transforms.py:itrs_to_observed()",
            ...,
        ],
        "symbols_deleted": [],
        "error": null
    }

    Symbol Naming:
    - Top-level functions: "module/file.py:function_name()"
    - Classes: "module/file.py:ClassName"
    - Class methods: "module/file.py:ClassName.method_name()"
"""

import argparse
import json
import os
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..code_chunking import create_chunker
from ..log_utils import get_logger
from .swebench import SwebenchDataset

logger = get_logger(__name__)


class GTLocator:
    """Ground truth locator that analyzes patches to extract symbol-level changes."""

    def __init__(self, work_dir: str = None, language: str = "python"):
        """
        Initialize the ground truth locator.

        Args:
            work_dir: Working directory for cloning repos (default: ~/.codeminer/tmp)
            language: Programming language to analyze (default: python)
        """
        if work_dir is None:
            # Create a temporary directory under ~/.codeminer
            cache_dir = str(Path.home()) + "/.codeminer"
            os.makedirs(cache_dir, exist_ok=True)
            self.work_dir = os.path.join(cache_dir, "tmp")
            os.makedirs(self.work_dir, exist_ok=True)
            self.is_temp_dir = True
        else:
            self.work_dir = work_dir
            self.is_temp_dir = False
        self.language = language
        self.chunker = create_chunker(language)
        logger.info(f"Initialized GTLocator with work_dir: {self.work_dir}")

    def get_target_files(self, patch_content: str) -> List[str]:
        """
        Extract target file paths from a unified diff patch.

        Args:
            patch_content: Content of the patch in unified diff format

        Returns:
            List of file paths affected by the patch
        """
        target_files = []

        # Match file paths in diff headers
        # Format: --- a/path/to/file.py or +++ b/path/to/file.py
        file_pattern = re.compile(r"^(?:\+\+\+|---) [ab]/(.+)$", re.MULTILINE)

        for match in file_pattern.finditer(patch_content):
            file_path = match.group(1)
            # Skip /dev/null entries (for new/deleted files)
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
            Dictionary mapping file paths to list of (start_line, end_line) tuples
        """
        changed_ranges = defaultdict(list)
        current_file = None
        old_file = None  # Track the "before" file for deleted files

        # Parse the patch line by line
        lines = patch_content.split("\n")
        i = 0

        while i < len(lines):
            line = lines[i]

            # Currently no detections
            # Check for "before" file header (--- a/...)
            if line.startswith("--- a/"):
                old_file = line[6:]  # Remove '--- a/' prefix
                if old_file == "/dev/null":
                    old_file = None
                i += 1
                continue

            # Check for "after" file header (+++ b/...)
            if line.startswith("+++ b/"):
                current_file = line[6:]  # Remove '+++ b/' prefix
                if current_file == "/dev/null":
                    # File is being deleted, use the old file path
                    current_file = old_file
                i += 1
                continue

            # Check for hunk header: @@ -old_start,old_count +new_start,new_count @@
            hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if hunk_match and current_file:
                new_start = int(hunk_match.group(1))
                new_count = int(hunk_match.group(2)) if hunk_match.group(2) else 1

                if new_count > 0:
                    changed_ranges[current_file].append(
                        (new_start, new_start + new_count - 1)
                    )

            i += 1

        logger.debug(f"Extracted changed line ranges for {len(changed_ranges)} files")
        return dict(changed_ranges)

    def extract_symbols_from_file(
        self, file_path: str, relative_path: Optional[str] = None
    ) -> Dict[str, any]:
        """
        Extract all symbols from a file with their chunks.

        Args:
            file_path: Absolute path to the source file
            relative_path: Relative path for node_id generation. If provided,
                          the returned dictionary keys will use this path prefix.

        Returns:
            Dictionary mapping node_id (file:symbol format) to CodeChunk objects
        """
        if not os.path.exists(file_path):
            logger.debug(f"File does not exist: {file_path}")
            return {}

        try:
            chunks = self.chunker.chunk_file(file_path, relative_path)
            symbols = {}

            for chunk in chunks:
                if chunk.chunk_type in ("function", "method", "class"):
                    # Use chunk.node_id directly (already in file:symbol format)
                    symbols[chunk.node_id] = chunk

            logger.debug(f"Extracted {len(symbols)} symbols from {file_path}")
            return symbols
        except Exception as e:
            logger.warning(f"Failed to extract symbols from {file_path}: {e}")
            return {}

    def clone_repo(self, repo_url: str, target_dir: str) -> bool:
        """
        Clone a git repository if it doesn't exist.

        Args:
            repo_url: URL of the repository
            target_dir: Directory to clone into

        Returns:
            True if successful, False otherwise
        """
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
        """
        Checkout a specific commit in a git repository.

        Args:
            repo_dir: Path to the repository
            commit_hash: Commit hash to checkout

        Returns:
            True if successful, False otherwise
        """
        # Check if it's a valid git repository
        git_dir = os.path.join(repo_dir, ".git")
        if not os.path.exists(git_dir):
            logger.error(f"Not a git repository: {repo_dir}")
            return False

        try:
            # Reset any local changes to ensure clean state
            logger.debug("Resetting repository to clean state")
            subprocess.run(
                ["git", "reset", "--hard"],
                cwd=repo_dir,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Clean untracked files
            subprocess.run(
                ["git", "clean", "-fd"],
                cwd=repo_dir,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Fetch all updates to ensure we can checkout the commit
            logger.info("Fetching updates from remote repository")
            subprocess.run(
                ["git", "fetch", "--all"],
                cwd=repo_dir,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Checkout to the base commit
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
        """
        Apply a patch to a repository.

        Args:
            repo_dir: Path to the repository
            patch_content: Content of the patch to apply

        Returns:
            True if successful, False otherwise
        """
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
        symbols_before: Dict[str, any],
        symbols_after: Dict[str, any],
        changed_ranges: Dict[str, List[Tuple[int, int]]],
    ) -> Tuple[List[str], List[str], List[str]]:
        """
        Compare symbols before and after patch to identify changes.

        Args:
            symbols_before: Dictionary mapping symbol names to CodeChunk objects before
                patch
            symbols_after: Dictionary mapping symbol names to CodeChunk objects after
                patch
            changed_ranges: Dictionary mapping file paths to list of changed line ranges

        Returns:
            Tuple of (symbols_modified, symbols_added, symbols_deleted)
        """
        logger.debug(
            (
                f"Comparing symbols: {len(symbols_before)} before, "
                f"{len(symbols_after)} after"
            )
        )

        before_set = set(symbols_before.keys())
        after_set = set(symbols_after.keys())

        # Symbols that were deleted
        symbols_deleted = sorted(list(before_set - after_set))
        logger.debug(f"Found {len(symbols_deleted)} deleted symbols")

        # Symbols that were added
        symbols_added = sorted(list(after_set - before_set))
        logger.debug(f"Found {len(symbols_added)} added symbols")

        # Symbols that exist in both - check if they were modified
        common = before_set & after_set
        symbols_modified = []

        for symbol_name in common:
            file_path, _, _ = symbol_name.partition(":")
            if file_path not in changed_ranges:
                continue

            chunk_before = symbols_before[symbol_name]
            chunk_after = symbols_after[symbol_name]

            # First check if length changed (cheapest check)
            length_before = chunk_before.end_line - chunk_before.start_line + 1
            length_after = chunk_after.end_line - chunk_after.start_line + 1

            if length_before != length_after:
                symbols_modified.append(symbol_name)
                logger.debug(
                    f"Symbol modified (length changed): {symbol_name} "
                    f"(before: {length_before} lines, after: {length_after} lines)"
                )
                continue

            # Length is the same, check if any changed lines overlap with this symbol's
            # range.
            has_overlap = any(
                change_end >= chunk_after.start_line
                and change_start <= chunk_after.end_line
                for change_start, change_end in changed_ranges[file_path]
            )

            if has_overlap:
                # Length is same but has changes in range, compare content directly
                if chunk_before.content != chunk_after.content:
                    symbols_modified.append(symbol_name)
                    logger.debug(
                        f"Symbol modified (content changed): {symbol_name} "
                        f"(lines {chunk_after.start_line}-{chunk_after.end_line})"
                    )
                else:
                    logger.debug(
                        f"Content identical for {symbol_name}, not marking as modified"
                    )

        symbols_modified = sorted(symbols_modified)
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

        # Setup repository - use shared repo directory (one per repository, not per
        # instance).
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

        # Filter for Python files only (for now)
        python_files = [f for f in target_files if f.endswith(".py")]
        logger.info(
            (
                f"Found {len(python_files)} Python files in {len(target_files)} "
                "target files"
            )
        )

        if not python_files:
            logger.info(f"No Python files affected in {instance_id}")
            return result

        # Extract symbols before patch
        logger.info("Extracting symbols BEFORE patch")
        symbols_before = {}
        for file_path in python_files:
            full_path = os.path.join(repo_dir, file_path)
            if os.path.exists(full_path):
                # Pass relative_path for proper node_id generation
                file_symbols = self.extract_symbols_from_file(full_path, file_path)
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
        for file_path in python_files:
            full_path = os.path.join(repo_dir, file_path)
            if os.path.exists(full_path):
                # Pass relative_path for proper node_id generation
                file_symbols = self.extract_symbols_from_file(full_path, file_path)
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


def build_gt_metadata(
    dataset,
    work_dir: Optional[str] = None,
    keep_repos: bool = True,
) -> List[Dict[str, Optional[str]]]:
    locator = GTLocator(work_dir=work_dir)
    results = []
    for i, instance in enumerate(dataset):
        logger.info(f"Processing instance {i+1}/{len(dataset)}")
        try:
            result = locator.analyze_instance(instance)
            results.append(result)
        except Exception as e:
            logger.error(
                f"Error processing {instance['instance_id']}: {e}", exc_info=True
            )
            results.append({"instance_id": instance["instance_id"], "error": str(e)})
    if not keep_repos:
        locator.cleanup()
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Analyze SWE-bench patches to extract symbol-level changes"
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default="verified",
        choices=["verified", "lite"],
        help="Dataset type to use: 'verified' or 'lite' (default: verified)",
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
        help="Output JSON file path (default: ~/.codeminer/swebench_{dataset}_gt.json)",
    )
    parser.add_argument(
        "--work-dir",
        type=str,
        default=None,
        help="Working directory for cloning repos (default: ~/.codeminer)",
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
        default=".*",
        help="Regex filter for instance IDs (default: .*)",
    )
    parser.add_argument(
        "--keep-repos",
        action="store_true",
        help="Keep cloned repositories after analysis (default: cleanup)",
    )

    args = parser.parse_args()

    # Set default output path if not specified
    if args.output is None:
        cache_dir = str(Path.home()) + "/.codeminer"
        os.makedirs(cache_dir, exist_ok=True)
        args.output = os.path.join(
            cache_dir, f"swebench_{args.dataset}_{args.split}_gt.json"
        )

    dataset_name = (
        "princeton-nlp/SWE-bench_Lite"
        if args.dataset == "lite"
        else "princeton-nlp/SWE-bench_Verified"
    )
    dataset_obj = SwebenchDataset(
        dataset=dataset_name,
        split=args.split,
        filter_instance=args.filter,
    )
    dataset = dataset_obj.load()

    logger.info(f"Processing {len(dataset)} instances")

    if args.limit is not None:
        dataset = dataset.select(range(min(args.limit, len(dataset))))

    results = build_gt_metadata(
        dataset=dataset,
        work_dir=args.work_dir,
        keep_repos=args.keep_repos,
    )

    # Save results
    output_path = args.output
    logger.info(f"Saving results to {output_path}")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    # Print summary
    success_count = sum(1 for r in results if not r.get("error"))
    error_count = len(results) - success_count

    logger.info(f"\n{'='*60}")
    logger.info(f"Analysis complete!")
    logger.info(f"Total instances: {len(results)}")
    logger.info(f"Successful: {success_count}")
    logger.info(f"Errors: {error_count}")
    logger.info(f"Results saved to: {output_path}")
    logger.info(f"Repositories cached in: {args.work_dir or '~/.codeminer/tmp'}")
    logger.info(f"{'='*60}")

    if args.keep_repos:
        logger.info(
            f"Keeping all repositories in: {args.work_dir or '~/.codeminer/tmp'}"
        )


if __name__ == "__main__":
    main()
