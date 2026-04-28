#!/usr/bin/env python3
"""
Codeminer-base × embedding-rerank matrix sweep.

Loads each (small, large) embedding pair ONCE per group and iterates the
dataset, instead of reloading models per instance like
``examples/retrieve_rerank.py``. Outer loop groups by small model so the small
encoder is loaded once per group; inner loop swaps the large reranker.

For each instance:
  1. Hot-swap the small model's pre-built FAISS index from disk.
  2. Run dense retrieval on the small model to get RERANK_TOP_K candidates.
  3. Score the candidate ``content`` blobs with the resident large model
     online (mirrors ``RetrieveRerankPipeline._rerank_embedding``).
  4. Evaluate metrics, optionally collect results.

Output filenames match the existing sweep conventions so downstream
tooling keeps working. ``<pair>`` below is ``<small>__x__<large>``:
  - results: ``<results>/rerank_codeminer_base_<split>_<pair>.json``
  - profile: ``<profile>/codeminer_base__dense_retrieve_plus_``
    ``embedding_rerank__<small>__<pair>.json``

Usage:
    python examples/codeminer_base_rerank_matrix.py \\
        --smalls Salesforce/SweRankEmbed-Small:768 Qwen/Qwen3-Embedding-0.6B:1024 \\
        --larges fishmingyu/SweRankEmbed-Large:3584 \\
                 Qwen/Qwen3-Embedding-4B:2560 \\
                 jinaai/jina-code-embeddings-1.5b:1536 \\
        --rerank-top-k 30
"""
import argparse
import gc
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from codeminer.dataset.codeminer_base import CodeMinerBaseDataset
from codeminer.eval.retrieval_eval import (
    aggregate_metrics,
    average_metrics,
    collect_targets,
    evaluate_predictions,
    extract_predictions,
)
from codeminer.index.embedding import CodeVectorStore
from codeminer.log_utils import get_logger
from codeminer.model.embedding_retrieve_pipeline import EmbeddingRetrievePipeline
from codeminer.profiler import Profiler
from codeminer.types import QueriedNode

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _parse_pair(entry: str) -> Tuple[str, int]:
    if ":" not in entry:
        raise argparse.ArgumentTypeError(f"Expected MODEL:DIM, got {entry!r}")
    model, dim = entry.rsplit(":", 1)
    try:
        return model, int(dim)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Bad dimension in {entry!r}: {exc}") from exc


def _sanitize(value: str) -> str:
    return str(value).replace("/", "__").replace(" ", "_")


