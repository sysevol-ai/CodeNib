# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""GraphRAG retrieval pipeline — run as a SCRIPT, not an agent tool (#133).

The ``codenib_context`` composer (bm25 + embedding seeds -> call-graph
expansion → assembled context) is GraphRAG over a code call-graph. The agent
ablation showed it does not earn its keep as an in-loop agent tool (additive
overhead). Its honest home is here: a deterministic RETRIEVAL method, evaluated
as a retriever (files/symbols/blocks@k) alongside the other retrieval baselines — not in
the agent baseline.

This runner loads an instance's prebuilt indexes, runs the pipeline once, and
reports ranked retrieval metrics vs ground truth. Use
``--no-graph`` to compare against search-only (isolates the graph's recall
contribution, same as the agent-side CODENIB_COMPOSER_NO_GRAPH ablation but
with no LLM in the loop).

Usage:
    python -m scripts.retrieval_ablation.graphrag_retrieve \
        --instances-json /tmp/have_instances.json \
        --out results/agent_compile/graphrag_retrieval.json --k 1 5 10
    # search-only baseline:
    python -m scripts.retrieval_ablation.graphrag_retrieve ... --no-graph
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

from codenib.paths import prebuilt_data_dir


def _norm(p: str) -> str:
    return (p or "").strip().strip("`'\"").replace("\\", "/")


def _covers(cands: List[str], target: str) -> bool:
    t = _norm(target)
    return any(c == t or c.endswith("/" + t) or t.endswith("/" + c) for c in cands)


def _score_ranked(nodes: List[Any]) -> List[Any]:
    """Score-desc retrieval order, matching the span scorer."""
    return sorted(nodes, key=lambda n: getattr(n, "score", 0.0) or 0.0, reverse=True)


def _actionable_nodes(nodes: List[Any]) -> List[Any]:
    """Nodes with concrete line spans, matching what block metrics can score."""
    out: List[Any] = []
    for node in nodes:
        if getattr(node, "start_line", None) is None:
            continue
        if getattr(node, "end_line", None) is None:
            continue
        out.append(node)
    return out


def _node_payload(nodes: List[Any], limit: int = 30) -> List[Dict[str, Any]]:
    payload: List[Dict[str, Any]] = []
    for rank, node in enumerate(nodes[:limit], 1):
        payload.append(
            {
                "rank": rank,
                "node_id": getattr(node, "node_id", None),
                "node_name": getattr(node, "node_name", None),
                "type": getattr(node, "type", None),
                "file": getattr(node, "file", None),
                "start_line": getattr(node, "start_line", None),
                "end_line": getattr(node, "end_line", None),
                "score": getattr(node, "score", None),
            }
        )
    return payload


def _retrieve(instance_id: str, cfg: Any, prebuilt_dir: str, cache_root: str):
    """Run the GraphRAG composer once; return nodes + target metadata."""
    from codenib.agent.skills.registry import SkillRegistry
    from codenib.eval.agent_runner.prebuilt import repo_path_for, stage_prebuilt_indexes
    from codenib.eval.agent_runner.sweep import load_dataset_rows, load_full_contexts
    from codenib.eval.retrieval_eval import collect_target_blocks, collect_targets

    rows_by_id, eval_lookup = load_dataset_rows(cfg)
    row = rows_by_id[instance_id]
    query = row["problem_statement"]
    meta = eval_lookup.get(instance_id, row)
    target_files, target_symbols = collect_targets(meta, simplified_symbols=True)
    target_blocks = collect_target_blocks(meta)

    cache_dir = os.path.join(cache_root, instance_id)
    repo_path = repo_path_for(prebuilt_dir, instance_id)
    stage_prebuilt_indexes(prebuilt_dir, instance_id, cache_dir)
    load_full_contexts(cfg, repo_path, cache_dir)
    compose = SkillRegistry().get("codenib_context").executor_fn
    nodes = compose(query=query, seeds=5, max_results=30)
    ranked_files: List[str] = []
    for n in nodes:  # preserve composer order; dedup
        f = _norm(getattr(n, "file", "") or "")
        if f and f not in ranked_files:
            ranked_files.append(f)
    return (
        nodes,
        ranked_files,
        [_norm(t) for t in target_files if t],
        [_norm(t) for t in target_symbols if t],
        target_blocks,
        row.get("language_group"),
    )


