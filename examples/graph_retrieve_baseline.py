#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Sparse-seeded graph retrieval baseline script.

This script demonstrates a three-stage retrieval pipeline:
1. Stage 1: Select a small number of initial nodes (e.g., 5) using BM25.
2. Stage 2: Expand these nodes using the code graph (k-hop BFS) to gather
   a larger set of related nodes (e.g., up to 50).
3. Stage 3 (optional): Use embeddings or a cross-encoder within the expanded
   set to rank/select the final results.

This is a graph-first baseline for comparison with embedding-only retrieval.
It is not the dense-first graph-augmentation pipeline.

Usage:
    # Graph-only (no embedding rerank)
    python examples/graph_retrieve_baseline.py --dataset swebench_lite

    # With embedding rerank on the expanded set
    python examples/graph_retrieve_baseline.py --dataset swebench_lite --embedding

    # With cross-encoder rerank on the expanded set
    python examples/graph_retrieve_baseline.py --dataset swebench_lite \
        --rerank-strategy cross-encoder \
        --rerank-model Qwen/Qwen3-Reranker-0.6B

    # Single instance
    python examples/graph_retrieve_baseline.py --dataset swebench_lite \\
        --filter-instance "^(astropy__astropy-12907)$"

    # Adjust stage parameters
    python examples/graph_retrieve_baseline.py --dataset swebench_lite \\
        --stage1-topk 5 --stage2-topk 50 --k-hop 2

    # codenib_base (multi-language; GT read from the dataset)
    python examples/graph_retrieve_baseline.py --dataset codenib_base \\
        --metrics-k 1 3 5 10

    # Profiling sweep over a fixed corpus CSV
    python examples/graph_retrieve_baseline.py --dataset swebench_lite \\
        --filter-csv examples/selected_instance.csv \\
        --enable-profiler --record-samples --record-memory
"""

import argparse
import csv
import json
import logging
import re
import time
from pathlib import Path
from typing import List, Optional

from codenib.dataset.locbench import LocbenchDataset
from codenib.dataset.swebench import SwebenchDataset
from codenib.eval.retrieval_eval import (
    aggregate_metrics,
    average_metrics,
    collect_targets,
    evaluate_predictions,
    extract_predictions,
)
from codenib.index.rerank import build_reranker
from codenib.log_utils import get_logger
from codenib.model import SparseSeededGraphRetrievePipeline
from codenib.paths import prebuilt_data_dir, user_state_dir
from codenib.profiler import Profiler, percentile_from_sorted

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Language helpers (mirrors embedding_retrieve_baseline / build_embeddings)
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
    """Return chunker languages for this instance.

    CodeNib-base instances carry a ``language_group`` column; fall back to
    the CLI ``--languages`` for datasets that don't have it (SWE-bench /
    Loc-Bench). Only Stage 3 embedding rerank consumes this — graph build and
    BM25 are language-agnostic — so SWE-bench runs are unchanged (default
    ``["python"]``).
    """
    lang_group = instance.get("language_group")
    if lang_group:
        return _map_language_group(lang_group, fallback=cli_languages[0])
    return list(cli_languages)


# ---------------------------------------------------------------------------
# Corpus / query helpers
# ---------------------------------------------------------------------------

# Token-count bins for per-query latency attribution (issue #131 Phase 2).
_QUERY_LENGTH_BUCKETS = ("<128", "128-512", "512-2k", ">2k")
_TIKTOKEN_ENCODING = None


def _load_instance_ids_from_csv(path: str) -> List[str]:
    """Read the ``instance_id`` column from a CSV (header required).

    Mirrors the sampled-CSV reader in ``scripts/swebench_graph_index.py``.
    Returns instance ids in file order with duplicates removed.
    """
    resolved = Path(path).expanduser()
    ids: List[str] = []
    seen = set()
    with open(resolved, newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        if reader.fieldnames is None or "instance_id" not in reader.fieldnames:
            raise ValueError(
                f"CSV {resolved} must have an 'instance_id' column; "
                f"got header {reader.fieldnames!r}"
            )
        for row in reader:
            iid = (row.get("instance_id") or "").strip()
            if iid and iid not in seen:
                seen.add(iid)
                ids.append(iid)
    if not ids:
        raise ValueError(f"No instance_id values found in {resolved}")
    return ids


def _filter_regex_from_ids(ids: List[str]) -> str:
    """Build an anchored regex matching exactly the given instance ids.

    The dataset loaders filter via ``re.match(filter_instance, instance_id)``,
    so we anchor with ``^...$`` and escape each id (instance ids carry no regex
    metacharacters today, but escaping keeps this robust to future ids).
    """
    return "^(" + "|".join(re.escape(i) for i in ids) + ")$"


def _count_query_tokens(text: str) -> int:
    """Token count of a query string.

    Prefers ``tiktoken`` (cl100k_base) for a model-relevant count and caches
    the encoder; falls back to whitespace splitting so the harness never hard
    depends on tiktoken being installed.
    """
    if not text:
        return 0
    global _TIKTOKEN_ENCODING
    if _TIKTOKEN_ENCODING is None:
        try:
            import tiktoken

            _TIKTOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _TIKTOKEN_ENCODING = False  # sentinel: tiktoken unavailable
    if _TIKTOKEN_ENCODING:
        try:
            return len(_TIKTOKEN_ENCODING.encode(text))
        except Exception:
            pass
    return len(text.split())


def _query_length_bucket(text: str) -> str:
    """Bin a query's token count into ``<128 / 128-512 / 512-2k / >2k``."""
    n = _count_query_tokens(text)
    if n < 128:
        return "<128"
    if n < 512:
        return "128-512"
    if n < 2048:
        return "512-2k"
    return ">2k"


