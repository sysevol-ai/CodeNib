#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Export fixed-slot DeepSWE Guardian outputs for the static dashboard."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from codenib.eval.benchmarks.deepswe import (
    export_dashboard_data as _export_dashboard_data,
)
from codenib.eval.benchmarks.deepswe import summary_rows, task_rows, trial_row

from .results import DEFAULT_OUTPUT_ROOT

DEFAULT_DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"

# Compatibility aliases for tests and scripts that imported the old helpers.
_summary_rows = summary_rows
_task_rows = task_rows
_trial_row = trial_row


def export_dashboard_data(args: argparse.Namespace) -> dict[str, Any]:
    """Compatibility adapter from the historical CLI namespace to the API."""

    return _export_dashboard_data(
        args.output_root,
        input_usd_per_mtok=args.input_usd_per_mtok,
        cached_input_usd_per_mtok=args.cached_input_usd_per_mtok,
        output_usd_per_mtok=args.output_usd_per_mtok,
        output_includes_reasoning=not args.output_excludes_reasoning,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--dashboard-dir", type=Path, default=DEFAULT_DASHBOARD_DIR)
    parser.add_argument("--input-usd-per-mtok", type=float)
    parser.add_argument("--cached-input-usd-per-mtok", type=float)
    parser.add_argument("--output-usd-per-mtok", type=float)
    parser.add_argument("--output-excludes-reasoning", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data = export_dashboard_data(args)
    args.dashboard_dir.mkdir(parents=True, exist_ok=True)
    path = args.dashboard_dir / "data.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[done] wrote {path}")
    print(f"[done] trials={len(data['trials'])} summary_rows={len(data['summary'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
