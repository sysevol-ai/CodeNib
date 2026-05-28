#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Phase-2 aggregator for the agent-compile subset sweep (issue #133).

Phase 0's ``aggregate.py`` only answers the kill-switch question (A6 cost +
accuracy). This aggregator produces the Phase-2 testing-standard artifacts
that the RFC requires for deriving ``compile_table``:

1. **Per-subset metrics** — files@k / symbols@k (mean across reps then
   instances), tokens / turns / cost (min across reps then mean across
   instances), cap-hit rate.
2. **Skill-invocation histogram** — per subset, each skill's invocation rate
   (fraction of cells that called it ≥ 1×), mean calls/cell, and conditional
   success rate (files@5 hit | skill invoked). Flags skills that were forced
   into a subset but effectively ignored.
3. **Saturation guard (#130)** — splits instances into ``easy`` (BM25-alone /
   A0 already hits GT files@5) and ``hard`` (the rest), reports files@5 per
   slice, and marks a subset *ineligible* for ``compile_table`` when its lift
   over A0 lives entirely in the easy slice.
4. **Per-(subset × scenario)** accuracy / cost cells.
5. **compile_table** — for each scenario, ``argmin(tokens)`` over *eligible*
   subsets whose files@5 ≥ τ (τ = A6 files@5 − ``--tau-margin`` by default).
6. **Pareto front** — subsets non-dominated in (files@5 ↑, tokens ↓).

Outputs ``phase2_report.md``, ``phase2_metrics.json``, ``compile_table.json``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

A0_SUBSET = "A0"
FULL_SUBSET = "A6"
EASY_FILES_AT_5 = 0.5  # A0 mean-rep files@5 >= this => "easy" instance

# Always-on default tool layer (file_read / file_search). These are unioned
# into every subset by AgentRunner, so they are NOT sweep variables: they must
# never appear in a derived compile_table. They DO show in the invocation
# histogram (tagged "always-on") so we can see how the agent uses them.
# Kept as a literal to keep this script free of heavy agent imports.
ALWAYS_ON_SKILLS = frozenset({"file_read", "file_search"})


# ---------------------------------------------------------------------------
# Loading / small helpers
# ---------------------------------------------------------------------------


def load_cells(cells_dir: Path) -> List[Dict[str, Any]]:
    cells: List[Dict[str, Any]] = []
    for path in sorted(cells_dir.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as f:
                cells.append(json.load(f))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"aggregate2: WARN unreadable {path}: {exc}", file=sys.stderr)
    return cells


def _at_k(cell: Dict[str, Any], scope: str, k: int) -> Optional[float]:
    bucket = (cell.get("metrics") or {}).get(scope) or {}
    stats = bucket.get(k, bucket.get(str(k)))
    if isinstance(stats, dict):
        return stats.get("accuracy")
    return stats


