#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Aggregate an agent-compile cost-arm sweep into a comparison report.

Reads the per-cell JSON written by ``run_sweep.py`` and folds it into a
per-arm comparison answering the cost-study question: *which tool harness
localizes as accurately as the grep/read baseline, at what token cost?*

It produces:

1. **Per-arm metrics** — files@k / symbols@k (mean across reps then
   instances), tokens / turns / cost (min across reps then mean across
   instances — rep noise on cost is one-sided), cap-hit rate.
2. **Skill-invocation histogram** — per arm, each tool's invocation rate
   (fraction of cells that called it ≥ 1×), mean calls/cell, and conditional
   files@5 (hit | tool invoked). Surfaces tools the agent was offered but
   effectively ignored — the core "does it voluntarily use the graph?" signal.
3. **Easy/hard split** — instances where the baseline arm already hits GT
   files@5 ("easy") vs the rest ("hard"), with files@5 per slice, so an arm's
   accuracy is attributed to where it actually helps.
4. **Per-(arm × scenario)** files@5 / tokens, scenario = ``language:stacktrace``.
5. **Pareto front** — arms non-dominated in (files@5 ↑, tokens ↓).

Outputs ``report.md`` and ``metrics.json``.

Rep-folding rule (one canonical rule, applied everywhere): cost-like metrics
(tokens, turns, duration, cost) take the **min** across reps — provider/GC
noise only inflates them, so the minimum is the closest estimate to the true
lower bound; accuracy metrics (files@k, symbols@k) take the **mean** across
reps — that noise is two-sided; cap-hit is "any rep touched the turn ceiling".
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

EASY_FILES_AT_5 = 0.5  # baseline-arm mean-rep files@5 >= this => "easy" instance

# Baseline arm: the grep/read agent everyone already has. The easy/hard split
# is computed relative to it. Falls back to the first arm if absent.
_DEFAULT_BASELINE_CANDIDATES = ("GREP", "grep", "A0")

# Always-on default tool layer (read / grep / glob / bash). These are unioned
# into every arm by AgentRunner, so they are not sweep variables. They DO show
# in the invocation histogram (tagged "always-on") so we can see how the agent
# leans on them vs the index-backed skills. Kept as a literal to keep this
# offline JSON aggregator free of heavy agent imports.
ALWAYS_ON_SKILLS = frozenset({"read", "grep", "glob", "bash"})


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
            print(f"aggregate: WARN unreadable {path}: {exc}", file=sys.stderr)
    return cells


def _at_k(cell: Dict[str, Any], scope: str, k: int) -> Optional[float]:
    bucket = (cell.get("metrics") or {}).get(scope) or {}
    stats = bucket.get(k, bucket.get(str(k)))
    if isinstance(stats, dict):
        return stats.get("accuracy")
    return stats


def _field_at_k(
    cell: Dict[str, Any], scope: str, k: int, field: str
) -> Optional[float]:
    """Read an arbitrary metric field (precision/recall/...) at cutoff k."""
    bucket = (cell.get("metrics") or {}).get(scope) or {}
    stats = bucket.get(k, bucket.get(str(k)))
    return stats.get(field) if isinstance(stats, dict) else None


