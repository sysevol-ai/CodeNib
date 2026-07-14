#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0
"""Run one Repository Guardian cycle over a repo and emit a dated report.

Phase 1 skeleton: sync -> index -> observe (churn) -> investigate -> report.
Non-modifying: writes only the report, never the target repo.

By default uses BM25-only retrieval (no GPU / embedding model required).
Add --with-embeddings to also build a vector index and use HybridRetrievePipeline.

Usage:
    python scripts/guardian_cycle.py .
    python scripts/guardian_cycle.py /path/to/repo --output-dir ./guardian_out
    python scripts/guardian_cycle.py . --top-n 5 --since "30 days ago"
    python scripts/guardian_cycle.py . --use-llm              # LLM narratives (Vertex AI)
    python scripts/guardian_cycle.py . --with-embeddings      # BM25 + vector retrieval
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO, stream=sys.stdout, format="%(levelname)-8s %(message)s"
)

from codeminer.guardian import GuardianConfig, render_markdown, run_cycle


def _normalize_languages(value: str) -> list[str]:
    import re

    langs = [t.strip().lower() for t in re.split(r"[,/]", value or "") if t.strip()]
    return langs or ["python"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Repository Guardian cycle")
    parser.add_argument("repo_dir", help="Absolute or relative path to the repo")
    parser.add_argument(
        "--output-dir",
        default="guardian_output",
        help="Parent directory for episode output (default: guardian_output)",
    )
    parser.add_argument(
        "--language",
        default="python",
        help="Comma/slash-separated languages to index (default: python)",
    )
    parser.add_argument(
        "--top-n", type=int, default=10, help="Max churn hotspots (default: 10)"
    )
    parser.add_argument(
        "--since",
        default="90 days ago",
        help='Churn window as a git --since expression (default: "90 days ago")',
    )
    parser.add_argument(
        "--retrieval-top-k",
        type=int,
        default=5,
        help="Evidence locations per hotspot (default: 5)",
    )
    parser.add_argument(
        "--run-tests",
        action="store_true",
        help="Also run the target's pytest suite and report failures",
    )
    parser.add_argument(
        "--with-embeddings",
        action="store_true",
        help=(
            "Also build a vector index and use HybridRetrievePipeline "
            "(requires a GPU / embedding model)"
        ),
    )
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Run LLM investigation step via litellm (Vertex AI by default; see --llm-model)",
    )
    parser.add_argument(
        "--llm-model",
        default="vertex_ai/gemini-2.5-flash",
        help="LiteLLM model string for the investigation step "
        "(default: vertex_ai/gemini-2.5-flash)",
    )
    parser.add_argument(
        "--llm-max-tool-rounds",
        type=int,
        default=3,
        help="Max search rounds per hotspot (default: 3)",
    )
    parser.add_argument(
        "--index-cache-dir",
        default=None,
        help="Index cache dir (default: <repo>/.codeminer_cache)",
    )
    parser.add_argument(
        "--hypotheses-only",
        action="store_true",
        help="Stop after the hypothesize step; print hypotheses without running the investigator",
    )
    args = parser.parse_args()

    repo_dir = os.path.abspath(args.repo_dir)
    if not os.path.isdir(repo_dir):
        print(f"ERROR: Directory not found: {repo_dir}")
        sys.exit(1)

    languages = _normalize_languages(args.language)
    index_types = ("bm25", "vector") if args.with_embeddings else ("bm25",)

    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    episode_dir = os.path.join(os.path.abspath(args.output_dir), stamp)
    os.makedirs(episode_dir, exist_ok=True)

    config = GuardianConfig(
        repo_path=repo_dir,
        languages=languages,
        index_cache_dir=args.index_cache_dir,
        index_types=index_types,
        top_n=args.top_n,
        since=args.since,
        run_tests=args.run_tests,
        retrieval_top_k=args.retrieval_top_k,
        use_llm=args.use_llm,
        llm_model=args.llm_model,
        llm_max_tool_rounds=args.llm_max_tool_rounds,
        episode_dir=episode_dir,
        hypotheses_only=args.hypotheses_only,
    )

    print(f"Running Guardian cycle on {repo_dir} (languages: {', '.join(languages)})")
    print(f"Episode:  {episode_dir}")
    report = run_cycle(config)

    md_path = Path(episode_dir) / "report.md"
    json_path = Path(episode_dir) / "report.json"

    md_path.write_text(render_markdown(report), encoding="utf-8")
    json_path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\n{len(report.findings)} finding(s).")
    print(f"Report:  {md_path}")
    print(f"JSON:    {json_path}")
    print(f"Log:     {Path(episode_dir) / 'guardian.log'}")


if __name__ == "__main__":
    main()
