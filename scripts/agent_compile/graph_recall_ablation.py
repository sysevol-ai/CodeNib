# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Retrieval-level graph-value ablation (#133): where does the call graph reach
a target that search alone cannot?

For each instance, with NO agent/LLM (cheap, scannable over the whole dataset):

  - deep-search files = files of bm25.search(K) ∪ embedding.search(K), the best
    plain retrieval can do at budget K,
  - composer files = files returned by ``codeminer_context`` (5 search seeds +
    call-graph expansion), the graph-aware set,

and asks the one question that isolates the graph's UNIQUE value:

  does the composer surface a target file that deep search at the same budget
  does NOT? (``graph_recovers``) — i.e. a target reachable only by traversing a
  caller/callee edge from a search hit, not by retrieving more search results.

This is the honest way to find the regime where the graph helps without
overselling it: report the fraction of instances (per language) where graph
adds a target deep search misses, and list them so we can confirm end-to-end.

Usage:
    python -m scripts.agent_compile.graph_recall_ablation \
        --instances-json /tmp/have_instances.json \
        --out results/agent_compile/graph_recall.json --budget 30
"""

from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


def _norm(p: str) -> str:
    return (p or "").strip().strip("`'\"").replace("\\", "/")


def _files_of(nodes: Sequence[Any]) -> List[str]:
    """Raw (slash-normalized) candidate file paths.

    Kept un-rel-normalized on purpose: the embedding store carries ABSOLUTE
    paths rooted at the build location (``~/.codeminer/<repo>/...``), not the
    staged repo_path, so ``relpath`` can't strip them. Coverage is decided by
    suffix matching (``_covers``), which is robust to that.
    """
    return [_norm(getattr(n, "file", "")) for n in nodes if getattr(n, "file", None)]


def _covers(cands: Sequence[str], target: str) -> bool:
    """True if some candidate path is/ends-with the repo-relative *target*."""
    t = _norm(target)
    return any(c == t or c.endswith("/" + t) or t.endswith("/" + c) for c in cands)


def _deep_search_files(retrieve: Any, query: str, budget: int) -> List[str]:
    from codeminer.ops.retrieve import to_queried_nodes

    nodes: List[Any] = []
    if getattr(retrieve, "bm25", None) is not None:
        nodes += to_queried_nodes(
            retrieve.bm25.search(
                query=query,
                top_k=budget,
                return_code_content=False,
                wrap_with_ln=False,
                filter_test=True,
            )
        )
    if getattr(retrieve, "vector_store", None) is not None:
        level = getattr(retrieve, "default_level", "l2")
        nodes += to_queried_nodes(
            retrieve.vector_store.search(query=query, top_k=budget, level=level)
        )
    return _files_of(nodes)


def _analyze_instance(
    instance_id: str,
    cfg: Any,
    prebuilt_dir: str,
    cache_root: str,
    budget: int,
) -> Dict[str, Any]:
    from codeminer.agent.skills.registry import SkillRegistry
    from codeminer.eval.retrieval_eval import collect_targets
    from scripts.agent_compile.prebuilt import repo_path_for, stage_prebuilt_indexes
    from scripts.agent_compile.run_agent_sweep import (
        _load_dataset_rows,
        _load_full_contexts,
    )

    rows_by_id, eval_lookup = _load_dataset_rows(cfg)
    row = rows_by_id[instance_id]
    query = row["problem_statement"]
    meta = eval_lookup.get(instance_id, row)
    target_files_raw, _ = collect_targets(meta, simplified_symbols=True)
    targets = [_norm(t) for t in target_files_raw if t]

    repo_path = repo_path_for(prebuilt_dir, instance_id)
    cache_dir = os.path.join(cache_root, instance_id)
    stage_prebuilt_indexes(prebuilt_dir, instance_id, cache_dir)
    contexts = _load_full_contexts(cfg, repo_path, cache_dir)
    retrieve = contexts.get("retrieve")

    deep = _deep_search_files(retrieve, query, budget)
    ex = SkillRegistry().get("codeminer_context").executor_fn
    comp = _files_of(ex(query=query, seeds=5, max_results=budget))

    missed_by_search = [t for t in targets if not _covers(deep, t)]
    recovered_by_graph = [t for t in missed_by_search if _covers(comp, t)]
    return {
        "instance_id": instance_id,
        "language": row.get("language_group"),
        "n_targets": len(targets),
        "search_hit": bool(targets) and all(_covers(deep, t) for t in targets),
        "composer_hit": bool(targets) and all(_covers(comp, t) for t in targets),
        "missed_by_search": missed_by_search,
        "graph_recovers": recovered_by_graph,
        "graph_helps": bool(recovered_by_graph),
    }


def main(argv: Optional[List[str]] = None) -> int:
    from scripts.agent_compile.run_agent_sweep import SampleConfig

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instances-json", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--budget", type=int, default=30)
    ap.add_argument("--prebuilt-dir", default="/mnt/data/codeminer")
    args = ap.parse_args(argv)

    instances = json.loads(args.instances_json.read_text())
    cache_root = os.path.join(os.environ.get("CLAUDE_JOB_DIR", "/tmp"), "recall_cache")
    cfg = SampleConfig(
        sweep_id="recall",
        subsets={"CTX": ["codeminer_context"]},
        instances=instances,
        embedding_model="Qwen/Qwen3-Embedding-0.6B",
        embedding_dimension=1024,
    )

    results: List[Dict[str, Any]] = []
    for i, inst in enumerate(instances, 1):
        t0 = time.time()
        try:
            r = _analyze_instance(inst, cfg, args.prebuilt_dir, cache_root, args.budget)
        except Exception as e:  # noqa: BLE001 — keep scanning; record the failure
            r = {"instance_id": inst, "error": repr(e)}
            traceback.print_exc()
        results.append(r)
        flag = "GRAPH+" if r.get("graph_helps") else ("err" if "error" in r else "")
        print(
            f"[{i}/{len(instances)}] {inst} {r.get('language', '')} "
            f"search_hit={r.get('search_hit')} graph_recovers={r.get('graph_recovers')} "
            f"{flag} ({time.time() - t0:.1f}s)",
            flush=True,
        )
        args.out.write_text(json.dumps(results, indent=2))  # checkpoint each step

    # Summary
    ok = [r for r in results if "error" not in r and r.get("n_targets")]
    by_lang_total: Counter = Counter()
    by_lang_graph: Counter = Counter()
    by_lang_searchmiss: Counter = Counter()
    for r in ok:
        lang = r["language"]
        by_lang_total[lang] += 1
        if not r["search_hit"]:
            by_lang_searchmiss[lang] += 1
        if r["graph_helps"]:
            by_lang_graph[lang] += 1
    print(
        "\n=== graph-value summary (instances where graph recovers a search miss) ==="
    )
    for lang in sorted(by_lang_total):
        print(
            f"{lang:24s} graph_helps {by_lang_graph[lang]:2d} / "
            f"search_miss {by_lang_searchmiss[lang]:2d} / total {by_lang_total[lang]:2d}"
        )
    helps = [r["instance_id"] for r in ok if r["graph_helps"]]
    print(f"\nTOTAL graph_helps: {len(helps)}/{len(ok)} scored instances")
    print("instances where graph recovers a target search misses:")
    for r in ok:
        if r["graph_helps"]:
            print(f"  {r['instance_id']} ({r['language']}): {r['graph_recovers']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
