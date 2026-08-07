#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end example of the skill agent pipeline.

Demonstrates **two execution paths**:

1. **Standalone (baseline)** — call a skill executor directly.
   No LLM, no agent loop.

2. **Agent** — uses ``AgentRunner`` with LLM-driven tool selection.
   Phase 1 (IndexCompiler) builds indexes and writes ``repo_manifest.json``.
   Phase 2 (AgentRunner) loads the manifest, filters unavailable skills,
   and lets the LLM decide which skills to call.

Usage:
    # Standalone BM25 search:
    python examples/skill_agent.py --repo . \
        --query "how does the skill registry work" --path standalone

    # Agent with LLM tool selection (requires LITELLM-compatible model):
    python examples/skill_agent.py --repo . \
        --query "how does the skill registry work" --path agent \
        --model vertex_ai/gemini-2.5-flash --mode sparse
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path when running as a script
# ---------------------------------------------------------------------------
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from codenib.agent.skills.loader import SkillLoader
from codenib.agent.skills.registry import SkillRegistry
from codenib.code_chunker import CodeChunker, RepoChunkingConfig
from codenib.compiler.params import SessionContext
from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer
from codenib.ops.rerank import RerankContext
from codenib.ops.retrieve import RetrieveContext
from codenib.paths import repo_index_dir
from codenib.types import QueriedNode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Skill agent demo (standalone + agent)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--repo", type=str, default=".")
    parser.add_argument(
        "--query",
        type=str,
        default="how does the skill registry work",
    )
    parser.add_argument(
        "--path",
        type=str,
        default="standalone",
        choices=["standalone", "agent"],
        help="Execution path",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="sparse",
        choices=["sparse", "dense", "hybrid"],
        help="Retrieval mode (determines which indexes to build)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="vertex_ai/gemini-2.5-flash",
        help="LiteLLM model name for agent path",
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--languages", type=str, nargs="+", default=["python"])
    parser.add_argument("--index-path", type=str, default=None)
    parser.add_argument(
        "--embedding-model",
        type=str,
        default="nomic-ai/CodeRankEmbed",
    )
    parser.add_argument("--embedding-dimension", type=int, default=768)
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Index builders
# ---------------------------------------------------------------------------


def build_bm25_index(
    repo_path: str, languages: List[str], max_k: int = 128
) -> BM25CodeIndexer:
    """Chunk the repo and build a BM25 index."""
    primary = languages[0] if languages else "python"
    chunker = CodeChunker(
        language=primary,
        repo_config=RepoChunkingConfig(languages=languages),
        max_lines_per_chunk=300,
    )
    chunks = chunker.chunk_repository(repo_path=repo_path)
    if not chunks:
        raise RuntimeError(f"No code chunks generated from {repo_path}")
    print(f"  BM25: indexed {len(chunks)} chunks")
    return BM25CodeIndexer(chunks=chunks, max_k=max_k)


def build_vector_store(
    repo_path: str,
    index_path: str,
    languages: List[str],
    embedding_model: str,
    embedding_dimension: int,
):
    """Build (or load) hierarchical embedding index."""
    from codenib.index.embedding import CodeVectorStore, build_hierarchical_vector_store

    store_path = Path(index_path)
    store_path.mkdir(parents=True, exist_ok=True)

    l0 = store_path / "l0"
    l2 = store_path / "l2"
    if l0.exists() and l2.exists():
        print("  Embedding: loading cached index")
        vs = CodeVectorStore(
            embedding_model=embedding_model,
            embedding_provider="huggingface",
            dimension=embedding_dimension,
            store_path=str(store_path),
        )
        vs.load(str(store_path))
        if vs.l0_documents and vs.l2_documents:
            return vs
        print("  Embedding: cache incomplete, rebuilding")

    print("  Embedding: building hierarchical index (may take a minute)...")
    vs = build_hierarchical_vector_store(
        repo_path=repo_path,
        index_path=str(store_path),
        plan_name=None,
        languages=languages,
        max_lines_per_chunk=300,
        build_levels=["l0", "l2"],
        embedding_model=embedding_model,
        embedding_provider="huggingface",
        embedding_dimension=embedding_dimension,
        index_metric="ip",
    )
    return vs


def count_files(repo_path: str) -> int:
    """Count source files for SessionContext.repo_size."""
    total = 0
    for _, _, files in os.walk(repo_path):
        total += len(files)
    return total


# ---------------------------------------------------------------------------
# Pretty-printing
# ---------------------------------------------------------------------------


def print_manifest(manifest) -> None:
    """Print the repo manifest summary."""
    print(f"\n=== Repo Manifest ===")
    print(f"  repo     : {manifest.repo_path}")
    print(f"  commit   : {manifest.commit[:12] if manifest.commit else '(none)'}...")
    print(f"  languages: {manifest.languages}")
    print(f"  files    : {manifest.file_count}")
    print(f"\nIndexes:")
    for idx_type, entry in manifest.indexes.items():
        status_tag = f"[{entry.status}]"
        print(f"  {idx_type}: {status_tag} path={entry.path}")
        if entry.metadata.get("build_duration_seconds"):
            print(f"    built in {entry.metadata['build_duration_seconds']:.1f}s")
        if entry.metadata.get("file_count"):
            print(f"    {entry.metadata['file_count']} chunks")
        if entry.metadata.get("document_count"):
            print(f"    documents: {entry.metadata['document_count']}")
    print(f"\nCapabilities: {manifest.capabilities}")


def print_results(results: List[Any], top_k: int) -> None:
    """Print search results."""
    nodes = [n for n in results if isinstance(n, QueriedNode)][:top_k]

    print(f"\n=== Search Results ({len(nodes)} hits) ===")
    for i, node in enumerate(nodes, 1):
        loc = node.file or "?"
        if node.start_line is not None:
            loc += f":{node.start_line}"
            if node.end_line is not None and node.end_line != node.start_line:
                loc += f"-{node.end_line}"
        score = f"{node.score:.4f}" if node.score is not None else "n/a"
        print(f"\n  [{i}] {node.node_name or '(unnamed)'}  (score={score})")
        print(f"      {loc}  type={node.type or '?'}")
        if node.content:
            snippet = node.content[:200]
            if len(node.content) > 200:
                snippet += "..."
            for line in snippet.splitlines()[:5]:
                print(f"      | {line}")
            if len(node.content.splitlines()) > 5:
                print(f"      | ... ({len(node.content.splitlines())} lines total)")


# ---------------------------------------------------------------------------
# Path 1: Standalone execution (baseline)
# ---------------------------------------------------------------------------


def run_standalone(args: argparse.Namespace) -> None:
    """
    Call a skill executor directly — no LLM, no agent loop.

    This is the baseline path: load the skill, resolve parameters from
    config defaults + query inputs, and invoke the executor function.
    """
    repo_path = os.path.abspath(args.repo)

    print(f"Repository : {repo_path}")
    print(f"Query      : {args.query}")
    print(f"Path       : standalone (call executor directly)")
    print(f"Top-k      : {args.top_k}")

    # Step 1: Build BM25 index
    print("\n--- Building index ---")
    bm25_index = build_bm25_index(repo_path, args.languages)

    # Step 2: Create context and load skills
    print("\n--- Loading skills ---")
    retrieve_ctx = RetrieveContext(
        bm25=bm25_index,
        default_top_k=args.top_k,
        default_level="l2",
    )
    skills_dir = os.path.join(_PROJECT_ROOT, "codenib", "agent", "skills")
    loader = SkillLoader()
    loaded = loader.load_all(skills_dir, contexts={"retrieve": retrieve_ctx})
    print(f"  Loaded {len(loaded)} skills: {[s.skill_id for s in loaded]}")

    # Step 3: Agent reads skill.md to decide which skill to use
    registry = SkillRegistry()
    bm25_meta = registry.get("bm25_search")
    if bm25_meta is None:
        print("Error: bm25_search skill not loaded", file=sys.stderr)
        sys.exit(1)

    print("\n--- Agent reads skill.md ---")
    if bm25_meta.skill_doc:
        for line in bm25_meta.skill_doc.strip().splitlines()[:8]:
            print(f"  {line}")

    # Step 4: Call executor directly with query-time params
    print("\n--- Executing bm25_search (standalone) ---")
    executor = bm25_meta.executor_fn
    if executor is None:
        print("Error: no executor loaded for bm25_search", file=sys.stderr)
        sys.exit(1)

    results = executor(query=args.query, top_k=args.top_k)
    print(f"  Got {len(results)} results")

    print_results(results, args.top_k)


# ---------------------------------------------------------------------------
# Path 2: Agent with LLM tool selection
# ---------------------------------------------------------------------------


def run_agent(args: argparse.Namespace) -> None:
    """
    Two-phase architecture with AgentRunner.

    Phase 1: IndexCompiler builds indexes and writes repo_manifest.json.
    Phase 2: AgentRunner loads the manifest, filters unavailable skills,
    and lets the LLM decide which skills to call.
    """
    from codenib.agent.runner import AgentRunner
    from codenib.compiler.index_builders import (
        BM25IndexBuilder,
        IndexBuilderRegistry,
        VectorIndexBuilder,
    )
    from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig
    from codenib.compiler.manifest import RepoManifest
    from codenib.index.embedding import CodeVectorStore
    from codenib.llm.litellm_chat import LiteLLMChat

    repo_path = os.path.abspath(args.repo)
    cache_dir = args.index_path or str(repo_index_dir(repo_path))

    print(f"Repository : {repo_path}")
    print(f"Query      : {args.query}")
    print(f"Path       : agent (IndexCompiler -> AgentRunner)")
    print(f"Mode       : {args.mode}")
    print(f"Model      : {args.model}")
    print(f"Top-k      : {args.top_k}")

    # ==================================================================
    # Phase 1: Index Compilation
    # ==================================================================
    print("\n" + "=" * 50)
    print("  PHASE 1: Index Compilation (repo-time)")
    print("=" * 50)

    index_types = []
    if args.mode in ("sparse", "hybrid"):
        index_types.append("bm25")
    if args.mode in ("dense", "hybrid"):
        index_types.append("vector")

    builder_registry = IndexBuilderRegistry()
    builder_registry.register(
        "bm25",
        BM25IndexBuilder(languages=args.languages),
    )
    builder_registry.register(
        "vector",
        VectorIndexBuilder(
            languages=args.languages,
            embedding_model=args.embedding_model,
            embedding_dimension=args.embedding_dimension,
        ),
    )

    config = IndexCompilerConfig(
        index_types=index_types,
        languages=args.languages,
    )
    compiler = IndexCompiler(builder_registry, config)

    print(f"  Building indexes: {index_types}")
    manifest = compiler.compile_repo(repo_path, cache_dir=cache_dir)
    print_manifest(manifest)

    manifest_path = os.path.join(cache_dir, "repo_manifest.json")

    # ==================================================================
    # Phase 2: Agent Execution
    # ==================================================================
    print("\n" + "=" * 50)
    print("  PHASE 2: Agent Execution (query-time)")
    print("=" * 50)

    # Reload manifest
    manifest = RepoManifest.load(manifest_path)
    print(f"  Loaded manifest: {len(manifest.indexes)} indexes")

    # Load actual index objects from manifest paths
    bm25_index = None
    vector_store = None

    if "bm25" in manifest.indexes and manifest.indexes["bm25"].status == "fresh":
        bm25_index = BM25CodeIndexer()
        bm25_index.load_index(manifest.indexes["bm25"].path)
        print(f"  Loaded BM25 from {manifest.indexes['bm25'].path}")

    if "vector" in manifest.indexes and manifest.indexes["vector"].status == "fresh":
        emb_model = manifest.indexes["vector"].config.get(
            "embedding_model", args.embedding_model
        )
        emb_dim = manifest.indexes["vector"].config.get(
            "embedding_dimension", args.embedding_dimension
        )
        vector_store = CodeVectorStore(
            embedding_model=emb_model,
            embedding_provider="huggingface",
            dimension=emb_dim,
            store_path=manifest.indexes["vector"].path,
        )
        vector_store.load(manifest.indexes["vector"].path)
        print(f"  Loaded vector store from {manifest.indexes['vector'].path}")

    # Create contexts and load skills
    retrieve_ctx = RetrieveContext(
        bm25=bm25_index,
        vector_store=vector_store,
        default_top_k=args.top_k,
        default_level="l2",
    )
    contexts: Dict[str, Any] = {"retrieve": retrieve_ctx}
    if vector_store:
        contexts["rerank"] = RerankContext(embedding_store=vector_store)

    skills_dir = os.path.join(_PROJECT_ROOT, "codenib", "agent", "skills")
    loader = SkillLoader()
    loaded = loader.load_all(skills_dir, contexts=contexts)
    print(f"  Loaded {len(loaded)} skills: {[s.skill_id for s in loaded]}")

    # Session context for parameter scaling
    session_ctx = SessionContext(
        repo_path=repo_path,
        repo_size=manifest.file_count,
        primary_language=manifest.languages[0] if manifest.languages else "python",
    )

    # Create LLM and AgentRunner
    llm = LiteLLMChat(
        model=args.model,
        temperature=0.0,
        max_tokens=1024,
    )

    runner = AgentRunner(
        llm=llm,
        registry=SkillRegistry(),
        max_turns=5,
        manifest=manifest,
        session_ctx=session_ctx,
    )

    print(f"\n--- Running agent (max 5 turns) ---")
    result = runner.run(args.query)

    print(f"\n=== Agent Result ===")
    print(f"  Turns: {result.total_turns}")
    print(f"  Tool calls: {len(result.tool_calls)}")
    for tc in result.tool_calls:
        print(f"    - {tc.skill_id}({tc.arguments})")

    # Collect results from tool call outputs
    all_results = []
    for tc in result.tool_calls:
        if isinstance(tc.result, list):
            all_results.extend(tc.result)

    if all_results:
        print_results(all_results, args.top_k)
    else:
        print(f"\nAgent response: {result.final_answer}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()
    if args.path == "standalone":
        run_standalone(args)
    else:
        run_agent(args)


if __name__ == "__main__":
    main()
