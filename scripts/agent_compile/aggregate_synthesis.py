#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Aggregate a codeminer-synthesis per-query sweep, broken down by CATEGORY.

The synthesis ``category`` is the axis that discriminates grep vs retrieval:
``behavioral`` queries name no identifiers (grep must explore the repo blind),
``symbol_hint`` names the symbol (grep wins), ``traversal`` needs call-graph
navigation. So we report span-overlap localization PER (category x arm):

  answer_rec@k  — the agent's committed-answer recall (headline / deliverable)
  answer_acc@k  — strict variant (all GT blocks hit)
  retr_rec@k    — retriever recall (RAG-comparable ceiling)
  contrib       — fraction of answer spans that came from a pre-injected
                  candidate (pre-load arms only)
  turns/tokens/cost — the Pareto cost axis

Rep folding mirrors aggregate.py: accuracy/recall mean across reps, cost-like
min across reps.

Usage::

    python scripts/agent_compile/aggregate_synthesis.py \
        --cells-dir results/agent_compile/synth_python/cells \
        --output-dir results/agent_compile/synth_python
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from codeminer.eval.agent_runner.metrics import (
    load_cell_jsons,
    metric_at_k,
    safe_mean,
    safe_min,
)

KS = [1, 3, 5, 10]


def _load(cells_dir: Path) -> List[Dict[str, Any]]:
    return load_cell_jsons(cells_dir)


def _field(cell: Dict[str, Any], scope: str, k: int, field: str) -> Optional[float]:
    return metric_at_k(cell, scope, k, field=field)


def _mean(xs: Sequence[Optional[float]]) -> Optional[float]:
    return safe_mean(xs)


def _min(xs: Sequence[Optional[float]]) -> Optional[float]:
    return safe_min(xs)


def _fmt(v: Optional[float], nd: int = 3) -> str:
    return "n/a" if v is None else f"{v:.{nd}f}"


def aggregate(cells: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    # Fold reps: group by (category, arm, query_id) -> reps.
    by_q: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for c in cells:
        if not c.get("success"):
            continue
        by_q[(c.get("category"), c.get("subset_id"), c.get("query_id"))].append(c)

    # Per (category, arm): accumulate query-level folded values.
    cat_arm: Dict[tuple, Dict[str, List]] = defaultdict(lambda: defaultdict(list))
    for (cat, arm, _qid), reps in by_q.items():
        acc = cat_arm[(cat, arm)]
        for k in KS:
            acc[f"answer_acc@{k}"].append(
                _mean([_field(r, "answer_blocks", k, "accuracy") for r in reps])
            )
            acc[f"answer_rec@{k}"].append(
                _mean([_field(r, "answer_blocks", k, "recall") for r in reps])
            )
            acc[f"retr_rec@{k}"].append(
                _mean([_field(r, "retrieval_blocks", k, "recall") for r in reps])
            )
        acc["contrib"].append(_mean([r.get("preload_contribution") for r in reps]))
        acc["turns"].append(_min([r.get("total_turns") for r in reps]))
        acc["tokens"].append(_min([r.get("total_tokens") for r in reps]))
        acc["cost"].append(_min([r.get("cost_usd") for r in reps]))

    rows = {}
    for (cat, arm), acc in cat_arm.items():
        rows[(cat, arm)] = {
            "n_queries": len(acc["turns"]),
            **{key: _mean(vals) for key, vals in acc.items()},
        }
    return {"by_cat_arm": rows}


def render(agg: Dict[str, Any]) -> str:
    rows = agg["by_cat_arm"]
    cats = sorted({c for (c, _a) in rows})
    arms = sorted({a for (_c, a) in rows})
    L = ["# CodeMiner-synthesis per-category localization", ""]
    L.append(
        "Headline = `answer_rec@5` (the agent's committed-answer span recall). "
        "`retr_rec@10` is the retriever ceiling. `contrib` = fraction of answer "
        "spans from a pre-injected candidate. Categories ordered easy→hard for "
        "grep (symbol_hint names the target; behavioral/traversal hide it)."
    )
    L.append("")
    head = [
        "category",
        "arm",
        "n",
        "answer_rec@5",
        "answer_acc@5",
        "retr_rec@10",
        "contrib",
        "turns",
        "tokens",
        "cost$",
    ]
    L.append("| " + " | ".join(head) + " |")
    L.append("| " + " | ".join("---" for _ in head) + " |")
    for cat in cats:
        for arm in arms:
            m = rows.get((cat, arm))
            if not m:
                continue
            L.append(
                "| "
                + " | ".join(
                    [
                        str(cat),
                        arm,
                        str(m["n_queries"]),
                        _fmt(m.get("answer_rec@5")),
                        _fmt(m.get("answer_acc@5")),
                        _fmt(m.get("retr_rec@10")),
                        _fmt(m.get("contrib")),
                        _fmt(m.get("turns"), 1),
                        _fmt(m.get("tokens"), 0),
                        _fmt(m.get("cost"), 4),
                    ]
                )
                + " |"
            )
    L.append("")
    # Pareto delta vs the grep_only baseline, per arm, per category. The decisive
    # "等精度省 token" view: accuracy delta AND cost delta side by side. An arm
    # "saves" on a category iff accuracy holds (Δrec ≳ 0) AND cost drops (Δ$ < 0).
    baseline = "grep_only"
    EPS = 0.02  # rec within ±EPS counts as "equal accuracy"
    for arm in arms:
        if arm == baseline:
            continue
        L.append(f"## {arm} vs {baseline} — Pareto delta per category")
        L.append("")
        head2 = [
            "category",
            "rec@5 base",
            "rec@5 arm",
            "Δrec@5",
            "cost$ base",
            "cost$ arm",
            "Δcost%",
            "Δturns",
            "verdict",
        ]
        L.append("| " + " | ".join(head2) + " |")
        L.append("| " + " | ".join("---" for _ in head2) + " |")
        for cat in cats:
            b = rows.get((cat, baseline))
            a = rows.get((cat, arm))
            if not b or not a:
                continue
            gr, ar = b.get("answer_rec@5"), a.get("answer_rec@5")
            gc, ac = b.get("cost"), a.get("cost")
            gt_, at_ = b.get("turns"), a.get("turns")
            drec = (ar - gr) if (gr is not None and ar is not None) else None
            dcostp = (
                100 * (ac - gc) / gc
                if (gc not in (None, 0) and ac is not None)
                else None
            )
            dturns = (at_ - gt_) if (gt_ is not None and at_ is not None) else None
            if drec is None or dcostp is None:
                verdict = "n/a"
            elif drec < -EPS:
                verdict = "REGRESS (acc)"
            elif dcostp < 0:
                verdict = "SAVE"  # equal/up accuracy AND cheaper
            else:
                verdict = "costlier"
            L.append(
                "| "
                + " | ".join(
                    [
                        str(cat),
                        _fmt(gr),
                        _fmt(ar),
                        _fmt(drec),
                        _fmt(gc, 4),
                        _fmt(ac, 4),
                        _fmt(dcostp, 1),
                        _fmt(dturns, 1),
                        verdict,
                    ]
                )
                + " |"
            )
        L.append("")
    return "\n".join(L)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cells-dir", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    args = p.parse_args(argv)
    cells = _load(args.cells_dir)
    agg = aggregate(cells)
    report = render(agg)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report_by_category.md").write_text(report, encoding="utf-8")
    # JSON with string keys
    js = {f"{c}/{a}": v for (c, a), v in agg["by_cat_arm"].items()}
    (args.output_dir / "metrics_by_category.json").write_text(
        json.dumps(js, indent=2), encoding="utf-8"
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
