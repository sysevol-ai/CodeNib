# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reusable metric helpers for agent-runner evaluation cells."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

Cell = Dict[str, Any]


def load_cell_jsons(cells_dir: Union[Path, str]) -> List[Cell]:
    """Load JSON cell dictionaries from a directory, skipping unreadable files."""

    cells: List[Cell] = []
    for path in sorted(Path(cells_dir).glob("*.json")):
        try:
            cell = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(cell, dict):
            cells.append(cell)
    return cells


def usable_cells(
    cells: Sequence[Cell],
    *,
    require_success: bool = True,
    require_metrics_meaningful: bool = False,
) -> List[Cell]:
    """Filter cells to the subset usable for a diagnostic calculation."""

    out: List[Cell] = []
    for cell in cells:
        if require_success and not cell.get("success"):
            continue
        if require_metrics_meaningful and not cell.get("metrics_meaningful"):
            continue
        out.append(cell)
    return out


def metric_at_k(
    cell: Cell,
    scope: str,
    k: int,
    *,
    field: str = "accuracy",
) -> Optional[float]:
    """Read a numeric metric field for one scope and cutoff."""

    bucket = (cell.get("metrics") or {}).get(scope) or {}
    stats = bucket.get(k, bucket.get(str(k)))
    if isinstance(stats, dict):
        value = stats.get(field)
    elif field == "accuracy":
        value = stats
    else:
        value = None
    return float(value) if isinstance(value, (int, float)) else None


def safe_mean(values: Sequence[Optional[float]], *, default: Optional[float] = None):
    """Mean over present numeric values, returning default for an empty input."""

    present = [value for value in values if isinstance(value, (int, float))]
    return statistics.fmean(present) if present else default


def safe_min(values: Sequence[Optional[float]], *, default: Optional[float] = None):
    """Minimum over present numeric values, returning default for an empty input."""

    present = [value for value in values if isinstance(value, (int, float))]
    return min(present) if present else default
