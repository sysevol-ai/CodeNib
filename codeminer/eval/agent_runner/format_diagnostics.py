# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Answer-format diagnostics for agent-runner evaluation cells."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence, Union

from .metrics import load_cell_jsons, metric_at_k, safe_mean, usable_cells


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


def load_meaningful_cells(result_dir: Union[Path, str]) -> List[Dict[str, Any]]:
    """Load successful cells whose metrics can be interpreted."""

    root = Path(result_dir)
    return usable_cells(
        load_cell_jsons(root.joinpath("cells")),
        require_metrics_meaningful=True,
    )


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
                files_all=safe_mean(
                    [
                        metric_at_k(cell, "files", k, field="recall")
                        for cell in arm_cells
                    ],
                    default=0.0,
                ),
                files_formatted=safe_mean(
                    [
                        metric_at_k(cell, "files", k, field="recall")
                        for cell in formatted
                    ],
                    default=0.0,
                ),
                answer_blocks_all=safe_mean(
                    [
                        metric_at_k(cell, "answer_blocks", k, field="recall")
                        for cell in arm_cells
                    ],
                    default=0.0,
                ),
                answer_blocks_formatted=safe_mean(
                    [
                        metric_at_k(cell, "answer_blocks", k, field="recall")
                        for cell in formatted
                    ],
                    default=0.0,
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
