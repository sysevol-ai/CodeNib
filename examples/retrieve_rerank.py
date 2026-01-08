#!/usr/bin/env python3
"""
This script demonstrates RetrieveRerankPipeline on SWE-bench or LocBench
datasets. Before running the pipeline, start a vLLM server for the rerank
model:

```bash
python scripts/start_vllm_server.py --model Qwen/Qwen2.5-Coder-7B
```

Usage:
    # Run on SWE-bench with default settings
    python examples/retrieve_rerank.py --dataset swebench_lite

    # Run on LocBench with custom filter
    python examples/retrieve_rerank.py --dataset locbench_v1 \\
        --filter-instance "^(joselc__life-sim-first-try-2)$"

    # Run on SWE-bench with custom embedding model
    python examples/retrieve_rerank.py --dataset swebench_lite \\
        --embedding-model nomic-ai/CodeRankEmbed \\
        --embedding-provider huggingface

    # Run with hybrid (dense + sparse) retrieval before rerank
    python examples/retrieve_rerank.py --dataset swebench_lite \\
        --retrieval-mode hybrid

    # Use embedding-based rerank instead of LLM listwise reranker
    python examples/retrieve_rerank.py --dataset swebench_lite \\
        --rerank-strategy embedding \\
        --rerank-embedding-model jinaai/jina-code-embeddings-v2-base

    # Override cache directories (one for indices, one for repos)
    python examples/retrieve_rerank.py --dataset swebench_lite \\
        --index-cache-dir /tmp/codeminer/index \\
        --repo-cache-dir ~/.codeminer/
"""

import argparse
import json
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
from codeminer.llm.llm_config import LLMProvider
from codeminer.log_utils import get_logger
from codeminer.model import RetrieveRerankPipeline, build_retrieve_plan
from codeminer.model.retrieve_rerank_pipeline import RETRIEVAL_TOP_K

logger = get_logger(__name__)


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Run Retrieve + Rerank Pipeline on benchmark datasets",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Dataset configuration
    parser.add_argument(
        "--dataset",
        type=str,
        required=True,
        choices=["swebench_lite", "locbench_v1"],
        help="Type of dataset to run on",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="test",
        help="Dataset split to use",
    )
    parser.add_argument(
        "--filter-instance",
        type=str,
        default=".*",
        help="Regex pattern to filter instances (`.*` processes all instances)",
    )

    # Embedding configuration
    parser.add_argument(
        "--embedding-model",
        type=str,
        default="nomic-ai/CodeRankEmbed",
        help="Embedding model name for dense retrieval",
    )
    parser.add_argument(
        "--embedding-provider",
        type=str,
        default="huggingface",
        choices=["openai", "huggingface"],
        help="Embedding provider",
    )
    parser.add_argument(
        "--embedding-dimension",
        type=int,
        default=768,
        help="Embedding dimension",
    )
    parser.add_argument(
        "--trust-remote-code",
        action="store_true",
        default=True,
        help="Trust remote code for embedding model",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help="Batch size for embedding encoding",
    )

    # Rerank configuration
    parser.add_argument(
        "--retrieval-only",
        action="store_true",
        default=False,
        help=(
            "Run retrieval only without reranking "
            "(for evaluating retrieval performance)"
        ),
    )
    parser.add_argument(
        "--rerank-strategy",
        type=str,
        default="llm",
        choices=["llm", "embedding"],
        help="Rerank method to apply after retrieval.",
    )
    parser.add_argument(
        "--rerank-model",
        type=str,
        default="Qwen/Qwen2.5-Coder-7B",
        help="Rerank model name (ignored if --retrieval-only is set)",
    )
    parser.add_argument(
        "--rerank-provider",
        type=str,
        default="vllm_openai",
        choices=[pv.value for pv in LLMProvider],
        help="Rerank provider (ignored if --retrieval-only is set)",
    )
    parser.add_argument(
        "--rerank-window-size",
        type=int,
        default=10,
        help=(
            "Number of candidates per LLM rerank window "
            "(None processes all candidates at once)."
        ),
    )
    parser.add_argument(
        "--rerank-window-step",
        type=int,
        default=None,
        help=(
            "Stride between rerank windows; defaults to the window size "
            "when unspecified."
        ),
    )
    parser.add_argument(
        "--rerank-top-k",
        type=int,
        default=None,
        help=(
            "Maximum number of candidates to pass to the reranker."
            "Must be >= max(metrics-k) and <= retrieve top_k."
        ),
    )
    parser.add_argument(
        "--rerank-embedding-model",
        type=str,
        default=None,
        help=(
            "Optional embedding model for embedding-based rerank "
            "(defaults to retrieval model)."
        ),
    )
    parser.add_argument(
        "--rerank-embedding-provider",
        type=str,
        default=None,
        choices=["openai", "huggingface"],
        help=(
            "Embedding provider for rerank embeddings "
            "(defaults to retrieval provider)."
        ),
    )
    parser.add_argument(
        "--rerank-embedding-dimension",
        type=int,
        default=None,
        help=(
            "Embedding dimension for rerank embeddings "
            "(defaults to retrieval dimension)."
        ),
    )
    parser.add_argument(
        "--rerank-embedding-batch-size",
        type=int,
        default=32,
        help="Batch size for rerank embedding encoding (embedding strategy only).",
    )

    # Repository processing configuration
    parser.add_argument(
        "--languages",
        type=str,
        nargs="+",
        default=["python"],
        help="Programming languages to process",
    )
    parser.add_argument(
        "--max-lines-per-chunk",
        type=int,
        default=300,
        help="Maximum lines per code chunk",
    )

    parser.add_argument(
        "--retrieval-mode",
        type=str,
        default="dense",
        choices=["dense", "sparse", "hybrid"],
        help="Retrieval plan to run (dense-only, BM25-only, or hybrid).",
    )
    parser.add_argument(
        "--retrieval-level",
        type=str,
        default="l2",
        choices=["l0", "l2"],
        help=(
            "Hierarchical index level for dense retrieval "
            "(l0=file skeletons, l2=functions/methods)."
        ),
    )

    # Evaluation configuration
    default_eval_path = Path.home() / ".codeminer" / "swebench_lite_dev_gt.json"
    parser.add_argument(
        "--eval-instances",
        type=str,
        default=str(default_eval_path),
        help=(
            "Path to JSON file containing evaluation annotations "
            "(target_files, symbols_*). "
            "Defaults to ~/.codeminer/swebench_lite_dev_gt.json. "
            "For SWE-bench, the file is generated if missing."
        ),
    )
    parser.add_argument(
        "--metrics-k",
        type=int,
        nargs="+",
        default=[1, 3, 5, 10, 20, 30, 40],
        help="Cutoffs for accuracy/precision/recall reporting",
    )

    # Cache configuration
    parser.add_argument(
        "--index-cache-dir",
        type=str,
        default="/mnt/data/codeminer",
        help="Directory to store embedding/vector indices",
    )
    parser.add_argument(
        "--repo-cache-dir",
        type=str,
        default="~/.codeminer/",
        help="Directory to cache cloned repositories for dataset instances",
    )
    parser.add_argument(
        "--result-path",
        type=str,
        default=None,
        help="Path to store retrieval results in json format",
    )

    return parser.parse_args()


