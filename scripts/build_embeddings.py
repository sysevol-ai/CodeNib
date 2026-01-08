#!/usr/bin/env python3
"""
This script builds and caches hierarchical embedding indices for all SWE-bench Lite instances.
Each instance's embedding will be stored in /mnt/data/codeminer/{instance_id}/

Test Usage:
    python scripts/build_embeddings.py \\
        --filter-instance "^(astropy__astropy-6938)$" \\
        --force-rebuild
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from codeminer.dataset.swebench import SwebenchDataset
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
        "--dataset",
        type=str,
        default="princeton-nlp/SWE-bench_Lite",
        help="Dataset name",
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
    return parser.parse_args()


def build_embeddings(args):
    """Build hierarchical embedding indices for all SWE-bench Lite instances."""

    build_levels = [level.lower() for level in args.build_levels]

    # Load dataset
    dataset_obj = SwebenchDataset(
        dataset=args.dataset,
        split=args.split,
        filter_instance=args.filter_instance,
    )
    dataset_instances = dataset_obj.load()

    if len(dataset_instances) == 0:
        raise ValueError(f"No instances found in {args.dataset}")

    logger.info(f"Loaded {len(dataset_instances)} instance(s)")
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

            logger.info(f"Repository path: {repo_path}")
            logger.info(f"Target directory: {instance_final_dir}")

            # Check if embedding already exists (model-specific config)
            model_suffix = args.embedding_model.replace("/", "__")
            config_file = instance_final_dir / f"config_{model_suffix}.json"
            if config_file.exists() and not args.force_rebuild:
                logger.info(
                    f"✓ Embedding already exists at {instance_final_dir}, skipping..."
                )
                continue
            elif config_file.exists() and args.force_rebuild:
                logger.info(
                    f" Embedding already exists but force-rebuild is enabled, rebuilding..."
                )

            embedding_kwargs = {}
            if args.trust_remote_code:
                embedding_kwargs["model_kwargs"] = {"trust_remote_code": True}
            if args.batch_size:
                embedding_kwargs["encode_kwargs"] = {"batch_size": args.batch_size}

            logger.info("Building hierarchical vector store...")
            plan_name = args.plan_name
            with instance_profiler.section("build_vector_store"):
                build_hierarchical_vector_store(
                    repo_path=repo_path,
                    index_path=str(instance_final_dir),
                    plan_name=plan_name,
                    languages=list(args.languages),
                    max_lines_per_chunk=args.max_lines_per_chunk,
                    build_levels=build_levels,
                    embedding_model=args.embedding_model,
                    embedding_provider=args.embedding_provider,
                    embedding_dimension=args.embedding_dimension,
                    embedding_kwargs=embedding_kwargs,
                    index_metric=args.index_metric,
                    profiler=instance_profiler,
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

                profile_payload = {
                    "instance_id": instance_id,
                    "repo": instance.get("repo", "unknown"),
                    "base_commit": instance.get("base_commit", "unknown"),
                    "embedding_model": args.embedding_model,
                    "embedding_dimension": args.embedding_dimension,
                    "total_duration": sum(
                        section["total"] for section in sections_payload
                    ),
                    "sections": sections_payload,
                }

                model_suffix = args.embedding_model.replace("/", "__")
                profile_file = (
                    profile_output_dir
                    / f"{instance_id.replace('/', '__')}__{model_suffix}.json"
                )
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
            continue

    logger.info(f"\n{'='*80}")
    logger.info("Hierarchical embedding build complete!")
    logger.info(f"Processed {len(dataset_instances)} instance(s)")
    if args.profile_dir:
        logger.info(f"Profile logs stored in: {profile_output_dir}")
    logger.info(f"{'='*80}")


def main():
    """Main entry point."""
    args = parse_args()

    build_embeddings(args)


if __name__ == "__main__":
    main()
