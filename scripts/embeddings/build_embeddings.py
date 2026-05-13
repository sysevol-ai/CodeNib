#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Build and cache hierarchical embedding indices for SWE-bench or CodeMiner-base instances.
Each instance's embedding will be stored in <storage-dir>/{instance_id}/

Usage:
    # SWE-bench Lite (default, Python-only)
    python scripts/embeddings/build_embeddings.py \\
        --filter-instance "^(astropy__astropy-6938)$" \\
        --force-rebuild

    # CodeMiner-base (multi-language, auto-detects language per instance)
    python scripts/embeddings/build_embeddings.py \\
        --dataset-class codeminer_base \\
        --dataset fishmingyu/codeminer-base-dataset \\
        --enable-profiler
"""

import argparse
import gc
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

from codeminer.index.embedding import build_hierarchical_vector_store
from codeminer.log_utils import get_logger
from codeminer.profiler import Profiler

logger = get_logger(__name__)

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Build hierarchical embedding indices for instances",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Dataset configuration
    parser.add_argument(
        "--dataset-class",
        type=str,
        choices=["swebench", "codeminer_base"],
        default="swebench",
        help=(
            "Dataset class to use. 'swebench' for SWE-bench Lite/Verified, "
            "'codeminer_base' for the multi-language CodeMiner-base dataset "
            "(auto-detects language per instance from 'language_group' column)."
        ),
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default=None,
        help=(
            "HuggingFace dataset name. Defaults to "
            "'princeton-nlp/SWE-bench_Lite' for swebench, "
            "'fishmingyu/codeminer-base-dataset' for codeminer_base."
        ),
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
        help="Regex pattern to filter instances (default: .* processes all instances)",
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
        default=8,
        help="Batch size for embedding encoding",
    )
    parser.add_argument(
        "--max-seq-length",
        type=int,
        default=None,
        help=(
            "Override the model's max sequence length for tokenization. "
            "Use to prevent CUDA OOM on models with long context windows "
            "when flash-attn is not installed (e.g. 8192 for jina-code-1.5b)."
        ),
    )
    parser.add_argument(
        "--index-metric",
        type=str,
        default="ip",
        choices=["ip", "l2"],
        help="Distance metric for FAISS index (ip: inner product, l2: L2 distance)",
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
        default=None,
        help=(
            "Maximum lines per L2 code chunk (default: None, no splitting to "
            "preserve function integrity)"
        ),
    )
    parser.add_argument(
        "--build-levels",
        type=str,
        nargs="+",
        default=["L0", "L2"],
        choices=["L0", "L2"],
        help="Chunk levels to build and index (choose one or both)",
    )
    parser.add_argument(
        "--plan-name",
        type=str,
        default=None,
        help="Optional plan name to nest embedding artifacts under.",
    )

    # Storage configuration
    parser.add_argument(
        "--storage-dir",
        type=str,
        default="/mnt/data/codeminer",
        help="Base directory to store embeddings",
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        default=False,
        help="Force rebuild embeddings even if they already exist",
    )

    # Profiling configuration
    parser.add_argument(
        "--profile-dir",
        type=str,
        default=None,
        help="Directory to store profiler summaries (default: <storage-dir>/profile_log)",
    )
    parser.add_argument(
        "--enable-profiler",
        action="store_true",
        help="Enable profiler summaries even if --profile-dir is not provided.",
    )
    parser.add_argument(
        "--profile-tag",
        type=str,
        default=None,
        help=(
            "Optional tag appended to profiler output filename to avoid "
            "overwriting runs (e.g., dev_run1, rerank_expA)."
        ),
    )
    parser.add_argument(
        "--isolate-instances",
        action="store_true",
        default=False,
        help=(
            "Run each instance in a separate subprocess for CUDA fault "
            "isolation. Prevents OOM in one instance from corrupting the "
            "GPU state for subsequent instances."
        ),
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Language mapping (dataset language_group -> chunker language)
# ---------------------------------------------------------------------------

_DATASET_DEFAULTS = {
    "swebench": "princeton-nlp/SWE-bench_Lite",
    "codeminer_base": "fishmingyu/codeminer-base-dataset",
}


def _map_language_group(label: Optional[str], fallback: str = "python") -> List[str]:
    """Map a dataset ``language_group`` value to chunker language string(s).

    Mirrors ``swebench_graph_index._map_language_label`` with an added Go
    mapping so the codeminer-base multilingual instances get the right chunker.

    Returns a list because some language groups (e.g. "TypeScript/JavaScript")
    cover multiple chunker languages with disjoint file extensions.
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


def _resolve_languages(instance: dict, cli_languages: List[str]) -> List[str]:
    """Return the language list for a single instance.

    If the instance has a ``language_group`` column (codeminer-base), derive
    the chunker language from it.  Otherwise fall back to ``cli_languages``.
    """
    lang_group = instance.get("language_group")
    if lang_group:
        return _map_language_group(lang_group, fallback=cli_languages[0])
    return list(cli_languages)


