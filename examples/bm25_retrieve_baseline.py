#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
BM25-only retrieval baseline script.

Retrieves top-K nodes using BM25 sparse search over the code graph.
Useful for isolating whether poor graph retrieval performance is due
to bad BM25 seeds (Stage 1) or bad graph expansion (Stage 2).

Usage:
    python examples/bm25_retrieve_baseline.py --dataset swebench_lite --split dev

    # Single instance
    python examples/bm25_retrieve_baseline.py --dataset swebench_lite \\
        --filter-instance "^(astropy__astropy-12907)$"
"""
import argparse
import json
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
from codeminer.model import BM25RetrievePipeline

logger = get_logger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="BM25-only retrieval baseline.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dataset",
        type=str,
        required=True,
        choices=["swebench_lite", "locbench_v1"],
    )
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument("--filter-instance", type=str, default=".*")
    parser.add_argument("--topk", type=int, default=50)

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
        default=[1, 3, 5, 10, 15, 20],
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

    return parser.parse_args()


def run_bm25_pipeline(args):
    """Run the BM25-only retrieval baseline."""

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

    eval_path = args.eval_instances or str(
        Path.home() / ".codeminer" / f"swebench_lite_{args.split}_gt.json"
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
        try:
            t0 = time.time()

            dataset_obj.process_instance(instance)
            repo_path = dataset_obj.get_repo_path(instance)
            index_path = str(
                Path(args.index_cache_dir) / instance_id.replace("/", "__")
            )

            pipeline = BM25RetrievePipeline(
                repo_path=repo_path,
                index_path=index_path,
                top_k=args.topk,
                project_name=instance_id.replace("/", "__"),
            )
            results = pipeline.query(instance["problem_statement"])
            elapsed = time.time() - t0

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
                        "method": "bm25_baseline",
                        "topk": args.topk,
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
            "=== BM25 Baseline Aggregate (%d instances) ===",
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


def main():
    args = parse_args()
    logger.info("Dataset: %s", args.dataset)
    logger.info("Pipeline: BM25(top%d)", args.topk)
    run_bm25_pipeline(args)


if __name__ == "__main__":
    main()
