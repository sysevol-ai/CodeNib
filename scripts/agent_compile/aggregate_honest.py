#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Honest aggregation: separate localization accuracy from format-failure.

A cell can score 0 for two very different reasons:
  1. the agent localized to the WRONG place (a real miss), or
  2. the agent FOUND the right place but never emitted a parseable
     Files:/Symbols:/Locations: answer even after the force-schema retries
     (``format_failed=True``) — an LLM format-following weakness, worst on small
     models, NOT a localization error.

Burying (2) inside the accuracy denominator conflates the two and unfairly
punishes pre-load arms (which make weak models more verbose). So this reporter
prints, per (model-dir, arm):
  * format_fail_rate                         — honesty: how often we couldn't score
  * files@5 / answer_blocks@k on ALL cells   — the as-measured number
  * files@5 / answer_blocks@k on FORMATTED   — localization ability where scorable

Both are shown; neither is hidden. Usage::

    python scripts/agent_compile/aggregate_honest.py \
        results/agent_compile/qwen35_4b_base_v2 [more dirs...]
"""

from __future__ import annotations

import glob
import json
import statistics as st
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence


def _f(cell: Dict[str, Any], scope: str, k: int) -> Optional[float]:
    b = (cell.get("metrics") or {}).get(scope) or {}
    s = b.get(k, b.get(str(k)))
    return s.get("recall") if isinstance(s, dict) else None


def _mean(xs: Sequence[Optional[float]]) -> float:
    v = [x for x in xs if x is not None]
    return st.fmean(v) if v else 0.0


def report(d: str) -> List[str]:
    cells = []
    for p in glob.glob(f"{d}/cells/*.json"):
        try:
            c = json.loads(open(p).read())
        except (OSError, json.JSONDecodeError):
            continue
        if c.get("success") and c.get("metrics_meaningful"):
            cells.append(c)
    by = defaultdict(list)
    for c in cells:
        by[c.get("subset_id")].append(c)

    L = [f"\n## {d.rstrip('/').split('/')[-1]}  (n_meaningful={len(cells)})"]
    L.append(
        f"{'arm':14} {'n':>4} {'fmt_fail':>9} "
        f"{'files@5(all)':>13} {'files@5(fmt)':>13} "
        f"{'ansBlk@5(all)':>14} {'ansBlk@5(fmt)':>14}"
    )
    for arm in sorted(by):
        cs = by[arm]
        fmt_ok = [c for c in cs if not c.get("format_failed")]
        ff = sum(1 for c in cs if c.get("format_failed")) / len(cs) if cs else 0
        files_all = _mean([_f(c, "files", 5) for c in cs])
        files_fmt = _mean([_f(c, "files", 5) for c in fmt_ok])
        blk_all = _mean([_f(c, "answer_blocks", 5) for c in cs])
        blk_fmt = _mean([_f(c, "answer_blocks", 5) for c in fmt_ok])
        L.append(
            f"{arm:14} {len(cs):>4} {ff * 100:>8.0f}% "
            f"{files_all:>13.3f} {files_fmt:>13.3f} "
            f"{blk_all:>14.3f} {blk_fmt:>14.3f}"
        )
    return L


def main(argv: Optional[Sequence[str]] = None) -> int:
    dirs = list(argv if argv is not None else sys.argv[1:])
    if not dirs:
        print("usage: aggregate_honest.py <result-dir> [more...]", file=sys.stderr)
        return 2
    out = [
        "# Honest report — localization accuracy vs format-failure",
        "",
        "`(all)` = scored over every meaningful cell (format-fail counts as 0).",
        "`(fmt)` = scored only where the answer was parseable (true localization "
        "ability). `fmt_fail` = share of cells unscorable after force-schema "
        "retries — reported, not hidden.",
    ]
    for d in dirs:
        out += report(d)
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