def _load_dataset(args):
    """Instantiate the dataset object based on ``--dataset-class``."""
    dataset_name = args.dataset or _DATASET_DEFAULTS[args.dataset_class]

    if args.dataset_class == "codeminer_base":
        from codeminer.dataset.codeminer_base import CodeMinerBaseDataset

        return CodeMinerBaseDataset(
            dataset=dataset_name,
            split=args.split,
            filter_instance=args.filter_instance,
        )

    from codeminer.dataset.swebench import SwebenchDataset

    return SwebenchDataset(
        dataset=dataset_name,
        split=args.split,
        filter_instance=args.filter_instance,
    )


def build_embeddings(args):
    """Build hierarchical embedding indices for dataset instances."""

    build_levels = [level.lower() for level in args.build_levels]

    # Load dataset
    dataset_obj = _load_dataset(args)
    dataset_instances = dataset_obj.load()

    if len(dataset_instances) == 0:
        raise ValueError(
            f"No instances found in {args.dataset or _DATASET_DEFAULTS[args.dataset_class]}"
        )

    logger.info(f"Loaded {len(dataset_instances)} instance(s)")
    logger.info(f"Dataset class: {args.dataset_class}")
    logger.info(f"Embeddings will be stored in: {args.storage_dir}")

    # Setup profile output directory
    profile_output_dir = (
        Path(args.profile_dir).expanduser()
        if args.profile_dir
        else Path(args.storage_dir) / "profile_log"
    )
    profile_output_dir.mkdir(parents=True, exist_ok=True)
    if args.profile_dir:
        logger.info(f"Profiler summaries will be stored in: {profile_output_dir}")

    # Process each instance
    for idx, instance in enumerate(dataset_instances):
        instance_id = instance["instance_id"]
        logger.info(f"\n{'='*80}")
        logger.info(f"Processing [{idx+1}/{len(dataset_instances)}]: {instance_id}")
        logger.info(f"{'='*80}")

        vector_store = None
        try:
            # Create profiler for this instance
            instance_profiler = Profiler(
                name=f"build_embeddings[{instance_id}]",
                logger=logger,
                emit_events=False,
                summary_level=logging.INFO,
            )
            instance_profiler.enabled = (
                args.enable_profiler or args.profile_dir is not None
            )

            # Process instance to get repo path
            dataset_obj.process_instance(instance)
            repo_path = dataset_obj.get_repo_path(instance)

            # Convert instance_id to directory name (replace / with __)
            instance_dir_name = instance_id.replace("/", "__")

            # Set final directory for this instance
            instance_final_dir = Path(args.storage_dir) / instance_dir_name
            instance_final_dir.mkdir(parents=True, exist_ok=True)

            # Resolve per-instance language (uses language_group when available)
            instance_languages = _resolve_languages(instance, args.languages)

            logger.info(f"Repository path: {repo_path}")
            logger.info(f"Target directory: {instance_final_dir}")
            logger.info(f"Languages: {instance_languages}")

            # Check if embedding already exists (model-specific config)
            model_suffix = args.embedding_model.replace("/", "__")
            config_file = instance_final_dir / f"config_{model_suffix}.json"
            if config_file.exists() and not args.force_rebuild:
                logger.info(
                    f"Embedding already exists at {instance_final_dir}, skipping..."
                )
                continue
            elif config_file.exists() and args.force_rebuild:
                logger.info(
                    "Embedding already exists but force-rebuild is enabled, rebuilding..."
                )

            embedding_kwargs = {}
            if args.trust_remote_code:
                embedding_kwargs["model_kwargs"] = {"trust_remote_code": True}
            if args.batch_size:
                embedding_kwargs["encode_kwargs"] = {"batch_size": args.batch_size}
            if args.max_seq_length:
                embedding_kwargs["max_seq_length"] = args.max_seq_length

            logger.info("Building hierarchical vector store...")
            plan_name = args.plan_name
            with instance_profiler.section("build_vector_store"):
                vector_store = build_hierarchical_vector_store(
                    repo_path=repo_path,
                    index_path=str(instance_final_dir),
                    plan_name=plan_name,
                    languages=instance_languages,
                    max_lines_per_chunk=args.max_lines_per_chunk,
                    build_levels=build_levels,
                    embedding_model=args.embedding_model,
                    embedding_provider=args.embedding_provider,
                    embedding_dimension=args.embedding_dimension,
                    embedding_kwargs=embedding_kwargs,
                    index_metric=args.index_metric,
                    profiler=instance_profiler,
                    force_rebuild=args.force_rebuild,
                )

            # Save profiler report
            if args.enable_profiler or args.profile_dir:
                logger.info(f"Profiler summary for {instance_id}:")
                profile_summary = instance_profiler.report(reset=True)

                sections_payload = [
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

                chunk_stats = getattr(vector_store, "chunk_stats", {})
                profile_payload = {
                    "instance_id": instance_id,
                    "repo": instance.get("repo", "unknown"),
                    "base_commit": instance.get("base_commit", "unknown"),
                    "language_group": instance.get("language_group"),
                    "languages": instance_languages,
                    "dataset_class": args.dataset_class,
                    "embedding_model": args.embedding_model,
                    "embedding_provider": args.embedding_provider,
                    "embedding_dimension": args.embedding_dimension,
                    "profile_tag": args.profile_tag,
                    "total_duration": sum(
                        section["total"] for section in sections_payload
                    ),
                    "chunk_stats": chunk_stats,
                    "sections": sections_payload,
                }

                model_suffix = args.embedding_model.replace("/", "__")
                provider_suffix = args.embedding_provider.replace("/", "__")
                dim_suffix = f"dim{args.embedding_dimension}"
                profile_parts = [
                    instance_id.replace("/", "__"),
                    model_suffix,
                    provider_suffix,
                    dim_suffix,
                ]
                if args.profile_tag:
                    profile_parts.append(args.profile_tag.replace("/", "__"))
                profile_file = profile_output_dir / f"{'__'.join(profile_parts)}.json"
                profile_file.write_text(json.dumps(profile_payload, indent=2))
                logger.info(f"Saved profiler results to {profile_file}")

            logger.info(
                f"✓ Successfully built hierarchical embedding for {instance_id}"
            )
            logger.info(f"  - Saved to: {instance_final_dir}")

        except Exception as e:
            logger.error(f"✗ Failed to process {instance_id}: {e}")
            import traceback

            logger.error(traceback.format_exc())
        finally:
            if vector_store is not None:
                vector_store.close()
                vector_store = None
            gc.collect()
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
    logger.info(f"\n{'='*80}")
    logger.info("Hierarchical embedding build complete!")
    logger.info(f"Processed {len(dataset_instances)} instance(s)")
    if args.enable_profiler or args.profile_dir:
        logger.info(f"Profile logs stored in: {profile_output_dir}")
    logger.info(f"{'='*80}")


def build_embeddings_isolated(args):
    """Run each instance in a separate subprocess for CUDA fault isolation.

    Loads the dataset once to discover instance IDs, then spawns a fresh
    ``python build_embeddings.py`` subprocess per instance (with
    ``--filter-instance`` pinned to that single ID and ``--isolate-instances``
    removed).  Each subprocess gets its own CUDA context, so an OOM or
    segfault in one instance cannot poison subsequent ones.
    """
    import re
    import subprocess

    dataset_obj = _load_dataset(args)
    dataset_instances = dataset_obj.load()

    if len(dataset_instances) == 0:
        raise ValueError(
            f"No instances found in "
            f"{args.dataset or _DATASET_DEFAULTS[args.dataset_class]}"
        )

    logger.info(f"Isolated mode: will process {len(dataset_instances)} instance(s)")

    # Rebuild the argv without --isolate-instances, and without any existing
    # --filter-instance (we'll supply our own per-instance filter).
    child_argv = [sys.executable, __file__]
    skip_next = False
    for arg in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg == "--isolate-instances":
            continue
        if arg == "--filter-instance":
            skip_next = True  # skip the next token (the regex value)
            continue
        if arg.startswith("--filter-instance="):
            continue
        child_argv.append(arg)

    succeeded, failed, skipped = 0, 0, 0
    for idx, instance in enumerate(dataset_instances):
        instance_id = instance["instance_id"]
        # Exact-match filter so only this instance is processed
        instance_filter = f"^({re.escape(instance_id)})$"
        cmd = child_argv + ["--filter-instance", instance_filter]

        logger.info(
            f"\n{'='*80}\n"
            f"[isolated {idx+1}/{len(dataset_instances)}] {instance_id}\n"
            f"{'='*80}"
        )

        # Check if already built (mirrors the skip logic in build_embeddings)
        instance_dir_name = instance_id.replace("/", "__")
        instance_final_dir = Path(args.storage_dir) / instance_dir_name
        model_suffix = args.embedding_model.replace("/", "__")
        config_file = instance_final_dir / f"config_{model_suffix}.json"
        if config_file.exists() and not args.force_rebuild:
            logger.info(f"  Already exists, skipping: {config_file}")
            skipped += 1
            continue

        result = subprocess.run(cmd)
        if result.returncode == 0:
            succeeded += 1
        else:
            logger.error(
                f"  Subprocess exited with code {result.returncode} "
                f"for {instance_id}"
            )
            failed += 1

    logger.info(
        f"\n{'='*80}\n"
        f"Isolated build complete: {succeeded} succeeded, {failed} failed, "
        f"{skipped} skipped\n"
        f"{'='*80}"
    )
    if failed:
        sys.exit(1)


def main():
    """Main entry point."""
    args = parse_args()

    if args.isolate_instances:
        build_embeddings_isolated(args)
    else:
        build_embeddings(args)


if __name__ == "__main__":
    main()
