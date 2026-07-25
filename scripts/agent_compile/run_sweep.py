#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent-compile cost-arm sweep on ``codenib_base`` with prebuilt indexes.

Sweeps ``{subsets} × {instances} × {reps}`` for one model, reusing the
offline-built per-instance indexes under ``--prebuilt-dir`` (see
``codenib.eval.agent_runner.prebuilt``) instead of cloning + reindexing.

Per instance the *full* (union-of-subsets) index set is loaded once and all
skills are registered; each subset cell then runs ``AgentRunner`` with
``allow_skills=<subset>`` so the only thing that varies across subsets is the
tool allowlist. The vector store is therefore loaded once per instance, not
once per cell.

Each cell is written as ``<output-dir>/cells/<cell_id>.json`` in the schema
``aggregate.py`` consumes, plus ``scenario`` / ``tool_calls`` fields for the
invocation histogram and per-scenario reporting.

Usage::

    python scripts/agent_compile/run_sweep.py \\
        --config scripts/agent_compile/configs/harness_grep.yaml \\
        --output-dir results/agent_compile/grep
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Agent-compile cost-arm sweep on codenib_base (prebuilt indexes).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--reps", type=int, default=None, help="Override config reps.")
    parser.add_argument(
        "--instances", nargs="+", default=None, help="Override config instance list."
    )
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--model", default=None, help="Override config model.")
    parser.add_argument(
        "--vertex-location", default=None, help="Override config vertex_location."
    )
    args = parser.parse_args(argv)

    from codenib.eval.agent_runner.sweep import run_sweep
    from codenib.eval.agent_runner.sweep_config import SweepConfig

    cfg = SweepConfig.from_yaml(args.config)
    if args.reps is not None:
        cfg.reps = args.reps
    if args.instances:
        cfg.instances = args.instances
    if args.model:
        cfg.model = args.model
    if args.vertex_location:
        cfg.vertex_location = args.vertex_location

    summary = run_sweep(cfg, args.output_dir, resume=not args.no_resume)
    print(
        "sweep done: completed={c} skipped={s} failed={f} cells={d}".format(
            c=len(summary["completed"]),
            s=len(summary["skipped"]),
            f=len(summary["failed"]),
            d=args.output_dir / "cells",
        )
    )
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