def main(argv: Optional[List[str]] = None) -> int:
    from codenib.eval.agent_runner.sweep_config import SweepConfig as SampleConfig

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instances-json", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--k", type=int, nargs="+", default=[1, 5, 10])
    ap.add_argument("--prebuilt-dir", default=str(prebuilt_data_dir()))
    ap.add_argument(
        "--no-graph",
        action="store_true",
        help="search-only (sets CODENIB_COMPOSER_NO_GRAPH=1 for this process).",
    )
    args = ap.parse_args(argv)

    if args.no_graph:
        os.environ["CODENIB_COMPOSER_NO_GRAPH"] = "1"

    instances = json.loads(args.instances_json.read_text())
    cache_root = os.path.join(
        os.environ.get("CLAUDE_JOB_DIR", "/tmp"), "graphrag_cache"
    )
    cfg = SampleConfig(
        sweep_id="graphrag",
        subsets={"CTX": ["codenib_context"]},
        instances=instances,
        embedding_model="Qwen/Qwen3-Embedding-0.6B",
        embedding_dimension=1024,
    )

    results: List[Dict[str, Any]] = []
    hit_at: Counter = Counter()
    symbol_hit_at: Counter = Counter()
    block_hit_at: Counter = Counter()
    score_symbol_hit_at: Counter = Counter()
    score_block_hit_at: Counter = Counter()
    scored = 0
    symbol_scored = 0
    block_scored = 0
    for i, inst in enumerate(instances, 1):
        t0 = time.time()
        try:
            from codenib.eval.retrieval_eval import (
                evaluate_predictions,
                nodes_to_spans,
                score_localization_spans,
            )

            nodes, ranked, targets, target_symbols, target_blocks, lang = _retrieve(
                inst, cfg, args.prebuilt_dir, cache_root
            )
            score_ranked_nodes = _score_ranked(nodes)
            actionable_nodes = _actionable_nodes(nodes)
            score_actionable_nodes = _score_ranked(actionable_nodes)
            rec = {
                f"files@{k}": (
                    bool(targets) and all(_covers(ranked[:k], t) for t in targets)
                )
                for k in args.k
            }
            metrics = evaluate_predictions(nodes, targets, target_symbols, args.k)
            score_metrics = evaluate_predictions(
                score_ranked_nodes, targets, target_symbols, args.k
            )
            actionable_metrics = evaluate_predictions(
                actionable_nodes, targets, target_symbols, args.k
            )
            score_actionable_metrics = evaluate_predictions(
                score_actionable_nodes, targets, target_symbols, args.k
            )
            block_metrics = score_localization_spans(
                answer_spans=[],
                retrieval_spans=nodes_to_spans(nodes, sort_by_score=False),
                gt_blocks=target_blocks,
                ks=args.k,
            )
            score_block_metrics = score_localization_spans(
                answer_spans=[],
                retrieval_spans=nodes_to_spans(score_ranked_nodes),
                gt_blocks=target_blocks,
                ks=args.k,
            )
            r = {
                "instance_id": inst,
                "language": lang,
                "n_targets": len(targets),
                "n_target_symbols": len(target_symbols),
                "n_target_blocks": len(target_blocks),
                "target_files": targets,
                "target_symbols": target_symbols,
                "target_blocks": target_blocks,
                "recall": rec,
                "metrics": metrics,
                "score_metrics": score_metrics,
                "actionable_metrics": actionable_metrics,
                "score_actionable_metrics": score_actionable_metrics,
                "block_metrics": block_metrics,
                "score_block_metrics": score_block_metrics,
                "n_ranked": len(ranked),
                "n_ranked_nodes": len(nodes),
                "n_actionable_nodes": len(actionable_nodes),
                "ordered_nodes": _node_payload(nodes),
                "ranked_nodes": _node_payload(score_ranked_nodes),
            }
            if targets:
                scored += 1
                if target_symbols:
                    symbol_scored += 1
                if target_blocks:
                    block_scored += 1
                for k in args.k:
                    hit_at[k] += int(rec[f"files@{k}"])
                    symbol_hit_at[k] += int(
                        actionable_metrics["symbols"][k]["accuracy"]
                        if target_symbols
                        else 0
                    )
                    block_hit_at[k] += int(
                        block_metrics["retrieval_blocks"][k]["accuracy"]
                        if target_blocks
                        else 0
                    )
                    score_symbol_hit_at[k] += int(
                        score_actionable_metrics["symbols"][k]["accuracy"]
                        if target_symbols
                        else 0
                    )
                    score_block_hit_at[k] += int(
                        score_block_metrics["retrieval_blocks"][k]["accuracy"]
                        if target_blocks
                        else 0
                    )
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
    print(f"\n=== {mode} retrieval over {scored} scored instances ===")
    for k in args.k:
        pct = 100.0 * hit_at[k] / scored if scored else 0.0
        print(f"  files@{k}: {hit_at[k]}/{scored} = {pct:.0f}%")
    for k in args.k:
        pct = 100.0 * symbol_hit_at[k] / symbol_scored if symbol_scored else 0.0
        print(
            f"  actionable_symbols@{k}: "
            f"{symbol_hit_at[k]}/{symbol_scored} = {pct:.0f}%"
        )
    for k in args.k:
        pct = 100.0 * block_hit_at[k] / block_scored if block_scored else 0.0
        print(
            f"  retrieval_blocks@{k}: " f"{block_hit_at[k]}/{block_scored} = {pct:.0f}%"
        )
    for k in args.k:
        pct = 100.0 * score_symbol_hit_at[k] / symbol_scored if symbol_scored else 0.0
        print(
            f"  score_actionable_symbols@{k}: "
            f"{score_symbol_hit_at[k]}/{symbol_scored} = {pct:.0f}%"
        )
    for k in args.k:
        pct = 100.0 * score_block_hit_at[k] / block_scored if block_scored else 0.0
        print(
            f"  score_retrieval_blocks@{k}: "
            f"{score_block_hit_at[k]}/{block_scored} = {pct:.0f}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
