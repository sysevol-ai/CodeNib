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
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

KS = [1, 3, 5, 10]


def _load(cells_dir: Path) -> List[Dict[str, Any]]:
    out = []
    for p in sorted(cells_dir.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return out


def _field(cell: Dict[str, Any], scope: str, k: int, field: str) -> Optional[float]:
    b = (cell.get("metrics") or {}).get(scope) or {}
    s = b.get(k, b.get(str(k)))
    return s.get(field) if isinstance(s, dict) else None


def _mean(xs: Sequence[Optional[float]]) -> Optional[float]:
    v = [x for x in xs if x is not None]
    return statistics.fmean(v) if v else None


def _min(xs: Sequence[Optional[float]]) -> Optional[float]:
    v = [x for x in xs if x is not None]
    return min(v) if v else None


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
    # grep vs preinj delta on answer_rec@5 per category
    L.append("## answer_rec@5: preinj − grep, per category")
    L.append("")
    L.append("| category | grep_only | preinj_embed | Δ |")
    L.append("| --- | --- | --- | --- |")
    for cat in cats:
        g = rows.get((cat, "grep_only"), {}).get("answer_rec@5")
        p = rows.get((cat, "preinj_embed"), {}).get("answer_rec@5")
        d = (p - g) if (g is not None and p is not None) else None
        L.append(f"| {cat} | {_fmt(g)} | {_fmt(p)} | {_fmt(d)} |")
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