def _safe_mean(xs: Sequence[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return statistics.fmean(vals) if vals else None


def _safe_min(xs: Sequence[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return min(vals) if vals else None


def _fmt(v: Optional[float], nd: int) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{nd}f}"


# ---------------------------------------------------------------------------
# Core grouping: arm -> instance -> [reps]
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


def _pick_baseline(arms: Sequence[str], baseline: Optional[str]) -> Optional[str]:
    if baseline and baseline in arms:
        return baseline
    for cand in _DEFAULT_BASELINE_CANDIDATES:
        if cand in arms:
            return cand
    return arms[0] if arms else None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate(
    cells: Sequence[Dict[str, Any]],
    ks: Sequence[int],
    max_turns: int,
    baseline: Optional[str] = None,
):
    grouped = group(cells)
    arms = sorted(grouped.keys())

    # --- easy/hard split from the baseline arm ---
    base = _pick_baseline(arms, baseline)
    easy_instances, hard_instances = set(), set()
    if base:
        for iid, reps in grouped[base].items():
            f5 = instance_files_at_5(reps)
            (easy_instances if (f5 or 0) >= EASY_FILES_AT_5 else hard_instances).add(
                iid
            )

    per_arm: Dict[str, Any] = {}
    for sid in arms:
        by_inst = grouped[sid]
        n_inst = len(by_inst)

        files_k = {k: [] for k in ks}
        symbols_k = {k: [] for k in ks}
        # Line-span localization (overlap-based, replaces the brittle string
        # symbols@k). ``answer`` = the agent's committed final answer (its
        # deliverable); ``retrieval`` = the retriever's ranked spans (the
        # RAG-comparable alignment axis). Both scopes report BOTH acc (hit@k,
        # all GT blocks covered) and recall (fraction covered) — mirroring the
        # original RAG/rerank baselines, which logged acc+recall symmetrically;
        # acc==recall for single-block instances (the majority).
        answer_acc_k = {k: [] for k in ks}
        answer_recall_k = {k: [] for k in ks}
        retrieval_acc_k = {k: [] for k in ks}
        retrieval_recall_k = {k: [] for k in ks}
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
                aa = _safe_mean([_at_k(r, "answer_blocks", k) for r in reps])
                ar = _safe_mean(
                    [_field_at_k(r, "answer_blocks", k, "recall") for r in reps]
                )
                ra = _safe_mean([_at_k(r, "retrieval_blocks", k) for r in reps])
                rr = _safe_mean(
                    [_field_at_k(r, "retrieval_blocks", k, "recall") for r in reps]
                )
                if aa is not None:
                    answer_acc_k[k].append(aa)
                if ar is not None:
                    answer_recall_k[k].append(ar)
                if ra is not None:
                    retrieval_acc_k[k].append(ra)
                if rr is not None:
                    retrieval_recall_k[k].append(rr)
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
        # offered-but-ignored: in the arm's skill list but rarely invoked
        arm_skills = next(
            (c.get("skills") for c in cells if c.get("subset_id") == sid), []
        )
        ignored = [
            s
            for s in (arm_skills or [])
            if histogram.get(s, {}).get("invocation_rate", 0.0) < 0.05
        ]

        per_arm[sid] = {
            "instance_count": n_inst,
            "files_at_k": {k: _safe_mean(files_k[k]) for k in ks},
            "symbols_at_k": {k: _safe_mean(symbols_k[k]) for k in ks},
            "answer_acc_at_k": {k: _safe_mean(answer_acc_k[k]) for k in ks},
            "answer_recall_at_k": {k: _safe_mean(answer_recall_k[k]) for k in ks},
            "retrieval_acc_at_k": {k: _safe_mean(retrieval_acc_k[k]) for k in ks},
            "retrieval_recall_at_k": {k: _safe_mean(retrieval_recall_k[k]) for k in ks},
            "mean_total_tokens": _safe_mean(tokens),
            "mean_total_turns": _safe_mean(turns),
            "mean_total_duration_ms": _safe_mean(durations),
            "mean_cost_usd": _safe_mean(cost),
            "cap_hit_rate": (sum(cap_hit) / len(cap_hit)) if cap_hit else 0.0,
            "files_at_5_easy": _safe_mean(f5_easy),
            "files_at_5_hard": _safe_mean(f5_hard),
            "invocation_histogram": histogram,
            "offered_but_ignored": ignored,
        }

    # --- per (arm x scenario) ---
    scen = defaultdict(lambda: defaultdict(list))  # scenario -> arm -> [cells]
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
        "baseline_arm": base,
        "arms": per_arm,
        "per_scenario": per_scenario,
        "easy_instances": sorted(easy_instances),
        "hard_instances": sorted(hard_instances),
    }


# ---------------------------------------------------------------------------
# Pareto
# ---------------------------------------------------------------------------


def pareto_front(agg) -> List[str]:
    """Arms non-dominated in (files@5 high, tokens low)."""
    pts = []
    for sid, m in agg["arms"].items():
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


def render_markdown(agg, front, ks) -> str:
    L: List[str] = ["# Agent-compile cost-arm report", ""]
    L.append(f"Baseline arm (easy/hard split reference): `{agg.get('baseline_arm')}`")
    L.append(
        f"Easy instances (baseline files@5 ≥ {EASY_FILES_AT_5}): "
        f"{agg['easy_instances'] or '(none)'}"
    )
    L.append(f"Hard instances: {agg['hard_instances'] or '(none)'}")
    L.append("")
    L.append("## Per-arm metrics")
    L.append("")
    head = (
        ["arm", "n", "tokens", "turns", "cost$", "cap%"]
        + [f"files@{k}" for k in ks]
        + ["f@5 easy", "f@5 hard"]
    )
    L.append("| " + " | ".join(head) + " |")
    L.append("| " + " | ".join("---" for _ in head) + " |")
    for sid in sorted(agg["arms"]):
        m = agg["arms"][sid]
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
        ]
        L.append("| " + " | ".join(row) + " |")
    L.append("")
    L.append(f"**Pareto front (files@5 ↑ / tokens ↓):** {', '.join(front) or '(none)'}")
    L.append("")

    L.append("## Symbol-level localization (span recall@k — replaces symbols@k*)")
    L.append("")
    L.append(
        "Overlap-based, so it is robust to the symbol-name string-match artifact "
        "(`symbols@k*` in the per-arm table above scores agent prose ≈0 and is "
        "kept only for back-reference). Two scopes, scored from different sources "
        "— neither bounds the other:\n\n"
        "- **`answer_rec@k`** — recall over the agent's committed final answer "
        "(`Locations:` + graph-resolved `Symbols:`). The agent's deliverable / "
        "headline. `answer_acc@k` is the strict variant (ALL gt blocks hit).\n"
        "- **`retr_rec@k`** — recall over the retriever's ranked spans (nodes "
        "only). The cross-system ALIGNMENT axis: identical to a plain RAG "
        "pipeline's recall@k (node_id match == span overlap), so RAG / "
        "pure-retrieval / agent are directly comparable on it.\n\n"
        "`answer_rec > retr_rec` means the agent localized via grep/reasoning "
        "beyond what retrieval returned; `answer_rec < retr_rec` means it failed "
        "to use what retrieval surfaced."
    )
    L.append("")
    head2 = (
        ["arm"]
        + [f"answer_acc@{k}" for k in ks]
        + [f"answer_rec@{k}" for k in ks]
        + [f"retr_acc@{k}" for k in ks]
        + [f"retr_rec@{k}" for k in ks]
    )
    L.append("| " + " | ".join(head2) + " |")
    L.append("| " + " | ".join("---" for _ in head2) + " |")
    for sid in sorted(agg["arms"]):
        m = agg["arms"][sid]
        row2 = [sid]
        row2 += [_fmt((m.get("answer_acc_at_k") or {}).get(k), 3) for k in ks]
        row2 += [_fmt((m.get("answer_recall_at_k") or {}).get(k), 3) for k in ks]
        row2 += [_fmt((m.get("retrieval_acc_at_k") or {}).get(k), 3) for k in ks]
        row2 += [_fmt((m.get("retrieval_recall_at_k") or {}).get(k), 3) for k in ks]
        L.append("| " + " | ".join(row2) + " |")
    L.append("")

    L.append("## Skill-invocation histogram")
    L.append("")
    L.append(
        "`read` / `grep` / `glob` / `bash` are the always-on default tool layer "
        "(present in every arm that includes defaults, not swept)."
    )
    L.append("")
    for sid in sorted(agg["arms"]):
        h = agg["arms"][sid]["invocation_histogram"]
        if not h:
            continue
        ignored = agg["arms"][sid]["offered_but_ignored"]
        L.append(
            f"### {sid}" + (f"  — offered-but-ignored: {ignored}" if ignored else "")
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
        L.append("| arm | files@5 | tokens | n |")
        L.append("| --- | --- | --- | --- |")
        for sid in sorted(agg["per_scenario"][scenario]):
            m = agg["per_scenario"][scenario][sid]
            f5 = _fmt(m["files_at_5"], 3)
            tok = _fmt(m["mean_total_tokens"], 0)
            L.append(f"| {sid} | {f5} | {tok} | {m['instance_count']} |")
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Aggregate an agent-compile cost-arm sweep into a report.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--cells-dir", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--metrics-k", type=int, nargs="+", default=[1, 3, 5, 10])
    p.add_argument(
        "--max-turns",
        type=int,
        default=16,
        help="Turn ceiling the sweep ran with (for cap-hit-rate). Match the config.",
    )
    p.add_argument(
        "--baseline",
        default=None,
        help="Arm to use as the easy/hard split reference (default: GREP, else first).",
    )
    args = p.parse_args(argv)

    cells = load_cells(args.cells_dir)
    if not cells:
        print(f"aggregate: no cells under {args.cells_dir}", file=sys.stderr)
        return 2

    agg = aggregate(
        cells, ks=args.metrics_k, max_turns=args.max_turns, baseline=args.baseline
    )
    front = pareto_front(agg)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "metrics.json").open("w", encoding="utf-8") as f:
        json.dump(
            {
                "aggregate": agg,
                "pareto_front": front,
                "cell_count": len(cells),
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    md = render_markdown(agg, front, args.metrics_k)
    (args.output_dir / "report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
