# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime-probe Pareto diagnostics for agent-runner evaluation.

``aggregate_synthesis.py`` gives point estimates. But pre-load is a variance
trade: it rescues queries grep fails on and distracts on others. At small n,
a point delta is noise. This module pairs each arm against ``grep_only`` per query
and reports a paired bootstrap 95% CI on recall@5, cost delta, turns delta, plus
win/tie/loss counts, and a significance-aware verdict:

  SAVE         - accuracy is not significantly worse and cost is significantly
                 lower.
  REGRESS      - accuracy is significantly worse.
  inconclusive - CI spans the threshold; need more n.

Rep folding mirrors aggregate_synthesis (rec mean across reps, cost/turns min).
"""

from __future__ import annotations

import json
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

EPS = 0.02  # Recall deltas within this count as equal accuracy.


def load_pareto_cells(cells_dir: Path) -> List[Dict[str, Any]]:
    out = []
    for p in sorted(cells_dir.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return out


def _rec5(cell: Dict[str, Any]) -> Optional[float]:
    b = (cell.get("metrics") or {}).get("answer_blocks") or {}
    s = b.get(5, b.get("5"))
    return s.get("recall") if isinstance(s, dict) else None


def _fold_reps(cells: Sequence[Dict[str, Any]]) -> Dict[Tuple, Dict[str, float]]:
    """(category, arm, query_id) -> {rec: mean, cost: min, turns: min}."""
    by: Dict[Tuple, List[Dict[str, Any]]] = defaultdict(list)
    for c in cells:
        if not c.get("success"):
            continue
        by[(c.get("category"), c.get("subset_id"), c.get("query_id"))].append(c)
    folded: Dict[Tuple, Dict[str, float]] = {}
    for key, reps in by.items():
        recs = [r for r in (_rec5(c) for c in reps) if r is not None]
        costs = [c.get("cost_usd") for c in reps if c.get("cost_usd") is not None]
        turns = [c.get("total_turns") for c in reps if c.get("total_turns") is not None]
        folded[key] = {
            "rec": statistics.fmean(recs) if recs else None,
            "cost": min(costs) if costs else None,
            "turns": min(turns) if turns else None,
        }
    return folded


def _ci(
    samples: List[float], rng: random.Random, boot: int
) -> Tuple[float, float, float]:
    """Mean + 95% paired-bootstrap CI (resample the per-query deltas)."""
    n = len(samples)
    mean = statistics.fmean(samples)
    means = []
    for _ in range(boot):
        means.append(statistics.fmean(rng.choices(samples, k=n)))
    means.sort()
    lo = means[int(0.025 * boot)]
    hi = means[int(0.975 * boot)]
    return mean, lo, hi


def analyze_pareto(
    cells: Sequence[Dict[str, Any]], baseline: str, boot: int, seed: int
) -> str:
    folded = _fold_reps(cells)
    cats = sorted({c for (c, _a, _q) in folded})
    arms = sorted({a for (_c, a, _q) in folded if a != baseline})

    L = ["# Runtime probe - paired-bootstrap Pareto decision", ""]
    L.append(
        f"Baseline = `{baseline}`. Each arm paired per query (rep-folded: rec mean, "
        f"cost/turns min). 95% CI from {boot} paired bootstrap resamples. "
        f"SAVE = recall delta CI_lo >= -{EPS} AND cost delta % CI_hi < 0."
    )
    L.append("")
    head = [
        "category",
        "arm",
        "n",
        "delta recall@5 [95% CI]",
        "delta cost% [95% CI]",
        "delta turns",
        "win/tie/loss",
        "verdict",
    ]
    L.append("| " + " | ".join(head) + " |")
    L.append("| " + " | ".join("---" for _ in head) + " |")

    # include a pooled "ALL" pseudo-category
    for cat in cats + ["ALL"]:
        for arm in arms:
            drec, dcost, dturn = [], [], []
            w = t = ls = 0
            pair_keys = {
                (c, q)
                for (c, a, q) in folded
                if a == baseline and (c == cat or cat == "ALL")
            }
            for ckey, q in pair_keys:
                b = folded.get((ckey, baseline, q))
                a_ = folded.get((ckey, arm, q))
                if not b or not a_:
                    continue
                if b["rec"] is None or a_["rec"] is None:
                    continue
                dr = a_["rec"] - b["rec"]
                drec.append(dr)
                if b["cost"] and a_["cost"] is not None:
                    dcost.append(100 * (a_["cost"] - b["cost"]) / b["cost"])
                if b["turns"] is not None and a_["turns"] is not None:
                    dturn.append(a_["turns"] - b["turns"])
                if dr > EPS:
                    w += 1
                elif dr < -EPS:
                    ls += 1
                else:
                    t += 1
            if not drec:
                continue
            rng = random.Random(seed)
            mr, rlo, rhi = _ci(drec, rng, boot)
            if dcost:
                mc, clo, chi = _ci(dcost, rng, boot)
            else:
                mc = clo = chi = None
            mt = statistics.fmean(dturn) if dturn else None
            if rhi < 0:
                verdict = "REGRESS"
            elif rlo >= -EPS and chi is not None and chi < 0:
                verdict = "SAVE"
            else:
                verdict = "inconclusive"
            rec_s = f"{mr:+.3f} [{rlo:+.3f},{rhi:+.3f}]"
            cost_s = f"{mc:+.1f} [{clo:+.1f},{chi:+.1f}]" if mc is not None else "n/a"
            turn_s = f"{mt:+.1f}" if mt is not None else "n/a"
            L.append(
                f"| {cat} | {arm} | {len(drec)} | {rec_s} | {cost_s} | {turn_s} "
                f"| {w}/{t}/{ls} | {verdict} |"
            )
    L.append("")
    return "\n".join(L)


analyze = analyze_pareto
