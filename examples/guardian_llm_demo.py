#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0
"""
End-to-end demo of the Repository Guardian LLM investigation step.

Two-phase architecture (mirrors skill_agent_aot.py):

    Phase 1 — index compilation (run once per commit)
              Builds BM25 / FAISS-embedding / symbol-graph artifacts under
              <cache_dir>/ and writes repo_manifest.json.  Skipped
              automatically when the manifest is already fresh for the
              current HEAD commit.  Pass --force-reindex to override.

    Phase 2 — Guardian investigation (runs every time)
              Loads the cached retriever from the manifest, detects churn
              hotspots, and runs the LLM agentic loop on each one.

Authentication follows the litellm convention — no API key needed for Vertex AI
(uses gcloud application default credentials); pass --api-key for other providers.

Usage:
    # BM25 (default — SCIP symbol_graph + BM25 on unified symbol names):
    python examples/guardian_llm_demo.py .

    # Embedding (FAISS, GPU recommended):
    python examples/guardian_llm_demo.py . --retriever embedding

    # Force a full re-index even if cache is fresh:
    python examples/guardian_llm_demo.py . --force-reindex

    # Point at a shared cache directory:
    python examples/guardian_llm_demo.py . --cache-dir /mnt/data/myrepo_cache
"""

from __future__ import annotations

import argparse
import logging as _logging
import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from codeminer.guardian.llm_investigator import LLMUsage, investigate_with_llm
from codeminer.guardian.report import Finding, GuardianReport, render_markdown
from codeminer.guardian.signals import churn_hotspots
from codeminer.llm.litellm_chat import LiteLLMChat

# ---------------------------------------------------------------------------
# Retriever wrappers
# ---------------------------------------------------------------------------


class _BM25Retriever:
    """Adapt BM25CodeIndexer.search() to the .query() protocol."""

    def __init__(self, indexer) -> None:
        self._idx = indexer

    def query(self, query: str, top_k: int = 5):
        return self._idx.search(query, top_k=top_k)

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RETRIEVER_TO_INDEX_TYPE = {
    "bm25": "symbol_graph",
    "embedding": "vector",
}


def _head_commit(repo_path: str) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def _manifest_is_fresh(manifest_path: str, repo_path: str, index_type: str) -> bool:
    """Return True if the manifest exists, covers index_type, and matches HEAD."""
    if not os.path.exists(manifest_path):
        return False
    try:
        from codeminer.compiler.manifest import RepoManifest
        manifest = RepoManifest.load(manifest_path)
        entry = manifest.indexes.get(index_type)
        if entry is None or entry.status != "fresh":
            return False
        return manifest.last_indexed_commit == _head_commit(repo_path)
    except Exception:
        return False


def _compile(repo_path: str, index_type: str, cache_dir: str, languages: list,
             embedding_model: str, embedding_dimension: int):
    """Phase 1: build the index and write repo_manifest.json."""
    from codeminer.agent import compile_repo
    print(f"[Phase 1] Compiling {index_type!r} index ...")
    manifest = compile_repo(
        repo_path,
        index_types=(index_type,),
        languages=tuple(languages),
        cache_dir=cache_dir,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
    )
    entry = manifest.indexes.get(index_type)
    if entry:
        dur = entry.metadata.get("build_duration_seconds", "?")
        print(f"  {index_type}: [{entry.status}] built in {dur}s  →  {entry.path}")
    return manifest


