#!/usr/bin/env python3
"""
Graph-based retrieval baseline script.

This script demonstrates a three-stage retrieval pipeline:
1. Stage 1: Select a small number of initial nodes (e.g., 5) using BM25.
2. Stage 2: Expand these nodes using the code graph (k-hop BFS) to gather
   a larger set of related nodes (e.g., up to 50).
3. Stage 3 (optional): Use embeddings within the expanded set to rank/select
   the final results.

This is a baseline for comparison with embedding-only retrieval.

Usage:
    # Graph-only (no embedding rerank)
    python examples/graph_retrieve_baseline.py --dataset swebench_lite

    # With embedding rerank on the expanded set
    python examples/graph_retrieve_baseline.py --dataset swebench_lite --embedding

    # Single instance
    python examples/graph_retrieve_baseline.py --dataset swebench_lite \\
        --filter-instance "^(astropy__astropy-12907)$"

    # Adjust stage parameters
    python examples/graph_retrieve_baseline.py --dataset swebench_lite \\
        --stage1-topk 5 --stage2-topk 50 --k-hop 2
"""
import argparse
import json
import logging
import time
from pathlib import Path

from codeminer.dataset.locbench import LocbenchDataset
from codeminer.dataset.swebench import SwebenchDataset
from codeminer.eval.retrieval_eval import (
    aggregate_metrics,
    average_metrics,
    collect_targets,
    evaluate_predictions,
    extract_predictions,
)
from codeminer.log_utils import get_logger
from codeminer.model import GraphRetrievePipeline
from codeminer.profiler import Profiler, percentile_from_sorted

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Profiling helpers
# ---------------------------------------------------------------------------


def _to_sections_payload(profile_summary):
    """Convert ``Profiler.report()`` output to a JSON-serialisable list.

    When the profiler was configured with ``record_samples`` or
    ``record_memory``, additional fields are included so the aggregator can
    recompute global percentiles / memory rollups across instances.
    """
    payload = []
    for label, stats in profile_summary:
        entry = {
            "label": label,
            "total": stats.total,
            "count": stats.count,
            "average": stats.average,
            "min": stats.safe_min,
            "max": stats.max_duration,
            "errors": stats.errors,
        }
        if stats.samples:
            pct = stats.percentiles(50.0, 95.0, 99.0)
            entry["p50"] = pct[50.0]
            entry["p95"] = pct[95.0]
            entry["p99"] = pct[99.0]
            entry["samples"] = list(stats.samples)
        if stats.rss_deltas:
            entry["rss_delta_avg"] = stats.rss_delta_avg
            entry["rss_delta_max"] = stats.rss_delta_max
            entry["rss_deltas"] = list(stats.rss_deltas)
        if stats.gpu_peaks:
            entry["gpu_peak_avg"] = stats.gpu_peak_avg
            entry["gpu_peak_max"] = stats.gpu_peak_max
            entry["gpu_peaks"] = list(stats.gpu_peaks)
        payload.append(entry)
    return payload


