# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Aggregate cost-arm agent-runner cells into comparison reports.

Reads per-cell JSON records written by agent-runner sweeps and folds them into
a per-arm comparison answering the cost-study question: *which tool harness
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

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from codenib.eval.agent_runner.metrics import metric_at_k, safe_mean, safe_min

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
FOCUSED_ADOPTION_TOOLS = ("lsp_route",)


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
    return metric_at_k(cell, scope, k)


def _field_at_k(
    cell: Dict[str, Any], scope: str, k: int, field: str
) -> Optional[float]:
    """Read an arbitrary metric field (precision/recall/...) at cutoff k."""
    return metric_at_k(cell, scope, k, field=field)


def _safe_mean(xs: Sequence[Optional[float]]) -> Optional[float]:
    return safe_mean(xs)


def _safe_min(xs: Sequence[Optional[float]]) -> Optional[float]:
    return safe_min(xs)


def _fmt(v: Optional[float], nd: int) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{nd}f}"


def _context_source_count(cell: Dict[str, Any], source: str) -> int:
    trace = cell.get("trace_summary") or {}
    if not isinstance(trace, dict):
        return 0
    sources = trace.get("context_sources") or {}
    if not isinstance(sources, dict):
        return 0
    raw = sources.get(source, 0)
    return int(raw) if isinstance(raw, (int, float)) else 0


def _lsp_route_context_summary(cell: Dict[str, Any]) -> Dict[str, Any]:
    trace = cell.get("trace_summary") or {}
    if not isinstance(trace, dict):
        return {}
    summary = trace.get("lsp_route_context") or {}
    return dict(summary) if isinstance(summary, dict) else {}


def _lsp_route_tool_call_summaries(cell: Dict[str, Any]) -> List[Dict[str, Any]]:
    trace = cell.get("trace_summary") or {}
    if not isinstance(trace, dict):
        return []
    calls = trace.get("lsp_route_tool_calls") or []
    return [dict(call) for call in calls if isinstance(call, dict)]


def _lsp_route_startup_context_count(cell: Dict[str, Any]) -> int:
    summary = _lsp_route_context_summary(cell)
    return 1 if summary.get("status") == "offered" else 0


def _tool_call_count(cell: Dict[str, Any], skill_id: str) -> int:
    return sum(
        1 for call in (cell.get("tool_calls") or []) if call.get("skill_id") == skill_id
    )


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


