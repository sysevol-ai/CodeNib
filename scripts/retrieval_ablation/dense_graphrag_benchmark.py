# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Evaluate dense-seeded GraphRAG with equal file-level ranking budgets.

The historical GraphRAG ablation mixed BM25, identifiers, and dense results in
five seeds before appending unranked graph neighbors. That does not isolate the
value of graph expansion over a dense baseline. This runner instead:

1. retrieves one shared dense candidate pool,
2. expands only the highest-ranked dense seeds by one graph hop,
3. measures the union/oracle coverage before ranking, and
4. embedding-reranks the candidate pool, then fuses semantic and graph file
   ranks with weighted reciprocal-rank fusion under the same unique-file
   ``recall@k`` metric, and
5. optionally cross-encoder-reranks equal-size unique-file candidate pools for
   the dense and graph-fused arms.

Use the deterministic repository-level ``dev`` partition while selecting
parameters. Run ``test`` only after the configuration is locked, then ``all``
for the final descriptive table.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import statistics
import time
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

DATASET = "fishmingyu/codeminer-base-dataset"
DEFAULT_SPLIT_SEED = "dense-graphrag-v1"


def _norm(path: str) -> str:
    return (path or "").strip().strip("`'\"").replace("\\", "/")


def _covers(candidates: Sequence[str], target: str) -> bool:
    normalized = _norm(target)
    return any(
        candidate == normalized
        or candidate.endswith("/" + normalized)
        or normalized.endswith("/" + candidate)
        for candidate in candidates
    )


def _same_file(left: str, right: str) -> bool:
    return left == right or left.endswith("/" + right) or right.endswith("/" + left)


def _ranked_files(nodes: Iterable[Any]) -> List[str]:
    files: List[str] = []
    for node in nodes:
        file_path = _norm(getattr(node, "file", "") or "")
        if file_path and not any(_same_file(file_path, seen) for seen in files):
            files.append(file_path)
    return files


def _recall(
    ranked_files: Sequence[str], targets: Sequence[str], ks: Sequence[int]
) -> Dict[str, bool]:
    return {
        f"files@{k}": bool(targets)
        and all(_covers(ranked_files[:k], target) for target in targets)
        for k in ks
    }


def _repo_key(instance_id: str) -> str:
    return instance_id.split("__", 1)[0]


def build_repo_partitions(
    rows_by_id: Mapping[str, Mapping[str, Any]],
    instance_ids: Sequence[str],
    *,
    seed: str = DEFAULT_SPLIT_SEED,
    test_repos_per_language: int = 2,
) -> Dict[str, str]:
    """Return a deterministic language-stratified repository split.

    Instances from the same repository always stay together. Repositories are
    hash-ordered independently inside each language so the 25-repository base
    benchmark yields three development and two test repositories per language.
    """

    repos_by_language: Dict[str, set[str]] = defaultdict(set)
    for instance_id in instance_ids:
        row = rows_by_id[instance_id]
        repos_by_language[str(row.get("language_group") or "unknown")].add(
            _repo_key(instance_id)
        )

    repo_partition: Dict[str, str] = {}
    for language, repos in sorted(repos_by_language.items()):
        ordered = sorted(
            repos,
            key=lambda repo: hashlib.sha256(
                f"{seed}:{language}:{repo}".encode("utf-8")
            ).hexdigest(),
        )
        test_count = min(max(0, test_repos_per_language), len(ordered))
        test_repos = set(ordered[:test_count])
        for repo in ordered:
            repo_partition[repo] = "test" if repo in test_repos else "dev"

    return {
        instance_id: repo_partition[_repo_key(instance_id)]
        for instance_id in instance_ids
    }


def _select_instances(
    instance_ids: Sequence[str],
    partitions: Mapping[str, str],
    partition: str,
    limit: Optional[int],
) -> List[str]:
    selected = [
        instance_id
        for instance_id in instance_ids
        if partition == "all" or partitions[instance_id] == partition
    ]
    if limit is not None:
        selected = selected[: max(0, limit)]
    return selected


