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
    # BM25 retriever (default — no GPU needed):
    python examples/guardian_llm_demo.py .
    python examples/guardian_llm_demo.py /path/to/repo --top-n 3 --since "30 days ago"

    # Embedding retriever (GPU recommended; indexes actual code content):
    python examples/guardian_llm_demo.py . --retriever embedding
    python examples/guardian_llm_demo.py . --retriever embedding \
        --embedding-model nomic-ai/CodeRankEmbed --embedding-dimension 768

    # Anthropic (requires ANTHROPIC_API_KEY or --api-key):
    python examples/guardian_llm_demo.py . --model claude-opus-4-8 --api-key $ANTHROPIC_API_KEY

    # Any litellm-compatible provider:
    python examples/guardian_llm_demo.py . --model openai/gpt-4o --api-key $OPENAI_API_KEY
"""

from __future__ import annotations

import argparse
import logging as _logging
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from codeminer.code_chunker import CodeChunker, RepoChunkingConfig
from codeminer.guardian.llm_investigator import LLMUsage, investigate_with_llm
from codeminer.guardian.report import Finding, GuardianReport, render_markdown
from codeminer.guardian.signals import churn_hotspots
from codeminer.index.sparse_idx.bm25_index import BM25CodeIndexer
from codeminer.llm.litellm_chat import LiteLLMChat

# ---------------------------------------------------------------------------
# Retriever wrappers (duck-typed to _Retriever .query() protocol)
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
    p.add_argument(
        "--retriever",
        default="bm25",
        choices=["bm25", "embedding"],
        help="Retriever backend for search_code tool (default: bm25)",
    )
    p.add_argument(
        "--embedding-model",
        default="nomic-ai/CodeRankEmbed",
        help="Embedding model (used when --retriever=embedding)",
    )
    p.add_argument(
        "--embedding-dimension",
        type=int,
        default=768,
        help="Embedding vector dimension (used when --retriever=embedding)",
    )
    p.add_argument(
        "--index-cache-dir",
        default=None,
        help="Cache dir for embedding index (default: <episode_dir>/embed_index)",
    )
    p.add_argument(
        "--output-dir",
        default="guardian_output",
        help="Parent directory for episode output (default: guardian_output)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    import os
    from datetime import datetime

    repo_path = os.path.abspath(args.repo)
    languages = [lang.strip() for lang in args.language.split(",") if lang.strip()]

    # Create a timestamped episode directory and attach a debug log handler.
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    episode_dir = os.path.join(os.path.abspath(args.output_dir), stamp)
    os.makedirs(episode_dir, exist_ok=True)

    log_path = os.path.join(episode_dir, "guardian.log")
    _fh = _logging.FileHandler(log_path, encoding="utf-8")
    _fh.setLevel(_logging.DEBUG)
    _fh.setFormatter(
        _logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")
    )
    # get_logger sets propagate=False on each logger, so we must attach to each
    # guardian logger individually rather than relying on the parent.
    _guardian_loggers = [
        obj
        for name, obj in _logging.Logger.manager.loggerDict.items()
        if name.startswith("codeminer.guardian")
        and isinstance(obj, _logging.Logger)
    ]
    for _lg in _guardian_loggers:
        _lg.addHandler(_fh)

    print(f"Repository : {repo_path}")
    print(f"Languages  : {', '.join(languages)}")
    print(f"Churn since: {args.since}")
    print(f"Model      : {args.model}")
    print(f"Episode    : {episode_dir}")
    print()

    # ------------------------------------------------------------------
    # 1. Build retriever index
    # ------------------------------------------------------------------
    _embed_pipeline = None  # kept for cleanup
    if args.retriever == "embedding":
        from codeminer.model.embedding_retrieve_pipeline import EmbeddingRetrievePipeline

        index_cache = args.index_cache_dir or os.path.join(episode_dir, "embed_index")
        print(f"Building embedding index (model={args.embedding_model}) ...")
        _embed_pipeline = EmbeddingRetrievePipeline(
            repo_path=repo_path,
            index_path=index_cache,
            embedding_model=args.embedding_model,
            embedding_dimension=args.embedding_dimension,
            languages=languages,
        )
        retriever = _embed_pipeline
        print(f"  Embedding index ready.\n")
    else:
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
    # 3. LLM setup
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

    usage_acc = LLMUsage()
    findings = []
    for i, hotspot in enumerate(hotspots, start=1):
        print(f"── Hotspot {i}/{len(hotspots)}: {hotspot.path} ──")

        # ------------------------------------------------------------------
        # 4. LLM investigation
        # ------------------------------------------------------------------
        print(f"  Running LLM investigation (model={args.model}) ...")
        narrative = investigate_with_llm(
            hotspot,
            retriever,
            repo_path=repo_path,
            since=args.since,
            llm=llm,
            max_tool_rounds=args.max_tool_rounds,
            top_k=args.retrieval_top_k,
            usage_acc=usage_acc,
        )

        findings.append(
            Finding(
                kind="churn",
                title=f"High-churn file: {hotspot.path}",
                detail=(
                    f"Changed in **{hotspot.commit_count}** commits over {args.since}."
                ),
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
        llm_usage=usage_acc,
    )

    md = render_markdown(report)
    print("=" * 60)
    print("FULL MARKDOWN REPORT")
    print("=" * 60)
    print(md)

    md_path = Path(episode_dir) / "report.md"
    json_path = Path(episode_dir) / "report.json"
    import json as _json

    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(
        _json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    for _lg in _guardian_loggers:
        _lg.removeHandler(_fh)
    _fh.close()
    if _embed_pipeline is not None:
        _embed_pipeline.close()
    print(f"\nReport: {md_path}")
    print(f"Log:    {log_path}")


if __name__ == "__main__":
    main()