def run_pipeline(args):
    """Run the retrieve + rerank pipeline on the specified dataset."""

    # Get dataset configuration
    dataset = args.dataset
    if dataset == "swebench_lite":
        dataset_name = "princeton-nlp/SWE-bench_Lite"
        dataset_split = "dev"
        dataset_class = SwebenchDataset
    elif dataset == "locbench_v1":
        dataset_name = "czlll/Loc-Bench_V1"
        dataset_split = "test"
        dataset_class = LocbenchDataset
    else:
        raise ValueError(f"Unsupported dataset: {dataset}")

    # Load dataset
    dataset_obj = dataset_class(
        dataset=dataset_name,
        split=dataset_split,
        filter_instance=args.filter_instance,
        repo_root=args.repo_cache_dir,
    )
    dataset_instances = dataset_obj.load()

    if len(dataset_instances) == 0:
        raise ValueError(f"No instances found in {dataset} dataset")

    logger.info("Loaded %d instance(s)", len(dataset_instances))

    eval_metadata = dataset_obj.load_eval_metadata(args.eval_instances)
    retrieve_plan = build_retrieve_plan(args.retrieval_mode)
    retrieve_top_k = max((stage.top_k or RETRIEVAL_TOP_K) for stage in retrieve_plan)
    aggregate = {}
    metrics_k = sorted(set(args.metrics_k))
    metric_max_k = max(metrics_k)
    if not args.retrieval_only and args.rerank_top_k is not None:
        if args.rerank_top_k < metric_max_k:
            raise ValueError(
                "rerank_top_k must be >= the largest metrics-k value "
                f"({metric_max_k}), got {args.rerank_top_k}."
            )
        if args.rerank_top_k > retrieve_top_k:
            raise ValueError(
                "rerank_top_k must be <= retrieve top_k "
                f"({retrieve_top_k}), got {args.rerank_top_k}."
            )
    query_top_k = metric_max_k
    eval_count = 0
    all_results = [] if args.result_path else None

    # Process each instance
    for instance in dataset_instances:
        instance_id = instance["instance_id"]
        metadata = eval_metadata.get(instance_id)
        if metadata:
            target_files, target_symbols = collect_targets(metadata)
            if not target_symbols:
                logger.info(f"Skipping {instance_id} - no valid target symbols")
                continue
        else:
            logger.info(f"Skipping {instance_id} - no eval metadata")
            continue

        # Process instance to get repo path
        dataset_obj.process_instance(instance)
        repo_path = dataset_obj.get_repo_path(instance)

        # Compute index path
        instance_dir_name = instance_id.replace("/", "__")
        index_path = Path(args.index_cache_dir) / instance_dir_name

        # Initialize pipeline
        embedding_model_kwargs = {
            "trust_remote_code": args.trust_remote_code,
            "encode_kwargs": {
                "batch_size": args.batch_size,
            },
        }
        rerank_embedding_kwargs = {
            "trust_remote_code": args.trust_remote_code,
            "encode_kwargs": {
                "batch_size": args.rerank_embedding_batch_size,
            },
        }

        pipeline = RetrieveRerankPipeline(
            repo_path=repo_path,
            index_path=str(index_path),
            embedding_model=args.embedding_model,
            embedding_provider=args.embedding_provider,
            embedding_dimension=args.embedding_dimension,
            embedding_model_kwargs=embedding_model_kwargs,
            rerank_model=args.rerank_model,
            rerank_provider=LLMProvider(args.rerank_provider),
            rerank_strategy=args.rerank_strategy,
            rerank_embedding_model=args.rerank_embedding_model,
            rerank_embedding_provider=args.rerank_embedding_provider,
            rerank_embedding_dimension=args.rerank_embedding_dimension,
            rerank_embedding_model_kwargs=rerank_embedding_kwargs,
            languages=args.languages,
            max_lines_per_chunk=args.max_lines_per_chunk,
            retrieval_plan=retrieve_plan,
            retrieval_level=args.retrieval_level,
            rerank_window_size=args.rerank_window_size,
            rerank_window_step=args.rerank_window_step,
            enable_rerank=not args.retrieval_only,
            rerank_candidate_top_k=args.rerank_top_k,
        )

        # Query the pipeline
        query = instance["problem_statement"]
        results = pipeline.query(query=query, top_k=query_top_k)

        metrics = evaluate_predictions(
            nodes=results,
            target_files=target_files,
            target_symbols=target_symbols,
            ks=metrics_k,
        )
        aggregate_metrics(aggregate, metrics)
        eval_count += 1
        logger.info("Evaluation metrics for %s:", instance_id)
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

        # Collect results if result_path is provided
        if all_results is not None:
            unique_files_ordered, normalized_symbols = extract_predictions(results)
            metric_k_files = unique_files_ordered[:metric_max_k]
            metric_k_node_ids = normalized_symbols[:metric_max_k]

            result_entry = {
                "instance_id": instance_id,
                "metric_k_node_ids": metric_k_node_ids,
                "metric_k_files": metric_k_files,
                "target_files": (
                    list(metadata.get("target_files", [])) if metadata else []
                ),
                "symbols_modified": (
                    list(metadata.get("symbols_modified", [])) if metadata else []
                ),
                "symbols_added": (
                    list(metadata.get("symbols_added", [])) if metadata else []
                ),
                "symbols_deleted": (
                    list(metadata.get("symbols_deleted", [])) if metadata else []
                ),
                "metrics": metrics,
            }
            all_results.append(result_entry)

    if aggregate and eval_count:
        averaged = average_metrics(aggregate, eval_count)
        logger.info("=== Aggregate Metrics (over %d instances) ===", eval_count)
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

    # Save results to JSON file if result_path is provided
    if args.result_path and all_results is not None:
        result_path = Path(args.result_path).expanduser().resolve()
        result_path.parent.mkdir(parents=True, exist_ok=True)

        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)

        logger.info("Results saved to %s (%d instances)", result_path, len(all_results))


def main():
    """Main entry point."""
    args = parse_args()
    logger.info("Dataset type: %s", args.dataset)
    logger.info("Embedding model: %s", args.embedding_model)
    logger.info("Retrieval mode: %s", args.retrieval_mode)
    logger.info("Retrieval level: %s", args.retrieval_level)
    if args.retrieval_only:
        logger.info("Mode: Retrieval-only (reranking disabled)")
    else:
        logger.info("Mode: Retrieval + Rerank")
        logger.info("Rerank model: %s", args.rerank_model)

    run_pipeline(args)


if __name__ == "__main__":
    main()