def _payload_node(node: Any) -> Dict[str, Any]:
    return {
        "node_id": getattr(node, "node_id", None),
        "node_name": getattr(node, "node_name", None),
        "file": getattr(node, "file", None),
        "start_line": getattr(node, "start_line", None),
        "end_line": getattr(node, "end_line", None),
        "score": getattr(node, "score", None),
        "has_content": bool(getattr(node, "content", None)),
    }


def _weight_label(weight: float) -> str:
    return f"{weight:g}".replace("-", "m").replace(".", "p")


def _prepare_cross_encoder_candidates(
    *,
    ranked_nodes: Sequence[Any],
    candidate_k: int,
    repo_path: str,
    git_commit: str,
) -> tuple[List[Any], float, List[str]]:
    from codeminer.ops.expand import hydrate_candidate_contents
    from codeminer.ops.retrieve import merge_file_rankings

    candidates = merge_file_rankings(
        [list(ranked_nodes)],
        top_k=None,
    )
    eligible: List[Any] = []
    dropped: List[str] = []
    hydration_ms = 0.0
    for candidate in candidates:
        hydrated = candidate
        if not getattr(candidate, "content", None):
            hydration_started = time.perf_counter()
            hydrated = hydrate_candidate_contents(
                [candidate],
                repo_path=repo_path,
                git_commit=git_commit,
            )[0]
            hydration_ms += (time.perf_counter() - hydration_started) * 1000
        if getattr(hydrated, "content", None):
            eligible.append(hydrated)
            if len(eligible) >= candidate_k:
                break
        else:
            dropped.append(_norm(getattr(candidate, "file", "") or ""))
    return eligible, hydration_ms, dropped


