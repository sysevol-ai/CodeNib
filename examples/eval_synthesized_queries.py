#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Evaluate synthesized behavioral queries against retrieval backends.

Thin adapter that reuses the existing pipeline classes (BM25RetrievePipeline,
EmbeddingRetrievePipeline, AgentRunner) and evaluation utilities without
duplicating any logic.  The only new code is the glue that loads the
synthesized JSON format and feeds it through the standard eval functions.

Input format — a directory of per-instance JSON files, each containing a list
of query entries::

    queries_dir/
        astral-sh__ruff-15309.json
        astral-sh__ruff-15330.json

Or a single JSON file containing a flat list of query entries.

Usage:
    # BM25 — folder of per-instance JSONs
    python examples/eval_synthesized_queries.py \
        --pipeline bm25 \
        --queries-dir synthesized_queries/

    # Embedding — single file
    python examples/eval_synthesized_queries.py \
        --pipeline embedding \
        --queries-file filtered_behavioral_queries.json

    # Agent
    python examples/eval_synthesized_queries.py \
        --pipeline agent \
        --queries-dir synthesized_queries/ \
        --model vertex_ai/gemini-2.5-flash --agent-mode hybrid

    # Single instance
    python examples/eval_synthesized_queries.py \
        --pipeline bm25 \
        --queries-dir synthesized_queries/ \
        --filter-instance "astral-sh__ruff-15309"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from codenib.eval.retrieval_eval import (
    aggregate_metrics,
    average_metrics,
    evaluate_predictions,
    extract_predictions,
    normalize_file_path,
    normalize_symbol_identifier,
)
from codenib.log_utils import get_logger
from codenib.paths import prebuilt_data_dir, repo_index_dir, user_state_dir
from codenib.types import QueriedNode

logger = get_logger(__name__)


def _load_vector_from_local_admin_boundary(
    store: Any,
    path: str | Path,
    *,
    semantic_contract: Mapping[str, Any],
    evidence: str,
) -> None:
    """Authorize one compiler-produced local view for this eval process."""

    from codenib.index.embedding.artifact_integrity import (
        capture_authenticated_vector_view,
    )
    from codenib.native_index_authorization import (
        _mint_trusted_local_admin_authorization,
    )

    if not evidence:
        raise ValueError("local vector authorization evidence must be non-empty")
    with capture_authenticated_vector_view(path) as vector_view:
        authorization = _mint_trusted_local_admin_authorization(
            vector_view.ownership,
            view_type="vector",
            semantic_contract=semantic_contract,
            evidence=(
                evidence,
                "synthesized-query-eval-local-admin",
                "captured-vector-tree-subject",
            ),
        )
        store.load(
            str(path),
            native_index_authorization=authorization,
        )


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

_EXT_TO_LANGUAGE = {
    ".py": "python",
    ".rs": "rust",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".java": "java",
    ".go": "go",
    ".cpp": "cpp",
    ".c": "c",
    ".h": "cpp",
    ".hpp": "cpp",
}


_LANGUAGE_GROUP_MAP = {
    "rust": "rust",
    "javascript": "javascript",
    "typescript": "typescript",
    "python": "python",
    "c++": "cpp",
    "c": "c",
    "java": "java",
    "go": "go",
}


def _infer_language(entries: List[Dict[str, Any]]) -> str:
    """Infer the primary language from language_group or gt_files extensions."""
    # Prefer explicit language_group from the synthesized record.
    for entry in entries:
        lg = (entry.get("language_group") or "").lower()
        for key, lang in _LANGUAGE_GROUP_MAP.items():
            if key in lg:
                return lang

    # Fallback: infer from file extensions in gt_files.
    counts: Counter[str] = Counter()
    for entry in entries:
        for f in entry.get("gt_files") or []:
            ext = os.path.splitext(f)[1].lower()
            lang = _EXT_TO_LANGUAGE.get(ext)
            if lang:
                counts[lang] += 1
    if not counts:
        return "python"
    return max(counts, key=counts.get)


# ---------------------------------------------------------------------------
# Lightweight repo checkout (no HF dataset dependency)
# ---------------------------------------------------------------------------


