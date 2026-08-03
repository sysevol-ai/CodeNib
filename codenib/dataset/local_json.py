# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..log_utils import get_logger
from .base import DatasetBase

logger = get_logger(__name__)


class LocalJsonDataset(DatasetBase):
    """Scaffold dataset loader for a local JSON file.

    Expects a JSON array where each element has at least:
        repo, instance_id, base_commit, query, query_id,
        gt_symbols, gt_files
    See ``examples/`` for the full schema.
    """

    def __init__(
        self,
        path: str,
        filter_instance: str = ".*",
        root: str | None = None,
        repo_root: str | None = None,
        log: bool = True,
    ) -> None:
        super().__init__(root=root, log=log)
        self.path = os.path.abspath(os.path.expanduser(path))
        self.filter_instance = filter_instance
        self.repo_root = (
            self._resolve_root(repo_root) if repo_root else self.processed_dir
        )
        self._data: Optional[List[Dict[str, Any]]] = None

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load(self) -> List[Dict[str, Any]]:
        if self._data is not None:
            return self._data
        with open(self.path, "r", encoding="utf-8") as fh:
            raw: List[Dict[str, Any]] = json.load(fh)

        pattern = re.compile(self.filter_instance)
        rows: List[Dict[str, Any]] = []
        for entry in raw:
            match_key = entry.get("query_id") or entry["instance_id"]
            if not pattern.match(match_key):
                continue
            # Map ``query`` → ``problem_statement`` for pipeline compat.
            row = dict(entry)
            row.setdefault("problem_statement", row.get("query", ""))
            rows.append(row)

        logger.info(
            "Loaded %d entries from %s (filter: %s)",
            len(rows),
            self.path,
            self.filter_instance,
        )
        self._data = rows
        return self._data

    # ------------------------------------------------------------------
    # Eval metadata — built from the dataset itself (gt is inline).
    # ------------------------------------------------------------------

    def load_eval_metadata(self, path: str = "") -> Dict[str, Any]:
        """Build eval metadata keyed by ``query_id``.

        The *path* argument is accepted for interface compatibility but
        ignored — ground-truth annotations are embedded in the dataset.
        """
        data = self._data or self.load()
        metadata: Dict[str, Any] = {}
        for entry in data:
            key = entry.get("query_id") or entry["instance_id"]
            metadata[key] = {
                "instance_id": entry["instance_id"],
                "target_files": entry.get("gt_files", []),
                # Store gt_symbols as symbols_modified so collect_targets
                # can pick them up with simplified_symbols=False.
                "symbols_modified": entry.get("gt_symbols", []),
                "symbols_added": [],
                "symbols_deleted": [],
            }
        return metadata

    # ------------------------------------------------------------------
    # Repo helpers (same as SwebenchDataset)
    # ------------------------------------------------------------------

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
        repo_name = dataset_row["repo"]
        base_commit = dataset_row["base_commit"]
        repo_path = self.get_repo_path(dataset_row, repo_root=repo_root)
        os.makedirs(os.path.dirname(repo_path), exist_ok=True)

        if not os.path.exists(repo_path):
            logger.info("Cloning repository %s to %s", repo_name, repo_path)
            git_url = f"https://github.com/{repo_name}.git"
            subprocess.run(
                ["git", "clone", git_url, repo_path],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        else:
            logger.info("Repository %s already exists at %s", repo_name, repo_path)

        original_dir = os.getcwd()
        os.chdir(repo_path)
        try:
            subprocess.run(
                ["git", "fetch", "--all"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
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
            logger.info("Checked out %s at commit %s", repo_name, base_commit)
        except subprocess.CalledProcessError as exc:
            logger.error("Failed to checkout %s at %s: %s", repo_name, base_commit, exc)
            raise
        finally:
            os.chdir(original_dir)

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def process(self) -> Any:
        return self.load()

    def get_summary(self) -> Dict[str, Any]:
        data = self._data or self.load()
        repos = {e["repo"] for e in data}
        instances = {e["instance_id"] for e in data}
        return {
            "path": self.path,
            "entries": len(data),
            "unique_instances": len(instances),
            "repos": len(repos),
        }