def _cross_encoder_rerank(
    *,
    query: str,
    candidates: Sequence[Any],
    context: Any,
    candidate_k: int,
) -> tuple[List[Any], float]:
    from codeminer.ops.rerank import rerank_by_cross_encoder

    selected = list(candidates[:candidate_k])
    if len(selected) != candidate_k:
        raise RuntimeError(
            f"cross-encoder arm produced {len(selected)} eligible unique-file "
            f"candidates; expected exactly {candidate_k}"
        )
    missing_content = [
        _norm(getattr(candidate, "file", "") or "")
        for candidate in selected
        if not getattr(candidate, "content", None)
    ]
    if missing_content:
        raise RuntimeError(
            "cross-encoder candidates are missing content: "
            + ", ".join(missing_content[:5])
        )

    started = time.perf_counter()
    reranked = rerank_by_cross_encoder(
        query,
        selected,
        context,
        top_k=candidate_k,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    reranked_files = _ranked_files(reranked)
    if len(reranked_files) != candidate_k:
        raise RuntimeError(
            f"cross-encoder returned {len(reranked_files)} unique files; "
            f"expected exactly {candidate_k}"
        )
    return reranked, elapsed_ms


def _paired_cross_encoder_budget(
    *,
    requested: int,
    minimum: int,
    eligible_rankings: Mapping[str, Sequence[Any]],
) -> tuple[int, Dict[str, int]]:
    available = {
        method: len(_ranked_files(nodes)) for method, nodes in eligible_rankings.items()
    }
    actual = min(requested, *available.values())
    if actual < minimum:
        raise RuntimeError(
            f"paired cross-encoder budget is only {actual}; "
            f"files@{minimum} requires at least {minimum} candidates "
            f"(available={available})"
        )
    return actual, available


def _analyze_instance(
    *,
    instance_id: str,
    row: Mapping[str, Any],
    eval_metadata: Mapping[str, Any],
    pipeline: Any,
    retriever: Any,
    prebuilt_dir: str,
    ks: Sequence[int],
    rerank_mode: str,
    graph_weights: Sequence[float],
    cross_encoder_context: Any = None,
    cross_encoder_candidate_k: Optional[int] = None,
) -> Dict[str, Any]:
    from codeminer.eval.agent_runner.prebuilt import (
        instance_dir,
        load_prebuilt_code_graph,
        repo_path_for,
    )
    from codeminer.eval.retrieval_eval import collect_targets
    from codeminer.ops.rerank import rerank_by_embedding, rerank_by_indexed_embedding
    from codeminer.ops.retrieve import dedup_queried_nodes, merge_file_rankings
    from codeminer.profiler import Profiler

    query = str(row["problem_statement"])
    base_commit = str(row.get("base_commit") or "")
    if cross_encoder_context is not None and not base_commit:
        raise RuntimeError(f"{instance_id} is missing base_commit")
    target_files, _ = collect_targets(eval_metadata, simplified_symbols=True)
    targets = [_norm(target) for target in target_files if target]
    repo_path = repo_path_for(prebuilt_dir, instance_id)

    retriever.load_index(instance_dir(prebuilt_dir, instance_id))
    graph = load_prebuilt_code_graph(prebuilt_dir, instance_id)
    pipeline.load_graph(graph, repo_path=repo_path)

    profiler = Profiler(
        f"dense_graphrag.{instance_id}",
        emit_events=False,
        summary_level=logging.DEBUG,
        record_samples=True,
    )
    candidate_limit = pipeline.dense_top_k + (
        (pipeline.graph_seed_top_k or pipeline.dense_top_k)
        * pipeline.graph_neighbors_per_seed
    )
    started = time.perf_counter()
    pipeline.query(query, top_k=max(candidate_limit, max(ks)), profiler=profiler)
    total_ms = (time.perf_counter() - started) * 1000

    dense_nodes = pipeline.last_dense_results
    graph_nodes = pipeline.last_graph_results
    candidates = pipeline.last_candidates
    dense_files = _ranked_files(dense_nodes)
    graph_files = _ranked_files(graph_nodes)
    pool_files = _ranked_files(candidates)
    if len(dense_files) < max(ks):
        raise RuntimeError(
            f"dense pool produced only {len(dense_files)} unique files; "
            f"files@{max(ks)} would not be a fair graph comparison"
        )

    recalls: Dict[str, Dict[str, bool]] = {
        "dense": _recall(dense_files, targets, ks),
    }
    ranked_files: Dict[str, List[str]] = {"dense": dense_files}
    cross_encoder_counts: Dict[str, int] = {}
    cross_encoder_budget: Optional[Dict[str, Any]] = None
    hydration_timings: Dict[str, float] = {}
    cross_encoder_timings: Dict[str, float] = {}

    dense_rerank_ms: Optional[float] = None
    if rerank_mode != "none":
        rerank_started = time.perf_counter()
        if rerank_mode == "index":
            dense_reranked = rerank_by_indexed_embedding(
                query,
                dense_nodes,
                retriever.vector_store,
                top_k=len(dense_nodes),
            )
        else:
            dense_reranked = rerank_by_embedding(
                query,
                dense_nodes,
                retriever.vector_store,
                top_k=len(dense_nodes),
            )
        dense_rerank_ms = (time.perf_counter() - rerank_started) * 1000
        dense_reranked_files = _ranked_files(dense_reranked)
        semantic_nodes = pipeline.last_semantic_results
        semantic_files = _ranked_files(semantic_nodes)
        recalls["dense_rerank"] = _recall(dense_reranked_files, targets, ks)
        ranked_files["dense_rerank"] = dense_reranked_files
        recalls["dense_graph_semantic"] = _recall(semantic_files, targets, ks)
        ranked_files["dense_graph_semantic"] = semantic_files

        graph_branch = dedup_queried_nodes(graph_nodes)
        fused_rankings: Dict[str, List[Any]] = {}
        for weight in graph_weights:
            method = f"dense_graph_fusion_w{_weight_label(weight)}"
            fused = merge_file_rankings(
                [semantic_nodes, graph_branch],
                weights=[1.0, weight],
                top_k=max(candidate_limit, max(ks)),
                rrf_k=pipeline.graph_fusion_rrf_k,
            )
            fused_files = _ranked_files(fused)
            recalls[method] = _recall(fused_files, targets, ks)
            ranked_files[method] = fused_files
            fused_rankings[method] = fused

        if cross_encoder_context is not None:
            if cross_encoder_candidate_k is None:
                raise RuntimeError("cross-encoder candidate budget is missing")
            cross_encoder_inputs = {"dense_cross_encoder": dense_nodes}
            cross_encoder_inputs.update(
                {
                    f"{method}_cross_encoder": fused
                    for method, fused in fused_rankings.items()
                }
            )
            eligible_rankings: Dict[str, List[Any]] = {}
            dropped_unrecoverable: Dict[str, List[str]] = {}
            for method, ranking in cross_encoder_inputs.items():
                eligible, hydration_ms, dropped = _prepare_cross_encoder_candidates(
                    ranked_nodes=ranking,
                    candidate_k=cross_encoder_candidate_k,
                    repo_path=repo_path,
                    git_commit=base_commit,
                )
                eligible_rankings[method] = eligible
                hydration_timings[method] = hydration_ms
                dropped_unrecoverable[method] = dropped
            actual_candidate_k, available_files = _paired_cross_encoder_budget(
                requested=cross_encoder_candidate_k,
                minimum=max(ks),
                eligible_rankings=eligible_rankings,
            )
            cross_encoder_budget = {
                "requested": cross_encoder_candidate_k,
                "actual": actual_candidate_k,
                "eligible_files_up_to_requested": available_files,
                "dropped_unrecoverable_files": dropped_unrecoverable,
            }
            for method, eligible in eligible_rankings.items():
                reranked, elapsed_ms = _cross_encoder_rerank(
                    query=query,
                    candidates=eligible,
                    context=cross_encoder_context,
                    candidate_k=actual_candidate_k,
                )
                reranked_files = _ranked_files(reranked)
                recalls[method] = _recall(reranked_files, targets, ks)
                ranked_files[method] = reranked_files
                cross_encoder_counts[method] = len(reranked_files)
                cross_encoder_timings[method] = elapsed_ms

    max_k = max(ks)
    missed_by_dense_pool = [
        target for target in targets if not _covers(dense_files, target)
    ]
    graph_pool_recovers = [
        target for target in missed_by_dense_pool if _covers(graph_files, target)
    ]
    missed_by_dense_at_k = [
        target for target in targets if not _covers(dense_files[:max_k], target)
    ]
    graph_recovers_at_k = [
        target for target in missed_by_dense_at_k if _covers(graph_files, target)
    ]
    timing_ms = {
        label: round(stats.total * 1000, 3) for label, stats in profiler.report()
    }
    timing_ms["pipeline_total"] = round(total_ms, 3)
    dense_retrieve_ms = timing_ms.get("query.dense_retrieve", 0.0)
    timing_ms["arm.dense"] = dense_retrieve_ms
    for weight in graph_weights:
        method = f"dense_graph_fusion_w{_weight_label(weight)}"
        timing_ms[f"arm.{method}"] = timing_ms["pipeline_total"]
    if dense_rerank_ms is not None:
        timing_ms["dense_only_rerank"] = round(dense_rerank_ms, 3)
    for method, elapsed_ms in cross_encoder_timings.items():
        hydration_ms = hydration_timings[method]
        timing_ms[f"content_hydration.{method}"] = round(hydration_ms, 3)
        timing_ms[f"cross_encoder.{method}"] = round(elapsed_ms, 3)
        base_ms = (
            dense_retrieve_ms
            if method == "dense_cross_encoder"
            else timing_ms["pipeline_total"]
        )
        timing_ms[f"arm.{method}"] = round(
            base_ms + hydration_ms + elapsed_ms,
            3,
        )

    return {
        "instance_id": instance_id,
        "language": row.get("language_group"),
        "n_targets": len(targets),
        "target_files": targets,
        "counts": {
            "dense_nodes": len(dense_nodes),
            "dense_files": len(dense_files),
            "graph_nodes": len(graph_nodes),
            "graph_files": len(graph_files),
            "graph_nodes_with_content": sum(
                bool(getattr(node, "content", None)) for node in graph_nodes
            ),
            "candidate_nodes": len(candidates),
            "candidate_files": len(pool_files),
            "cross_encoder_files": cross_encoder_counts,
            "cross_encoder_budget": cross_encoder_budget,
        },
        "recall": recalls,
        "coverage": {
            "dense_pool_hit": bool(targets)
            and all(_covers(dense_files, target) for target in targets),
            "union_pool_hit": bool(targets)
            and all(_covers(pool_files, target) for target in targets),
            "graph_pool_recovers": graph_pool_recovers,
            f"graph_recovers_dense_miss@{max_k}": graph_recovers_at_k,
        },
        "ranked_files": ranked_files,
        "graph_candidates": [_payload_node(node) for node in graph_nodes],
        "timing_ms": timing_ms,
    }


def summarize(
    results: Sequence[Mapping[str, Any]], ks: Sequence[int]
) -> Dict[str, Any]:
    ok = [
        result
        for result in results
        if "error" not in result and result.get("n_targets")
    ]
    methods = sorted(
        {method for result in ok for method in (result.get("recall") or {}).keys()}
    )
    recall_summary: Dict[str, Dict[str, Any]] = {}
    for method in methods:
        recall_summary[method] = {}
        for k in ks:
            key = f"files@{k}"
            hits = sum(
                bool((result.get("recall") or {}).get(method, {}).get(key))
                for result in ok
            )
            recall_summary[method][key] = {
                "hits": hits,
                "total": len(ok),
                "rate": hits / len(ok) if ok else 0.0,
            }

    graph_pool_helps = [
        result["instance_id"]
        for result in ok
        if (result.get("coverage") or {}).get("graph_pool_recovers")
    ]
    dense_pool_hits = sum(
        bool((result.get("coverage") or {}).get("dense_pool_hit")) for result in ok
    )
    union_pool_hits = sum(
        bool((result.get("coverage") or {}).get("union_pool_hit")) for result in ok
    )
    paired: Dict[str, Dict[str, Any]] = {}
    for method in methods:
        if method == "dense":
            continue
        paired[method] = {}
        for k in ks:
            key = f"files@{k}"
            gains = sum(
                not result["recall"]["dense"][key] and result["recall"][method][key]
                for result in ok
            )
            losses = sum(
                result["recall"]["dense"][key] and not result["recall"][method][key]
                for result in ok
            )
            paired[method][key] = {
                "gains": gains,
                "losses": losses,
                "net_hits": gains - losses,
                "delta_rate": (gains - losses) / len(ok) if ok else 0.0,
            }

    timing_labels = sorted(
        {label for result in ok for label in (result.get("timing_ms") or {}).keys()}
    )
    timing = {}
    for label in timing_labels:
        values = [
            float(result["timing_ms"][label])
            for result in ok
            if label in (result.get("timing_ms") or {})
        ]
        timing[label] = {
            "median": statistics.median(values),
            "p90": _nearest_rank(values, 0.9),
        }
    return {
        "scored": len(ok),
        "errors": len(results) - len(ok),
        "recall": recall_summary,
        "coverage": {
            "dense_pool_hits": dense_pool_hits,
            "union_pool_hits": union_pool_hits,
            "graph_pool_helps": graph_pool_helps,
        },
        "paired_vs_dense": paired,
        "timing_ms": timing,
    }


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _write_checkpoint(
    output: Path,
    metadata: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
    ks: Sequence[int],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        **metadata,
        "results": list(results),
        "summary": summarize(results, ks),
    }
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(output)


def main(argv: Optional[List[str]] = None) -> int:
    from codeminer.eval.agent_runner.sweep import load_dataset_rows
    from codeminer.eval.agent_runner.sweep_config import SweepConfig
    from codeminer.model.dense_graph_expand_rerank_pipeline import (
        DenseGraphExpandRerankPipeline,
    )
    from codeminer.model.embedding_retrieve_pipeline import EmbeddingRetrievePipeline
    from codeminer.ops.rerank import RerankContext

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--instances-json",
        type=Path,
        help="Optional instance-id allowlist; defaults to the full dataset split.",
    )
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--partition", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--split-seed", default=DEFAULT_SPLIT_SEED)
    parser.add_argument("--test-repos-per-language", type=int, default=2)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--prebuilt-dir", default="/mnt/data/codeminer")
    parser.add_argument("--embedding-model", default="Qwen/Qwen3-Embedding-0.6B")
    parser.add_argument("--embedding-dim", type=int, default=1024)
    parser.add_argument("--dense-pool-k", type=int, default=300)
    parser.add_argument("--graph-seed-k", type=int, default=10)
    parser.add_argument("--neighbors-per-seed", type=int, default=10)
    parser.add_argument(
        "--direction", choices=("callers", "callees", "both"), default="both"
    )
    parser.add_argument("--k", type=int, nargs="+", default=[1, 5, 10])
    parser.add_argument(
        "--rerank-mode",
        choices=("index", "content", "none"),
        default="index",
        help="Reuse indexed vectors (default), re-embed content, or skip reranking.",
    )
    parser.add_argument(
        "--cross-encoder-model",
        help="Optional HuggingFace cross-encoder applied to equal-size file pools.",
    )
    parser.add_argument("--cross-encoder-backend", choices=("qwen", "st"))
    parser.add_argument("--cross-encoder-candidate-k", type=int, default=50)
    parser.add_argument("--cross-encoder-batch-size", type=int, default=8)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse successful instance rows from a compatible checkpoint.",
    )
    parser.add_argument("--oracle-only", action="store_true")
    parser.add_argument("--graph-weight", type=float, nargs="+", default=[1.0])
    parser.add_argument("--rrf-k", type=int, default=60)
    args = parser.parse_args(argv)
    if args.dense_pool_k < max(args.k):
        parser.error("dense-pool-k must be at least the largest reported k")
    if any(weight < 0 for weight in args.graph_weight):
        parser.error("graph weights must be non-negative")
    if args.rrf_k <= 0:
        parser.error("rrf-k must be positive")
    if args.cross_encoder_candidate_k < max(args.k):
        parser.error("cross-encoder-candidate-k must cover the largest reported k")
    if args.cross_encoder_batch_size <= 0:
        parser.error("cross-encoder-batch-size must be positive")
    if args.cross_encoder_model and args.rerank_mode == "none":
        parser.error("cross-encoder arms require index or content reranking")

    instance_ids = (
        json.loads(args.instances_json.read_text(encoding="utf-8"))
        if args.instances_json
        else []
    )
    config = SweepConfig(
        sweep_id="dense-graphrag-benchmark",
        dataset=DATASET,
        subsets={},
        instances=instance_ids,
        embedding_model=args.embedding_model,
        embedding_dimension=args.embedding_dim,
    )
    rows_by_id, eval_lookup = load_dataset_rows(config)
    if not instance_ids:
        instance_ids = sorted(rows_by_id)
    missing_rows = [
        instance_id for instance_id in instance_ids if instance_id not in rows_by_id
    ]
    if missing_rows:
        parser.error(f"dataset is missing {len(missing_rows)} requested instances")

    partitions = build_repo_partitions(
        rows_by_id,
        instance_ids,
        seed=args.split_seed,
        test_repos_per_language=args.test_repos_per_language,
    )
    selected = _select_instances(instance_ids, partitions, args.partition, args.limit)
    if not selected:
        parser.error("selected partition is empty")

    rerank_mode = "none" if args.oracle_only else args.rerank_mode
    retriever = EmbeddingRetrievePipeline(
        embedding_model=args.embedding_model,
        embedding_dimension=args.embedding_dim,
        top_k=args.dense_pool_k,
    )
    rerank_context = (
        None
        if rerank_mode == "none"
        else RerankContext(
            embedding_store=retriever.vector_store,
            embedding_source=rerank_mode,
        )
    )
    pipeline = DenseGraphExpandRerankPipeline(
        retriever=retriever,
        rerank_context=rerank_context,
        dense_top_k=args.dense_pool_k,
        graph_seed_top_k=args.graph_seed_k,
        graph_neighbors_per_seed=args.neighbors_per_seed,
        graph_direction=args.direction,
        graph_fusion_weight=(args.graph_weight[0] if rerank_mode != "none" else 0.0),
        graph_fusion_rrf_k=args.rrf_k,
        final_top_k=max(args.k),
        include_graph_content=rerank_mode == "content",
        close_retriever=True,
    )
    cross_encoder_context = None
    if args.cross_encoder_model:
        from codeminer.ops.rerank import CrossEncoderContext

        cross_encoder_context = CrossEncoderContext(
            model_name=args.cross_encoder_model,
            backend=args.cross_encoder_backend,
            batch_size=args.cross_encoder_batch_size,
        )
    metadata = {
        "benchmark": "dense_embedding_seeded_graphrag",
        "dataset": {"name": DATASET, "split": config.split},
        "partition": args.partition,
        "split_seed": args.split_seed,
        "partitions": partitions,
        "instance_ids": selected,
        "config": {
            "embedding_model": args.embedding_model,
            "embedding_dimension": args.embedding_dim,
            "dense_pool_k": args.dense_pool_k,
            "graph_seed_k": args.graph_seed_k,
            "neighbors_per_seed": args.neighbors_per_seed,
            "direction": args.direction,
            "ks": args.k,
            "rerank_mode": rerank_mode,
            "graph_weights": args.graph_weight,
            "rrf_k": args.rrf_k,
            "cross_encoder_model": args.cross_encoder_model,
            "cross_encoder_backend": args.cross_encoder_backend,
            "cross_encoder_candidate_k": (
                args.cross_encoder_candidate_k if args.cross_encoder_model else None
            ),
            "cross_encoder_batch_size": (
                args.cross_encoder_batch_size if args.cross_encoder_model else None
            ),
            "cross_encoder_content_hydration": (
                "missing_content_from_base_commit_before_budget"
                if args.cross_encoder_model
                else None
            ),
            "cross_encoder_candidate_policy": (
                "content_available_or_base_commit_recoverable_unique_files"
                if args.cross_encoder_model
                else None
            ),
            "cross_encoder_budget_policy": (
                "paired_min_eligible" if args.cross_encoder_model else None
            ),
        },
    }

    results_by_id: Dict[str, Dict[str, Any]] = {}
    if args.resume and args.out.exists():
        checkpoint = json.loads(args.out.read_text(encoding="utf-8"))
        if checkpoint.get("instance_ids") != metadata["instance_ids"]:
            parser.error("resume checkpoint has a different instance set")
        if checkpoint.get("config") != metadata["config"]:
            parser.error("resume checkpoint has a different benchmark config")
        results_by_id = {
            str(result["instance_id"]): dict(result)
            for result in checkpoint.get("results") or []
            if "error" not in result
        }
    pending = [
        instance_id for instance_id in selected if instance_id not in results_by_id
    ]
    if results_by_id:
        print(
            f"Resuming with {len(results_by_id)} completed instances; "
            f"{len(pending)} remaining",
            flush=True,
        )
    try:
        for index, instance_id in enumerate(pending, 1):
            started = time.perf_counter()
            try:
                result = _analyze_instance(
                    instance_id=instance_id,
                    row=rows_by_id[instance_id],
                    eval_metadata=eval_lookup.get(instance_id, rows_by_id[instance_id]),
                    pipeline=pipeline,
                    retriever=retriever,
                    prebuilt_dir=args.prebuilt_dir,
                    ks=args.k,
                    rerank_mode=rerank_mode,
                    graph_weights=args.graph_weight,
                    cross_encoder_context=cross_encoder_context,
                    cross_encoder_candidate_k=(
                        args.cross_encoder_candidate_k
                        if cross_encoder_context is not None
                        else None
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - checkpoint long scans
                traceback.print_exc()
                result = {"instance_id": instance_id, "error": repr(exc)}
            results_by_id[instance_id] = result
            results = [
                results_by_id[selected_id]
                for selected_id in selected
                if selected_id in results_by_id
            ]
            _write_checkpoint(args.out, metadata, results, args.k)
            print(
                f"[{index}/{len(pending)}] {instance_id} "
                f"recall={result.get('recall')} "
                f"graph_rescue={(result.get('coverage') or {}).get('graph_pool_recovers')} "
                f"({time.perf_counter() - started:.1f}s)",
                flush=True,
            )
    finally:
        pipeline.close()
        if cross_encoder_context is not None:
            cross_encoder_context.close()

    results = [
        results_by_id[instance_id]
        for instance_id in selected
        if instance_id in results_by_id
    ]
    summary = summarize(results, args.k)
    print(json.dumps(summary, indent=2))
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
