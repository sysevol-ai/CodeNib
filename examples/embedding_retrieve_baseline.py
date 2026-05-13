#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Embedding-only retrieval baseline script.

This script retrieves top-K nodes using embedding similarity search over the
entire codebase, for comparison with the graph-based baseline.

Supports both SWE-bench (Python-only) and CodeMiner-base (multi-language)
datasets. For CodeMiner-base, ground-truth annotations are read directly from
the dataset — no external eval JSON is required.

Usage:
    # SWE-bench Lite
    python examples/embedding_retrieve_baseline.py --dataset swebench_lite

    # CodeMiner-base (uses pre-built indices from build_embeddings.py)
    python examples/embedding_retrieve_baseline.py \\
        --dataset codeminer_base \\
        --embedding-model Qwen/Qwen3-Embedding-0.6B \\
        --embedding-dimension 1024 \\
        --index-cache-dir /mnt/data/codeminer

    # With profiling
    python examples/embedding_retrieve_baseline.py \\
        --dataset codeminer_base \\
        --embedding-model Qwen/Qwen3-Embedding-0.6B \\
        --embedding-dimension 1024 \\
        --enable-profiler \\
        --profile-tag qwen3_0.6b_run1

    # Single instance
    python examples/embedding_retrieve_baseline.py --dataset swebench_lite \\
        --filter-instance "^(astropy__astropy-12907)$"
