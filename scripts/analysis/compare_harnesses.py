# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Accuracy-first proof aggregator: does the graph-aware composer beat the
grep/read agent on tokens at EQUAL accuracy? (#133)

Compares two reps-aware sweep output dirs (a baseline and a treatment) cell-by
cell. For each instance it reduces the reps to:

  - accuracy: MEAN files@k / symbols@k over reps (a run either finds the
    target or not; the mean is the success rate),
  - cost: MEDIAN total_tokens over reps (robust to the large single-rep token
    variance we measured — redis FREE swung 97k->161k on the same config).

The headline is a PAIRED comparison across instances: per-instance token delta
% = (treat_median - base_median) / base_median, then the mean delta with a
95% confidence interval (CI — *confidence interval*, not continuous integration;
Student-t, appropriate for the small N) over the N instances. Accuracy parity is
checked first and
any per-instance regression (treatment mean files@5 < baseline) is flagged —
a token win at the cost of accuracy is NOT a win.

Usage:
    python -m scripts.analysis.compare_harnesses \
        --baseline results/agent_compile/grep \
        --treatment results/agent_compile/graph \
        --k 5
"""

from __future__ import annotations

import argparse
import json
import math
import statistics as st
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Student-t two-sided 95% critical values by degrees of freedom (n-1).
_T95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    15: 2.131,
    20: 2.086,
    30: 2.042,
}


def _t95(df: int) -> float:
    if df <= 0:
        return float("nan")
    if df in _T95:
        return _T95[df]
    keys = sorted(_T95)
    for k in keys:
        if df <= k:
            return _T95[k]
    return 1.96  # large-sample normal approx


def _load_cells(out_dir: Path) -> Dict[str, List[Dict[str, Any]]]:
    """instance_id -> list of rep cells."""
    by_inst: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for p in sorted((out_dir / "cells").glob("*.json")):
        with p.open("r", encoding="utf-8") as f:
            c = json.load(f)
        by_inst[c["instance_id"]].append(c)
    return by_inst


def _acc(cell: Dict[str, Any], kind: str, k: int) -> float:
    return cell.get("metrics", {}).get(kind, {}).get(str(k), {}).get("accuracy", 0.0)


def _reduce(cells: List[Dict[str, Any]], k: int) -> Tuple[float, float, float, int]:
    """(mean files@k, mean symbols@k, median tokens, n_reps)."""
    f = st.mean(_acc(c, "files", k) for c in cells)
    s = st.mean(_acc(c, "symbols", k) for c in cells)
    tok = st.median(c["total_tokens"] for c in cells)
    return f, s, tok, len(cells)


def compare(baseline: Path, treatment: Path, k: int) -> Dict[str, Any]:
    base = _load_cells(baseline)
    treat = _load_cells(treatment)
    shared = sorted(set(base) & set(treat))

    rows: List[Dict[str, Any]] = []
    deltas: List[float] = []
    regressions: List[str] = []
    for inst in shared:
        bf, bs, btok, bn = _reduce(base[inst], k)
        tf, ts, ttok, tn = _reduce(treat[inst], k)
        d = 100.0 * (ttok - btok) / btok if btok else float("nan")
        deltas.append(d)
        lang = base[inst][0].get("language", "?")
        if tf + 1e-9 < bf:  # treatment less accurate at files@k
            regressions.append(inst)
        rows.append(
            {
                "instance": inst,
                "lang": lang,
                "base_f": bf,
                "treat_f": tf,
                "base_s": bs,
                "treat_s": ts,
                "base_tok": btok,
                "treat_tok": ttok,
                "dtok_pct": d,
                "reps": (bn, tn),
            }
        )

    mean_d = st.mean(deltas) if deltas else float("nan")
    ci: Optional[Tuple[float, float]] = None
    if len(deltas) >= 2:
        sd = st.stdev(deltas)
        se = sd / math.sqrt(len(deltas))
        h = _t95(len(deltas) - 1) * se
        ci = (mean_d - h, mean_d + h)

    return {
        "k": k,
        "rows": rows,
        "mean_dtok_pct": mean_d,
        "ci95": ci,
        "regressions": regressions,
        "n_instances": len(shared),
    }


def render(report: Dict[str, Any]) -> str:
    k = report["k"]
    lines: List[str] = []
    lines.append(
        f"{'instance':28s} {'lang':5s} | "
        f"{'GREP f@' + str(k):>8s} {'CTX f@' + str(k):>8s} | "
        f"{'GREP tok':>9s} {'CTX tok':>9s} | {'dtok%':>7s}"
    )
    lines.append("-" * 92)
    for r in report["rows"]:
        flag = "  !ACC" if r["treat_f"] + 1e-9 < r["base_f"] else ""
        lines.append(
            f"{r['instance']:28s} {r['lang']:5s} | "
            f"{r['base_f']:8.2f} {r['treat_f']:8.2f} | "
            f"{r['base_tok']:9.0f} {r['treat_tok']:9.0f} | "
            f"{r['dtok_pct']:+7.0f}{flag}"
        )
    lines.append("-" * 92)
    ci = report["ci95"]
    ci_s = f" 95% confidence interval [{ci[0]:+.0f}%, {ci[1]:+.0f}%]" if ci else ""
    lines.append(
        f"mean token delta (paired, median-of-reps): "
        f"{report['mean_dtok_pct']:+.0f}%{ci_s}  "
        f"over N={report['n_instances']} instances"
    )
    if report["regressions"]:
        lines.append(f"ACCURACY REGRESSIONS (files@{k}): {report['regressions']}")
    else:
        lines.append(f"accuracy: files@{k} parity or better on all instances ✓")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", required=True, type=Path)
    ap.add_argument("--treatment", required=True, type=Path)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args(argv)

    report = compare(args.baseline, args.treatment, args.k)
    print(render(report))
    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
