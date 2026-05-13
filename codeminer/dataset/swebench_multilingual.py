# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Union

import datasets

from ..log_utils import get_logger
from .base import DatasetBase

logger = get_logger(__name__)


class SwebenchMultilingualDataset(DatasetBase):
    """Dataset wrapper for SWE-bench Multilingual."""

    def __init__(
        self,
        dataset: str = "SWE-bench/SWE-bench_Multilingual",
        split: str = "test",
        filter_instance: str = ".*",
        root: str | None = None,
        repo_root: str | None = None,
        log: bool = True,
        force_reload: bool = False,
    ) -> None:
        super().__init__(root=root, log=log, force_reload=force_reload)
        self.dataset = dataset
        self.split = split
        self.filter_instance = filter_instance
        self.repo_root = (
            self._resolve_root(repo_root) if repo_root else self.processed_dir
        )
        self._data = None

    @classmethod
    def from_args(
        cls,
        args: argparse.Namespace,
        *,
        root: str | None = None,
        repo_root: str | None = None,
    ) -> "SwebenchMultilingualDataset":
        cache_dir = getattr(args, "cache_dir", None)
        repo_cache_dir = getattr(args, "repo_cache_dir", None)
        return cls(
            dataset=args.dataset,
            split=args.split,
            filter_instance=args.filter_instance,
            root=cache_dir if root is None else root,
            repo_root=repo_cache_dir if repo_root is None else repo_root,
            force_reload=getattr(args, "force_reload", False),
            log=getattr(args, "log", True),
        )

    def load(
        self,
        idx_list: Optional[Sequence[int]] = None,
        idx_range: Optional[Sequence[int]] = None,
    ) -> datasets.arrow_dataset.Dataset:
        data = self._load_dataset()
        if idx_list is not None and idx_range is not None:
            raise ValueError("Cannot have both idx_list and idx_range")
        if idx_list is not None:
            if self.filter_instance != ".*":
                logger.info(
                    (
                        "Running idx_list on a filtered (non-full) dataset."
                        "Please make sure this is expected."
                    )
                )
            return data.select(list(idx_list))
        if idx_range is not None:
            if self.filter_instance != ".*":
                logger.info(
                    (
                        "Running idx_range on a filtered (non-full) dataset."
                        "Please make sure this is expected."
                    )
                )
            start_idx = idx_range[0]
            end_idx = idx_range[1]
            if start_idx >= end_idx:
                raise ValueError("start_idx should be smaller than end_idx")
            return data.select(range(start_idx, end_idx))
        return data

    def _load_dataset(self) -> datasets.arrow_dataset.Dataset:
        if self._data is not None and not self.force_reload:
            return self._data
        cache_dir = os.path.abspath(os.path.expanduser(self.processed_dir))
        if not os.path.exists(cache_dir):
            logger.info(f"Creating cache directory at {cache_dir}")
            os.makedirs(cache_dir, exist_ok=True)
        dataset_file = f'{self.dataset.replace("/", "__")}_{self.split}.json'
        dataset_path = f"{cache_dir}/{dataset_file}"
        if self.force_reload and os.path.exists(dataset_path):
            logger.info(f"Force reloading dataset cache at {dataset_path}")
            os.remove(dataset_path)
        if not os.path.exists(dataset_path):
            ds = datasets.load_dataset(self.dataset, split=self.split)
            logger.info(
                (
                    f"Loaded {len(ds)} instances from {self.dataset} dataset, "
                    f"split {self.split}"
                )
            )
            ds.to_json(dataset_path)
        else:
            logger.info(f"Dataset already exists at {dataset_path}")
            data_files = {self.split: dataset_path}
            ds = datasets.load_dataset("json", data_files=data_files, split=self.split)
            logger.info(
                f"Loaded {len(ds)} instances from cached dataset at {dataset_path}"
            )
        self._data = ds.filter(
            input_columns=["instance_id"],
            function=lambda x: bool(re.match(self.filter_instance, x)),
        )
        return self._data

    def get_repo_path(
        self, dataset_row: Dict[str, Any], repo_root: Union[Path, str, None] = None
    ) -> str:
        repo_name = dataset_row["repo"]
        root = self.repo_root
        if repo_root is not None:
            root = os.path.abspath(os.path.expanduser(str(repo_root)))
        repo_dir_name = repo_name.replace("/", "_")
        return os.path.join(root, repo_dir_name)

    def process_instance(
        self, dataset_row: Dict[str, Any], repo_root: Union[Path, str, None] = None
    ) -> None:
        """
        Process a dataset instance by:
        1. Downloading the repository if not exists
        2. Checking out the specific commit

        Args:
            dataset_row: A row from the SWE-bench dataset containing repo,
                instance_id, and base_commit
            repo_root: Directory to store repositories (defaults to self.repo_root)
        """
        repo_name = dataset_row["repo"]
        base_commit = dataset_row["base_commit"]
        repo_path = self.get_repo_path(dataset_row, repo_root=repo_root)
        os.makedirs(os.path.dirname(repo_path), exist_ok=True)

        if not os.path.exists(repo_path):
            logger.info(f"Downloading repository {repo_name} to {repo_path}")
            git_url = f"https://github.com/{repo_name}.git"
            try:
                subprocess.run(
                    ["git", "clone", git_url, repo_path],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to clone repository: {e}")
                logger.error(f"STDERR: {e.stderr.decode('utf-8')}")
                raise RuntimeError(f"Failed to clone repository {repo_name}") from e
        else:
            logger.info(f"Repository {repo_name} already exists at {repo_path}")

        original_dir = os.getcwd()
        os.chdir(repo_path)

        try:
            logger.info("Fetching updates from remote repository")
            subprocess.run(
                ["git", "fetch", "--all"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            logger.info(f"Checking out commit {base_commit}")
            try:
                subprocess.run(
                    ["git", "reset", "--hard"],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                subprocess.run(
                    ["git", "clean", "-fd"],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                subprocess.run(
                    ["git", "checkout", "-f", base_commit],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except subprocess.CalledProcessError as e:
                stderr = e.stderr.decode("utf-8")
                logger.warning(
                    "Initial checkout failed for %s at %s: %s",
                    repo_name,
                    base_commit,
                    stderr.strip(),
                )

                # Reused repo caches may be shallow clones from sampling.
                try:
                    shallow = subprocess.run(
                        ["git", "rev-parse", "--is-shallow-repository"],
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    ).stdout.strip()
                    if shallow == "true":
                        logger.info(
                            "Repository is shallow; running git fetch --unshallow"
                        )
                        subprocess.run(
                            ["git", "fetch", "--unshallow"],
                            check=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                except subprocess.CalledProcessError:
                    logger.warning("Failed to determine/expand shallow clone state")

                # Try targeted fetch for detached commit IDs.
                subprocess.run(
                    ["git", "fetch", "origin", base_commit],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                subprocess.run(
                    ["git", "fetch", "--all", "--tags"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                try:
                    subprocess.run(
                        ["git", "checkout", "-f", base_commit],
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                except subprocess.CalledProcessError as e2:
                    logger.error(f"Failed to checkout commit {base_commit}: {e2}")
                    logger.error(f"STDERR: {e2.stderr.decode('utf-8')}")
                    raise RuntimeError(
                        f"Failed to checkout commit {base_commit} for repo {repo_name}"
                    ) from e2

            logger.info(f"Successfully checked out {repo_name} at commit {base_commit}")
        finally:
            os.chdir(original_dir)

    def process(self) -> Any:
        self._data = self._load_dataset()
        return self._data

    def get_summary(self) -> Dict[str, Any]:
        data = self._data or self._load_dataset()
        summary = {
            "dataset": self.dataset,
            "split": self.split,
            "instances": len(data),
        }
        if "repo" in data.column_names:
            summary["repos"] = len(set(data["repo"]))
        return summary
