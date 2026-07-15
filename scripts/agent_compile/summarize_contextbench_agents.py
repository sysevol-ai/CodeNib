#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Summarize the frozen ContextBench agent matrix and quality guardrail."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _cell_paths(root: Path):
    yield from sorted((root / "cells").glob("*.json"))
    yield from sorted(root.glob("*/cells/*.json"))


def main() -> int:
    from codeminer.eval.contextbench_study import summarize_agent_context_policies

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--results-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--bootstrap-samples", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20_260_713)
    parser.add_argument("--quality-margin", type=float, default=-0.05)
    args = parser.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    paths = list(
        dict.fromkeys(path.resolve() for path in _cell_paths(args.results_root))
    )
    records = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    summary = summarize_agent_context_policies(
        protocol,
        records,
        bootstrap_samples=args.bootstrap_samples,
        seed=args.seed,
        quality_margin=args.quality_margin,
    )
    summary["input_cell_files"] = len(paths)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary["completion"], sort_keys=True))
    return 0 if summary["completion"]["complete_matrix"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
