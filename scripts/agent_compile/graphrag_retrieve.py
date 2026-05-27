# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""GraphRAG retrieval pipeline — run as a SCRIPT, not an agent tool (#133).

The ``codeminer_context`` composer (bm25 ⊕ embedding seeds → call-graph
expansion → assembled context) is GraphRAG over a code call-graph. The agent
ablation showed it does not earn its keep as an in-loop agent tool (additive
overhead). Its honest home is here: a deterministic RETRIEVAL method, evaluated
as a retriever (files@k recall) alongside the other retrieval baselines — not in
the agent baseline.

This runner loads an instance's prebuilt indexes, runs the pipeline once, and
reports the ranked candidate files + files@k recall vs ground truth. Use
``--no-graph`` to compare against search-only (isolates the graph's recall
contribution, same as the agent-side CODEMINER_COMPOSER_NO_GRAPH ablation but
with no LLM in the loop).

Usage:
    python -m scripts.agent_compile.graphrag_retrieve \
        --instances-json /tmp/have_instances.json \
        --out results/agent_compile/graphrag_retrieval.json --k 1 5 10
    # search-only baseline:
    python -m scripts.agent_compile.graphrag_retrieve ... --no-graph
"""

from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional


def _norm(p: str) -> str:
    return (p or "").strip().strip("`'\"").replace("\\", "/")


def _covers(cands: List[str], target: str) -> bool:
    t = _norm(target)
    return any(c == t or c.endswith("/" + t) or t.endswith("/" + c) for c in cands)


def _retrieve(instance_id: str, cfg: Any, prebuilt_dir: str, cache_root: str):
    """Run the GraphRAG composer once; return (ranked_files, target_files)."""
    from codeminer.agent.skills.registry import SkillRegistry
    from codeminer.eval.retrieval_eval import collect_targets
    from scripts.agent_compile.prebuilt import stage_prebuilt_indexes
    from scripts.agent_compile.run_agent_sweep import (
        _load_dataset_rows,
        _load_full_contexts,
    )

    rows_by_id, eval_lookup = _load_dataset_rows(cfg)
    row = rows_by_id[instance_id]
    query = row["problem_statement"]
    meta = eval_lookup.get(instance_id, row)
    target_files, _ = collect_targets(meta, simplified_symbols=True)

    cache_dir = os.path.join(cache_root, instance_id)
    repo_path = stage_prebuilt_indexes(prebuilt_dir, instance_id, cache_dir)
    _load_full_contexts(cfg, repo_path, cache_dir)
    compose = SkillRegistry().get("codeminer_context").executor_fn
    nodes = compose(query=query, seeds=5, max_results=30)
    ranked_files: List[str] = []
    for n in nodes:  # preserve composer order; dedup
        f = _norm(getattr(n, "file", "") or "")
        if f and f not in ranked_files:
            ranked_files.append(f)
    return (
        ranked_files,
        [_norm(t) for t in target_files if t],
        row.get("language_group"),
    )


def main(argv: Optional[List[str]] = None) -> int:
    from scripts.agent_compile.run_agent_sweep import SampleConfig

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instances-json", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--k", type=int, nargs="+", default=[1, 5, 10])
    ap.add_argument("--prebuilt-dir", default="/mnt/data/codeminer")
    ap.add_argument(
        "--no-graph",
        action="store_true",
        help="search-only (sets CODEMINER_COMPOSER_NO_GRAPH=1 for this process).",
    )
    args = ap.parse_args(argv)

    if args.no_graph:
        os.environ["CODEMINER_COMPOSER_NO_GRAPH"] = "1"

    instances = json.loads(args.instances_json.read_text())
    cache_root = os.path.join(
        os.environ.get("CLAUDE_JOB_DIR", "/tmp"), "graphrag_cache"
    )
    cfg = SampleConfig(
        sweep_id="graphrag",
        subsets={"CTX": ["codeminer_context"]},
        instances=instances,
        embedding_model="Qwen/Qwen3-Embedding-0.6B",
        embedding_dimension=1024,
    )

    results: List[Dict[str, Any]] = []
    hit_at: Counter = Counter()
    scored = 0
    for i, inst in enumerate(instances, 1):
        t0 = time.time()
        try:
            ranked, targets, lang = _retrieve(inst, cfg, args.prebuilt_dir, cache_root)
            rec = {
                f"files@{k}": (
                    bool(targets) and all(_covers(ranked[:k], t) for t in targets)
                )
                for k in args.k
            }
            r = {
                "instance_id": inst,
                "language": lang,
                "n_targets": len(targets),
                "recall": rec,
                "n_ranked": len(ranked),
            }
            if targets:
                scored += 1
                for k in args.k:
                    hit_at[k] += int(rec[f"files@{k}"])
        except Exception as e:  # noqa: BLE001 — keep scanning
            r = {"instance_id": inst, "error": repr(e)}
            traceback.print_exc()
        results.append(r)
        print(
            f"[{i}/{len(instances)}] {inst} {r.get('language', '')} "
            f"recall={r.get('recall')} ({time.time() - t0:.1f}s)",
            flush=True,
        )
        args.out.write_text(json.dumps(results, indent=2))

    mode = "search-only" if args.no_graph else "GraphRAG (search+graph)"
    print(f"\n=== {mode} retrieval recall over {scored} scored instances ===")
    for k in args.k:
        pct = 100.0 * hit_at[k] / scored if scored else 0.0
        print(f"  files@{k}: {hit_at[k]}/{scored} = {pct:.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