def parse_args():
    p = argparse.ArgumentParser(
        description="Codeminer-base × embedding-rerank matrix sweep.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--smalls",
        nargs="+",
        required=True,
        type=_parse_pair,
        help="Small (retrieval) models as MODEL:DIM (one or more).",
    )
    p.add_argument(
        "--larges",
        nargs="+",
        required=True,
        type=_parse_pair,
        help="Large (rerank) models as MODEL:DIM (one or more).",
    )
    p.add_argument(
        "--dataset",
        type=str,
        default="fishmingyu/codeminer-base-dataset",
        help="HuggingFace dataset name.",
    )
    p.add_argument("--split", type=str, default="test")
    p.add_argument("--filter-instance", type=str, default=".*")
    p.add_argument(
        "--rerank-top-k",
        type=int,
        default=30,
        help="Number of small-model candidates handed to the large reranker.",
    )
    p.add_argument(
        "--small-batch-size",
        type=int,
        default=32,
        help="Encoder batch size for the small model (used only when an "
        "instance's index is missing and would need rebuild).",
    )
    p.add_argument(
        "--large-batch-size",
        type=int,
        default=8,
        help="Encoder batch size for the large reranker (per-query candidate "
        "embedding).",
    )
    p.add_argument(
        "--max-seq-length",
        type=int,
        default=8192,
        help="Cap large-model tokenization (matches build script).",
    )
    p.add_argument(
        "--metrics-k",
        type=int,
        nargs="+",
        default=[1, 3, 5, 10, 15, 20],
    )
    p.add_argument(
        "--repo-cache-dir",
        type=str,
        default="~/.codeminer/",
    )
    p.add_argument(
        "--index-cache-dir",
        type=str,
        default="/mnt/data/codeminer",
    )
    p.add_argument(
        "--results-dir",
        type=str,
        default=None,
        help="Directory for per-pair result JSON. "
        "Defaults to <index-cache-dir>/eval_results.",
    )
    p.add_argument(
        "--profile-dir",
        type=str,
        default=None,
        help="Directory for runtime profile JSON. "
        "Defaults to <index-cache-dir>/profile_log/query_runtime.",
    )
    p.add_argument(
        "--profile-tag",
        type=str,
        default=None,
        help="Optional suffix appended to profile/result filenames.",
    )
    p.add_argument(
        "--no-profile",
        action="store_true",
        help="Disable runtime profiling.",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Profiling helpers (mirror retrieve_rerank.py / embedding_retrieve_baseline.py)
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


# ---------------------------------------------------------------------------
# Model + rerank helpers
# ---------------------------------------------------------------------------


def _build_large_store(
    model: str,
    dim: int,
    batch_size: int,
    max_seq_length: Optional[int],
) -> CodeVectorStore:
    """Construct a model-only CodeVectorStore (no index) for online rerank."""
    embedding_kwargs = {
        "model_kwargs": {"trust_remote_code": True},
        "encode_kwargs": {"batch_size": batch_size},
    }
    if max_seq_length:
        embedding_kwargs["max_seq_length"] = max_seq_length
    return CodeVectorStore(
        embedding_model=model,
        embedding_provider="huggingface",
        dimension=dim,
        index_metric="ip",
        store_path=None,
        **embedding_kwargs,
    )


def _rerank_with_large(
    query: str,
    candidates: List[QueriedNode],
    large_store: CodeVectorStore,
    top_k: int,
) -> List[QueriedNode]:
    """Score candidates online with the resident large model.

    Mirrors ``RetrieveRerankPipeline._rerank_embedding``.
    """
    candidates_with_content = [c for c in candidates if c.content]
    if not candidates_with_content:
        return []

    q_vec = np.array(large_store.embedding.embed_query(query), dtype=np.float32)
    d_vecs = np.array(
        large_store.embedding.embed_documents(
            [c.content for c in candidates_with_content]
        ),
        dtype=np.float32,
    )
    metric = large_store.index_metric
    if metric == "ip":
        scores = np.dot(d_vecs, q_vec).tolist()
    elif metric == "l2":
        scores = (-np.linalg.norm(d_vecs - q_vec, axis=1)).tolist()
    else:
        raise ValueError(f"Unsupported index metric: {metric!r}")

    ranked = sorted(
        zip(scores, candidates_with_content, strict=True),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return [c.model_copy(update={"score": float(score)}) for score, c in ranked[:top_k]]


def _release_gpu(*objs):
    for obj in objs:
        if obj is None:
            continue
        try:
            obj.close()
        except Exception:
            logger.exception("Error closing %r", obj)
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------


def run_sweep(args):
    metrics_k = sorted(set(args.metrics_k))
    metric_max_k = max(metrics_k)
    if args.rerank_top_k < metric_max_k:
        raise ValueError(
            "--rerank-top-k must be >= the largest --metrics-k value "
            f"({metric_max_k}), got {args.rerank_top_k}."
        )

    index_cache_dir = Path(args.index_cache_dir).expanduser().resolve()
    results_dir = (
        Path(args.results_dir).expanduser().resolve()
        if args.results_dir
        else index_cache_dir / "eval_results"
    )
    profile_dir = (
        Path(args.profile_dir).expanduser().resolve()
        if args.profile_dir
        else index_cache_dir / "profile_log" / "query_runtime"
    )
    results_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)

    profiling_enabled = not args.no_profile

    # ----- Load dataset ONCE for the entire sweep -----
    dataset_obj = CodeMinerBaseDataset(
        dataset=args.dataset,
        split=args.split,
        filter_instance=args.filter_instance,
        repo_root=args.repo_cache_dir,
    )
    instances = dataset_obj.load()
    if not instances:
        raise ValueError(
            f"No instances matched filter {args.filter_instance!r} in "
            f"{args.dataset} ({args.split})."
        )
    logger.info("Loaded %d instance(s)", len(instances))

    # codeminer-base bakes GT into the dataset, so eval_path can be empty.
    eval_metadata = dataset_obj.load_eval_metadata("")

    # ----- Group pairs by small model so we load each small once. -----
    pairs_by_small = defaultdict(list)
    for s_model, s_dim in args.smalls:
        for l_model, l_dim in args.larges:
            if s_model == l_model:
                logger.info("[skip] identity pair %s", s_model)
                continue
            pairs_by_small[(s_model, s_dim)].append((l_model, l_dim))

    if not pairs_by_small:
        raise ValueError("No (small × large) pairs to run after deduping.")

    total_pairs = sum(len(v) for v in pairs_by_small.values())
    logger.info(
        "Sweep plan: %d pair(s) across %d small group(s).",
        total_pairs,
        len(pairs_by_small),
    )

    # ----- Outer loop: small group -----
    for (s_model, s_dim), large_specs in pairs_by_small.items():
        logger.info("================================================================")
        logger.info(
            "Small group: %s (%dd) — %d large(s) to sweep",
            s_model,
            s_dim,
            len(large_specs),
        )
        logger.info("================================================================")

        small_pipe: Optional[EmbeddingRetrievePipeline] = None
        try:
            small_pipe = EmbeddingRetrievePipeline(
                embedding_model=s_model,
                embedding_provider="huggingface",
                embedding_dimension=s_dim,
                top_k=args.rerank_top_k,
                embedding_kwargs={
                    "model_kwargs": {"trust_remote_code": True},
                    "encode_kwargs": {"batch_size": args.small_batch_size},
                },
            )
            logger.info("Small model loaded: %s", s_model)

            # ----- Inner loop: each large reranker -----
            for l_model, l_dim in large_specs:
                pair_tag = f"{_sanitize(s_model)}__x__{_sanitize(l_model)}"
                if args.profile_tag:
                    pair_tag = f"{pair_tag}__{_sanitize(args.profile_tag)}"

                result_file = (
                    results_dir / f"rerank_codeminer_base_{args.split}_{pair_tag}.json"
                )
                profile_file = profile_dir / (
                    "codeminer_base__"
                    "dense_retrieve_plus_embedding_rerank__"
                    f"{_sanitize(s_model)}__{pair_tag}.json"
                )

                logger.info(
                    "  Pair: small=%s × large=%s (%dd)",
                    s_model,
                    l_model,
                    l_dim,
                )
                logger.info("  → %s", result_file)

                large_store: Optional[CodeVectorStore] = None
                try:
                    large_store = _build_large_store(
                        l_model,
                        l_dim,
                        args.large_batch_size,
                        args.max_seq_length,
                    )
                    logger.info("  Large model loaded: %s", l_model)

                    aggregate = {}
                    eval_count = 0
                    pair_results = []
                    instance_profiles = []

                    # ----- Iterate instances under this (small, large) -----
                    for instance in instances:
                        instance_id = instance["instance_id"]
                        prof = Profiler(
                            name=f"rerank_matrix[{instance_id}]",
                            logger=logger,
                            emit_events=False,
                            summary_level=logging.INFO,
                        )
                        prof.enabled = profiling_enabled

                        metadata = eval_metadata.get(instance_id)
                        if not metadata:
                            logger.info("  Skipping %s — no eval metadata", instance_id)
                            continue
                        target_files, target_symbols = collect_targets(
                            metadata,
                            simplified_symbols=dataset_obj.simplified_symbols,
                        )
                        if not target_symbols:
                            logger.info(
                                "  Skipping %s — no valid target symbols",
                                instance_id,
                            )
                            continue

                        try:
                            with prof.section("instance_total"):
                                with prof.section("dataset.process_instance"):
                                    dataset_obj.process_instance(instance)
                                index_path = str(
                                    index_cache_dir / instance_id.replace("/", "__")
                                )

                                with prof.section("pipeline.load_index"):
                                    small_pipe.load_index(index_path)

                                with prof.section("retrieve.small"):
                                    candidates = small_pipe.query(
                                        instance["problem_statement"],
                                        top_k=args.rerank_top_k,
                                    )

                                with prof.section("rerank.large"):
                                    reranked = _rerank_with_large(
                                        instance["problem_statement"],
                                        candidates,
                                        large_store,
                                        top_k=metric_max_k,
                                    )

                                with prof.section("evaluate_predictions"):
                                    metrics = evaluate_predictions(
                                        nodes=reranked,
                                        target_files=target_files,
                                        target_symbols=target_symbols,
                                        ks=metrics_k,
                                    )

                            aggregate_metrics(aggregate, metrics)
                            eval_count += 1

                            with prof.section("collect_results"):
                                unique_files, normalized_symbols = extract_predictions(
                                    reranked
                                )
                                pair_results.append(
                                    {
                                        "instance_id": instance_id,
                                        "method": (
                                            "dense_retrieve_plus_" "embedding_rerank"
                                        ),
                                        "small_model": s_model,
                                        "large_model": l_model,
                                        "rerank_top_k": args.rerank_top_k,
                                        "metric_k_files": (unique_files[:metric_max_k]),
                                        "metric_k_node_ids": (
                                            normalized_symbols[:metric_max_k]
                                        ),
                                        "target_files": list(
                                            metadata.get("target_files", [])
                                        ),
                                        "metrics": metrics,
                                    }
                                )

                            for scope, per_k in metrics.items():
                                for k, stats in per_k.items():
                                    logger.debug(
                                        "    [%s] %s k=%d acc=%.3f recall=%.3f",
                                        instance_id,
                                        scope,
                                        k,
                                        stats["accuracy"],
                                        stats["recall"],
                                    )

                        except Exception:
                            logger.exception("  Error processing %s", instance_id)
                            continue
                        finally:
                            if profiling_enabled:
                                summary = prof.report(reset=True)
                                sections_payload = _to_sections_payload(summary)
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

                    # ----- Pair done: write results + profile -----
                    if aggregate and eval_count:
                        averaged = average_metrics(aggregate, eval_count)
                        logger.info(
                            "  === Pair aggregate (%d instances) ===",
                            eval_count,
                        )
                        for scope, per_k in averaged.items():
                            for k, stats in per_k.items():
                                logger.info(
                                    "  [%s] k=%d acc=%.3f prec=%.3f "
                                    "recall=%.3f avg_hits=%.3f",
                                    scope,
                                    k,
                                    stats["accuracy"],
                                    stats["precision"],
                                    stats["recall"],
                                    stats["avg_hits"],
                                )

                    result_file.write_text(
                        json.dumps(pair_results, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    logger.info(
                        "  Saved results: %s (%d instances)",
                        result_file,
                        len(pair_results),
                    )

                    if profiling_enabled:
                        runtime_payload = {
                            "dataset": "codeminer_base",
                            "split": args.split,
                            "filter_instance": args.filter_instance,
                            "method": "dense_retrieve_plus_embedding_rerank",
                            "small_model": s_model,
                            "small_dimension": s_dim,
                            "large_model": l_model,
                            "large_dimension": l_dim,
                            "rerank_top_k": args.rerank_top_k,
                            "instance_count": len(instance_profiles),
                            "aggregate_sections": _aggregate_section_stats(
                                instance_profiles
                            ),
                            "instances": instance_profiles,
                        }
                        profile_file.write_text(
                            json.dumps(runtime_payload, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        logger.info("  Saved profile: %s", profile_file)
                finally:
                    _release_gpu(large_store)
                    large_store = None
        finally:
            _release_gpu(small_pipe)
            small_pipe = None


def main():
    args = parse_args()
    logger.info("Smalls: %s", [f"{m}:{d}" for m, d in args.smalls])
    logger.info("Larges: %s", [f"{m}:{d}" for m, d in args.larges])
    logger.info("Rerank top-K: %d", args.rerank_top_k)
    run_sweep(args)


if __name__ == "__main__":
    main()
