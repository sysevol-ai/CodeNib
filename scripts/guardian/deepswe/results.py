#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Generate an aggregate table from DeepSWE Guardian ablation outputs.

The reusable artifact and reporting logic lives in
``codenib.eval.benchmarks.deepswe``.  This module retains the historical CLI
and private aliases used by existing experiment scripts.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from codenib.eval.benchmarks.deepswe import COUNTED_JOB_IDS as _COUNTED_JOB_IDS
from codenib.eval.benchmarks.deepswe import (
    DEFAULT_PRICES_USD_PER_MTOK as _DEFAULT_PRICES_USD_PER_MTOK,
)
from codenib.eval.benchmarks.deepswe import (
    Pricing,
    add_costs,
    load_rows,
    metric_mean,
    metric_sum,
    pricing_for_model,
    write_summary,
)

DEFAULT_OUTPUT_ROOT = Path("/mnt/data/xiangye/deepswe_outputs")
DEFAULT_SUMMARY_CSV = DEFAULT_OUTPUT_ROOT / "guardian_ablation_summary.csv"

# Compatibility aliases for existing experiment imports.
COUNTED_JOB_IDS = _COUNTED_JOB_IDS
DEFAULT_PRICES_USD_PER_MTOK = _DEFAULT_PRICES_USD_PER_MTOK
_load_rows = load_rows
_metric_mean = metric_mean
_metric_sum = metric_sum


def _pricing_for_model(model: str, args: argparse.Namespace) -> Pricing:
    return pricing_for_model(
        model,
        input_usd_per_mtok=args.input_usd_per_mtok,
        cached_input_usd_per_mtok=args.cached_input_usd_per_mtok,
        output_usd_per_mtok=args.output_usd_per_mtok,
        output_includes_reasoning=not args.output_excludes_reasoning,
    )


def _with_costs(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    return add_costs(
        row,
        input_usd_per_mtok=args.input_usd_per_mtok,
        cached_input_usd_per_mtok=args.cached_input_usd_per_mtok,
        output_usd_per_mtok=args.output_usd_per_mtok,
        output_includes_reasoning=not args.output_excludes_reasoning,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--summary-csv", type=Path, default=DEFAULT_SUMMARY_CSV)
    parser.add_argument("--input-usd-per-mtok", type=float)
    parser.add_argument("--cached-input-usd-per-mtok", type=float)
    parser.add_argument("--output-usd-per-mtok", type=float)
    parser.add_argument(
        "--output-excludes-reasoning",
        action="store_true",
        help="Add reasoning_output_tokens to output_tokens for main Codex cost.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    rows = [_with_costs(row, args) for row in load_rows(args.output_root)]
    write_summary(rows, args.summary_csv)
    print(f"[done] scanned {len(rows)} trial(s)")
    print(f"[done] summary: {args.summary_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