def _fold_instance_lsp_feedback(
    reps: Sequence[Dict[str, Any]],
    *,
    lsp_tool: str = "lsp_route",
) -> Dict[str, Any]:
    """Fold one arm/instance's reps into the LSP feedback table shape."""

    if not reps:
        return {}
    tool_counts = [_tool_call_count(r, lsp_tool) for r in reps]
    context_counts = [_lsp_route_startup_context_count(r) for r in reps]
    route_summaries = [_lsp_route_context_summary(r) for r in reps]
    route_summary = next((summary for summary in route_summaries if summary), {})
    dynamic_calls = []
    for rep in reps:
        dynamic_calls.extend(_lsp_route_tool_call_summaries(rep))
    dynamic_durations = [
        call.get("duration_ms")
        for call in dynamic_calls
        if isinstance(call.get("duration_ms"), (int, float))
    ]
    dynamic_visible_turns = [
        call.get("model_can_use_turn")
        for call in dynamic_calls
        if isinstance(call.get("model_can_use_turn"), (int, float))
    ]
    dynamic_visible_ms = [
        call.get("model_can_use_ms")
        for call in dynamic_calls
        if isinstance(call.get("model_can_use_ms"), (int, float))
    ]
    first_dynamic = next((call for call in dynamic_calls if call), {})
    context_duration = route_summary.get("duration_ms")
    route_status = route_summary.get("status")
    context_visible_turn = route_summary.get("visible_turn")
    if context_visible_turn is None and route_status == "offered":
        context_visible_turn = 0
    context_visible_ms = route_summary.get("route_visible_ms")
    if context_visible_ms is None and route_status == "offered":
        context_visible_ms = context_duration
    return {
        "scenario": reps[0].get("scenario", "unknown"),
        "total_tokens": _safe_min([r.get("total_tokens") for r in reps]),
        "total_turns": _safe_min([r.get("total_turns") for r in reps]),
        "files_at_5": instance_files_at_5(reps),
        "answer_recall_at_5": _safe_mean(
            [_field_at_k(r, "answer_blocks", 5, "recall") for r in reps]
        ),
        "lsp_tool_calls": _safe_mean(tool_counts),
        "lsp_context_entries": _safe_mean(context_counts),
        "lsp_context_status": route_status,
        "lsp_context_reason": route_summary.get("reason"),
        "lsp_route_seeds": route_summary.get("seeds"),
        "lsp_route_preview": route_summary.get("route_preview"),
        "lsp_route_args": route_summary.get("route_args")
        or first_dynamic.get("route_args"),
        "lsp_route_fingerprint": route_summary.get("route_fingerprint")
        or first_dynamic.get("route_fingerprint"),
        "lsp_route_tool_calls": dynamic_calls,
        "lsp_tool_duration_ms": _safe_mean(dynamic_durations),
        "lsp_tool_visible_turn": _safe_min(dynamic_visible_turns),
        "lsp_tool_visible_ms": _safe_mean(dynamic_visible_ms),
        "lsp_context_duration_ms": (
            float(context_duration)
            if isinstance(context_duration, (int, float))
            else None
        ),
        "lsp_context_visible_turn": context_visible_turn,
        "lsp_context_visible_ms": (
            float(context_visible_ms)
            if isinstance(context_visible_ms, (int, float))
            else None
        ),
        "lsp_seed_source": route_summary.get("seed_source"),
        "lsp_touched": any(tool_counts) or any(context_counts),
    }


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
        focused_tool_invoked_cells: "Counter[str]" = Counter()
        focused_tool_total_calls: "Counter[str]" = Counter()
        focused_context_cells: "Counter[str]" = Counter()
        focused_context_entries: "Counter[str]" = Counter()
        focused_touched_cells: "Counter[str]" = Counter()
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
                for tool in FOCUSED_ADOPTION_TOOLS:
                    n_tool_calls = called.get(tool, 0)
                    n_context_entries = (
                        _lsp_route_startup_context_count(r)
                        if tool == "lsp_route"
                        else _context_source_count(r, tool)
                    )
                    if n_tool_calls:
                        focused_tool_invoked_cells[tool] += 1
                        focused_tool_total_calls[tool] += n_tool_calls
                    if n_context_entries:
                        focused_context_cells[tool] += 1
                        focused_context_entries[tool] += n_context_entries
                    if n_tool_calls or n_context_entries:
                        focused_touched_cells[tool] += 1

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
        focused_adoption = {}
        for tool in FOCUSED_ADOPTION_TOOLS:
            focused_adoption[tool] = {
                "tool_invocation_rate": (
                    focused_tool_invoked_cells[tool] / total_cells
                    if total_cells
                    else 0.0
                ),
                "mean_tool_calls_per_cell": (
                    focused_tool_total_calls[tool] / total_cells if total_cells else 0.0
                ),
                "context_offer_rate": (
                    focused_context_cells[tool] / total_cells if total_cells else 0.0
                ),
                "mean_context_entries_per_cell": (
                    focused_context_entries[tool] / total_cells if total_cells else 0.0
                ),
                "touch_rate": (
                    focused_touched_cells[tool] / total_cells if total_cells else 0.0
                ),
            }

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
            "focused_adoption": focused_adoption,
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

    # --- per-instance LSP-route feedback ---
    per_instance_lsp = []
    all_instances = sorted({iid for by_inst in grouped.values() for iid in by_inst})
    for iid in all_instances:
        baseline_fold = (
            _fold_instance_lsp_feedback(grouped[base][iid])
            if base and iid in grouped.get(base, {})
            else {}
        )
        base_turns = baseline_fold.get("total_turns")
        base_tokens = baseline_fold.get("total_tokens")
        for sid in arms:
            reps = grouped[sid].get(iid)
            if not reps:
                continue
            folded = _fold_instance_lsp_feedback(reps)
            turns_value = folded.get("total_turns")
            tokens_value = folded.get("total_tokens")
            folded.update(
                {
                    "instance_id": iid,
                    "arm": sid,
                    "delta_turns_vs_baseline": (
                        turns_value - base_turns
                        if turns_value is not None and base_turns is not None
                        else None
                    ),
                    "delta_tokens_vs_baseline": (
                        tokens_value - base_tokens
                        if tokens_value is not None and base_tokens is not None
                        else None
                    ),
                }
            )
            per_instance_lsp.append(folded)

    return {
        "baseline_arm": base,
        "arms": per_arm,
        "per_scenario": per_scenario,
        "per_instance_lsp": per_instance_lsp,
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

    L.append("## Focused LSP-route adoption")
    L.append("")
    L.append(
        "`tool invoke` measures voluntary dynamic `lsp_route` calls. "
        "`context offered` measures startup `lsp_route` context injected before "
        "turn 1. `touched` is either path. This separates dynamic tool adoption "
        "from preloaded context exposure."
    )
    L.append("")
    head_lsp = [
        "arm",
        "tool invoke",
        "calls/cell",
        "context offered",
        "ctx/cell",
        "touched",
        "turns",
        "tokens",
        "files@5",
        "answer_rec@5",
    ]
    L.append("| " + " | ".join(head_lsp) + " |")
    L.append("| " + " | ".join("---" for _ in head_lsp) + " |")
    for sid in sorted(agg["arms"]):
        m = agg["arms"][sid]
        lsp = (m.get("focused_adoption") or {}).get("lsp_route") or {}
        row_lsp = [
            sid,
            f"{lsp.get('tool_invocation_rate', 0.0):.0%}",
            _fmt(lsp.get("mean_tool_calls_per_cell"), 2),
            f"{lsp.get('context_offer_rate', 0.0):.0%}",
            _fmt(lsp.get("mean_context_entries_per_cell"), 2),
            f"{lsp.get('touch_rate', 0.0):.0%}",
            _fmt(m["mean_total_turns"], 1),
            _fmt(m["mean_total_tokens"], 0),
            _fmt((m.get("files_at_k") or {}).get(5), 3),
            _fmt((m.get("answer_recall_at_k") or {}).get(5), 3),
        ]
        L.append("| " + " | ".join(row_lsp) + " |")
    L.append("")

    L.append("## Per-instance LSP-route feedback")
    L.append("")
    L.append(
        "Rows are folded by instance and arm. `dturns` and `dtokens` are relative "
        "to the selected baseline arm for that same instance, so this table "
        "separates real LSP exposure from coincidental per-case wins."
    )
    L.append("")
    head_inst = [
        "instance",
        "arm",
        "scenario",
        "lsp calls",
        "lsp ctx",
        "tool ms",
        "ctx ms",
        "tool vis turn",
        "ctx vis turn",
        "tool vis ms",
        "ctx vis ms",
        "route hash",
        "lsp status",
        "lsp reason",
        "seed src",
        "touched",
        "turns",
        "dturns",
        "tokens",
        "dtokens",
        "files@5",
        "answer_rec@5",
    ]
    L.append("| " + " | ".join(head_inst) + " |")
    L.append("| " + " | ".join("---" for _ in head_inst) + " |")
    for row in agg.get("per_instance_lsp") or []:
        row_inst = [
            row.get("instance_id", "unknown"),
            row.get("arm", "unknown"),
            row.get("scenario", "unknown"),
            _fmt(row.get("lsp_tool_calls"), 2),
            _fmt(row.get("lsp_context_entries"), 2),
            _fmt(row.get("lsp_tool_duration_ms"), 1),
            _fmt(row.get("lsp_context_duration_ms"), 1),
            _fmt(row.get("lsp_tool_visible_turn"), 1),
            _fmt(row.get("lsp_context_visible_turn"), 1),
            _fmt(row.get("lsp_tool_visible_ms"), 1),
            _fmt(row.get("lsp_context_visible_ms"), 1),
            str(row.get("lsp_route_fingerprint") or "n/a")[:12],
            str(row.get("lsp_context_status") or "n/a"),
            str(row.get("lsp_context_reason") or "n/a"),
            str(row.get("lsp_seed_source") or "n/a"),
            "yes" if row.get("lsp_touched") else "no",
            _fmt(row.get("total_turns"), 1),
            _fmt(row.get("delta_turns_vs_baseline"), 1),
            _fmt(row.get("total_tokens"), 0),
            _fmt(row.get("delta_tokens_vs_baseline"), 0),
            _fmt(row.get("files_at_5"), 3),
            _fmt(row.get("answer_recall_at_5"), 3),
        ]
        L.append("| " + " | ".join(row_inst) + " |")
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
# File output
# ---------------------------------------------------------------------------


def write_report(
    *,
    cells: Sequence[Dict[str, Any]],
    output_dir: Path,
    metrics_k: Sequence[int],
    max_turns: int,
    baseline: Optional[str] = None,
) -> str:
    """Write ``metrics.json`` and ``report.md`` for cost-arm cells.

    Returns the markdown body so CLI wrappers can print it without duplicating
    rendering or JSON payload logic.
    """

    agg = aggregate(cells, ks=metrics_k, max_turns=max_turns, baseline=baseline)
    front = pareto_front(agg)

    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "metrics.json").open("w", encoding="utf-8") as f:
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
    md = render_markdown(agg, front, metrics_k)
    (output_dir / "report.md").write_text(md, encoding="utf-8")
    return md


__all__ = [
    "ALWAYS_ON_SKILLS",
    "EASY_FILES_AT_5",
    "FOCUSED_ADOPTION_TOOLS",
    "aggregate",
    "group",
    "instance_files_at_5",
    "load_cells",
    "pareto_front",
    "render_markdown",
    "write_report",
]