def _aggregate_query_length_buckets(instance_query_profiles) -> dict:
    """Histogram of per-instance ``query_length_bucket`` labels."""
    counts = {bucket: 0 for bucket in _QUERY_LENGTH_BUCKETS}
    for profile in instance_query_profiles:
        bucket = profile.get("query_length_bucket")
        if bucket in counts:
            counts[bucket] += 1
    return counts


def _build_dataset(args):
    """Dataset factory — mirrors ``embedding_retrieve_baseline._build_dataset``.

    ``codenib_base`` is loaded lazily so SWE-bench / Loc-Bench runs don't
    import the HuggingFace-backed wrapper (and its ``datasets`` dependency
    chain) unless they need it.
    """
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
    if args.dataset == "codenib_base":
        from codenib.dataset.codenib_base import CodeNibBaseDataset

        return CodeNibBaseDataset(
            dataset="fishmingyu/codenib-base-dataset",
            split=args.split,
            filter_instance=args.filter_instance,
            repo_root=args.repo_cache_dir,
        )
    raise ValueError(f"Unsupported dataset: {args.dataset}")


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
        choices=["swebench_lite", "locbench_v1", "codenib_base"],
    )
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument("--filter-instance", type=str, default=".*")
    parser.add_argument(
        "--filter-csv",
        type=str,
        default=None,
        help=(
            "Path to a CSV with an 'instance_id' column (e.g. "
            "examples/selected_instance.csv). Restricts the run to exactly "
            "those instances. Takes precedence over --filter-instance."
        ),
    )
    # Graph/chunker languages when rebuilding an index.
    # codenib_base auto-detects per instance from its language_group column;
    # SWE-bench / Loc-Bench fall back to this value (default python).
    parser.add_argument(
        "--languages",
        type=str,
        nargs="+",
        default=["python"],
        help=(
            "Primary graph language plus Stage 3 chunker languages. Ignored "
            "for codenib_base instances (read from the language_group column)."
        ),
    )
    parser.add_argument(
        "--graph-route",
        type=str,
        choices=["active", "lsp", "scip-candidate"],
        default="active",
        help=(
            "Graph indexing route. Use lsp for backend comparison and "
            "scip-candidate only for explicit candidate SCIP cold-start "
            "evaluation."
        ),
    )

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
    # Stage 3: Optional rerank
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
        default=str(prebuilt_data_dir()),
    )
    parser.add_argument(
        "--repo-cache-dir",
        type=str,
        default=str(user_state_dir()),
    )
    parser.add_argument("--result-path", type=str, default=None)

    # Rerank strategy. Keep ``cross-encoder`` as the CLI spelling used by the
    # evaluation matrix; the model pipeline's internal spelling is unrelated.
    parser.add_argument(
        "--rerank-strategy",
        type=str,
        choices=["none", "embedding", "cross-encoder"],
        default=None,
        help=(
            "Rerank strategy for Stage 3. 'none' disables rerank, "
            "'embedding' uses FAISS within the expanded set (legacy), "
            "and 'cross-encoder' uses a pairwise Qwen or sentence-transformers "
            "reranker within the expanded set."
        ),
    )
    parser.add_argument(
        "--rerank-model",
        type=str,
        default="Qwen/Qwen3-Reranker-0.6B",
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
            "(default: <index-cache-dir>/profile_log/sparse_seed_graph)."
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

    return parser.parse_args()


def _resolve_rerank_strategy(args):
    """Combine legacy ``--embedding`` flag with ``--rerank-strategy``.

    Returns one of: ``"none"``, ``"embedding"``, ``"cross-encoder"``.
    """
    if args.rerank_strategy is not None:
        return args.rerank_strategy
    return "embedding" if args.embedding else "none"


def _method_label(args, rerank_strategy: str) -> str:
    """Stable method label for sparse-seeded graph-first baselines."""
    expansion = "ppr" if args.ppr else "bfs"
    label = f"sparse_seed_graph_{expansion}"
    if rerank_strategy != "none":
        strategy = rerank_strategy.replace("-", "_")
        label = f"{label}_plus_{strategy}_rerank"
    return label


def run_graph_pipeline(args):
    """Run the graph baseline and own the optional reranker's lifetime."""
    rerank_strategy = _resolve_rerank_strategy(args)
    reranker = None
    if rerank_strategy == "cross-encoder":
        reranker = build_reranker(args.rerank_model)
        logger.info("Cross-encoder reranker loaded: %s", args.rerank_model)

    try:
        return _run_graph_pipeline(args, rerank_strategy, reranker)
    finally:
        if reranker is not None:
            reranker.close()


def _rerank_with_cross_encoder(query, candidates, reranker, top_k):
    """Score graph-expanded candidates through the shared reranker contract."""
    candidates_with_content = [
        candidate for candidate in candidates if candidate.content
    ]
    if not candidates_with_content:
        return candidates[:top_k]

    scores = reranker.score(
        query, [candidate.content for candidate in candidates_with_content]
    )
    ranked = sorted(
        zip(scores, candidates_with_content, strict=True),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return [
        candidate.model_copy(update={"score": float(score)})
        for score, candidate in ranked[:top_k]
    ]


def _query_pipeline(pipeline, query, reranker, top_k, profiler=None):
    """Query the graph pipeline, then optionally apply cross-encoder rerank."""
    if profiler is not None:
        results = pipeline.query(query, profiler=profiler)
    else:
        results = pipeline.query(query)

    if reranker is None:
        return results

    if profiler is not None:
        with profiler.section(
            "query.cross_encoder_rerank", {"candidates": len(results)}
        ):
            results = _rerank_with_cross_encoder(query, results, reranker, top_k)
    else:
        results = _rerank_with_cross_encoder(query, results, reranker, top_k)
    logger.info("Stage 3: %d nodes after cross-encoder rerank", len(results))
    return results


def _run_graph_pipeline(args, rerank_strategy, reranker):
    """Run the graph-based retrieval baseline with resolved rerank resources."""
    use_embedding_rerank = rerank_strategy == "embedding"

    # --filter-csv restricts the run to an explicit instance allowlist; build
    # an anchored regex and let it override --filter-instance (warn if both).
    if args.filter_csv:
        ids = _load_instance_ids_from_csv(args.filter_csv)
        csv_regex = _filter_regex_from_ids(ids)
        if args.filter_instance not in (".*", csv_regex):
            logger.warning(
                "Both --filter-instance and --filter-csv given; "
                "--filter-csv (%d ids) takes precedence.",
                len(ids),
            )
        args.filter_instance = csv_regex
        logger.info("Restricting to %d instance(s) from %s", len(ids), args.filter_csv)

    # Profiler setup ---------------------------------------------------------
    profiling_enabled = args.enable_profiler or args.profile_dir is not None

    def _resolve_profile_dir() -> Path:
        base = (
            Path(args.profile_dir).expanduser().resolve()
            if args.profile_dir
            else Path(args.index_cache_dir).expanduser().resolve()
            / "profile_log"
            / "sparse_seed_graph"
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
    dataset_obj = _build_dataset(args)

    dataset_instances = dataset_obj.load()
    if not dataset_instances:
        raise ValueError(f"No instances found in {args.dataset}")

    logger.info("Loaded %d instance(s)", len(dataset_instances))

    # GT files are dataset-specific; derive the default from --dataset so a
    # Loc-Bench run can't silently align against the SWE-bench ground truth.
    # codenib_base carries GT in-dataset, so an empty path makes
    # load_eval_metadata project the gt_* columns directly (no on-disk file).
    if args.dataset == "codenib_base":
        eval_path = args.eval_instances or ""
    else:
        eval_path = args.eval_instances or str(
            Path.home() / ".codenib" / f"{args.dataset}_{args.split}_gt.json"
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
                name=f"sparse_seed_graph[index][{instance_id}]",
                logger=logger,
                emit_events=False,
                summary_level=logging.INFO,
                record_samples=args.record_samples,
                record_memory=args.record_memory,
            )
            query_profiler = Profiler(
                name=f"sparse_seed_graph[query][{instance_id}]",
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
            # Per-instance chunker language for Stage 3 rerank (codenib_base
            # is multi-language; SWE-bench / Loc-Bench fall back to --languages).
            instance_languages = _resolve_instance_languages(instance, args.languages)

            pipeline = SparseSeededGraphRetrievePipeline(
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
                languages=instance_languages,
                graph_route=args.graph_route,
                project_name=instance_id.replace("/", "__"),
                profiler=index_profiler,
            )
            if query_profiler is not None:
                with query_profiler.section("query.total"):
                    results = _query_pipeline(
                        pipeline,
                        instance["problem_statement"],
                        reranker,
                        args.stage2_topk,
                        profiler=query_profiler,
                    )
            else:
                results = _query_pipeline(
                    pipeline,
                    instance["problem_statement"],
                    reranker,
                    args.stage2_topk,
                )
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
                    {
                        "instance_id": instance_id,
                        "sections": query_only,
                        "query_length_bucket": _query_length_bucket(
                            instance["problem_statement"]
                        ),
                    }
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
                method_label = _method_label(args, rerank_strategy)
                all_results.append(
                    {
                        "instance_id": instance_id,
                        "method": method_label,
                        "stage1_topk": args.stage1_topk,
                        "stage2_topk": args.stage2_topk,
                        "k_hop": args.k_hop,
                        "use_ppr": args.ppr,
                        "ppr_damping": args.ppr_damping,
                        "embedding_rerank": use_embedding_rerank,
                        "rerank_strategy": rerank_strategy,
                        "rerank_model": (
                            args.rerank_model
                            if rerank_strategy == "cross-encoder"
                            else None
                        ),
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
            "filter_csv": args.filter_csv,
            "languages": args.languages,
            "graph_route": args.graph_route,
            "expansion": "ppr" if args.ppr else "bfs",
            "stage1_topk": args.stage1_topk,
            "stage2_topk": args.stage2_topk,
            "k_hop": args.k_hop,
            "ppr_damping": args.ppr_damping,
            "rerank_strategy": rerank_strategy,
            "method": _method_label(args, rerank_strategy),
            "embedding_model": (
                args.embedding_model if rerank_strategy == "embedding" else None
            ),
            "rerank_model": (
                args.rerank_model if rerank_strategy == "cross-encoder" else None
            ),
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
                "query_length_buckets": _aggregate_query_length_buckets(
                    instance_query_profiles
                ),
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
        # Sanitize args.dataset for symmetry with profile_tag — today
        # `choices=` constrains it to safe values, but if that ever
        # loosens we don't want to write outside the resolved profile dir.
        dataset_part = _sanitize_filename_part(args.dataset)
        method_part = _method_label(args, rerank_strategy)
        profile_filename = f"{method_part}_{dataset_part}{tag_part}.json"
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
        "cross-encoder": "-> Cross-encoder rerank",
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
