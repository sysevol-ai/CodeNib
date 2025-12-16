#!/usr/bin/env python3
"""
Example script for running hybrid retrieval with primary/secondary embeddings.

This assumes you have already built hybrid embeddings for the target repo using
``scripts/build_hybrid_embedding.sh`` or ``scripts/build_embeddings.py --hybrid``.
Primary and secondary vector stores are expected under:
  <index_dir>/<plan_name>/primary
  <index_dir>/<plan_name>/secondary
"""

import argparse
from pathlib import Path
from typing import List

from codeminer.code_chunker import CodeChunker, HybridChunkingConfig, RepoChunkingConfig
from codeminer.log_utils import get_logger
from codeminer.model.retrieve_rerank_pipeline import (
    RetrieveRerankPipeline,
    RetrieveStageConfig,
)

logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run hybrid retrieval with primary/secondary embeddings",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--repo-path",
        required=True,
        help="Path to the repository to search.",
    )
    parser.add_argument(
        "--index-dir",
        required=True,
        help="Directory containing hybrid vector stores (with plan name subdir).",
    )
    parser.add_argument(
        "--plan-name",
        default="hybrid",
        help="Plan name used during embedding build (subdir under index-dir).",
    )
    parser.add_argument(
        "--query",
        required=True,
        help="Query text to retrieve relevant code chunks.",
    )
    parser.add_argument(
        "--languages",
        nargs="+",
        default=["python"],
        help="Languages to consider during retrieval.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=20,
        help="Number of candidates to retrieve before optional rerank.",
    )
    parser.add_argument(
        "--max-lines-per-chunk",
        type=int,
        default=200,
        help="Maximum lines per chunk (must match the build step).",
    )
    parser.add_argument(
        "--primary-model",
        default="jinaai/jina-code-embeddings-1.5b",
        help="Primary embedding model used during build.",
    )
    parser.add_argument(
        "--primary-provider",
        default="huggingface",
        help="Primary embedding provider.",
    )
    parser.add_argument(
        "--primary-dim",
        type=int,
        default=1536,
        help="Primary embedding dimension.",
    )
    parser.add_argument(
        "--secondary-model",
        default="Salesforce/SweRankEmbed-Small",
        help="Secondary embedding model used during build.",
    )
    parser.add_argument(
        "--secondary-provider",
        default="huggingface",
        help="Secondary embedding provider.",
    )
    parser.add_argument(
        "--secondary-dim",
        type=int,
        default=768,
        help="Secondary embedding dimension.",
    )
    parser.add_argument(
        "--masked",
        action="store_true",
        help=(
            "Use masked mode: reuse a single index and filter results by hybrid "
            "importance without rebuilding hybrid indices."
        ),
    )
    parser.add_argument(
        "--high-importance-ratio",
        type=float,
        default=0.3,
        help="Hybrid masking: ratio of chunks treated as primary.",
    )
    parser.add_argument(
        "--min-high-importance-lines",
        type=int,
        default=10,
        help="Hybrid masking: minimum lines to qualify as primary.",
    )
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="Enable rerank stage (requires rerank model/server configured separately).",
    )
    parser.add_argument(
        "--retrieval-level",
        type=str,
        default="l2",
        choices=["l0", "l2"],
        help="Retrieval level to query.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    plan_dir = Path(args.index_dir) / args.plan_name
    primary_index = plan_dir / "primary"
    secondary_index = plan_dir / "secondary"
    if args.masked:
        # Masked mode expects a single index directory; reuse primary path for both stages
        if not primary_index.exists():
            raise FileNotFoundError(f"Expected index at {primary_index}")
        secondary_index = primary_index
    else:
        if not primary_index.exists() or not secondary_index.exists():
            raise FileNotFoundError(
                f"Expected hybrid indexes at {primary_index} and {secondary_index}"
            )

    vector_masks = {}
    if args.masked:
        mask_cfg = HybridChunkingConfig(
            high_importance_ratio=args.high_importance_ratio,
            min_high_importance_lines=args.min_high_importance_lines,
        )
        chunker = CodeChunker(
            language=args.languages[0],
            repo_config=RepoChunkingConfig(languages=args.languages),
            max_lines_per_chunk=args.max_lines_per_chunk,
            chunk_depth=2,
            l2_level_exclusive=True,
        )
        hybrid_result = chunker.hybrid_chunk_repository(
            repo_path=args.repo_path, hybrid_config=mask_cfg
        )
        vector_masks = {
            "primary": {chunk["node_id"] for chunk in hybrid_result.primary_chunks},
            "secondary": {chunk["node_id"] for chunk in hybrid_result.secondary_chunks},
        }

    # Define two dense retrieval stages; when masked,
    # both use the same index but different masks
    stages: List[RetrieveStageConfig] = [
        RetrieveStageConfig(
            engine="dense",
            weight=0.7,
            top_k=args.top_k,
            params={
                "embedding_model": args.primary_model,
                "embedding_provider": args.primary_provider,
                "embedding_dimension": args.primary_dim,
                "max_lines_per_chunk": args.max_lines_per_chunk,
                "languages": args.languages,
                "index_path": str(primary_index),
                "mask_name": "primary" if args.masked else None,
            },
        ),
        RetrieveStageConfig(
            engine="dense",
            weight=0.3,
            top_k=args.top_k,
            params={
                "embedding_model": args.secondary_model,
                "embedding_provider": args.secondary_provider,
                "embedding_dimension": args.secondary_dim,
                "max_lines_per_chunk": args.max_lines_per_chunk,
                "languages": args.languages,
                "index_path": str(secondary_index),
                "mask_name": "secondary" if args.masked else None,
            },
        ),
    ]

    pipeline = RetrieveRerankPipeline(
        repo_path=args.repo_path,
        index_path=str(primary_index),
        retrieval_plan=stages,
        retrieval_level=args.retrieval_level,
        embedding_model=args.primary_model,
        embedding_provider=args.primary_provider,
        embedding_dimension=args.primary_dim,
        max_lines_per_chunk=args.max_lines_per_chunk,
        languages=args.languages,
        enable_rerank=args.rerank,
        vector_masks=vector_masks,
    )

    results = pipeline.query(args.query)
    for idx, node in enumerate(results[: args.top_k], start=1):
        print(f"[{idx}] score={node.score:.4f} node_id={node.node_id}")
        print(node.content[:500])
        print("-" * 40)


if __name__ == "__main__":
    main()
