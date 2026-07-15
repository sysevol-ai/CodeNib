#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run the generic agent sweep over a frozen ContextBench protocol."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    from codeminer.eval.agent_runner.query_sweep import run_query_sweep
    from codeminer.eval.agent_runner.sweep_config import SweepConfig
    from codeminer.eval.contextbench_study import protocol_rows

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-instances", type=int)
    parser.add_argument("--reps", type=int)
    parser.add_argument("--model")
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()

    rows = protocol_rows(json.loads(args.protocol.read_text(encoding="utf-8")))
    config = SweepConfig.from_yaml(args.config)
    if args.reps is not None:
        config.reps = args.reps
    if args.model:
        config.model = args.model
    run_query_sweep(
        config,
        args.output_dir,
        rows,
        max_instances=args.max_instances,
        resume=not args.no_resume,
        summary_filename="contextbench_summary.json",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