"""
import argparse
import json
import logging
from pathlib import Path
from typing import List, Optional

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
from codeminer.model.embedding_retrieve_pipeline import EmbeddingRetrievePipeline
from codeminer.profiler import Profiler

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Language helpers (mirrors build_embeddings._map_language_group)
# ---------------------------------------------------------------------------


def _map_language_group(label: Optional[str], fallback: str = "python") -> List[str]:
    """Map a dataset ``language_group`` value to chunker language string(s)."""
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


def _resolve_instance_languages(instance: dict, cli_languages: List[str]) -> List[str]:
    """Return languages for this instance.

    CodeMiner-base instances carry a ``language_group`` column; fall back to
    the CLI ``--languages`` for datasets that don't have it (SWE-bench).
    """
    lang_group = instance.get("language_group")
    if lang_group:
        return _map_language_group(lang_group, fallback=cli_languages[0])
    return list(cli_languages)


# ---------------------------------------------------------------------------
# Profiling helpers (mirrors retrieve_rerank.py)
# ---------------------------------------------------------------------------


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


def _sanitize_filename_part(value: str) -> str:
    return str(value).replace("/", "__").replace(" ", "_")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(
        description="Embedding-only retrieval baseline.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dataset",
        type=str,
        required=True,
        choices=["swebench_lite", "locbench_v1", "codeminer_base"],
    )
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument("--filter-instance", type=str, default=".*")
    parser.add_argument("--topk", type=int, default=50)

    # Embedding config
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

    # Language / chunking (for SWE-bench; codeminer_base auto-detects per instance)
    parser.add_argument(
        "--languages",
        type=str,
        nargs="+",
        default=["python"],
        help=(
            "Chunker languages to use when rebuilding an index. "
            "Ignored for codeminer_base instances (language is read from "
            "language_group column)."
        ),
    )

    # Index rebuild control
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        default=False,
        help=(
            "Force rebuilding the embedding index even if a pre-built one "
            "exists. By default pre-built indices are loaded from disk."
        ),
    )

    # Evaluation
    parser.add_argument(
        "--eval-instances",
        type=str,
        default=None,
        help=(
            "Path to eval annotations JSON. For codeminer_base this is "
            "optional — GT is read directly from the dataset."
        ),
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

    # Profiling
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


# ---------------------------------------------------------------------------
# Dataset factory
# ---------------------------------------------------------------------------


def _build_dataset(args):
    if args.dataset == "swebench_lite":
        return SwebenchDataset(
            dataset="princeton-nlp/SWE-bench_Lite",
            split=args.split,
            filter_instance=args.filter_instance,
            repo_root=args.repo_cache_dir,
        )
    if args.dataset == "locbench_v1":
        return LocbenchDataset(
            dataset="czlll/Loc-Bench_V1",
            split=args.split,
            filter_instance=args.filter_instance,
            repo_root=args.repo_cache_dir,
        )
    if args.dataset == "codeminer_base":
        from codeminer.dataset.codeminer_base import CodeMinerBaseDataset

        return CodeMinerBaseDataset(
            dataset="fishmingyu/codeminer-base-dataset",
            split=args.split,
            filter_instance=args.filter_instance,
            repo_root=args.repo_cache_dir,
        )
    raise ValueError(f"Unsupported dataset: {args.dataset}")


# ---------------------------------------------------------------------------
# Main pipeline runner
# ---------------------------------------------------------------------------


def run_embedding_pipeline(args):
    """Run the embedding-only retrieval baseline."""
    dataset_obj = _build_dataset(args)
    dataset_instances = dataset_obj.load()
    if not dataset_instances:
        raise ValueError(f"No instances found in {args.dataset}")

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

    eval_path = args.eval_instances or ""
    eval_metadata = dataset_obj.load_eval_metadata(eval_path)

    metrics_k = sorted(set(args.metrics_k))
    metric_max_k = max(metrics_k)
    aggregate = {}
    eval_count = 0
    all_results = [] if args.result_path else None
    instance_profiles = []

    # ---------------------------------------------------------------------------
    # Initialise the embedding model ONCE outside the instance loop.
    # Each iteration then hot-swaps only the FAISS index via load_index(),
    # avoiding repeated GPU weight transfers for large models.
    # ---------------------------------------------------------------------------
    global_profiler = Profiler(
        name="embedding_baseline[global]",
        logger=logger,
        emit_events=False,
        summary_level=logging.INFO,
    )
    global_profiler.enabled = profiling_enabled

    with global_profiler.section("model.initialize"):
        pipeline = EmbeddingRetrievePipeline(
            embedding_model=args.embedding_model,
            embedding_provider=args.embedding_provider,
            embedding_dimension=args.embedding_dimension,
            top_k=args.topk,
        )
    logger.info(
        "Embedding model loaded: %s (provider=%s)",
        args.embedding_model,
        args.embedding_provider,
    )

    try:
        for instance in dataset_instances:
            instance_id = instance["instance_id"]
            instance_profiler = Profiler(
                name=f"embedding_baseline[{instance_id}]",
                logger=logger,
                emit_events=False,
                summary_level=logging.INFO,
            )
            instance_profiler.enabled = profiling_enabled

            metadata = eval_metadata.get(instance_id)
            if not metadata:
                logger.info("Skipping %s - no eval metadata", instance_id)
                continue
            target_files, target_symbols = collect_targets(
                metadata, simplified_symbols=dataset_obj.simplified_symbols
            )
            if not target_symbols:
                logger.info("Skipping %s - no valid target symbols", instance_id)
                continue

            try:
                with instance_profiler.section("instance_total"):
                    with instance_profiler.section("dataset.process_instance"):
                        dataset_obj.process_instance(instance)

                    index_path = str(
                        Path(args.index_cache_dir) / instance_id.replace("/", "__")
                    )

                    with instance_profiler.section("pipeline.load_index"):
                        pipeline.load_index(index_path)

                    with instance_profiler.section("pipeline.query"):
                        results = pipeline.query(instance["problem_statement"])

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

                if all_results is not None:
                    with instance_profiler.section("collect_results"):
                        unique_files, normalized_symbols = extract_predictions(results)
                        all_results.append(
                            {
                                "instance_id": instance_id,
                                "method": "embedding_baseline",
                                "dataset": args.dataset,
                                "embedding_model": args.embedding_model,
                                "topk": args.topk,
                                "num_results": len(results),
                                "metric_k_files": unique_files[:metric_max_k],
                                "metric_k_node_ids": normalized_symbols[:metric_max_k],
                                "metrics": metrics,
                            }
                        )

            except Exception:
                logger.exception("Error processing %s", instance_id)
                continue
            finally:
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

    finally:
        with global_profiler.section("model.close"):
            pipeline.close()

    # ---- Aggregate metrics ----
    if aggregate and eval_count:
        averaged = average_metrics(aggregate, eval_count)
        logger.info(
            "=== Embedding Baseline Aggregate (%d instances) ===",
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
        logger.info("Results saved to %s (%d instances)", result_path, len(all_results))

    # ---- Runtime profile JSON ----
    if profiling_enabled:
        model_tag = _sanitize_filename_part(args.embedding_model)
        filename_parts = [
            _sanitize_filename_part(args.dataset),
            "embedding_baseline",
            model_tag,
        ]
        if args.profile_tag:
            filename_parts.append(_sanitize_filename_part(args.profile_tag))
        runtime_profile_file = profile_output_dir / f"{'__'.join(filename_parts)}.json"

        runtime_payload = {
            "dataset": args.dataset,
            "split": args.split,
            "filter_instance": args.filter_instance,
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
    args = parse_args()
    logger.info("Dataset: %s", args.dataset)
    logger.info("Embedding model: %s", args.embedding_model)
    logger.info("Pipeline: Embedding(top%d)", args.topk)
    run_embedding_pipeline(args)


if __name__ == "__main__":
    main()
