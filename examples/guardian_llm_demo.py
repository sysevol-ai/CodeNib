#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0
"""
End-to-end demo of the Repository Guardian LLM investigation step.

Builds a BM25 index over a repo (no GPU, no embedding model), finds the
top-N churn hotspots, then runs the LLM agentic loop via litellm on each one.
The LLM issues search_code tool calls backed by the BM25 index and writes a
plain-text narrative per hotspot.

Authentication follows the litellm convention — no API key needed for Vertex AI
(uses gcloud application default credentials); pass --api-key for other providers.

Usage:
    # Vertex AI (default — uses gcloud ADC, no API key needed):
    python examples/guardian_llm_demo.py .
    python examples/guardian_llm_demo.py /path/to/repo --top-n 3 --since "30 days ago"

    # Anthropic (requires ANTHROPIC_API_KEY or --api-key):
    python examples/guardian_llm_demo.py . --model claude-opus-4-8 --api-key $ANTHROPIC_API_KEY

    # Any litellm-compatible provider:
    python examples/guardian_llm_demo.py . --model openai/gpt-4o --api-key $OPENAI_API_KEY
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from codeminer.code_chunker import CodeChunker, RepoChunkingConfig
from codeminer.guardian.investigate import investigate_hotspot
from codeminer.guardian.llm_investigator import investigate_with_llm
from codeminer.guardian.report import Finding, GuardianReport, render_markdown
from codeminer.guardian.signals import churn_hotspots
from codeminer.index.sparse_idx.bm25_index import BM25CodeIndexer
from codeminer.llm.litellm_chat import LiteLLMChat

# ---------------------------------------------------------------------------
# BM25 retriever wrapper (duck-typed to _Retriever protocol)
# ---------------------------------------------------------------------------


class _BM25Retriever:
    """Adapt BM25CodeIndexer.search() to the _Retriever .query() protocol."""

    def __init__(self, indexer: BM25CodeIndexer) -> None:
        self._idx = indexer

    def query(self, query: str, top_k: int = 5):
        return self._idx.search(query, top_k=top_k)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Guardian LLM investigation demo",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("repo", help="Path to the repository to analyse")
    p.add_argument("--top-n", type=int, default=3, help="Max churn hotspots to analyse")
    p.add_argument("--since", default="90 days ago", help="Git --since window")
    p.add_argument(
        "--model",
        default="vertex_ai/gemini-2.5-flash",
        help="LiteLLM model string (default: vertex_ai/gemini-2.5-flash)",
    )
    p.add_argument(
        "--api-key",
        default=None,
        help="API key forwarded to litellm (optional; Vertex AI uses gcloud ADC)",
    )
    p.add_argument(
        "--vertex-project",
        default=None,
        help="GCP project for Vertex AI (defaults to gcloud config)",
    )
    p.add_argument(
        "--vertex-location",
        default=None,
        help="GCP region for Vertex AI (defaults to gcloud config)",
    )
    p.add_argument(
        "--max-tool-rounds",
        type=int,
        default=3,
        help="Max search_code calls the LLM may make per hotspot",
    )
    p.add_argument(
        "--retrieval-top-k",
        type=int,
        default=5,
        help="Results returned per search_code call",
    )
    p.add_argument(
        "--language",
        default="python",
        help="Primary language to index (comma-separated for multiple)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    import os

    repo_path = os.path.abspath(args.repo)
    languages = [lang.strip() for lang in args.language.split(",") if lang.strip()]

    print(f"Repository : {repo_path}")
    print(f"Languages  : {', '.join(languages)}")
    print(f"Churn since: {args.since}")
    print(f"Model      : {args.model}")
    print()

    # ------------------------------------------------------------------
    # 1. Build BM25 index (no GPU / embedding model needed)
    # ------------------------------------------------------------------
    print("Building BM25 index...")
    chunker = CodeChunker(
        language=languages[0],
        repo_config=RepoChunkingConfig(languages=languages),
        max_lines_per_chunk=300,
    )
    chunks = chunker.chunk_repository(repo_path=repo_path)
    if not chunks:
        print(f"ERROR: No code chunks from {repo_path}", file=sys.stderr)
        sys.exit(1)
    bm25 = BM25CodeIndexer(chunks=chunks, max_k=128)
    retriever = _BM25Retriever(bm25)
    print(f"  Indexed {len(chunks)} chunks.\n")

    # ------------------------------------------------------------------
    # 2. Churn hotspots
    # ------------------------------------------------------------------
    hotspots = churn_hotspots(repo_path, since=args.since, top_n=args.top_n)
    if not hotspots:
        print("No churn hotspots found in the given window.")
        return
    print(f"Top {len(hotspots)} hotspot(s):")
    for h in hotspots:
        print(f"  {h.path}  ({h.commit_count} commits)")
    print()

    # ------------------------------------------------------------------
    # 3. Gather initial BM25 evidence for each hotspot
    # ------------------------------------------------------------------
    vertex_extra = {}
    if args.vertex_project:
        vertex_extra["vertex_project"] = args.vertex_project
    if args.vertex_location:
        vertex_extra["vertex_location"] = args.vertex_location
    llm = LiteLLMChat(
        model=args.model,
        temperature=0.0,
        max_tokens=1024,
        api_key=args.api_key,
        extra_kwargs=vertex_extra,
    )

    findings = []
    for i, hotspot in enumerate(hotspots, start=1):
        print(f"── Hotspot {i}/{len(hotspots)}: {hotspot.path} ──")

        initial_evidence = investigate_hotspot(
            hotspot, retriever, top_k=args.retrieval_top_k
        )
        print(f"  Initial BM25 evidence: {len(initial_evidence)} location(s)")

        # ------------------------------------------------------------------
        # 4. LLM investigation
        # ------------------------------------------------------------------
        print(f"  Running LLM investigation (model={args.model}) ...")
        narrative = investigate_with_llm(
            hotspot,
            retriever,
            repo_path=repo_path,
            since=args.since,
            initial_evidence=initial_evidence,
            llm=llm,
            max_tool_rounds=args.max_tool_rounds,
            top_k=args.retrieval_top_k,
        )

        findings.append(
            Finding(
                kind="churn",
                title=f"High-churn file: {hotspot.path}",
                detail=(
                    f"Changed in **{hotspot.commit_count}** commits over {args.since}."
                ),
                evidence=initial_evidence,
                narrative=narrative,
            )
        )

        print(f"\n  LLM narrative:\n")
        for line in narrative.splitlines():
            print(f"    {line}")
        print()

    # ------------------------------------------------------------------
    # 5. Render full Markdown report
    # ------------------------------------------------------------------
    import subprocess
    from datetime import datetime, timezone

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:
        commit = "(unknown)"

    report = GuardianReport(
        repo=repo_path,
        commit=commit,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        churn_window=args.since,
        findings=findings,
    )

    md = render_markdown(report)
    print("=" * 60)
    print("FULL MARKDOWN REPORT")
    print("=" * 60)
    print(md)


if __name__ == "__main__":
    main()
