# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fair retrieval comparison: flat vs IVF vs graph-scoped (#133, #25).

Our vector store is ``IndexFlatIP`` — exhaustive, exact, O(N). To judge whether
the call-graph adds value to *retrieval* fairly, you must control for the index:
an IVF (ANN) index speeds up flat search with NO graph at all, so any "graph is
faster" claim is confounded unless compared against IVF. And both IVF (approx)
and graph-scoping (candidate restriction) change *which* chunks are retrieved,
so both affect accuracy. This harness measures recall@k AND query latency for:

  - flat        : exhaustive IndexFlatIP over all N L2 vectors (exact, current).
  - ivf         : IndexIVFFlat (nlist/nprobe) over the SAME vectors (approx, fast).
  - graph-scoped: flat top-`seeds` -> resolve to graph -> expand_ppr k-hop region
                  -> rank that subgraph (graph restricts the candidate set).

No LLM. Per instance: stage prebuilt indexes, reconstruct vectors from the flat
index (same data for flat+ivf), embed the problem statement once, run all three.

Usage:
    python -m scripts.retrieval_ablation.index_compare \
        --instances-json /tmp/have.json --out results/agent_compile/index_compare.json \
        --k 1 5 10 --nlist 64 --nprobe 8 --seeds 5
"""

from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

import faiss
import numpy as np


def _norm(p: str) -> str:
    return (p or "").strip().strip("`'\"").replace("\\", "/")


def _covers(cands: List[str], target: str) -> bool:
    t = _norm(target)
    return any(c == t or c.endswith("/" + t) or t.endswith("/" + c) for c in cands)


def _recall(ranked: List[str], targets: List[str], ks: List[int]) -> Dict[str, bool]:
    out = {}
    for k in ks:
        top = [c for c in ranked[:k] if c]
        out[f"files@{k}"] = bool(targets) and all(_covers(top, t) for t in targets)
    return out


def _files(docs: List[Any], idxs: List[int]) -> List[str]:
    out = []
    for i in idxs:
        if 0 <= i < len(docs):
            out.append(_norm(docs[i].metadata.get("file", "")))
    return out


def _analyze(inst, cfg, prebuilt_dir, cache_root, ks, nlist, nprobe, seeds):
    from codeminer.eval.agent_runner.prebuilt import stage_prebuilt_indexes
    from codeminer.eval.retrieval_eval import collect_targets
    from codeminer.graph.roi_subgraph import ROISubgraph
    from scripts.agent_compile.lib.harness import load_dataset_rows, load_full_contexts

    rows_by_id, eval_lookup = load_dataset_rows(cfg)
    row = rows_by_id[inst]
    query = row["problem_statement"]
    targets = [
        _norm(t) for t in collect_targets(eval_lookup.get(inst, row), True)[0] if t
    ]
    cache_dir = os.path.join(cache_root, inst)
    repo = stage_prebuilt_indexes(prebuilt_dir, inst, cache_dir)
    ctx = load_full_contexts(cfg, repo, cache_dir)
    vs = ctx["retrieve"].vector_store
    graph = ctx["expand"].code_graph
    index, docs = vs._get_index_and_docs("l2")
    n, dim = index.ntotal, index.d
    topn = max(ks)
    qv = np.asarray(vs.embedding.embed_query(query), dtype="float32").reshape(1, -1)

    # FLAT (exact)
    t = time.perf_counter()
    _, fi = index.search(qv, topn)
    flat_ms = (time.perf_counter() - t) * 1000
    flat_files = _files(docs, list(fi[0]))
    flat_names = [docs[i].metadata.get("name", "") for i in fi[0] if 0 <= i < len(docs)]

    # IVF (approx) over the same reconstructed vectors
    xb = index.reconstruct_n(0, n)
    nl = max(1, min(nlist, n // 39))
    ivf = faiss.IndexIVFFlat(
        faiss.IndexFlatIP(dim), dim, nl, faiss.METRIC_INNER_PRODUCT
    )
    ivf.train(xb)
    ivf.add(xb)
    ivf.nprobe = min(nprobe, nl)
    t = time.perf_counter()
    _, ii = ivf.search(qv, topn)
    ivf_ms = (time.perf_counter() - t) * 1000
    ivf_files = _files(docs, list(ii[0]))

    # GRAPH-SCOPED: resolve flat-top-`seeds` names -> PPR subgraph region
    roi = ROISubgraph(graph)
    seed_canon = []
    for nm in flat_names[:seeds]:
        c, _ = graph.resolve_symbol(nm)
        if c:
            seed_canon.append(c)
    t = time.perf_counter()
    nodes = roi.expand_ppr(seed_canon, top_k=topn) if seed_canon else []
    scoped_ms = (time.perf_counter() - t) * 1000
    scoped_files = [_norm(getattr(nd, "file", "") or "") for nd in nodes]

    return {
        "instance_id": inst,
        "language": row.get("language_group"),
        "n_vectors": int(n),
        "n_targets": len(targets),
        "latency_ms": {
            "flat": round(flat_ms, 2),
            "ivf": round(ivf_ms, 2),
            "scoped": round(scoped_ms, 2),
        },
        "recall": {
            "flat": _recall(flat_files, targets, ks),
            "ivf": _recall(ivf_files, targets, ks),
            "scoped": _recall(scoped_files, targets, ks),
        },
        "scoped_resolved_seeds": len(seed_canon),
    }


def main(argv: Optional[List[str]] = None) -> int:
    from scripts.agent_compile.lib.config import SweepConfig as SampleConfig

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instances-json", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--k", type=int, nargs="+", default=[1, 5, 10])
    ap.add_argument("--nlist", type=int, default=64)
    ap.add_argument("--nprobe", type=int, default=8)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--prebuilt-dir", default="/mnt/data/codeminer")
    ap.add_argument("--embedding-model", default="Qwen/Qwen3-Embedding-0.6B")
    ap.add_argument("--embedding-dim", type=int, default=1024)
    args = ap.parse_args(argv)

    instances = json.loads(args.instances_json.read_text())
    cache_root = os.path.join(os.environ.get("CLAUDE_JOB_DIR", "/tmp"), "idxcmp_cache")
    cfg = SampleConfig(
        sweep_id="idxcmp",
        subsets={"CTX": ["codeminer_context"]},
        instances=instances,
        embedding_model=args.embedding_model,
        embedding_dimension=args.embedding_dim,
    )

    results: List[Dict[str, Any]] = []
    for i, inst in enumerate(instances, 1):
        t0 = time.time()
        try:
            r = _analyze(
                inst,
                cfg,
                args.prebuilt_dir,
                cache_root,
                args.k,
                args.nlist,
                args.nprobe,
                args.seeds,
            )
        except Exception as e:  # noqa: BLE001 — keep scanning
            r = {"instance_id": inst, "error": repr(e)}
            traceback.print_exc()
        results.append(r)
        print(
            f"[{i}/{len(instances)}] {inst} {r.get('latency_ms', r.get('error'))} "
            f"({time.time() - t0:.1f}s)",
            flush=True,
        )
        args.out.write_text(json.dumps(results, indent=2))

    ok = [r for r in results if "recall" in r and r.get("n_targets")]
    print(f"\n=== {len(ok)} scored instances ===")
    for method in ("flat", "ivf", "scoped"):
        lat = [r["latency_ms"][method] for r in ok]
        med_lat = sorted(lat)[len(lat) // 2] if lat else 0.0
        line = f"{method:7}: median query {med_lat:.2f} ms"
        for k in args.k:
            hits = sum(r["recall"][method][f"files@{k}"] for r in ok)
            line += f" | files@{k} {hits}/{len(ok)} ({100 * hits / len(ok):.0f}%)"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