def _aggregate_section_stats(instance_profiles):
    """Sum per-instance section payloads into a single aggregate list.

    When per-call sample lists are present, concatenate them and recompute
    global p50/p95/p99 in one sort pass per section; memory lists are rolled
    up the same way for ``rss_delta_*`` and ``gpu_peak_*``.
    """
    aggregate = {}
    for profile in instance_profiles:
        for section in profile.get("sections", []):
            label = section["label"]
            entry = aggregate.setdefault(
                label,
                {
                    "label": label,
                    "total": 0.0,
                    "count": 0,
                    "min": float("inf"),
                    "max": 0.0,
                    "errors": 0,
                    "samples": [],
                    "rss_deltas": [],
                    "gpu_peaks": [],
                },
            )
            entry["total"] += float(section["total"])
            entry["count"] += int(section["count"])
            entry["min"] = min(entry["min"], float(section["min"]))
            entry["max"] = max(entry["max"], float(section["max"]))
            entry["errors"] += int(section["errors"])
            entry["samples"].extend(section.get("samples", []))
            entry["rss_deltas"].extend(section.get("rss_deltas", []))
            entry["gpu_peaks"].extend(section.get("gpu_peaks", []))

    payload = []
    for label, stats in aggregate.items():
        count = stats["count"]
        item = {
            "label": label,
            "total": stats["total"],
            "count": count,
            "average": (stats["total"] / count) if count else 0.0,
            "min": 0.0 if stats["min"] == float("inf") else stats["min"],
            "max": stats["max"],
            "errors": stats["errors"],
        }
        if stats["samples"]:
            ordered = sorted(stats["samples"])
            item["p50"] = percentile_from_sorted(ordered, 50.0)
            item["p95"] = percentile_from_sorted(ordered, 95.0)
            item["p99"] = percentile_from_sorted(ordered, 99.0)
            item["sample_count"] = len(ordered)
        if stats["rss_deltas"]:
            rss = stats["rss_deltas"]
            item["rss_delta_avg"] = sum(rss) / len(rss)
            item["rss_delta_max"] = max(rss)
        if stats["gpu_peaks"]:
            gpu = stats["gpu_peaks"]
            item["gpu_peak_avg"] = sum(gpu) / len(gpu)
            item["gpu_peak_max"] = max(gpu)
        payload.append(item)

    payload.sort(key=lambda item: item["total"], reverse=True)
    return payload


def _compute_bm25_seed_recall(seeds, target_files, ks):
    """Compute bm25_seed_recall@k for each k in ``ks``.

    Uses the same file-extraction logic as the main retrieval evaluator
    (``extract_predictions``), so the recall numbers are directly comparable
    against ``files@k`` reported for the full pipeline output.

    Returns dict of {int(k): float} with recall in [0, 1]. Empty if no
    target files (instance has no GT files to recall against).
    """
    if not target_files:
        return {}
    seed_files, _ = extract_predictions(seeds)
    target_set = set(target_files)
    out = {}
    denom = len(target_set)
    for k in ks:
        # Normalise k to int up front so both the slice and the output key
        # use the same value — argparse hands us ints today, but downstream
        # callers (sweep scripts) may pass floats / numpy scalars.
        ik = int(k)
        topk = set(seed_files[:ik])
        out[ik] = (len(topk & target_set) / denom) if denom else 0.0
    return out


def _filter_index_sections(sections):
    """Return only the sections that belong in the ``index_time`` bucket.

    Anything starting with ``query.`` should never appear in the index
    profile, but if it does (e.g. a future label addition wires through
    the wrong profiler), log a warning rather than silently dropping so
    the leak is visible in CI rather than ghosting from the JSON.
    """
    keep, leaked = [], []
    for section in sections:
        if section["label"].startswith("query."):
            leaked.append(section["label"])
        else:
            keep.append(section)
    if leaked:
        logger.warning(
            "Dropping %d query.* section(s) leaked into index profile: %s",
            len(leaked),
            leaked,
        )
    return keep


def _filter_query_sections(sections):
    """Return only the sections that belong in the ``query_time`` bucket.

    Only ``query.*`` labels survive. Anything else — an ``index.*`` label
    routed through the query profiler, or any unrecognised prefix added in
    a future phase — is logged rather than silently discarded.
    """
    keep, leaked = [], []
    for section in sections:
        if section["label"].startswith("query."):
            keep.append(section)
        else:
            leaked.append(section["label"])
    if leaked:
        logger.warning(
            "Dropping %d non-query.* section(s) leaked into query profile: %s",
            len(leaked),
            leaked,
        )
    return keep


