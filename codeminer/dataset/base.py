# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from ..log_utils import get_logger

logger = get_logger(__name__)


class DatasetBase:
    """Base dataset wrapper for loading and summarizing cached datasets."""

    # Controls whether collect_targets uses simplified symbol filtering (functions
    # only, identified by '(' in the symbol name). Override to False in datasets
    # whose GT includes non-function symbols (e.g. JS/TS object constants).
    simplified_symbols: bool = True

    def __init__(
        self,
        root: str | None = None,
        log: bool = True,
        force_reload: bool = False,
    ) -> None:
        self.root = root
        self.log = log
        self.force_reload = force_reload
        self.processed_dir = self._resolve_root(root)
        os.makedirs(self.processed_dir, exist_ok=True)

    def _resolve_root(self, root: str | None) -> str:
        if root:
            return os.path.abspath(os.path.expanduser(root))
        return os.path.join(Path.home(), ".codeminer")

    def process(self) -> Any:
        """Process the dataset and store artifacts in self.processed_dir."""
        raise NotImplementedError

    def get_summary(self) -> Dict[str, Any]:
        """Collect summary statistics for the dataset."""
        raise NotImplementedError

    def print_summary(self) -> None:
        """Print summary statistics of the dataset to the console."""
        summary = self.get_summary()
        if not self.log:
            return
        for key, value in summary.items():
            logger.info(f"{key}: {value}")

    def load_eval_metadata(self, path: str) -> Dict[str, Any]:
        resolved = Path(os.path.expanduser(path)).resolve()
        if not resolved.exists():
            raise FileNotFoundError(
                f"Evaluation annotations file not found at {resolved}."
            )
        with open(resolved, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict) and "instances" in payload:
            records = payload["instances"]
        elif isinstance(payload, list):
            records = payload
        else:
            records = [payload]
        metadata = {}
        for entry in records:
            instance_id = entry.get("instance_id")
            if instance_id:
                metadata[instance_id] = entry
        return metadata
