"""DeepSWE experiment runners and reporting tools."""

from __future__ import annotations

import os
from pathlib import Path


def _environment_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


DEFAULT_CODENIB_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DEEPSWE_ROOT = _environment_path(
    "CODENIB_DEEPSWE_ROOT", DEFAULT_CODENIB_ROOT.parent / "deep-swe"
)
DEFAULT_OUTPUT_ROOT = _environment_path(
    "CODENIB_DEEPSWE_OUTPUT_ROOT",
    DEFAULT_CODENIB_ROOT / "data" / "deepswe_outputs",
)
DEFAULT_JOBS_DIR = DEFAULT_DEEPSWE_ROOT / "jobs"

__all__ = [
    "DEFAULT_CODENIB_ROOT",
    "DEFAULT_DEEPSWE_ROOT",
    "DEFAULT_JOBS_DIR",
    "DEFAULT_OUTPUT_ROOT",
]