def _checkout_repo(repo_name: str, base_commit: str, repo_cache_dir: str) -> str:
    """Clone (if needed) and checkout ``base_commit``. Returns repo path."""
    repo_dir_name = repo_name.replace("/", "_")
    repo_path = os.path.join(
        os.path.abspath(os.path.expanduser(repo_cache_dir)), repo_dir_name
    )
    os.makedirs(os.path.dirname(repo_path), exist_ok=True)

    if not os.path.exists(repo_path):
        logger.info("Cloning %s → %s", repo_name, repo_path)
        subprocess.run(
            ["git", "clone", f"https://github.com/{repo_name}.git", repo_path],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    original_dir = os.getcwd()
    os.chdir(repo_path)
    try:
        subprocess.run(
            ["git", "fetch", "--all"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "reset", "--hard"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "clean", "-fd"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            subprocess.run(
                ["git", "checkout", "-f", base_commit],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError:
            # Handle shallow clones / missing commits
            shallow = subprocess.run(
                ["git", "rev-parse", "--is-shallow-repository"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            if shallow == "true":
                subprocess.run(
                    ["git", "fetch", "--unshallow"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            subprocess.run(
                ["git", "fetch", "origin", base_commit],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "fetch", "--all", "--tags"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "checkout", "-f", base_commit],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        logger.info("Checked out %s @ %s", repo_name, base_commit[:12])
    finally:
        os.chdir(original_dir)

    return repo_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Evaluate synthesized queries against retrieval backends.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--pipeline", required=True, choices=["bm25", "embedding", "agent"])

    input_group = p.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--queries-file", help="Single JSON file with a list of query entries."
    )
    input_group.add_argument(
        "--queries-dir",
        help="Directory of per-instance JSON files (each a list of entries).",
    )

    p.add_argument("--topk", type=int, default=50)
    p.add_argument(
        "--graph-route",
        type=str,
        choices=["active", "lsp", "scip-candidate"],
        default="active",
        help=(
            "Graph indexing route for graph-backed pipelines. Use lsp for "
            "backend comparison and scip-candidate only for explicit "
            "candidate SCIP evaluation."
        ),
    )
    p.add_argument("--filter-instance", type=str, default=".*")
    p.add_argument("--metrics-k", type=int, nargs="+", default=[1, 3, 5, 10, 15, 20])
    p.add_argument("--index-cache-dir", type=str, default=str(prebuilt_data_dir()))
    p.add_argument("--repo-cache-dir", type=str, default=str(user_state_dir()))
    p.add_argument("--result-path", type=str, default=None)

    # Embedding
    p.add_argument("--embedding-model", default="nomic-ai/CodeRankEmbed")
    p.add_argument("--embedding-provider", default="huggingface")
    p.add_argument("--embedding-dimension", type=int, default=768)

    # Agent
    p.add_argument("--model", default="vertex_ai/gemini-2.5-flash")
    p.add_argument(
        "--agent-mode", default="sparse", choices=["sparse", "dense", "hybrid"]
    )
    p.add_argument("--max-turns", type=int, default=5)

    return p.parse_args()


# ---------------------------------------------------------------------------
# Query loading
# ---------------------------------------------------------------------------


def _load_queries(args: argparse.Namespace) -> List[Dict[str, Any]]:
    """Load query entries from either a single file or a directory."""
    if args.queries_file:
        path = Path(args.queries_file).expanduser().resolve()
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)
        logger.info("Loaded %d queries from %s", len(entries), path)
        return entries

    queries_dir = Path(args.queries_dir).expanduser().resolve()
    entries: List[Dict[str, Any]] = []
    for json_file in sorted(queries_dir.glob("*.json")):
        with open(json_file, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            entries.extend(data)
        elif isinstance(data, dict):
            entries.append(data)
    logger.info("Loaded %d queries from %s", len(entries), queries_dir)
    return entries


# ---------------------------------------------------------------------------
# Ground-truth adapter
# ---------------------------------------------------------------------------


def collect_synthesized_targets(
    entry: Dict[str, Any],
) -> Tuple[List[str], List[str]]:
    """Normalize gt_files / gt_symbols from the synthesized query format."""
    raw_files: list = entry.get("gt_files") or []
    raw_symbols: list = entry.get("gt_symbols") or []
    files = [p for p in (normalize_file_path(f) for f in raw_files) if p]
    symbols = [s for s in (normalize_symbol_identifier(s) for s in raw_symbols) if s]
    return files, symbols


# ---------------------------------------------------------------------------
# Pipeline factories — each returns (pipeline_or_runner, query_fn, close_fn)
# ---------------------------------------------------------------------------


def _make_bm25(
    repo_path: str, index_path: str, args: argparse.Namespace, language: str
):
    from codenib.model.bm25_retrieve_pipeline import BM25RetrievePipeline

    pipe = BM25RetrievePipeline(
        repo_path=repo_path,
        index_path=index_path,
        top_k=args.topk,
        project_name=Path(index_path).name,
        language=language,
        graph_route=args.graph_route,
    )
    return pipe, pipe.query, pipe.close


def _make_embedding(
    repo_path: str, index_path: str, args: argparse.Namespace, language: str
):
    from codenib.model.embedding_retrieve_pipeline import EmbeddingRetrievePipeline

    pipe = EmbeddingRetrievePipeline(
        repo_path=repo_path,
        index_path=index_path,
        embedding_model=args.embedding_model,
        embedding_provider=args.embedding_provider,
        embedding_dimension=args.embedding_dimension,
        languages=[language],
        top_k=args.topk,
    )
    return pipe, pipe.query, pipe.close


def _make_agent(
    repo_path: str, index_path: str, args: argparse.Namespace, language: str
):
    import os

    from codenib.agent.runner import AgentRunner
    from codenib.agent.skills.loader import SkillLoader
    from codenib.agent.skills.registry import SkillRegistry
    from codenib.compiler.index_builders import (
        BM25IndexBuilder,
        IndexBuilderRegistry,
        VectorIndexBuilder,
    )
    from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig
    from codenib.compiler.manifest import RepoManifest
    from codenib.compiler.params import SessionContext
    from codenib.index.embedding import CodeVectorStore
    from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer
    from codenib.llm.litellm_chat import LiteLLMChat
    from codenib.ops.rerank import RerankContext
    from codenib.ops.retrieve import RetrieveContext

    cache_dir = str(repo_index_dir(index_path))

    # Phase 1: compile indexes
    idx_types = []
    if args.agent_mode in ("sparse", "hybrid"):
        idx_types.append("bm25")
    if args.agent_mode in ("dense", "hybrid"):
        idx_types.append("vector")

    breg = IndexBuilderRegistry()
    breg.register("bm25", BM25IndexBuilder(languages=[language]))
    breg.register(
        "vector",
        VectorIndexBuilder(
            languages=[language],
            embedding_model=args.embedding_model,
            embedding_dimension=args.embedding_dimension,
        ),
    )
    compiler = IndexCompiler(
        breg,
        IndexCompilerConfig(
            index_types=idx_types,
            languages=[language],
        ),
    )
    compiler.compile_repo(repo_path, cache_dir=cache_dir)

    # Phase 2: load indexes, skills, build runner.  Keep the vector store
    # registered before loading so any partial native state is also released.
    with ExitStack() as resources:
        manifest = RepoManifest.load(os.path.join(cache_dir, "repo_manifest.json"))
        bm25_index, vector_store = None, None

        if "bm25" in manifest.indexes and manifest.indexes["bm25"].status == "fresh":
            bm25_index = BM25CodeIndexer()
            bm25_index.load_index(manifest.indexes["bm25"].path)

        if (
            "vector" in manifest.indexes
            and manifest.indexes["vector"].status == "fresh"
        ):
            entry = manifest.indexes["vector"]
            artifact_contract = dict(entry.config)
            vector_store = CodeVectorStore(
                embedding_model=artifact_contract.get(
                    "embedding_model", args.embedding_model
                ),
                embedding_provider="huggingface",
                dimension=artifact_contract.get(
                    "embedding_dimension", args.embedding_dimension
                ),
                store_path=entry.path,
                artifact_metadata=artifact_contract,
            )
            resources.callback(vector_store.close)
            _load_vector_from_local_admin_boundary(
                vector_store,
                entry.path,
                semantic_contract=artifact_contract,
                evidence="synthesized-query-eval-compiled-local-manifest",
            )

        ctx: Dict[str, Any] = {
            "retrieve": RetrieveContext(
                bm25=bm25_index,
                vector_store=vector_store,
                default_top_k=args.topk,
                default_level="l2",
            ),
        }
        if vector_store:
            ctx["rerank"] = RerankContext(embedding_store=vector_store)

        skills_dir = os.path.join(_PROJECT_ROOT, "codenib", "agent", "skills")
        SkillLoader().load_all(skills_dir, contexts=ctx)

        runner = AgentRunner(
            llm=LiteLLMChat(model=args.model, temperature=0.0, max_tokens=1024),
            registry=SkillRegistry(),
            max_turns=args.max_turns,
            manifest=manifest,
            session_ctx=SessionContext(
                repo_path=repo_path,
                repo_size=manifest.file_count,
                primary_language=(
                    manifest.languages[0] if manifest.languages else language
                ),
            ),
        )

        def query_fn(q: str, **_kw) -> List[QueriedNode]:
            result = runner.run(q)
            nodes: List[QueriedNode] = []
            for tc in result.tool_calls:
                if isinstance(tc.result, list):
                    nodes.extend(n for n in tc.result if isinstance(n, QueriedNode))
            return nodes

        retained_resources = resources.pop_all()

    return runner, query_fn, retained_resources.close


_PIPELINE_FACTORIES = {
    "bm25": _make_bm25,
    "embedding": _make_embedding,
    "agent": _make_agent,
}


# ---------------------------------------------------------------------------
# Eval loop
# ---------------------------------------------------------------------------


def run_eval(args: argparse.Namespace) -> None:
    all_queries = _load_queries(args)

    pat = re.compile(args.filter_instance)
    all_queries = [q for q in all_queries if pat.search(q.get("instance_id", ""))]
    logger.info("After filtering: %d queries", len(all_queries))
    if not all_queries:
        return

    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for q in all_queries:
        groups[q["instance_id"]].append(q)
    logger.info("Grouped into %d instance(s)", len(groups))

    factory = _PIPELINE_FACTORIES[args.pipeline]
    metrics_k = sorted(set(args.metrics_k))
    metric_max_k = max(metrics_k)
    aggregate: Dict = {}
    eval_count = 0
    all_results: Optional[List[Dict]] = [] if args.result_path else None

    for instance_id, queries in groups.items():
        rep = queries[0]
        language = _infer_language(queries)

        pipe, query_fn, close_fn = None, None, None
        try:
            repo_path = _checkout_repo(
                rep["repo"], rep["base_commit"], args.repo_cache_dir
            )
            index_path = str(
                Path(args.index_cache_dir) / instance_id.replace("/", "__")
            )

            t0 = time.time()
            pipe, query_fn, close_fn = factory(repo_path, index_path, args, language)
            logger.info(
                "[%s] Pipeline built in %.1fs (lang=%s)",
                instance_id,
                time.time() - t0,
                language,
            )

            for entry in queries:
                qid = entry.get("query_id", instance_id)
                tgt_files, tgt_syms = collect_synthesized_targets(entry)
                if not tgt_files and not tgt_syms:
                    logger.info("Skipping %s — no ground truth", qid)
                    continue

                t0 = time.time()
                results = query_fn(entry["query"])
                elapsed = time.time() - t0

                metrics = evaluate_predictions(results, tgt_files, tgt_syms, metrics_k)
                aggregate_metrics(aggregate, metrics)
                eval_count += 1

                logger.info("[%s] %.1fs  %d results", qid, elapsed, len(results))
                for scope, per_k in metrics.items():
                    for k, st in per_k.items():
                        logger.info(
                            "  [%s] k=%d acc=%.3f prec=%.3f rec=%.3f hits=%d",
                            scope,
                            k,
                            st["accuracy"],
                            st["precision"],
                            st["recall"],
                            int(st["hits"]),
                        )

                if all_results is not None:
                    uf, ns = extract_predictions(results)
                    all_results.append(
                        {
                            "query_id": qid,
                            "instance_id": instance_id,
                            "method": f"{args.pipeline}_synthesized",
                            "topk": args.topk,
                            "num_results": len(results),
                            "elapsed_s": elapsed,
                            "target_files": tgt_files,
                            "target_symbols": tgt_syms,
                            "metric_k_files": uf[:metric_max_k],
                            "metric_k_node_ids": ns[:metric_max_k],
                            "metrics": metrics,
                        }
                    )
        except Exception:
            logger.exception("Error processing %s", instance_id)
        finally:
            if close_fn:
                close_fn()

    if aggregate and eval_count:
        avg = average_metrics(aggregate, eval_count)
        logger.info(
            "=== %s Aggregate (%d queries) ===", args.pipeline.upper(), eval_count
        )
        for scope, per_k in avg.items():
            for k, st in per_k.items():
                logger.info(
                    "[%s] k=%d acc=%.3f prec=%.3f rec=%.3f avg_hits=%.3f",
                    scope,
                    k,
                    st["accuracy"],
                    st["precision"],
                    st["recall"],
                    st["avg_hits"],
                )

    if args.result_path and all_results is not None:
        out = Path(args.result_path).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)
        logger.info("Results saved to %s", out)


if __name__ == "__main__":
    args = parse_args()
    logger.info("Pipeline: %s  TopK: %d", args.pipeline, args.topk)
    run_eval(args)