def _sanitize_filename_part(value):
    return str(value).replace("/", "__").replace(" ", "_")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Graph-based retrieval baseline.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # Dataset
    parser.add_argument(
        "--dataset",
        type=str,
        required=True,
        choices=["swebench_lite", "locbench_v1"],
    )
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument("--filter-instance", type=str, default=".*")

    # Stage 1: BM25 seed selection
    parser.add_argument(
        "--stage1-topk",
        type=int,
        default=5,
        help="Number of seed nodes from BM25.",
    )
    # Stage 2: Graph expansion
    parser.add_argument(
        "--stage2-topk",
        type=int,
        default=50,
        help="Max nodes after graph expansion.",
    )
    parser.add_argument(
        "--k-hop",
        type=int,
        default=2,
        help="Number of hops for graph BFS expansion (ignored with --ppr).",
    )
    # Stage 2 alternative: PPR expansion
    parser.add_argument(
        "--ppr",
        action="store_true",
        help="Use Personalized PageRank instead of BFS for graph expansion.",
    )
    parser.add_argument(
        "--ppr-damping",
        type=float,
        default=0.85,
        help="PPR damping factor (0-1). Higher = more global spread.",
    )
    # Stage 3: Optional embedding rerank
    parser.add_argument(
        "--embedding",
        action="store_true",
        help="Use embedding rerank within expanded set.",
    )
    parser.add_argument(
        "--embedding-model",
        type=str,
        default="nomic-ai/CodeRankEmbed",
    )
    parser.add_argument(
        "--embedding-provider",
        type=str,
        default="huggingface",
    )
    parser.add_argument(
        "--embedding-dimension",
        type=int,
        default=768,
    )

    # Evaluation
    parser.add_argument(
        "--eval-instances",
        type=str,
        default=None,
        help="Path to eval annotations JSON. Auto-generated if not provided.",
    )
    parser.add_argument(
        "--metrics-k",
        type=int,
        nargs="+",
        default=[1, 5, 10],
    )

    # Cache
    parser.add_argument(
        "--index-cache-dir",
        type=str,
        default="/mnt/data/codeminer",
    )
    parser.add_argument(
        "--repo-cache-dir",
        type=str,
        default="~/.codeminer/",
    )
    parser.add_argument("--result-path", type=str, default=None)

    # Rerank strategy (forward-compatible flag; cross-encoder backends
    # are introduced in PR #128 and not yet available on main).
    parser.add_argument(
        "--rerank-strategy",
        type=str,
        choices=["none", "embedding", "cross-encoder"],
        default=None,
        help=(
            "Rerank strategy for Stage 3. 'none' disables rerank, "
            "'embedding' uses FAISS within the expanded set (legacy), "
            "'cross-encoder' requires PR #128 (Qwen3-Reranker / "
            "STCrossEncoderWrapper) and currently raises NotImplementedError."
        ),
    )
    # Forward-compat: wired in PR #128. Currently echoed only into the
    # profiler `config` payload (no rerank backend reads it), so it is
    # safe to pass but has no runtime effect until the cross-encoder
    # wrapper lands.
    parser.add_argument(
        "--rerank-model",
        type=str,
        default=None,
        help="Cross-encoder rerank model id (used when rerank-strategy=cross-encoder).",
    )

    # Profiling
    parser.add_argument(
        "--enable-profiler",
        action="store_true",
        help="Enable runtime profiler summaries.",
    )
    parser.add_argument(
        "--profile-dir",
        type=str,
        default=None,
        help=(
            "Directory to store runtime profiler summaries "
            "(default: <index-cache-dir>/profile_log/graph_rag)."
        ),
    )
    parser.add_argument(
        "--profile-tag",
        type=str,
        default=None,
        help="Optional tag appended to profiler filename.",
    )
    parser.add_argument(
        "--record-samples",
        action="store_true",
        help=(
            "Retain per-call durations so the harness can emit p50/p95/p99 "
            "per section (per-instance and aggregate). No effect unless "
            "--enable-profiler is also set."
        ),
    )
    parser.add_argument(
        "--record-memory",
        action="store_true",
        help=(
            "Capture RSS delta (psutil) and peak GPU memory (torch.cuda) "
            "per section. GPU fields are populated only when CUDA is "
            "available; otherwise the RSS deltas are emitted alone."
        ),
    )

    args = parser.parse_args()
    # Surface unsupported rerank strategies at parse time, before any
    # dataset load / profiler-dir creation, with argparse's exit code 2
    # rather than a stack trace mid-pipeline.
    try:
        _resolve_rerank_strategy(args)
    except NotImplementedError as exc:
        parser.error(str(exc))
    return args


