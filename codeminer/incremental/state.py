# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Persistent incremental indexing state.

Tracks the last-indexed commit SHA alongside paths to the chunk store,
embeddings cache, and FAISS index so that callers don't need to manage
this bookkeeping externally.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from ..log_utils import get_logger

logger = get_logger(__name__)

_STATE_FILENAME = "incremental_state.json"


@dataclass
class IncrementalState:
    """Serialisable snapshot of incremental indexing progress."""

    last_commit: str = ""
    chunk_store_path: str = "chunk_store.pkl"
    embeddings_cache_path: str = "embeddings_cache.pkl"
    index_path: str = ""
    build_levels: list = field(default_factory=lambda: ["l0", "l2"])

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, output_dir: Path) -> Path:
        """Write state as JSON to *output_dir*/:data:`_STATE_FILENAME`.

        Uses atomic write (write to temp file then rename) to avoid
        corruption if the process is interrupted mid-write.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        state_path = output_dir / _STATE_FILENAME
        fd, tmp_path = tempfile.mkstemp(
            dir=str(output_dir), suffix=".tmp", prefix=".state_"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(self), f, indent=2)
            os.replace(tmp_path, state_path)
        except BaseException:
            # Clean up the temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError as cleanup_err:
                logger.debug(
                    "Failed to remove temporary state file %s during error handling: %s",
                    tmp_path,
                    cleanup_err,
                )
            raise
        logger.debug("IncrementalState saved → %s", state_path)
        return state_path

    @classmethod
    def load(cls, output_dir: Path) -> Optional["IncrementalState"]:
        """Load from *output_dir* if the state file exists, else ``None``."""
        state_path = Path(output_dir) / _STATE_FILENAME
        if not state_path.exists():
            return None
        with open(state_path, "r") as f:
            data = json.load(f)
        state = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        logger.debug(
            "IncrementalState loaded ← %s (commit %s)",
            state_path,
            state.last_commit[:8] if state.last_commit else "none",
        )
        return state

    @classmethod
    def load_or_default(cls, output_dir: Path) -> "IncrementalState":
        """Load from *output_dir* or return a fresh default instance."""
        return cls.load(output_dir) or cls()
