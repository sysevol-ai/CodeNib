#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CLI wrapper for answer-format diagnostic aggregation.

Usage::

    python scripts/agent_compile/aggregate_honest.py \
        results/agent_compile/qwen35_4b_base_v2 [more dirs...]
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional, Sequence

from codeminer.eval.agent_runner.format_diagnostics import (
    FormatDiagnostics,
    load_format_diagnostics,
)


def report(d: str) -> List[str]:
    return render_report(load_format_diagnostics(Path(d)))


def render_report(diagnostics: FormatDiagnostics) -> List[str]:
    L = [f"\n## {diagnostics.result_name}  (n_meaningful={diagnostics.n_meaningful})"]
    L.append(
        f"{'arm':14} {'n':>4} {'fmt_fail':>9} "
        f"{'files@5(all)':>13} {'files@5(fmt)':>13} "
        f"{'ansBlk@5(all)':>14} {'ansBlk@5(fmt)':>14}"
    )
    for summary in diagnostics.arms:
        L.append(
            f"{summary.arm:14} {summary.n:>4} "
            f"{summary.format_fail_rate * 100:>8.0f}% "
            f"{summary.files_all:>13.3f} {summary.files_formatted:>13.3f} "
            f"{summary.answer_blocks_all:>14.3f} "
            f"{summary.answer_blocks_formatted:>14.3f}"
        )
    return L


def main(argv: Optional[Sequence[str]] = None) -> int:
    dirs = list(argv if argv is not None else sys.argv[1:])
    if not dirs:
        print("usage: aggregate_honest.py <result-dir> [more...]", file=sys.stderr)
        return 2
    out = [
        "# Honest report - localization accuracy vs format-failure",
        "",
        "`(all)` = scored over every meaningful cell (format-fail counts as 0).",
        "`(fmt)` = scored only where the answer was parseable (true localization "
        "ability). `fmt_fail` = share of cells unscorable after force-schema "
        "retries - reported, not hidden.",
    ]
    for d in dirs:
        out += report(d)
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
