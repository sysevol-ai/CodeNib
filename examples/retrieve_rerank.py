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
import logging
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
from codeminer.model import RetrieveRerankPipeline, build_retrieve_plan
from codeminer.model.retrieve_rerank_pipeline import RETRIEVAL_TOP_K
from codeminer.profiler import Profiler

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
        choices=["swebench_lite", "locbench_v1", "codeminer_base"],
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
        default="openai/Qwen/Qwen2.5-Coder-7B",
        help=(
            "Full litellm model identifier for reranking "
            "(e.g. 'openai/Qwen/Qwen2.5-Coder-7B'). "
            "Ignored if --retrieval-only is set."
        ),
    )
    parser.add_argument(
        "--rerank-listwise-format",
        type=str,
        default="structured",
        choices=["structured", "rankgpt"],
        help=(
            "Listwise output format for the LLM reranker. 'structured' "
            "(default) enforces a JSON schema via with_structured_output — "
            "right for general-purpose LLMs. 'rankgpt' uses SweRank/RankGPT "
            "text format ([3] > [5] > [1] > ...) and a regex parser; "
            "required for SweRankLLM-* and other models fine-tuned on this "
            "format (forcing JSON on them collapses output to a single "
            "index)."
        ),
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
        default=[1, 3, 5, 10, 15, 20],
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
    parser.add_argument(
        "--profile-dir",
        type=str,
        default=None,
        help=(
            "Directory to store runtime profiler summaries "
            "(default: <index-cache-dir>/profile_log/query_runtime)."
        ),
    )
    parser.add_argument(
        "--enable-profiler",
        action="store_true",
        help="Enable runtime profiler summaries even if --profile-dir is not set.",
    )
    parser.add_argument(
        "--profile-tag",
        type=str,
        default=None,
        help=(
            "Optional tag appended to runtime profiler filename to avoid "
            "overwriting runs."
        ),
    )

    return parser.parse_args()


_LANG_FALLBACK = "python"


def _map_language_group(label, fallback=_LANG_FALLBACK):
    """Map codeminer-base ``language_group`` to chunker language list.

    Mirrors ``scripts/embeddings/build_embeddings.py::_map_language_group``.
    """
    if not label:
        return [fallback]
    text = label.lower()
    if "rust" in text:
        return ["rust"]
    if "javascript" in text and "typescript" in text:
        return ["ts", "js"]
    if "typescript" in text or text == "ts":
        return ["ts"]
    if "javascript" in text or text == "js":
        return ["js"]
    if "c++" in text or text in ("cpp", "c"):
        return ["cpp"]
    if "go" in text or text == "golang":
        return ["go"]
    if "python" in text:
        return ["python"]
    return [fallback]


def _resolve_instance_languages(instance, cli_languages):
    """Use per-instance ``language_group`` if present, else CLI languages."""
    lang_group = instance.get("language_group")
    if lang_group:
        return _map_language_group(lang_group, fallback=cli_languages[0])
    return list(cli_languages)


def _build_method_tag(args) -> str:
    if args.retrieval_only:
        return f"{args.retrieval_mode}_retrieval_only"
    return f"{args.retrieval_mode}_retrieve_plus_{args.rerank_strategy}_rerank"


def _sanitize_filename_part(value: str) -> str:
    return str(value).replace("/", "__").replace(" ", "_")


def _to_sections_payload(profile_summary):
    return [
        {
            "label": label,
            "total": stats.total,
            "count": stats.count,
            "average": stats.average,
            "min": stats.safe_min,
            "max": stats.max_duration,
            "errors": stats.errors,
        }
        for label, stats in profile_summary
    ]


def _aggregate_section_stats(instance_profiles):
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
                },
            )
            entry["total"] += float(section["total"])
            entry["count"] += int(section["count"])
            entry["min"] = min(entry["min"], float(section["min"]))
            entry["max"] = max(entry["max"], float(section["max"]))
            entry["errors"] += int(section["errors"])

    payload = []
    for label, stats in aggregate.items():
        count = stats["count"]
        payload.append(
            {
                "label": label,
                "total": stats["total"],
                "count": count,
                "average": (stats["total"] / count) if count else 0.0,
                "min": 0.0 if stats["min"] == float("inf") else stats["min"],
                "max": stats["max"],
                "errors": stats["errors"],
            }
        )

    payload.sort(key=lambda item: item["total"], reverse=True)
    return payload


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
    elif dataset == "codeminer_base":
        from codeminer.dataset.codeminer_base import CodeMinerBaseDataset

        dataset_name = "fishmingyu/codeminer-base-dataset"
        dataset_split = args.split
        dataset_class = CodeMinerBaseDataset
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
    profiling_enabled = args.enable_profiler or args.profile_dir is not None
    profile_output_dir = (
        Path(args.profile_dir).expanduser().resolve()
        if args.profile_dir
        else Path(args.index_cache_dir).expanduser().resolve()
        / "profile_log"
        / "query_runtime"
    )
    if profiling_enabled:
        profile_output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Runtime profiler summaries will be stored in: %s", profile_output_dir
        )

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
    instance_profiles = []

    # Process each instance
    for instance in dataset_instances:
        instance_id = instance["instance_id"]
        instance_profiler = Profiler(
            name=f"retrieve_rerank[{instance_id}]",
            logger=logger,
            emit_events=False,
            summary_level=logging.INFO,
        )
        instance_profiler.enabled = profiling_enabled
        metadata = eval_metadata.get(instance_id)
        if metadata:
            target_files, target_symbols = collect_targets(metadata)
            if not target_symbols:
                logger.info(f"Skipping {instance_id} - no valid target symbols")
                continue
        else:
            logger.info(f"Skipping {instance_id} - no eval metadata")
            continue

        pipeline = None

        try:
            with instance_profiler.section("instance_total"):
                # Process instance to get repo path
                with instance_profiler.section("dataset.process_instance"):
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

                instance_languages = _resolve_instance_languages(
                    instance, args.languages
                )

                with instance_profiler.section("pipeline.initialize"):
                    pipeline = RetrieveRerankPipeline(
                        repo_path=repo_path,
                        index_path=str(index_path),
                        embedding_model=args.embedding_model,
                        embedding_provider=args.embedding_provider,
                        embedding_dimension=args.embedding_dimension,
                        embedding_model_kwargs=embedding_model_kwargs,
                        rerank_model=args.rerank_model,
                        rerank_strategy=args.rerank_strategy,
                        rerank_embedding_model=args.rerank_embedding_model,
                        rerank_embedding_provider=args.rerank_embedding_provider,
                        rerank_embedding_dimension=args.rerank_embedding_dimension,
                        rerank_embedding_model_kwargs=rerank_embedding_kwargs,
                        languages=instance_languages,
                        max_lines_per_chunk=args.max_lines_per_chunk,
                        retrieval_plan=retrieve_plan,
                        retrieval_level=args.retrieval_level,
                        rerank_window_size=args.rerank_window_size,
                        rerank_window_step=args.rerank_window_step,
                        rerank_listwise_format=args.rerank_listwise_format,
                        enable_rerank=not args.retrieval_only,
                        rerank_candidate_top_k=args.rerank_top_k,
                    )

                # Query the pipeline
                query = instance["problem_statement"]
                with instance_profiler.section("pipeline.query"):
                    results = pipeline.query(query=query, top_k=query_top_k)

                with instance_profiler.section("evaluate_predictions"):
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
                with instance_profiler.section("collect_results"):
                    unique_files_ordered, normalized_symbols = extract_predictions(
                        results
                    )
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
                            list(metadata.get("symbols_modified", []))
                            if metadata
                            else []
                        ),
                        "symbols_added": (
                            list(metadata.get("symbols_added", [])) if metadata else []
                        ),
                        "symbols_deleted": (
                            list(metadata.get("symbols_deleted", []))
                            if metadata
                            else []
                        ),
                        "metrics": metrics,
                    }
                    all_results.append(result_entry)
        finally:
            if pipeline is not None:
                with instance_profiler.section("pipeline.close"):
                    pipeline.close()
            if profiling_enabled:
                logger.info("Runtime profiler summary for %s:", instance_id)
                profile_summary = instance_profiler.report(reset=True)
                sections_payload = _to_sections_payload(profile_summary)
                total_duration = next(
                    (
                        s["total"]
                        for s in sections_payload
                        if s["label"] == "instance_total"
                    ),
                    0.0,
                )
                instance_profiles.append(
                    {
                        "instance_id": instance_id,
                        "total_duration": total_duration,
                        "sections": sections_payload,
                    }
                )

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

    if profiling_enabled:
        method_tag = _sanitize_filename_part(_build_method_tag(args))
        model_tag = _sanitize_filename_part(args.embedding_model)
        filename_parts = [
            _sanitize_filename_part(args.dataset),
            method_tag,
            model_tag,
        ]
        if args.profile_tag:
            filename_parts.append(_sanitize_filename_part(args.profile_tag))
        runtime_profile_file = profile_output_dir / f"{'__'.join(filename_parts)}.json"

        runtime_payload = {
            "dataset": args.dataset,
            "split": args.split,
            "filter_instance": args.filter_instance,
            "retrieval_mode": args.retrieval_mode,
            "retrieval_level": args.retrieval_level,
            "retrieval_only": args.retrieval_only,
            "rerank_strategy": None if args.retrieval_only else args.rerank_strategy,
            "rerank_model": None if args.retrieval_only else args.rerank_model,
            "embedding_model": args.embedding_model,
            "embedding_provider": args.embedding_provider,
            "embedding_dimension": args.embedding_dimension,
            "profile_tag": args.profile_tag,
            "instance_count": len(instance_profiles),
            "aggregate_sections": _aggregate_section_stats(instance_profiles),
            "instances": instance_profiles,
        }
        runtime_profile_file.write_text(
            json.dumps(runtime_payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Saved runtime profiler results to %s", runtime_profile_file)


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
