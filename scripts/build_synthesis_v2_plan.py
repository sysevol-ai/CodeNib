#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build the codeminer-synthesis v2 (multi-repo) generation plan.

v1 used ONE repo per language (5 repos total) → "Go graph wins" could be "this
one Go repo's graph is clean", not a language property. v2 spans ALL 5 repos per
language available in codeminer-base (25 repos, prebuilt indexes already on
disk), keeping per-repo DEPTH (a query floor) while adding multi-repo BREADTH.

This planner is deterministic + read-only (reads the base dataset + each
instance's prebuilt graph). It:
  1. groups codeminer-base by (language, repo),
  2. picks ONE instance per repo — the one whose prebuilt graph has the most
     symbol nodes (richest graph → most reliable GT spans + graph expansion),
  3. attaches a fixed per-category query budget (the v1 60-split scaled to ~20),
and emits a plan JSON consumed by the generation driver
(generate_codeminer_synthesis_config.py per instance, then
rebuild_codeminer_synthesis_dataset.py to assemble).

Importance sampling note: at Tier-1 every language has exactly 5 base repos and
we take all of them, so repo SELECTION is moot and per-repo counts are EQUAL
(unbiased per-repo comparison). Importance weighting re-enters at Tier-2 (when a
larger pool, e.g. SWE-bench Multilingual's 41 repos, must be down-selected).
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# base language_group -> synthesis HF config name
_LANG_TO_CONFIG = {
    "Python": "Python",
    "Go": "Go",
    "Rust": "Rust",
    "C++/C": "C++_C",
    "TypeScript/JavaScript": "TypeScript_JavaScript",
}

# v1 per-instance split (=60) scaled by 1/3 to ~20/repo; keeps all 6 categories.
_COUNTS = {
    "behavioral": 7,
    "file_hint": 3,
    "module_hint": 3,
    "traversal": 3,
    "reasoning": 2,
    "symbol_hint": 2,
}


def _graph_symbol_count(prebuilt_dir: str, instance_id: str) -> int:
    """#symbol-bearing vertices in the prebuilt graph (0 if missing)."""
    path = os.path.join(prebuilt_dir, instance_id, "graph.pkl")
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "rb") as f:
            g = pickle.load(f)
        G = g.get("graph")
        return G.vcount() if G is not None else 0
    except Exception:  # noqa: BLE001
        return 0


def build_plan(prebuilt_dir: str, repos_per_lang: int) -> List[Dict[str, Any]]:
    from codeminer.eval.agent_runner.sweep import load_dataset_rows
    from codeminer.eval.agent_runner.sweep_config import SweepConfig

    cfg = SweepConfig.from_yaml(
        str(_PROJECT_ROOT / "scripts/agent_compile/configs/design_space.yaml")
    )
    rows, _ = load_dataset_rows(cfg)

    # (lang, repo) -> [(instance_id, graph_symbol_count)]
    by_repo: Dict[tuple, List] = defaultdict(list)
    for iid, r in rows.items():
        lang = r.get("language_group")
        repo = r.get("repo")
        if lang not in _LANG_TO_CONFIG or not repo:
            continue
        by_repo[(lang, repo)].append((iid, _graph_symbol_count(prebuilt_dir, iid)))

    # group repos per language, pick richest instance per repo
    by_lang: Dict[str, List] = defaultdict(list)
    for (lang, repo), insts in by_repo.items():
        best_iid, best_n = max(insts, key=lambda t: t[1])
        by_lang[lang].append((repo, best_iid, best_n))

    plan: List[Dict[str, Any]] = []
    for lang in sorted(by_lang):
        # richest repos first; take up to repos_per_lang
        repos = sorted(by_lang[lang], key=lambda t: t[2], reverse=True)[:repos_per_lang]
        for repo, iid, n in repos:
            plan.append(
                {
                    "config": _LANG_TO_CONFIG[lang],
                    "language_group": lang,
                    "repo": repo,
                    "instance_id": iid,
                    "graph_symbols": n,
                    "counts": dict(_COUNTS),
                    "total_queries": sum(_COUNTS.values()),
                }
            )
    return plan


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--prebuilt-dir", default="/mnt/data/codeminer")
    p.add_argument("--repos-per-lang", type=int, default=5)
    p.add_argument("--out", type=Path, default=Path("synthesis_v2_plan.json"))
    args = p.parse_args(argv)

    plan = build_plan(args.prebuilt_dir, args.repos_per_lang)
    args.out.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    total_q = sum(e["total_queries"] for e in plan)
    print(f"plan: {len(plan)} repos, {total_q} queries -> {args.out}")
    print(f"{'config':22}{'repo':28}{'instance_id':28}{'graph':>7}{'q':>4}")
    for e in plan:
        print(
            f"{e['config']:22}{e['repo']:28}{e['instance_id']:28}"
            f"{e['graph_symbols']:>7}{e['total_queries']:>4}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