def _safe_mean(xs: Sequence[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return statistics.fmean(vals) if vals else None


def _safe_min(xs: Sequence[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return min(vals) if vals else None


# ---------------------------------------------------------------------------
# Core grouping: subset -> instance -> [reps]
# ---------------------------------------------------------------------------


def group(cells: Sequence[Dict[str, Any]]):
    out: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for c in cells:
        sid, iid = c.get("subset_id"), c.get("instance_id")
        if sid and iid and c.get("success"):
            out[sid][iid].append(c)
    return out


def instance_files_at_5(reps: Sequence[Dict[str, Any]]) -> Optional[float]:
    return _safe_mean([_at_k(r, "files", 5) for r in reps])


def per_subset_instance_metric(grouped, subset: str, metric_fn) -> Dict[str, float]:
    """metric_fn(reps) -> value; returns {instance_id: value}."""
    out = {}
    for iid, reps in grouped.get(subset, {}).items():
        v = metric_fn(reps)
        if v is not None:
            out[iid] = v
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate(cells: Sequence[Dict[str, Any]], ks: Sequence[int], max_turns: int):
    grouped = group(cells)
    subsets = sorted(grouped.keys())

    # --- easy/hard split from A0 (fallback: any subset if A0 absent) ---
    base = A0_SUBSET if A0_SUBSET in grouped else (subsets[0] if subsets else None)
    easy_instances, hard_instances = set(), set()
    if base:
        for iid, reps in grouped[base].items():
            f5 = instance_files_at_5(reps)
            (easy_instances if (f5 or 0) >= EASY_FILES_AT_5 else hard_instances).add(
                iid
            )

    per_subset: Dict[str, Any] = {}
    for sid in subsets:
        by_inst = grouped[sid]
        n_inst = len(by_inst)

        files_k = {k: [] for k in ks}
        symbols_k = {k: [] for k in ks}
        tokens, turns, cost, cap_hit, durations = [], [], [], [], []
        f5_easy, f5_hard = [], []

        # invocation histogram accumulators
        skill_invoked_cells: "Counter[str]" = Counter()
        skill_total_calls: "Counter[str]" = Counter()
        skill_hit_when_invoked: Dict[str, List[int]] = defaultdict(list)
        total_cells = 0

        for iid, reps in by_inst.items():
            for k in ks:
                fv = _safe_mean([_at_k(r, "files", k) for r in reps])
                sv = _safe_mean([_at_k(r, "symbols", k) for r in reps])
                if fv is not None:
                    files_k[k].append(fv)
                if sv is not None:
                    symbols_k[k].append(sv)
            tokens.append(_safe_min([r.get("total_tokens") for r in reps]))
            turns.append(_safe_min([r.get("total_turns") for r in reps]))
            durations.append(_safe_min([r.get("total_duration_ms") for r in reps]))
            cost.append(_safe_min([r.get("cost_usd") for r in reps]))
            cap_hit.append(
                1 if any((r.get("total_turns") or 0) >= max_turns for r in reps) else 0
            )
            f5 = instance_files_at_5(reps)
            if f5 is not None:
                (f5_easy if iid in easy_instances else f5_hard).append(f5)

            # invocation histogram per rep-cell
            for r in reps:
                total_cells += 1
                called: "Counter[str]" = Counter()
                for tc in r.get("tool_calls") or []:
                    called[tc.get("skill_id")] += 1
                hit = (_at_k(r, "files", 5) or 0) >= 1.0
                for skill, n in called.items():
                    skill_invoked_cells[skill] += 1
                    skill_total_calls[skill] += n
                    skill_hit_when_invoked[skill].append(1 if hit else 0)

        histogram = {}
        for skill in sorted(set(list(skill_invoked_cells) + list(skill_total_calls))):
            inv_cells = skill_invoked_cells[skill]
            histogram[skill] = {
                "invocation_rate": (inv_cells / total_cells) if total_cells else 0.0,
                "mean_calls_per_cell": (
                    skill_total_calls[skill] / total_cells if total_cells else 0.0
                ),
                "conditional_files_at_5": (
                    _safe_mean(skill_hit_when_invoked[skill])
                    if skill_hit_when_invoked[skill]
                    else None
                ),
            }
        # forced-but-ignored: in the subset's skill list but rarely invoked
        subset_skills = next(
            (c.get("skills") for c in cells if c.get("subset_id") == sid), []
        )
        ignored = [
            s
            for s in (subset_skills or [])
            if histogram.get(s, {}).get("invocation_rate", 0.0) < 0.05
        ]

        per_subset[sid] = {
            "instance_count": n_inst,
            "files_at_k": {k: _safe_mean(files_k[k]) for k in ks},
            "symbols_at_k": {k: _safe_mean(symbols_k[k]) for k in ks},
            "mean_total_tokens": _safe_mean(tokens),
            "mean_total_turns": _safe_mean(turns),
            "mean_total_duration_ms": _safe_mean(durations),
            "mean_cost_usd": _safe_mean(cost),
            "cap_hit_rate": (sum(cap_hit) / len(cap_hit)) if cap_hit else 0.0,
            "files_at_5_easy": _safe_mean(f5_easy),
            "files_at_5_hard": _safe_mean(f5_hard),
            "invocation_histogram": histogram,
            "forced_but_ignored": ignored,
        }

    # --- saturation eligibility: lift over A0 must not be only in easy ---
    a0 = per_subset.get(A0_SUBSET, {})
    a0_hard = a0.get("files_at_5_hard")
    for sid, m in per_subset.items():
        hard = m.get("files_at_5_hard")
        if sid == A0_SUBSET:
            m["compile_eligible"] = True
            m["eligibility_reason"] = "baseline"
            continue
        if hard is None or a0_hard is None:
            # no hard instances in this sample -> can't disqualify; allow w/ caveat
            m["compile_eligible"] = True
            m["eligibility_reason"] = (
                "no-hard-slice (sample too small to test saturation)"
            )
        else:
            # #130/#133 saturation guard: a subset earns its keep only if its
            # lift over A0 reaches the hard slice. Lift confined to easy
            # instances (already trivial for BM25) counts as zero.
            lift_hard = hard - a0_hard
            m["compile_eligible"] = lift_hard > 1e-9
            m["eligibility_reason"] = f"hard lift over A0 = {lift_hard:+.3f}"

    # --- per (subset x scenario) ---
    scen = defaultdict(lambda: defaultdict(list))  # scenario -> subset -> [cells]
    for c in cells:
        if c.get("success"):
            scen[c.get("scenario", "unknown")][c.get("subset_id")].append(c)
    per_scenario = {}
    for scenario, by_sub in scen.items():
        per_scenario[scenario] = {}
        for sid, cs in by_sub.items():
            by_inst = defaultdict(list)
            for c in cs:
                by_inst[c["instance_id"]].append(c)
            files_at_k = {
                k: _safe_mean(
                    [
                        _safe_mean([_at_k(r, "files", k) for r in reps])
                        for reps in by_inst.values()
                    ]
                )
                for k in ks
            }
            tok = _safe_mean(
                [
                    _safe_min([r.get("total_tokens") for r in reps])
                    for reps in by_inst.values()
                ]
            )
            per_scenario[scenario][sid] = {
                "files_at_k": files_at_k,
                "files_at_5": files_at_k.get(5),
                "mean_total_tokens": tok,
                "instance_count": len(by_inst),
            }

    return {
        "subsets": per_subset,
        "per_scenario": per_scenario,
        "easy_instances": sorted(easy_instances),
        "hard_instances": sorted(hard_instances),
    }


def _ge(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and a >= b


# ---------------------------------------------------------------------------
# compile_table + Pareto
# ---------------------------------------------------------------------------


def _scenario_files_at(m: Dict[str, Any], k: int) -> Optional[float]:
    fk = m.get("files_at_k") or {}
    return fk.get(k, fk.get(str(k), m.get("files_at_5") if k == 5 else None))


def derive_compile_table(
    agg, cells, *, tau_margin: float, target_k: int = 5
) -> Tuple[Dict[str, List[str]], Dict[str, Any]]:
    """Per scenario: cheapest *eligible* subset with files@target_k >= tau.

    ``target_k`` is the accuracy metric the floor is applied to. The #133
    standard uses files@5; on a files@5-saturated sample, pass ``target_k=1``
    to let top-1 precision (the discriminating signal) drive selection.
    """
    subsets_meta = agg["subsets"]
    full_acc = (subsets_meta.get(FULL_SUBSET, {}).get("files_at_k") or {}).get(target_k)
    tau_global = (full_acc - tau_margin) if full_acc is not None else 0.0

    skills_for = {}
    for c in cells:
        skills_for.setdefault(c.get("subset_id"), c.get("skills"))

    table: Dict[str, List[str]] = {}
    rationale: Dict[str, Any] = {
        "target_k": target_k,
        "tau_global": tau_global,
        "tau_margin": tau_margin,
        "scenarios": {},
    }
    for scenario, by_sub in agg["per_scenario"].items():
        full_s = _scenario_files_at(by_sub.get(FULL_SUBSET) or {}, target_k)
        tau = (full_s - tau_margin) if full_s is not None else tau_global
        candidates = []
        for sid, m in by_sub.items():
            acc = _scenario_files_at(m, target_k)
            tok = m.get("mean_total_tokens")
            eligible = subsets_meta.get(sid, {}).get("compile_eligible", True)
            if acc is not None and acc >= tau and eligible:
                candidates.append((tok if tok is not None else float("inf"), sid, acc))
        if candidates:
            candidates.sort()
            _, best, best_acc = candidates[0]
        else:
            # nothing clears tau -> fall back to the full registry
            best, best_acc = FULL_SUBSET, full_s
        # Defaults are always-on, never part of the swept compile_table.
        table[scenario] = [
            s for s in (skills_for.get(best) or []) if s not in ALWAYS_ON_SKILLS
        ]
        rationale["scenarios"][scenario] = {
            "chosen_subset": best,
            "tau": tau,
            "chosen_files_at_k": best_acc,
            "candidates": sorted(
                [
                    {
                        "subset": sid,
                        "files_at_k": _scenario_files_at(m, target_k),
                        "tokens": m.get("mean_total_tokens"),
                        "eligible": subsets_meta.get(sid, {}).get(
                            "compile_eligible", True
                        ),
                    }
                    for sid, m in by_sub.items()
                ],
                key=lambda d: (d["tokens"] is None, d["tokens"] or 0),
            ),
        }
    return table, rationale


def pareto_front(agg) -> List[str]:
    """Subsets non-dominated in (files@5 high, tokens low)."""
    pts = []
    for sid, m in agg["subsets"].items():
        f5 = (m.get("files_at_k") or {}).get(5)
        tok = m.get("mean_total_tokens")
        if f5 is not None and tok is not None:
            pts.append((sid, f5, tok))
    front = []
    for sid, f5, tok in pts:
        dominated = any(
            (of5 >= f5 and otok <= tok and (of5 > f5 or otok < tok))
            for osid, of5, otok in pts
            if osid != sid
        )
        if not dominated:
            front.append(sid)
    return sorted(front)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_markdown(agg, table, rationale, front, ks) -> str:
    L: List[str] = ["# Phase 2 agent-compile sample report", ""]
    L.append(
        f"Easy instances (A0 files@5 ≥ {EASY_FILES_AT_5}): {agg['easy_instances'] or '(none)'}"
    )
    L.append(f"Hard instances: {agg['hard_instances'] or '(none)'}")
    L.append("")
    L.append("## Per-subset metrics")
    L.append("")
    head = (
        ["subset", "n", "tokens", "turns", "cost$", "cap%"]
        + [f"files@{k}" for k in ks]
        + ["f@5 easy", "f@5 hard", "eligible"]
    )
    L.append("| " + " | ".join(head) + " |")
    L.append("| " + " | ".join("---" for _ in head) + " |")
    for sid in sorted(agg["subsets"]):
        m = agg["subsets"][sid]
        row = [
            sid,
            str(m["instance_count"]),
            _fmt(m["mean_total_tokens"], 0),
            _fmt(m["mean_total_turns"], 1),
            _fmt(m["mean_cost_usd"], 4),
            f"{m['cap_hit_rate']:.0%}",
        ]
        row += [_fmt((m["files_at_k"] or {}).get(k), 3) for k in ks]
        row += [
            _fmt(m["files_at_5_easy"], 3),
            _fmt(m["files_at_5_hard"], 3),
            "yes" if m["compile_eligible"] else "NO",
        ]
        L.append("| " + " | ".join(row) + " |")
    L.append("")
    L.append(f"**Pareto front (files@5 ↑ / tokens ↓):** {', '.join(front) or '(none)'}")
    L.append("")

    L.append("## Skill-invocation histogram")
    L.append("")
    L.append(
        "`file_read` / `file_search` are the always-on default tool layer "
        "(present in every subset, not swept)."
    )
    L.append("")
    for sid in sorted(agg["subsets"]):
        h = agg["subsets"][sid]["invocation_histogram"]
        if not h:
            continue
        ignored = agg["subsets"][sid]["forced_but_ignored"]
        L.append(
            f"### {sid}" + (f"  — forced-but-ignored: {ignored}" if ignored else "")
        )
        L.append("")
        L.append("| skill | invoke_rate | calls/cell | files@5 \\| invoked |")
        L.append("| --- | --- | --- | --- |")
        for skill, s in sorted(h.items(), key=lambda kv: -kv[1]["invocation_rate"]):
            label = skill + (" *(always-on)*" if skill in ALWAYS_ON_SKILLS else "")
            L.append(
                f"| {label} | {s['invocation_rate']:.0%} | "
                f"{s['mean_calls_per_cell']:.2f} | {_fmt(s['conditional_files_at_5'], 3)} |"
            )
        L.append("")

    L.append("## Per-scenario files@5 / tokens")
    L.append("")
    for scenario in sorted(agg["per_scenario"]):
        L.append(f"### {scenario}")
        L.append("")
        L.append("| subset | files@5 | tokens | n |")
        L.append("| --- | --- | --- | --- |")
        for sid in sorted(agg["per_scenario"][scenario]):
            m = agg["per_scenario"][scenario][sid]
            f5 = _fmt(m["files_at_5"], 3)
            tok = _fmt(m["mean_total_tokens"], 0)
            L.append(f"| {sid} | {f5} | {tok} | {m['instance_count']} |")
        L.append("")

    tk = rationale.get("target_k", 5)
    margin = rationale["tau_margin"]
    tau = _fmt(rationale["tau_global"], 3)
    L.append("## Derived compile_table")
    L.append("")
    L.append(
        f"Floor metric: files@{tk}; " f"τ_global = A6 files@{tk} − {margin} = {tau}"
    )
    L.append("")
    L.append(f"| scenario | chosen subset | files@{tk} | skills |")
    L.append("| --- | --- | --- | --- |")
    for scenario in sorted(table):
        r = rationale["scenarios"][scenario]
        L.append(
            f"| {scenario} | {r['chosen_subset']} | {_fmt(r['chosen_files_at_k'], 3)} | "
            f"{', '.join(table[scenario])} |"
        )
    L.append("")
    return "\n".join(L)


def _fmt(v: Optional[float], nd: int) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{nd}f}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Phase-2 aggregator: histograms, saturation guard, compile_table.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--cells-dir", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--metrics-k", type=int, nargs="+", default=[1, 3, 5, 10])
    p.add_argument("--max-turns", type=int, default=20)
    p.add_argument(
        "--tau-margin",
        type=float,
        default=0.05,
        help="files@k floor = A6 files@k − margin (RFC open-question 2).",
    )
    p.add_argument(
        "--target-k",
        type=int,
        default=5,
        help="Accuracy metric k the compile_table floor is applied to "
        "(use 1 when files@5 is saturated to let top-1 precision decide).",
    )
    args = p.parse_args(argv)

    cells = load_cells(args.cells_dir)
    if not cells:
        print(f"aggregate2: no cells under {args.cells_dir}", file=sys.stderr)
        return 2

    agg = aggregate(cells, ks=args.metrics_k, max_turns=args.max_turns)
    table, rationale = derive_compile_table(
        agg, cells, tau_margin=args.tau_margin, target_k=args.target_k
    )
    front = pareto_front(agg)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "phase2_metrics.json").open("w", encoding="utf-8") as f:
        json.dump(
            {
                "aggregate": agg,
                "compile_table": table,
                "rationale": rationale,
                "pareto_front": front,
                "cell_count": len(cells),
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    with (args.output_dir / "compile_table.json").open("w", encoding="utf-8") as f:
        json.dump(table, f, indent=2, ensure_ascii=False)
    md = render_markdown(agg, table, rationale, front, args.metrics_k)
    (args.output_dir / "phase2_report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
