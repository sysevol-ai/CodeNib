#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CLI wrapper for runtime-probe Pareto diagnostics."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional, Sequence

from codeminer.eval.agent_runner.pareto import analyze_pareto, load_pareto_cells

analyze = analyze_pareto


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cells-dir", required=True, type=Path)
    parser.add_argument("--baseline", default="grep_only")
    parser.add_argument("--boot", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    report = analyze_pareto(
        load_pareto_cells(args.cells_dir),
        baseline=args.baseline,
        boot=args.boot,
        seed=args.seed,
    )
    print(report)
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "pareto_ci.md").write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
