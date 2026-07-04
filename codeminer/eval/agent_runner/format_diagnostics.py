# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Answer-format diagnostics for agent-runner evaluation cells."""

from __future__ import annotations

import json
import statistics as st
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union


@dataclass(frozen=True)
class FormatArmSummary:
    """Summary for one arm after separating format failures."""

    arm: str
    n: int
    format_fail_rate: float
    files_all: float
    files_formatted: float
    answer_blocks_all: float
    answer_blocks_formatted: float


@dataclass(frozen=True)
class FormatDiagnostics:
    """Format-diagnostics summary for one result directory."""

    result_name: str
    n_meaningful: int
    arms: List[FormatArmSummary]


def _recall(cell: Dict[str, Any], scope: str, k: int) -> Optional[float]:
    blocks = (cell.get("metrics") or {}).get(scope) or {}
    score = blocks.get(k, blocks.get(str(k)))
    return score.get("recall") if isinstance(score, dict) else None


def _mean(values: Sequence[Optional[float]]) -> float:
    present = [value for value in values if value is not None]
    return st.fmean(present) if present else 0.0


def load_meaningful_cells(result_dir: Union[Path, str]) -> List[Dict[str, Any]]:
    """Load successful cells whose metrics can be interpreted."""

    root = Path(result_dir)
    cells: List[Dict[str, Any]] = []
    for path in sorted(root.joinpath("cells").glob("*.json")):
        try:
            cell = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if cell.get("success") and cell.get("metrics_meaningful"):
            cells.append(cell)
    return cells


def summarize_format_cells(
    cells: Sequence[Dict[str, Any]], *, k: int = 5
) -> List[FormatArmSummary]:
    """Summarize cells by arm while separating unparseable final answers."""

    by_arm: Dict[str, List[Dict[str, Any]]] = {}
    for cell in cells:
        by_arm.setdefault(str(cell.get("subset_id")), []).append(cell)

    summaries: List[FormatArmSummary] = []
    for arm in sorted(by_arm):
        arm_cells = by_arm[arm]
        formatted = [cell for cell in arm_cells if not cell.get("format_failed")]
        format_fail_rate = (
            sum(1 for cell in arm_cells if cell.get("format_failed")) / len(arm_cells)
            if arm_cells
            else 0.0
        )
        summaries.append(
            FormatArmSummary(
                arm=arm,
                n=len(arm_cells),
                format_fail_rate=format_fail_rate,
                files_all=_mean([_recall(cell, "files", k) for cell in arm_cells]),
                files_formatted=_mean(
                    [_recall(cell, "files", k) for cell in formatted]
                ),
                answer_blocks_all=_mean(
                    [_recall(cell, "answer_blocks", k) for cell in arm_cells]
                ),
                answer_blocks_formatted=_mean(
                    [_recall(cell, "answer_blocks", k) for cell in formatted]
                ),
            )
        )
    return summaries


def load_format_diagnostics(
    result_dir: Union[Path, str], *, k: int = 5
) -> FormatDiagnostics:
    """Load and summarize answer-format diagnostics for a result directory."""

    root = Path(result_dir)
    cells = load_meaningful_cells(root)
    return FormatDiagnostics(
        result_name=root.name,
        n_meaningful=len(cells),
        arms=summarize_format_cells(cells, k=k),
    )
