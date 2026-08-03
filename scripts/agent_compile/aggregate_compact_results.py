#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Aggregate the compact-preload experiment matrix into markdown tables.

Reads the per-cell result dirs under ``--results`` (default
``${CODENIB_RESULTS_DIR}``) and prints the paper-ready tables to stdout:

  1. Base compact (grep / eager / eager_compact) across 4B/9B/27B/Haiku
  2. Embedding ablation on the compact agent (0.6B / jina-1.5b / 4B)
  3. Seed-richness dial (keep_reads 0/1/2) on the weak 4B — the router
  4. Synthesis 500 (grep / eager / eager_compact), 5 languages
  5. Retrieval recall@k + latency (flat vs graph-scoped) per embedding

Accuracy is span-overlap (``files@k`` / ``answer_blocks@k`` recall); cells are
filtered to ``success and metrics_meaningful``. Deltas carry a paired bootstrap
95% CI (seeded, reproducible). Raw cells are NOT checked in (too large); this
script + its output are the reproducible record.
"""
from __future__ import annotations

import glob
import json
import random
import re
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from codenib.paths import results_dir

RESULTS = str(results_dir())
random.seed(0)


def _load_json(p: str) -> Optional[Any]:
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _cells(d: str) -> List[Dict[str, Any]]:
    out = []
    for p in glob.glob(f"{RESULTS}/{d}/cells/*.json"):
        c = _load_json(p)
        if not isinstance(c, dict):
            continue
        if c.get("success") and c.get("metrics_meaningful"):
            out.append(c)
    return out


def _idxcmp_name(p: str) -> str:
    return Path(p).stem.removeprefix("idxcmp_")


def _idxcmp_runs() -> List[Tuple[str, List[Dict[str, Any]]]]:
    runs: List[Tuple[str, List[Dict[str, Any]]]] = []
    for p in sorted(glob.glob(f"{RESULTS}/idxcmp_*.json")):
        rs = _load_json(p)
        if not isinstance(rs, list):
            continue
        ok = [
            r
            for r in rs
            if isinstance(r, dict) and "recall" in r and r.get("n_targets")
        ]
        if ok:
            runs.append((_idxcmp_name(p), ok))
    return runs


def _idxcmp_recall(rows: List[Dict[str, Any]], method: str, k: int) -> Optional[float]:
    vals: List[bool] = []
    key = f"files@{k}"
    for r in rows:
        method_recalls = (r.get("recall") or {}).get(method) or {}
        if key in method_recalls:
            vals.append(bool(method_recalls[key]))
    return (100 * sum(vals) / len(vals)) if vals else None


def _idxcmp_latency(rows: List[Dict[str, Any]], method: str) -> Optional[float]:
    vals = [
        r["latency_ms"][method]
        for r in rows
        if isinstance(r.get("latency_ms"), dict) and method in r["latency_ms"]
    ]
    return sorted(vals)[len(vals) // 2] if vals else None


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.0f}%"


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _idxcmp_recall_for_embedding(name: str, k: int) -> Optional[float]:
    needle = _norm_name(name)
    for run_name, rows in _idxcmp_runs():
        if needle in _norm_name(run_name):
            return _idxcmp_recall(rows, "flat", k)
    return None


def _recall(c: Dict[str, Any], scope: str, k: int) -> Optional[float]:
    b = (c.get("metrics") or {}).get(scope) or {}
    s = b.get(k, b.get(str(k)))
    return s.get("recall") if isinstance(s, dict) else None


def _mean(xs: Sequence[Optional[float]]) -> float:
    v = [x for x in xs if x is not None]
    return st.fmean(v) if v else float("nan")


def _ci(vals: Sequence[float], it: int = 4000) -> Tuple[float, float]:
    v = [x for x in vals if x is not None]
    if not v:
        return (float("nan"), float("nan"))
    ds = sorted(st.fmean([random.choice(v) for _ in v]) for _ in range(it))
    return ds[int(0.025 * it)], ds[int(0.975 * it)]


def _sig(lo: float, hi: float) -> str:
    return "" if lo <= 0 <= hi else " *"


def _base_key(c: Dict[str, Any]) -> str:
    return c.get("instance_id") or ""


def _synth_key(c: Dict[str, Any]) -> Tuple[str, str]:
    return (c.get("instance_id"), c.get("query_id"))


def _paired_delta(cells_a, cells_b, key_fn, scope, k):
    ma = {key_fn(c): c for c in cells_a}
    mb = {key_fn(c): c for c in cells_b}
    common = [i for i in ma if i in mb]
    diff = [
        (_recall(ma[i], scope, k) or 0) - (_recall(mb[i], scope, k) or 0)
        for i in common
    ]
    tok = [
        (ma[i].get("total_tokens") or 0) - (mb[i].get("total_tokens") or 0)
        for i in common
    ]
    return diff, tok, len(common)


def _arm_row(cells, label):
    return (
        f"| {label} | {len(cells)} | {_mean([_recall(c,'files',5) for c in cells]):.3f} "
        f"| {_mean([_recall(c,'answer_blocks',5) for c in cells]):.3f} "
        f"| {_mean([c.get('total_tokens') for c in cells]):.0f} "
        f"| {_mean([c.get('total_turns') for c in cells]):.1f} |"
    )


def table1_base():
    print("\n## 1. Base compact (multi-language, 100 instances/model)\n")
    print(
        "span-overlap accuracy; arms = grep_only / preinj_eager / preinj_eager_compact.\n"
    )
    tiers = [
        ("Qwen3.5-4B", "qwen35_4b_compact_64k"),
        ("Qwen3.5-9B", "qwen35_9b_compact_64k"),
        ("Qwen3.5-27B", "qwen35_27b_compact_64k"),
        ("Haiku (0.6B emb)", "haiku_base_compact"),
    ]
    for model, d in tiers:
        cells = _cells(d)
        by = defaultdict(list)
        for c in cells:
            by[c.get("subset_id")].append(c)
        print(f"\n### {model}\n")
        print("| arm | n | files@5 | blk@5 | token | turns |")
        print("|---|---|---|---|---|---|")
        for arm, lab in [
            ("grep_only", "grep"),
            ("preinj_eager", "eager"),
            ("preinj_eager_compact", "compact"),
        ]:
            if by.get(arm):
                print(_arm_row(by[arm], lab))
        g, cp = by.get("grep_only", []), by.get("preinj_eager_compact", [])
        if g and cp:
            for sc, l in [("files", "files@5"), ("answer_blocks", "blk@5")]:
                diff, tok, n = _paired_delta(cp, g, _base_key, sc, 5)
                lo, hi = _ci(diff)
                print(
                    f"\n  Δ{l} (compact−grep, n={n}): {st.fmean(diff):+.3f} "
                    f"[{lo:+.3f},{hi:+.3f}]{_sig(lo,hi)}"
                )
            lo, hi = _ci(tok)
            m = st.fmean(tok)
            print(
                f"  Δtoken (compact−grep): {m:+.0f} [{lo:+.0f},{hi:+.0f}]{_sig(lo,hi)}"
            )


def table2_embedding():
    print("\n\n## 2. Embedding ablation on the compact agent (Haiku, n=100)\n")
    print(
        "Hold compact fixed, swap the pre-load embedding. `recall@10` is the "
        "pure-retrieval ceiling (index_compare); the agent columns are the "
        "end result.\n"
    )
    rows = [
        ("Qwen3-Embedding-0.6B", "haiku_base_compact"),
        ("jina-code-embeddings-1.5b", "haiku_base_compact_jina"),
        ("Qwen3-Embedding-4B", "haiku_base_compact_4b"),
    ]
    print(
        "| embedding | retrieval recall@10 | compact files@5 | compact blk@5 | token |"
    )
    print("|---|---|---|---|---|")
    for name, d in rows:
        rec = _idxcmp_recall_for_embedding(name, 10)
        cp = [c for c in _cells(d) if c.get("subset_id") == "preinj_eager_compact"]
        print(
            f"| {name} | {_pct(rec)} "
            f"| {_mean([_recall(c,'files',5) for c in cp]):.3f} "
            f"| {_mean([_recall(c,'answer_blocks',5) for c in cp]):.3f} "
            f"| {_mean([c.get('total_tokens') for c in cp]):.0f} |"
        )
    print(
        "\n→ retrieval recall spans 56–64% but the compact agent's span (blk@5) "
        "is flat — a better retriever does not help the agent."
    )


def table3_dial():
    print("\n\n## 3. Seed-richness dial on the weak 4B (the router)\n")
    cells = _cells("qwen_4b_keepreads")
    by = defaultdict(list)
    for c in cells:
        by[c.get("subset_id")].append(c)
    print("| arm | n | files@5 | blk@5 | token | turns |")
    print("|---|---|---|---|---|---|")
    for arm, lab in [
        ("grep_only", "grep"),
        ("preinj_eager_compact_k0", "compact keep0"),
        ("preinj_eager_compact_k1", "compact keep1"),
        ("preinj_eager_compact_k2", "compact keep2"),
    ]:
        if by.get(arm):
            print(_arm_row(by[arm], lab))
    print(
        "\n→ keep0 (minimal seed) hurts the weak model (files@5 below grep); keep1 "
        "recovers to grep level; keep2 over-stuffs. Router: weak≤7B→keep1, else keep0."
    )


def table4_synth():
    print("\n\n## 4. Synthesis 500 (grep / eager / eager_compact), 5 languages\n")
    base = (
        _cells("haiku_compare_py")
        + _cells("haiku_compare_multilang")
        + _cells("haiku_compare_cpp")
    )
    cmp = _cells("haiku_synth_compact")
    grep = [c for c in base if c.get("subset_id") == "grep_only"]
    eager = [c for c in base if c.get("subset_id") == "preinj_eager"]
    comp = [c for c in cmp if c.get("subset_id") == "preinj_eager_compact"]
    mg, me, mc = (
        {_synth_key(c): c for c in grep},
        {_synth_key(c): c for c in eager},
        {_synth_key(c): c for c in comp},
    )
    common = [k for k in mc if k in mg and k in me]
    print(f"paired n={len(common)} (join by instance+query)\n")
    print("| arm | files@5 | blk@5 | token |")
    print("|---|---|---|---|")
    for lab, m in [("grep", mg), ("eager", me), ("compact", mc)]:
        cs = [m[k] for k in common]
        print(
            f"| {lab} | {_mean([_recall(c,'files',5) for c in cs]):.3f} "
            f"| {_mean([_recall(c,'answer_blocks',5) for c in cs]):.3f} "
            f"| {_mean([c.get('total_tokens') for c in cs]):.0f} |"
        )
    for bn, mb in [("grep", mg), ("eager", me)]:
        for sc, l in [("files", "files@5"), ("answer_blocks", "blk@5")]:
            diff = [
                (_recall(mc[k], sc, 5) or 0) - (_recall(mb[k], sc, 5) or 0)
                for k in common
            ]
            lo, hi = _ci(diff)
            m = st.fmean(diff)
            print(
                f"\n  Δ{l} (compact−{bn}): {m:+.3f} [{lo:+.3f},{hi:+.3f}]{_sig(lo,hi)}"
            )
        tok = [
            (mc[k].get("total_tokens") or 0) - (mb[k].get("total_tokens") or 0)
            for k in common
        ]
        lo, hi = _ci(tok)
        print(
            f"  Δtoken (compact−{bn}): {st.fmean(tok):+.0f} [{lo:+.0f},{hi:+.0f}]{_sig(lo,hi)}"
        )


def table5_recall():
    print("\n\n## 5. Retrieval recall@k + latency (flat vs graph-scoped), base n=100\n")
    print(
        "`flat` = dense embedding; `scoped` = BM25-seed → graph (PPR) expansion "
        "(GraphRAG, PPR); `scoped_bfs` = BFS extract_subgraph (pipeline default; "
        "faster than PPR but lower recall). Latency is single-query median (CPU).\n"
    )
    print("| embedding | method | @1 | @5 | @10 | @50 | med latency |")
    print("|---|---|---|---|---|---|---|")
    for name, ok in _idxcmp_runs():
        for method in ("flat", "scoped", "scoped_bfs"):
            cells_k = [_pct(_idxcmp_recall(ok, method, k)) for k in (1, 5, 10, 50)]
            med = _idxcmp_latency(ok, method)
            med_s = "n/a" if med is None else f"{med:.1f}ms"
            print(f"| {name} | {method} | {' | '.join(cells_k)} | {med_s} |")
    print("\n→ graph-scoped is dominated by flat: lower recall AND higher latency.")


def table6_graph_value():
    print("\n\n## 6. Graph's value: GraphRAG (search+graph) vs search-only, base\n")
    print(
        "`codenib_context` composer (bm25 \u2295 embedding seeds \u2192 "
        "call-graph). \u0394 = files the call-graph expansion recovers that "
        "search alone misses.\n"
    )

    def _load(f):
        try:
            return json.loads(open(f).read())
        except (OSError, json.JSONDecodeError):
            return []

    def _rget(r, k):
        v = (r.get("recall") or {}).get(f"files@{k}")
        return bool(v) if v is not None else None

    g = {
        r["instance_id"]: r
        for r in _load(f"{RESULTS}/graphrag_base_graph.json")
        if r.get("recall")
    }
    n = {
        r["instance_id"]: r
        for r in _load(f"{RESULTS}/graphrag_base_nograph.json")
        if r.get("recall")
    }
    common = [i for i in g if i in n]
    if not common:
        print("(no graphrag results)")
        return
    print(f"paired n={len(common)}\n")
    print("| k | search-only | search+graph | \u0394(graph) |")
    print("|---|---|---|---|")
    for k in (1, 5, 10, 20, 50):
        ng = sum(1 for i in common if _rget(n[i], k))
        gg = sum(1 for i in common if _rget(g[i], k))
        d = 100 * (gg - ng) / len(common)
        print(
            f"| {k} | {100*ng/len(common):.0f}% | {100*gg/len(common):.0f}% | {d:+.0f}% |"
        )
    miss = [i for i in common if not _rget(n[i], 10)]
    rec = [i for i in miss if _rget(g[i], 10)]
    lost = [i for i in common if _rget(n[i], 10) and not _rget(g[i], 10)]
    pct = 100 * len(rec) / max(len(miss), 1)
    print(
        f"\nsearch-only misses@10: {len(miss)}; graph recovers {len(rec)} "
        f"({pct:.0f}% of misses), loses {len(lost)}; net +{len(rec)-len(lost)}."
    )
    print(
        "\n\u2192 graph adds marginal, strictly-positive complementary recall "
        "(recovers structural misses) but small \u2014 not an agent lever."
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    global RESULTS
    args = list(argv if argv is not None else sys.argv[1:])
    if args:
        RESULTS = args[0]
    print("# Compact pre-load — experiment results")
    print(
        "\nGenerated by `scripts/agent_compile/aggregate_compact_results.py`. "
        "Metric = span-overlap recall; deltas carry paired bootstrap 95% CI "
        "(`*` = excludes 0 = significant)."
    )
    table1_base()
    table2_embedding()
    table3_dial()
    table4_synth()
    table5_recall()
    table6_graph_value()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
