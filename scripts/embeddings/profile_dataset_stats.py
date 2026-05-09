#!/usr/bin/env python3
"""
Compute chunk count and LOC statistics for codeminer-base dataset instances.

Reads existing embedding configs for document counts and optionally re-chunks
repos to compute detailed statistics (total LOC, file counts, chunk sizes)
without re-embedding.

Usage:
    # Quick: read existing configs only (no re-chunking)
    python scripts/embeddings/profile_dataset_stats.py \
        --storage-dir /mnt/data/codeminer

    # Full: re-chunk repos to get LOC stats
    python scripts/embeddings/profile_dataset_stats.py \
        --storage-dir /mnt/data/codeminer \
        --rechunk

    # Output JSON summary
    python scripts/embeddings/profile_dataset_stats.py \
        --storage-dir /mnt/data/codeminer \
        --rechunk \
        --output /mnt/data/codeminer/dataset_stats.json
"""

import argparse
import json
import sys
from pathlib import Path

from codeminer.code_chunker import CodeChunker, RepoChunkingConfig
from codeminer.log_utils import get_logger

logger = get_logger(__name__)

# Import dataset loading helpers from build_embeddings
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_embeddings import _load_dataset, _resolve_languages  # noqa: E402


def compute_chunk_stats(repo_path, languages):
    """Chunk the repo at L0 and L2 and return a stats dict."""
    repo_cfg = RepoChunkingConfig(languages=languages)
    stats = {}

    for level, depth, skeleton, l2_exclusive in [
        ("l0", 0, True, False),
        ("l2", 2, False, True),
    ]:
        chunker = CodeChunker(
            language=languages[0],
            repo_config=repo_cfg,
            chunk_depth=depth,
            skeleton_mode=skeleton,
            l2_level_exclusive=l2_exclusive,
        )
        chunks = chunker.chunk_repository(repo_path=repo_path)
        stats[f"{level}_chunks"] = len(chunks)

        if level == "l0":
            repo = Path(repo_path)
            unique_files = {c.file for c in chunks}
            loc_by_file = {}
            for rel_path in unique_files:
                try:
                    loc_by_file[rel_path] = len(
                        (repo / rel_path).read_text(errors="replace").splitlines()
                    )
                except OSError:
                    pass
            stats["total_loc"] = sum(loc_by_file.values())
            stats["total_files"] = len(loc_by_file)

    total_chunks = stats.get("l0_chunks", 0) + stats.get("l2_chunks", 0)
    stats["avg_chunk_loc"] = round(stats.get("total_loc", 0) / max(total_chunks, 1), 1)
    return stats


def read_existing_configs(instance_dir):
    """Read all config_*.json files in an instance directory."""
    configs = {}
    for cfg_path in Path(instance_dir).glob("config_*.json"):
        with open(cfg_path) as f:
            data = json.load(f)
        model = data.get("embedding_model", cfg_path.stem)
        configs[model] = {
            "l0_documents": data.get("l0_documents", 0),
            "l2_documents": data.get("l2_documents", 0),
        }
    return configs


def main():
    parser = argparse.ArgumentParser(
        description="Compute chunk/LOC stats for codeminer-base instances.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dataset-class",
        default="codeminer_base",
        choices=["codeminer_base", "swebench"],
    )
    parser.add_argument("--dataset", default=None, help="HuggingFace dataset name")
    parser.add_argument("--split", default="test")
    parser.add_argument(
        "--storage-dir",
        default="/mnt/data/codeminer",
        help="Base directory with embedding outputs",
    )
    parser.add_argument(
        "--rechunk",
        action="store_true",
        help="Re-chunk repos to compute LOC stats (slower, needs repo access)",
    )
    parser.add_argument(
        "--filter-instance",
        default=".*",
        help="Regex to filter instances",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write JSON summary to this file",
    )
    parser.add_argument(
        "--languages",
        nargs="+",
        default=["python"],
        help="Fallback languages",
    )
    args = parser.parse_args()

    # Load dataset
    # Build a minimal args namespace for _load_dataset
    import re
    from types import SimpleNamespace

    _DATASET_DEFAULTS = {
        "swebench": "princeton-nlp/SWE-bench_Lite",
        "codeminer_base": "fishmingyu/codeminer-base-dataset",
    }
    load_args = SimpleNamespace(
        dataset_class=args.dataset_class,
        dataset=args.dataset or _DATASET_DEFAULTS[args.dataset_class],
        split=args.split,
        filter_instance=args.filter_instance,
    )
    dataset_obj = _load_dataset(load_args)
    dataset_instances = dataset_obj.load()

    pattern = re.compile(args.filter_instance)
    dataset_instances = [
        inst
        for inst in dataset_instances
        if pattern.search(inst.get("instance_id", ""))
    ]

    storage_dir = Path(args.storage_dir)
    results = []

    for idx, instance in enumerate(dataset_instances):
        instance_id = instance.get("instance_id", f"unknown_{idx}")
        instance_dir = storage_dir / instance_id
        instance_languages = _resolve_languages(instance, args.languages)

        entry = {
            "instance_id": instance_id,
            "repo": instance.get("repo", "unknown"),
            "language_group": instance.get("language_group"),
            "languages": instance_languages,
        }

        # Read existing configs
        if instance_dir.exists():
            entry["embedding_configs"] = read_existing_configs(instance_dir)

        # Re-chunk for detailed stats
        if args.rechunk:
            try:
                dataset_obj.process_instance(instance)
                repo_path = dataset_obj.get_repo_path(instance)
                chunk_stats = compute_chunk_stats(repo_path, instance_languages)
                entry["chunk_stats"] = chunk_stats
                logger.info(
                    "[%d/%d] %s: %d files, %d LOC, %d L0, %d L2 chunks",
                    idx + 1,
                    len(dataset_instances),
                    instance_id,
                    chunk_stats["total_files"],
                    chunk_stats["total_loc"],
                    chunk_stats["l0_chunks"],
                    chunk_stats["l2_chunks"],
                )
            except Exception as e:
                logger.error(
                    "[%d/%d] %s: rechunk failed: %s",
                    idx + 1,
                    len(dataset_instances),
                    instance_id,
                    e,
                )
                entry["chunk_stats_error"] = str(e)
        else:
            logger.info(
                "[%d/%d] %s: %d model configs found",
                idx + 1,
                len(dataset_instances),
                instance_id,
                len(entry.get("embedding_configs", {})),
            )

        results.append(entry)

    # Print summary table
    print(f"\n{'Instance':<45} {'Lang':<8} {'Files':>6} {'LOC':>8} {'L0':>6} {'L2':>6}")
    print("-" * 85)
    for r in results:
        cs = r.get("chunk_stats", {})
        cfg = r.get("embedding_configs", {})
        # Use chunk_stats if available, else first config
        l0 = cs.get("l0_chunks") or next((v["l0_documents"] for v in cfg.values()), "?")
        l2 = cs.get("l2_chunks") or next((v["l2_documents"] for v in cfg.values()), "?")
        files = cs.get("total_files", "?")
        loc = cs.get("total_loc", "?")
        lang = ",".join(r.get("languages", []))
        print(f"{r['instance_id']:<45} {lang:<8} {files:>6} {loc:>8} {l0:>6} {l2:>6}")

    # Write JSON output
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nSaved {len(results)} entries to {args.output}")


if __name__ == "__main__":
    main()