def _load_retriever(index_type: str, retriever_name: str, manifest_path: str,
                    embedding_model: str, embedding_dimension: int, languages: list):
    """Phase 2: load the retriever from disk without rebuilding."""
    from codeminer.compiler.manifest import RepoManifest
    manifest = RepoManifest.load(manifest_path)
    entry = manifest.indexes[index_type]
    index_path = entry.path

    if retriever_name == "bm25":
        from codeminer.graph.code_graph import CodeGraph
        from codeminer.index.sparse_idx.bm25_index import BM25CodeIndexer
        graph_pkl = os.path.join(index_path, "graph.pkl")
        print(f"[Phase 2] Loading code graph from {graph_pkl} ...")
        graph = CodeGraph.load_graph(graph_pkl)
        print("  Building BM25 from graph (in-memory) ...")
        bm25 = BM25CodeIndexer(max_k=128, language="english")
        bm25.build_index_from_graph(graph)
        return _BM25Retriever(bm25)

    if retriever_name == "embedding":
        from codeminer.model.embedding_retrieve_pipeline import EmbeddingRetrievePipeline
        print(f"[Phase 2] Loading embedding index from {index_path} ...")
        pipeline = EmbeddingRetrievePipeline(
            repo_path=manifest.repo_path,
            index_path=index_path,
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
            languages=languages,
        )
        return pipeline

    raise ValueError(f"Unknown retriever: {retriever_name!r}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Guardian LLM investigation demo (AoT-compile + investigate)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("repo", help="Path to the repository to analyse")
    p.add_argument("--top-n", type=int, default=3, help="Max churn hotspots to analyse")
    p.add_argument("--since", default="90 days ago", help="Git --since window")
    p.add_argument(
        "--model",
        default="vertex_ai/gemini-2.5-flash",
        help="LiteLLM model string",
    )
    p.add_argument("--api-key", default=None, help="API key for litellm")
    p.add_argument("--vertex-project", default=None, help="GCP project for Vertex AI")
    p.add_argument("--vertex-location", default=None, help="GCP region for Vertex AI")
    p.add_argument(
        "--max-tool-rounds", type=int, default=6,
        help="Max search_code rounds before forcing conclusion",
    )
    p.add_argument("--retrieval-top-k", type=int, default=5, help="Results per search call")
    p.add_argument(
        "--language", default="python",
        help="Primary language (comma-separated for multiple)",
    )
    p.add_argument(
        "--retriever", default="bm25",
        choices=["bm25", "embedding"],
        help=(
            "bm25: SCIP symbol_graph + BM25 on unified symbol names (default). "
            "embedding: FAISS on full code content, GPU recommended."
        ),
    )
    p.add_argument(
        "--embedding-model", default="nomic-ai/CodeRankEmbed",
        help="Embedding model (used when --retriever=embedding)",
    )
    p.add_argument(
        "--embedding-dimension", type=int, default=768,
        help="Embedding vector dimension",
    )
    p.add_argument(
        "--cache-dir", default=None,
        help="Index cache directory (default: <repo>/.codeminer_cache)",
    )
    p.add_argument(
        "--force-reindex", action="store_true",
        help="Rebuild the index even if the manifest is fresh",
    )
    p.add_argument(
        "--output-dir", default="guardian_output",
        help="Parent directory for episode output",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()
    from datetime import datetime

    repo_path = os.path.abspath(args.repo)
    languages = [lang.strip() for lang in args.language.split(",") if lang.strip()]
    cache_dir = args.cache_dir or os.path.join(repo_path, ".codeminer_cache")
    index_type = _RETRIEVER_TO_INDEX_TYPE[args.retriever]
    manifest_path = os.path.join(cache_dir, "repo_manifest.json")

    # ── Episode directory + log handler ────────────────────────────────────
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    episode_dir = os.path.join(os.path.abspath(args.output_dir), stamp)
    os.makedirs(episode_dir, exist_ok=True)

    log_path = os.path.join(episode_dir, "guardian.log")
    _fh = _logging.FileHandler(log_path, encoding="utf-8")
    _fh.setLevel(_logging.DEBUG)
    _fh.setFormatter(_logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s"))
    _guardian_loggers = [
        obj for name, obj in _logging.Logger.manager.loggerDict.items()
        if name.startswith("codeminer.guardian") and isinstance(obj, _logging.Logger)
    ]
    for _lg in _guardian_loggers:
        _lg.addHandler(_fh)
    _fh.stream.write(
        f"# guardian_llm_demo session\n"
        f"# repo={repo_path}\n"
        f"# retriever={args.retriever}\n"
        f"# model={args.model}\n"
        f"# since={args.since}\n\n"
    )

    print(f"Repository : {repo_path}")
    print(f"Languages  : {', '.join(languages)}")
    print(f"Churn since: {args.since}")
    print(f"Model      : {args.model}")
    print(f"Retriever  : {args.retriever}")
    print(f"Cache dir  : {cache_dir}")
    print(f"Episode    : {episode_dir}")
    print()

    # ── Phase 1: compile (skip if fresh) ───────────────────────────────────
    if not args.force_reindex and _manifest_is_fresh(manifest_path, repo_path, index_type):
        print(f"[Phase 1] Index is fresh for HEAD — skipping rebuild.")
        print(f"          (pass --force-reindex to override)\n")
    else:
        _compile(
            repo_path, index_type, cache_dir, languages,
            args.embedding_model, args.embedding_dimension,
        )
        print()

    # ── Phase 2: load retriever from cache ─────────────────────────────────
    retriever = _load_retriever(
        index_type, args.retriever, manifest_path,
        args.embedding_model, args.embedding_dimension, languages,
    )
    print()

    # ── Churn hotspots ─────────────────────────────────────────────────────
    hotspots = churn_hotspots(repo_path, since=args.since, top_n=args.top_n)
    if not hotspots:
        print("No churn hotspots found.")
        return
    print(f"Top {len(hotspots)} hotspot(s):")
    for h in hotspots:
        print(f"  {h.path}  ({h.commit_count} commits)")
    print()

    # ── LLM setup ──────────────────────────────────────────────────────────
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

    # ── LLM investigation ──────────────────────────────────────────────────
    usage_acc = LLMUsage()
    findings = []
    for i, hotspot in enumerate(hotspots, start=1):
        print(f"── Hotspot {i}/{len(hotspots)}: {hotspot.path} ──")
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
        findings.append(Finding(
            kind="churn",
            title=f"High-churn file: {hotspot.path}",
            detail=f"Changed in **{hotspot.commit_count}** commits over {args.since}.",
            narrative=narrative,
        ))
        print(f"\n  LLM narrative:\n")
        for line in narrative.splitlines():
            print(f"    {line}")
        print()

    # ── Report ─────────────────────────────────────────────────────────────
    import json as _json
    from datetime import timezone

    commit = _head_commit(repo_path) or "(unknown)"
    report = GuardianReport(
        repo=repo_path,
        commit=commit,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        churn_window=args.since,
        findings=findings,
        llm_usage=usage_acc,
        retriever=args.retriever,
    )

    md = render_markdown(report)
    print("=" * 60)
    print("FULL MARKDOWN REPORT")
    print("=" * 60)
    print(md)

    md_path = Path(episode_dir) / "report.md"
    json_path = Path(episode_dir) / "report.json"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(_json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    for _lg in _guardian_loggers:
        _lg.removeHandler(_fh)
    _fh.close()
    if hasattr(retriever, "close"):
        retriever.close()

    print(f"\nReport: {md_path}")
    print(f"Log:    {log_path}")


if __name__ == "__main__":
    main()