def _resolve_rerank_strategy(args):
    """Combine legacy ``--embedding`` flag with ``--rerank-strategy``.

    Returns one of: ``"none"``, ``"embedding"``, ``"cross-encoder"``.
    """
    if args.rerank_strategy is not None:
        if args.rerank_strategy == "cross-encoder":
            raise NotImplementedError(
                "rerank-strategy='cross-encoder' requires the rerank "
                "wrappers from PR #128 (codeminer/index/rerank/cross_encoder.py). "
                "That PR has not landed on main yet."
            )
        return args.rerank_strategy
    return "embedding" if args.embedding else "none"


def run_graph_pipeline(args):
    """Run the graph-based retrieval baseline."""

    rerank_strategy = _resolve_rerank_strategy(args)
    use_embedding_rerank = rerank_strategy == "embedding"

    # Profiler setup ---------------------------------------------------------
    profiling_enabled = args.enable_profiler or args.profile_dir is not None

    def _resolve_profile_dir() -> Path:
        base = (
            Path(args.profile_dir).expanduser().resolve()
            if args.profile_dir
            else Path(args.index_cache_dir).expanduser().resolve()
            / "profile_log"
            / "graph_rag"
        )
        base.mkdir(parents=True, exist_ok=True)
        return base

    profile_dir = _resolve_profile_dir() if profiling_enabled else None
    if profile_dir is not None:
        logger.info("Profiler summaries will be stored in: %s", profile_dir)

    instance_index_profiles = []
    instance_query_profiles = []
    instance_seed_recalls = []

    # Load dataset
    if args.dataset == "swebench_lite":
        dataset_obj = SwebenchDataset(
            dataset="princeton-nlp/SWE-bench_Lite",
            split=args.split,
            filter_instance=args.filter_instance,
            repo_root=args.repo_cache_dir,
        )
    elif args.dataset == "locbench_v1":
        dataset_obj = LocbenchDataset(
            dataset="czlll/Loc-Bench_V1",
            split=args.split,
            filter_instance=args.filter_instance,
            repo_root=args.repo_cache_dir,
        )
    else:
        raise ValueError(f"Unsupported dataset: {args.dataset}")

    dataset_instances = dataset_obj.load()
    if not dataset_instances:
        raise ValueError(f"No instances found in {args.dataset}")

    logger.info("Loaded %d instance(s)", len(dataset_instances))

    # GT files are dataset-specific; derive the default from --dataset so a
    # Loc-Bench run can't silently align against the SWE-bench ground truth.
    eval_path = args.eval_instances or str(
        Path.home() / ".codeminer" / f"{args.dataset}_{args.split}_gt.json"
    )
    eval_metadata = dataset_obj.load_eval_metadata(eval_path)
    metrics_k = sorted(set(args.metrics_k))
    metric_max_k = max(metrics_k)
    aggregate = {}
    eval_count = 0
    all_results = [] if args.result_path else None

    for instance in dataset_instances:
        instance_id = instance["instance_id"]
        metadata = eval_metadata.get(instance_id)
        if not metadata:
            logger.info("Skipping %s - no eval metadata", instance_id)
            continue
        target_files, target_symbols = collect_targets(metadata)
        if not target_symbols:
            logger.info("Skipping %s - no valid target symbols", instance_id)
            continue

        pipeline = None
        index_profiler = None
        query_profiler = None
        if profiling_enabled:
            index_profiler = Profiler(
                name=f"graph_rag[index][{instance_id}]",
                logger=logger,
                emit_events=False,
                summary_level=logging.INFO,
                record_samples=args.record_samples,
                record_memory=args.record_memory,
            )
            query_profiler = Profiler(
                name=f"graph_rag[query][{instance_id}]",
                logger=logger,
                emit_events=False,
                summary_level=logging.INFO,
                record_samples=args.record_samples,
                record_memory=args.record_memory,
            )
        try:
            t0 = time.time()

            dataset_obj.process_instance(instance)
            repo_path = dataset_obj.get_repo_path(instance)
            index_path = str(
                Path(args.index_cache_dir) / instance_id.replace("/", "__")
            )

            pipeline = GraphRetrievePipeline(
                repo_path=repo_path,
                index_path=index_path,
                stage1_topk=args.stage1_topk,
                stage2_topk=args.stage2_topk,
                k_hop=args.k_hop,
                use_ppr=args.ppr,
                ppr_damping=args.ppr_damping,
                use_embedding_rerank=use_embedding_rerank,
                embedding_model=args.embedding_model,
                embedding_provider=args.embedding_provider,
                embedding_dimension=args.embedding_dimension,
                project_name=instance_id.replace("/", "__"),
                profiler=index_profiler,
            )
            if query_profiler is not None:
                with query_profiler.section("query.total"):
                    results = pipeline.query(
                        instance["problem_statement"], profiler=query_profiler
                    )
            else:
                results = pipeline.query(instance["problem_statement"])
            elapsed = time.time() - t0

            # bm25_seed_recall@k — saturation-guard signal. Compares the
            # stage-1 BM25 seeds against GT files at every k in --metrics-k,
            # using the same file-extraction logic as the main evaluator so
            # the numbers line up with files@k for the full pipeline.
            seed_recall = _compute_bm25_seed_recall(
                pipeline.last_bm25_seeds, target_files, metrics_k
            )
            if seed_recall:
                instance_seed_recalls.append(
                    {"instance_id": instance_id, "recall_at_k": seed_recall}
                )
                logger.info(
                    "  [bm25_seed] %s",
                    " ".join(f"r@{k}={v:.3f}" for k, v in seed_recall.items()),
                )

            if profiling_enabled:
                index_summary = index_profiler.report()
                query_summary = query_profiler.report()
                index_sections = _to_sections_payload(index_summary)
                query_sections = _to_sections_payload(query_summary)
                # Internal labels emitted by builders/vector_store land in
                # the index profiler — filter each side so any cross-talk
                # is logged rather than silently bucketed.
                index_only = _filter_index_sections(index_sections)
                query_only = _filter_query_sections(query_sections)
                instance_index_profiles.append(
                    {"instance_id": instance_id, "sections": index_only}
                )
                instance_query_profiles.append(
                    {"instance_id": instance_id, "sections": query_only}
                )

            metrics = evaluate_predictions(
                nodes=results,
                target_files=target_files,
                target_symbols=target_symbols,
                ks=metrics_k,
            )
            aggregate_metrics(aggregate, metrics)
            eval_count += 1

            logger.info(
                "[%s] Done in %.1fs (%d results)",
                instance_id,
                elapsed,
                len(results),
            )
            for scope, per_k in metrics.items():
                for k, stats in per_k.items():
                    logger.info(
                        "  [%s] k=%d acc=%.3f prec=%.3f recall=%.3f hits=%d",
                        scope,
                        k,
                        stats["accuracy"],
                        stats["precision"],
                        stats["recall"],
                        int(stats["hits"]),
                    )

            if all_results is not None:
                unique_files, normalized_symbols = extract_predictions(results)
                all_results.append(
                    {
                        "instance_id": instance_id,
                        "method": (
                            "graph_ppr_baseline" if args.ppr else "graph_baseline"
                        ),
                        "stage1_topk": args.stage1_topk,
                        "stage2_topk": args.stage2_topk,
                        "k_hop": args.k_hop,
                        "use_ppr": args.ppr,
                        "ppr_damping": args.ppr_damping,
                        "embedding_rerank": use_embedding_rerank,
                        "rerank_strategy": rerank_strategy,
                        "num_results": len(results),
                        "elapsed_s": elapsed,
                        "metric_k_files": unique_files[:metric_max_k],
                        "metric_k_node_ids": normalized_symbols[:metric_max_k],
                        "metrics": metrics,
                    }
                )

        except Exception:
            logger.exception("Error processing %s", instance_id)
            continue
        finally:
            if pipeline is not None:
                pipeline.close()

    # ---- Aggregate ----
    if aggregate and eval_count:
        averaged = average_metrics(aggregate, eval_count)
        logger.info(
            "=== Graph Baseline Aggregate (%d instances) ===",
            eval_count,
        )
        for scope, per_k in averaged.items():
            for k, stats in per_k.items():
                logger.info(
                    "[%s] k=%d acc=%.3f prec=%.3f recall=%.3f avg_hits=%.3f",
                    scope,
                    k,
                    stats["accuracy"],
                    stats["precision"],
                    stats["recall"],
                    stats["avg_hits"],
                )

    if args.result_path and all_results is not None:
        result_path = Path(args.result_path).expanduser().resolve()
        result_path.parent.mkdir(parents=True, exist_ok=True)
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)
        logger.info("Results saved to %s", result_path)

    if profiling_enabled:
        config_payload = {
            "dataset": args.dataset,
            "split": args.split,
            "filter_instance": args.filter_instance,
            "expansion": "ppr" if args.ppr else "bfs",
            "stage1_topk": args.stage1_topk,
            "stage2_topk": args.stage2_topk,
            "k_hop": args.k_hop,
            "ppr_damping": args.ppr_damping,
            "rerank_strategy": rerank_strategy,
            "embedding_model": (
                args.embedding_model if rerank_strategy == "embedding" else None
            ),
            "rerank_model": args.rerank_model,
            "record_samples": args.record_samples,
            "record_memory": args.record_memory,
        }
        # Aggregate bm25_seed_recall@k by averaging per-instance recall at each k.
        # Each instance contributes one observation per k (the recall for that
        # instance's GT set), so the aggregate is a plain mean across instances.
        aggregate_seed_recall = {}
        for k in metrics_k:
            values = [
                entry["recall_at_k"][int(k)]
                for entry in instance_seed_recalls
                if int(k) in entry["recall_at_k"]
            ]
            if values:
                aggregate_seed_recall[int(k)] = sum(values) / len(values)
        profile_payload = {
            "config": config_payload,
            "instances_profiled": len(instance_query_profiles),
            "index_time": {
                "per_instance": instance_index_profiles,
                "aggregate_sections": _aggregate_section_stats(instance_index_profiles),
            },
            "query_time": {
                "per_instance": instance_query_profiles,
                "aggregate_sections": _aggregate_section_stats(instance_query_profiles),
            },
            "bm25_seed_recall": {
                "per_instance": instance_seed_recalls,
                "aggregate": aggregate_seed_recall,
                "instances_with_targets": len(instance_seed_recalls),
            },
        }
        tag_part = (
            f"__{_sanitize_filename_part(args.profile_tag)}" if args.profile_tag else ""
        )
        expansion = "ppr" if args.ppr else "bfs"
        # Sanitize args.dataset for symmetry with profile_tag — today
        # `choices=` constrains it to safe values, but if that ever
        # loosens we don't want to write outside the resolved profile dir.
        dataset_part = _sanitize_filename_part(args.dataset)
        profile_filename = (
            f"graph_rag_{dataset_part}_{expansion}_{rerank_strategy}{tag_part}.json"
        )
        profile_path = profile_dir / profile_filename
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile_payload, f, indent=2, ensure_ascii=False)
        logger.info("Profiler summary saved to %s", profile_path)
        if aggregate_seed_recall:
            logger.info(
                "=== bm25_seed_recall (avg over %d instance(s)) ===",
                len(instance_seed_recalls),
            )
            for k in sorted(aggregate_seed_recall):
                logger.info("  r@%d = %.3f", k, aggregate_seed_recall[k])


def main():
    args = parse_args()
    logger.info("Dataset: %s", args.dataset)
    stage2_desc = (
        f"PPR(damping={args.ppr_damping}, max {args.stage2_topk})"
        if args.ppr
        else f"Graph({args.k_hop}-hop, max {args.stage2_topk})"
    )
    rerank_strategy = _resolve_rerank_strategy(args)
    rerank_desc = {
        "none": "",
        "embedding": "-> Embedding rerank",
        "cross-encoder": "-> Cross-encoder rerank (PR #128)",
    }[rerank_strategy]
    logger.info(
        "Pipeline: BM25(top%d) -> %s %s",
        args.stage1_topk,
        stage2_desc,
        rerank_desc,
    )
    run_graph_pipeline(args)


if __name__ == "__main__":
    main()
