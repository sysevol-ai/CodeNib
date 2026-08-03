#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CLI wrapper for agent-compile cost-arm aggregation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from codenib.eval.reports.cost_arm_report import load_cells, write_report


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Aggregate an agent-compile cost-arm sweep into a report.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--cells-dir", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--metrics-k", type=int, nargs="+", default=[1, 3, 5, 10])
    p.add_argument(
        "--max-turns",
        type=int,
        default=16,
        help="Turn ceiling the sweep ran with (for cap-hit-rate). Match the config.",
    )
    p.add_argument(
        "--baseline",
        default=None,
        help="Arm to use as the easy/hard split reference (default: GREP, else first).",
    )
    args = p.parse_args(argv)

    cells = load_cells(args.cells_dir)
    if not cells:
        print(f"aggregate: no cells under {args.cells_dir}", file=sys.stderr)
        return 2

    md = write_report(
        cells=cells,
        output_dir=args.output_dir,
        metrics_k=args.metrics_k,
        max_turns=args.max_turns,
        baseline=args.baseline,
    )
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
