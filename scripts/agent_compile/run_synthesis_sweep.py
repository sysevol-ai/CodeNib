#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Per-query sweep over the ``sysevol-ai/codeminer-synthesis`` dataset.

Unlike ``run_sweep.py`` (one query per SWE-bench instance), synthesis has many
queries (50-80) per repo, varying by ``category`` (behavioral / symbol_hint /
traversal / ...). That is exactly the setting where retrieval should pay off:

* category DISCRIMINATES grep vs retrieval — ``behavioral`` queries name no
  identifiers (grep must explore the whole repo), ``symbol_hint`` names the
  symbol (grep wins), ``traversal`` needs call-graph navigation.
* index REUSE — the repo index is built ONCE and amortized across all its
  queries (the shared query-sweep runner loads contexts once per instance, then
  runs every query), whereas grep re-searches per query.

Scoring is the same span-overlap harness as ``run_sweep.py`` (answer + retrieval
scopes); each cell additionally records ``category`` and ``query_id`` so the
aggregator can break ``answer_rec@k`` down per category.

Usage::

    python scripts/agent_compile/run_synthesis_sweep.py \
        --config scripts/agent_compile/configs/preload_probe.yaml \
        --output-dir results/agent_compile/synthesis \
        --synthesis-configs Python,Go,Rust,TypeScript_JavaScript
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _load_normalized(config_name: str) -> List[Dict[str, Any]]:
    """Load + normalize one synthesis config into common CodeNib rows."""
    from datasets import load_dataset

    from codenib.dataset.codenib_synthesis import normalize_synthesis_record

    ds = load_dataset("sysevol-ai/codeminer-synthesis", config_name, split="test")
    return [normalize_synthesis_record(r, config_name) for r in ds]


def main(argv: Optional[Sequence[str]] = None) -> int:
    from codenib.dataset.codenib_synthesis import ALL_CONFIGS

    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument(
        "--synthesis-configs",
        default=",".join(ALL_CONFIGS),
        help="comma list of HF configs (Python,Go,Rust,TypeScript_JavaScript,C++_C)",
    )
    p.add_argument(
        "--categories",
        default=None,
        help="comma list to filter (e.g. behavioral,symbol_hint)",
    )
    p.add_argument(
        "--max-queries", type=int, default=None, help="cap queries per instance (smoke)"
    )
    p.add_argument(
        "--max-instances",
        type=int,
        default=None,
        help="cap instances after config allowlist/category filtering (smoke)",
    )
    p.add_argument("--reps", type=int, default=None)
    p.add_argument("--no-resume", action="store_true")
    p.add_argument(
        "--model",
        default=None,
        help="Override config model (e.g. a local openai/ vLLM model).",
    )
    args = p.parse_args(argv)

    from codenib.eval.agent_runner.query_sweep import run_query_sweep
    from codenib.eval.agent_runner.sweep_config import SweepConfig

    cfg = SweepConfig.from_yaml(args.config)
    if args.reps is not None:
        cfg.reps = args.reps
    if args.model:
        cfg.model = args.model
    configs = [c.strip() for c in args.synthesis_configs.split(",") if c.strip()]
    categories = (
        {c.strip() for c in args.categories.split(",")} if args.categories else None
    )
    rows: List[Dict[str, Any]] = []
    for config_name in configs:
        rows.extend(_load_normalized(config_name))
    run_query_sweep(
        cfg,
        args.output_dir,
        rows,
        categories=categories,
        max_queries=args.max_queries,
        max_instances=args.max_instances,
        resume=not args.no_resume,
        summary_filename="synthesis_summary.json",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
