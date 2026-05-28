#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Skill-agent evaluation driver on SWE-bench.

Runs ``AgentRunner`` with an arbitrary subset of the skill registry against
SWE-bench instances and reports retrieval accuracy / token usage. The skill
subset is the unit of variation — the script delegates index lifecycle to
``codeminer.compiler.build_skill_contexts``, which resolves each skill's
declared ``index_requirements`` and builds (or reuses cached) BM25 / vector
/ symbol-graph indexes accordingly.

Examples:

    # BM25 only (LocAgent-style sparse baseline + agent both supported)
    python examples/skill_agent_eval.py \
        --skills bm25_search \
        --filter-instance "^(astropy__astropy-12907)$" \
        --eval-instances "$HOME/.codeminer/swebench_lite_test_gt_single.json" \
        --result-path "$HOME/skill_eval_bm25.json"

    # Embedding search (replaces the deleted skill_agent_eval_embedding.py)
    python examples/skill_agent_eval.py \
        --skills embedding_search \
        --embedding-model nomic-ai/CodeRankEmbed \
        --embedding-dimension 768 \
        --index-cache-dir "$HOME/.codeminer/index_cache" \
        --result-path "$HOME/skill_eval_embedding.json"

    # Multiple skills — agent picks among them
    python examples/skill_agent_eval.py \
        --skills bm25_search embedding_search graph_expand \
        --max-turns 10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure the project root is on sys.path when running as a script
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _ensure_user_writable_hf_cache() -> None:
    """Redirect HuggingFace caches when ``HF_HOME`` is not writable.

    On shared machines, ``HF_HOME`` may point to e.g. ``/mnt/conda/huggingface``
    where ``datasets`` then fails on lock-file creation. Must run before
    importing ``codeminer.dataset`` (which transitively imports ``datasets``).
    Relevant for any skill that needs the embedding-model side of the
    HuggingFace cache (e.g. ``embedding_search``).
    """
    home_hf = Path.home() / ".cache" / "huggingface"
    home_hf.mkdir(parents=True, exist_ok=True)

    def _use_fallback_if_unwritable(env_key: str, fallback: Path) -> None:
        val = os.environ.get(env_key, "").strip()
        if val:
            p = Path(os.path.expanduser(val))
            try:
                p.mkdir(parents=True, exist_ok=True)
                if os.access(p, os.W_OK):
                    return
            except OSError:
                # Ignore path creation/access errors for user-provided cache dirs;
                # we immediately fall back to a known user-writable location below.
                pass
        os.environ[env_key] = str(fallback)
        fallback.mkdir(parents=True, exist_ok=True)

    _use_fallback_if_unwritable("HF_HOME", home_hf)
    _use_fallback_if_unwritable("HF_DATASETS_CACHE", home_hf / "datasets")


_ensure_user_writable_hf_cache()

from codeminer.agent.skills.loader import SkillLoader
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.compiler import build_skill_contexts
from codeminer.dataset.swebench import SwebenchDataset
from codeminer.eval.retrieval_eval import (
    aggregate_metrics,
    average_metrics,
    collect_targets,
    evaluate_predictions,
)
from codeminer.llm.litellm_chat import LiteLLMChat
from codeminer.log_utils import get_logger
from codeminer.ops.retrieve import to_queried_nodes
from codeminer.types import QueriedNode

logger = get_logger(__name__)

# LocAgent / Agentless-style SWE-Bench-Lite subset: keep instances where the patch
# touches at least one existing function-shaped symbol in GT (modified/deleted with
# parentheses in simplified mode). Yields ~274 of 300 on Lite test.
LOCAGENT_LITE_SUBSET_DOC = (
    "Restrict to the SWE-Bench-Lite subset used in LocAgent (Table 4): instances "
    "with at least one function-like target in GT (simplified_symbols). Requires "
    "--eval-instances."
)


# Output Schema


@dataclass
class CodeSymbol:
    """A code symbol with its location (from hengjia branch)."""

    name: str  # Symbol name, e.g. "Foo::bar()"
    type: str  # function / class / method / field
    file_path: str  # Relative to repo root
    line_start: int
    line_end: int
    action: str  # modify / add / delete
    description: str = ""  # Brief description


@dataclass
class LocResult:
    """Localization result for a single instance (from hengjia branch)."""

    success: bool
    repo_path: str
    locations: List[CodeSymbol] = field(default_factory=list)
    error_message: Optional[str] = None
    execution_log: List[str] = field(default_factory=list)
    usage: Optional[Dict[str, Any]] = None


@dataclass
class SkillEvalReport:
    """Evaluation report for skill-based retrieval."""

    dataset: str
    model: str
    skill_ids: List[str]
    instance_count: int
    results: List[Dict[str, Any]] = field(default_factory=list)
    aggregate_metrics: Dict[str, Any] = field(default_factory=dict)
    eval_mode: str = "agent"
    locagent_lite_subset: bool = False
    gt_symbols_mode: str = "simplified"
    total_usage: Optional[Dict[str, Any]] = None


# QueriedNode -> CodeSymbol conversion


def queried_node_to_symbol(node: QueriedNode) -> CodeSymbol:
    """Convert a QueriedNode to CodeSymbol."""
    return CodeSymbol(
        name=node.node_name or "unknown",
        type=node.type or "unknown",
        file_path=node.file or "",
        line_start=node.start_line or 0,
        line_end=node.end_line or 0,
        action="modify",  # Default to modify, agent can't determine action
        description=f"score={node.score:.4f}" if node.score else "",
    )


# Core evaluation logic


def collect_gt_targets(
    meta_or_row: Dict[str, Any], simplified_symbols: bool
) -> tuple[List[str], List[str]]:
    """GT files + symbols (simplified = LocAgent-style modified/deleted functions only)."""
    return collect_targets(meta_or_row, simplified_symbols=simplified_symbols)


def passes_locagent_lite_subset(meta: Dict[str, Any], simplified_symbols: bool) -> bool:
    """True if GT has at least one function-like symbol (Table 4 comparable subset)."""
    _tf, ts = collect_gt_targets(meta, simplified_symbols=simplified_symbols)
    return len(ts) > 0


def run_bm25_baseline_retrieval(
    problem_statement: str,
    bm25_index: Any,
    retrieve_top_k: int,
) -> List[QueriedNode]:
    """Single BM25 call over problem_statement (LocAgent-style sparse baseline)."""
    raw = bm25_index.search(
        query=problem_statement,
        top_k=retrieve_top_k,
        return_code_content=False,
        wrap_with_ln=False,
        filter_test=False,
    )
    return to_queried_nodes(raw)


def run_agent_with_skills(
    query: str,
    contexts: Dict[str, Any],
    llm: LiteLLMChat,
    max_turns: int = 3,
    repo_path: str = ".",
    allow_skills: Optional[List[str]] = None,
    compile_table: Optional[Dict[str, Any]] = None,
    primary_language: Optional[str] = None,
) -> tuple[List[QueriedNode], List[str], Optional[Dict[str, Any]]]:
    """Run ``AgentRunner`` with the given skill allowlist + pre-built contexts.

    ``contexts`` is the dict returned by
    ``codeminer.compiler.build_skill_contexts`` — keyed by skill_type
    (``"retrieve"`` / ``"expand"`` / ...) and carrying the loaded index
    artifacts. The agent doesn't see the indexes directly; ``SkillLoader``
    wires each skill executor to the matching context.

    When ``compile_table`` is provided (CAR / issue #149), it narrows
    ``allow_skills`` at agent entry based on the classified scenario.
    ``allow_skills`` remains the upper bound.

    Returns:
        Tuple of (results, execution_log, usage_stats)
    """
    from codeminer.agent.runner import AgentRunner
    from codeminer.compiler.params import SessionContext

    execution_log = []
    usage_stats: Dict[str, Any] = {}
    allow_set = set(allow_skills or ["bm25_search"])

    try:
        skills_dir = os.path.join(_PROJECT_ROOT, "codeminer", "agent", "skills")

        # SkillRegistry is a singleton; reset so this instance's contexts
        # replace the previous one (load_all skips already-registered skills).
        SkillRegistry().reset()

        loader = SkillLoader()
        loaded = loader.load_all(skills_dir, contexts=contexts)
        execution_log.append(f"Loaded {len(loaded)} skills")

        session_ctx = SessionContext(
            repo_path=repo_path,
            repo_size=1000,
            primary_language=primary_language or "python",
        )

        registry = SkillRegistry()

        runner = AgentRunner(
            llm=llm,
            registry=registry,
            max_turns=max_turns,
            allow_skills=allow_set,
            session_ctx=session_ctx,
            compile_table=compile_table,
        )
        execution_log.append(
            f"Created AgentRunner (max_turns={max_turns}, "
            f"allow_skills={sorted(allow_set)}, "
            f"compile_table={'on' if compile_table else 'off'})"
        )

        # Run the agent
        result = runner.run(query)
        execution_log.append(f"Agent completed in {result.total_turns} turns")
        execution_log.append(f"Total tool calls: {len(result.tool_calls)}")

        # Collect results from tool calls
        all_results = []
        for tc in result.tool_calls:
            execution_log.append(f"  - {tc.skill_id}({tc.arguments})")
            if tc.error:
                execution_log.append(f"    Error: {tc.error}")
            elif isinstance(tc.result, list):
                all_results.extend(tc.result)
                execution_log.append(f"    Got {len(tc.result)} results")

        # Collect usage stats
        usage_stats = {
            "total_turns": result.total_turns,
            "total_duration_ms": result.total_duration_ms,
            "tool_call_count": len(result.tool_calls),
            "token_usage": result.usage.to_dict() if result.usage else None,
        }

        return all_results, execution_log, usage_stats

    except Exception as e:
        execution_log.append(f"Error: {str(e)}")
        logger.error(f"Failed to run agent: {e}", exc_info=True)
        return [], execution_log, usage_stats


def evaluate_instance(
    instance: Dict[str, Any],
    dataset: SwebenchDataset,
    llm: Optional[LiteLLMChat],
    args: argparse.Namespace,
    eval_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate a single SWE-bench instance."""
    instance_id = instance["instance_id"]
    problem_statement = instance["problem_statement"]

    logger.info(f"\n{'=' * 60}")
    logger.info(f"Instance: {instance_id}")
    logger.info(f"{'=' * 60}")

    start_time = time.time()
    execution_log = []

    try:
        # Step 1: Process instance (clone repo, checkout commit)
        execution_log.append(f"Processing instance {instance_id}")
        dataset.process_instance(instance)
        repo_path = dataset.get_repo_path(instance)
        execution_log.append(f"Repository prepared at {repo_path}")

        # Step 2: Build the union of indexes the requested skills need.
        # In bm25_baseline mode we still go through the compiler so the
        # build path stays single-source; we just read the BM25 index back
        # out for the direct-query call.
        skill_ids = list(args.skills) if args.eval_mode == "agent" else ["bm25_search"]
        execution_log.append(f"Building contexts for skills={skill_ids}...")
        contexts = build_skill_contexts(
            repo_path=repo_path,
            skill_ids=skill_ids,
            languages=args.languages,
            cache_dir=args.index_cache_dir,
            embedding_model=args.embedding_model,
            embedding_dimension=args.embedding_dimension,
            default_top_k=args.topk,
            default_level="l2",
        )
        execution_log.append(f"Built contexts for {sorted(contexts.keys())}")

        simplified_gt = args.gt_symbols != "full"

        # Step 3: Retrieve (agent tool-calling vs single-query BM25 baseline)
        if args.eval_mode == "bm25_baseline":
            retrieve_ctx = contexts.get("retrieve")
            if retrieve_ctx is None or retrieve_ctx.bm25 is None:
                raise RuntimeError(
                    "bm25_baseline mode requires bm25_search in --skills "
                    "(it provides the BM25 index used for the direct query)."
                )
            execution_log.append(
                "Running BM25 baseline (single query = problem_statement)..."
            )
            retrieve_k = max(max(args.metrics_k), 128)
            results = run_bm25_baseline_retrieval(
                problem_statement, retrieve_ctx.bm25, retrieve_k
            )
            search_log = [
                f"BM25 baseline retrieved {len(results)} nodes (top_k={retrieve_k})"
            ]
            execution_log.extend(search_log)
            usage = {
                "total_turns": 0,
                "total_duration_ms": 0.0,
                "tool_call_count": 0,
                "eval_mode": "bm25_baseline",
            }
        else:
            if llm is None:
                raise RuntimeError("eval_mode=agent requires an LLM")
            execution_log.append(f"Running agent with skills={args.skills}...")
            # Pick the per-instance language for CAR / classify().
            # SWE-bench Multilingual rows carry "language"; SWE-bench
            # (English) is python-only. Falls back to the first --languages
            # CLI value, then "python".
            instance_lang = (
                instance.get("language")
                or instance.get("Language")
                or (args.languages[0] if args.languages else "python")
            )
            results, search_log, usage = run_agent_with_skills(
                query=problem_statement,
                contexts=contexts,
                llm=llm,
                max_turns=args.max_turns,
                repo_path=repo_path,
                allow_skills=args.skills,
                compile_table=getattr(args, "_compile_table", None),
                primary_language=instance_lang,
            )
            execution_log.extend(search_log)

        # Step 4: Convert to CodeSymbol
        locations = [queried_node_to_symbol(node) for node in results]
        execution_log.append(f"Converted {len(locations)} nodes to CodeSymbol")

        # Step 5: Evaluate predictions (HF row and/or external GT JSON)
        if eval_metadata is not None:
            meta = eval_metadata.get(instance_id)
            if not meta:
                raise RuntimeError(
                    f"No eval metadata for {instance_id} in --eval-instances"
                )
            target_files, target_symbols = collect_gt_targets(meta, simplified_gt)
        else:
            target_files, target_symbols = collect_gt_targets(instance, simplified_gt)
        execution_log.append(
            f"Target: {len(target_files)} files, {len(target_symbols)} symbols"
        )

        gt_empty = not target_files and not target_symbols
        if gt_empty:
            logger.warning(
                "%s: GT has no target_files/target_symbols (HF rows often lack these). "
                "Retrieval metrics are not meaningful without --eval-instances "
                "(gt_locate JSON).",
                instance_id,
            )

        metrics = evaluate_predictions(
            nodes=results,
            target_files=target_files,
            target_symbols=target_symbols,
            ks=args.metrics_k,
        )

        loc_result = LocResult(
            success=True,
            repo_path=repo_path,
            locations=locations,
            execution_log=execution_log,
            usage=usage,
        )

        elapsed = time.time() - start_time
        logger.info(f"Completed in {elapsed:.2f}s")

        return {
            "instance_id": instance_id,
            "success": True,
            "loc_result": asdict(loc_result),
            "metrics": metrics,
            "target_files": target_files,
            "target_symbols": target_symbols,
            "metrics_meaningful": not gt_empty,
            "elapsed_seconds": elapsed,
        }

    except Exception as e:
        logger.error(f"Failed to evaluate {instance_id}: {e}", exc_info=True)
        elapsed = time.time() - start_time

        loc_result = LocResult(
            success=False,
            repo_path=dataset.get_repo_path(instance),
            error_message=str(e),
            execution_log=execution_log,
        )

        return {
            "instance_id": instance_id,
            "success": False,
            "loc_result": asdict(loc_result),
            "error": str(e),
            "elapsed_seconds": elapsed,
        }


# Main evaluation loop


def run_evaluation(args: argparse.Namespace) -> SkillEvalReport:
    """Run evaluation on the dataset."""
    logger.info(f"Starting evaluation with:")
    logger.info(f"  Dataset: {args.dataset}")
    logger.info(f"  Split: {args.split}")
    logger.info(f"  Model: {args.model}")
    logger.info(f"  Filter: {args.filter_instance}")
    logger.info(f"  Metrics K: {args.metrics_k}")
    logger.info(f"  Eval mode: {args.eval_mode}")
    logger.info(f"  GT symbols: {args.gt_symbols}")
    logger.info(f"  LocAgent Lite subset: {args.locagent_lite_subset}")
    if args.eval_instances:
        logger.info(f"  Eval instances: {args.eval_instances}")
    else:
        logger.warning(
            "No --eval-instances: default HF instances usually lack symbol-level GT; "
            "metrics may stay at zero. Use a gt_locate JSON path for valid retrieval_eval."
        )

    # Issue #149: load the CAR compile_table once. The agent loop reads
    # ``args._compile_table`` per instance.
    args._compile_table = None
    if getattr(args, "compile_table", None):
        from codeminer.agent.compile import load_compile_table

        table_path = Path(args.compile_table)
        args._compile_table = dict(load_compile_table(table_path))
        logger.info(
            f"  Compile table: {table_path} ({len(args._compile_table)} scenarios)"
        )

    # Load dataset
    dataset = SwebenchDataset(
        dataset=args.dataset,
        split=args.split,
        filter_instance=args.filter_instance,
        root=args.cache_dir,
        repo_root=args.repo_cache_dir,
    )
    instances = dataset.load()
    logger.info(f"Loaded {len(instances)} instances")

    eval_lookup: Optional[Dict[str, Any]] = None
    if args.eval_instances:
        eval_lookup = dataset.load_eval_metadata(args.eval_instances)

    if args.locagent_lite_subset:
        if not eval_lookup:
            raise ValueError(
                "--locagent-lite-subset requires --eval-instances (GT JSON)."
            )
        simplified_for_filter = args.gt_symbols != "full"
        before = len(instances)
        kept: List[Dict[str, Any]] = []
        for row in instances:
            iid = row["instance_id"]
            meta = eval_lookup.get(iid)
            if meta and passes_locagent_lite_subset(meta, simplified_for_filter):
                kept.append(row)
        instances = kept
        logger.info(
            "LocAgent Lite subset: kept %d / %d instances (function-shaped GT)",
            len(instances),
            before,
        )

    # Initialize LLM (agent mode only)
    llm: Optional[LiteLLMChat] = None
    if args.eval_mode == "agent":
        llm_kwargs = {}
        if args.api_base:
            llm_kwargs["api_base"] = args.api_base
        if args.api_key:
            llm_kwargs["api_key"] = args.api_key

        vertex_extra: Dict[str, Any] = {}
        if args.vertex_project:
            vertex_extra["vertex_project"] = args.vertex_project
        if args.vertex_location:
            vertex_extra["vertex_location"] = args.vertex_location

        llm = LiteLLMChat(
            model=args.model,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            extra_kwargs=vertex_extra,
            **llm_kwargs,
        )

    # Run evaluation
    results = []
    aggregate = {}
    metric_count = 0

    for idx, instance in enumerate(instances):
        logger.info(
            f"\n[{idx + 1}/{len(instances)}] Evaluating {instance['instance_id']}"
        )

        result = evaluate_instance(
            instance, dataset, llm, args, eval_metadata=eval_lookup
        )
        results.append(result)

        # Aggregate metrics
        if result["success"] and "metrics" in result:
            aggregate_metrics(aggregate, result["metrics"])
            metric_count += 1

    # Compute average metrics (only over instances that produced metrics)
    avg_metrics = average_metrics(aggregate, metric_count) if metric_count else {}

    # Build report
    skill_ids = list(args.skills) if args.eval_mode == "agent" else ["bm25_baseline"]
    report_model = args.model if args.eval_mode == "agent" else "bm25_baseline"

    # Aggregate token usage across all instances (agent mode only).
    total_usage: Optional[Dict[str, Any]] = None
    if args.eval_mode == "agent":
        prompt_total = 0
        completion_total = 0
        total_total = 0
        cost_total: Optional[float] = None
        for r in results:
            if not r.get("success"):
                continue
            usage = r.get("loc_result", {}).get("usage") or {}
            tu = usage.get("token_usage") or {}
            prompt_total += int(tu.get("prompt_tokens") or 0)
            completion_total += int(tu.get("completion_tokens") or 0)
            total_total += int(tu.get("total_tokens") or 0)
            c = tu.get("cost_usd")
            if c is not None:
                cost_total = (cost_total or 0.0) + float(c)
        total_usage = {
            "prompt_tokens": prompt_total,
            "completion_tokens": completion_total,
            "total_tokens": total_total,
            "cost_usd": cost_total,
        }

    report = SkillEvalReport(
        dataset=args.dataset,
        model=report_model,
        skill_ids=skill_ids,
        instance_count=len(instances),
        results=results,
        aggregate_metrics=avg_metrics,
        eval_mode=args.eval_mode,
        locagent_lite_subset=args.locagent_lite_subset,
        gt_symbols_mode=args.gt_symbols,
        total_usage=total_usage,
    )

    return report


# CLI


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Skill-agent evaluation on SWE-bench (any --skills subset).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # Dataset args
    parser.add_argument(
        "--dataset",
        type=str,
        default="princeton-nlp/SWE-bench_Lite",
        help="SWE-bench dataset to use",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="test",
        help="HuggingFace dataset split (e.g. test, dev)",
    )
    parser.add_argument(
        "--filter-instance",
        type=str,
        default=".*",
        help="Regex filter for instance_id",
    )
    parser.add_argument(
        "--eval-instances",
        type=str,
        default=None,
        help=(
            "Path to GT/eval JSON (instance_id -> targets). "
            "If missing, SwebenchDataset generates it (clone + patch parse; slow)."
        ),
    )
    parser.add_argument(
        "--locagent-lite-subset",
        action="store_true",
        help=LOCAGENT_LITE_SUBSET_DOC,
    )
    parser.add_argument(
        "--eval-mode",
        type=str,
        choices=["agent", "bm25_baseline"],
        default="agent",
        help=(
            "agent: AgentRunner + bm25_search tool-calling. "
            "bm25_baseline: one BM25 query per instance using problem_statement "
            "(LocAgent Table 4 sparse baseline style; no LLM calls)."
        ),
    )
    parser.add_argument(
        "--gt-symbols",
        type=str,
        choices=["simplified", "full"],
        default="simplified",
        help=(
            "simplified: symbols_modified + symbols_deleted, functions only (parens); "
            "LocAgent-style. full: also symbols_added (closer to broader patch targets)."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=None,
        help="Directory to cache dataset files",
    )
    parser.add_argument(
        "--repo-cache-dir",
        type=str,
        default=None,
        help="Directory to cache repositories",
    )

    # Model args
    parser.add_argument(
        "--model",
        type=str,
        default="vertex_ai/gemini-2.5-flash",
        help="LLM model to use (litellm format)",
    )
    parser.add_argument(
        "--api-base",
        type=str,
        default=None,
        help="API base URL (for vLLM, etc.)",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="API key (optional)",
    )
    parser.add_argument(
        "--vertex-project",
        type=str,
        default=None,
        help="Vertex AI GCP project id (passed to litellm; optional if env is set)",
    )
    parser.add_argument(
        "--vertex-location",
        type=str,
        default=None,
        help="Vertex AI region, e.g. us-central1 (optional)",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="LLM temperature",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=4096,
        help="Max tokens for LLM response",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=3,
        help="Max turns for agent execution",
    )
    parser.add_argument(
        "--topk",
        type=int,
        default=50,
        help="Default top_k passed through to retrieval skills via RetrieveContext.",
    )
    parser.add_argument(
        "--skills",
        type=str,
        nargs="+",
        default=["bm25_search"],
        help=(
            "Skill IDs the agent is allowed to use (allowlist). The compiler "
            "resolves the union of index_requirements across these skills and "
            "builds (or reuses cached) indexes accordingly. Examples: "
            "bm25_search / embedding_search / graph_expand / hybrid_search."
        ),
    )
    parser.add_argument(
        "--compile-table",
        type=str,
        default=None,
        help=(
            "Path to a CAR compile_table (JSON / YAML). When set, the "
            "AgentRunner narrows --skills per-query based on the classified "
            "scenario. --skills remains the upper bound."
        ),
    )

    # Index args (consumed by build_skill_contexts; only relevant for skills
    # that declare a vector index_requirement, e.g. embedding_search).
    parser.add_argument(
        "--embedding-model",
        type=str,
        default="nomic-ai/CodeRankEmbed",
        help="Embedding model used when a requested skill needs a vector index.",
    )
    parser.add_argument(
        "--embedding-dimension",
        type=int,
        default=768,
        help="Embedding vector dimension (must match the embedding model).",
    )
    parser.add_argument(
        "--index-cache-dir",
        type=str,
        default=None,
        help=(
            "Directory under which built indexes are cached "
            "(default: <repo_path>/.codeminer_cache). Reused across runs; "
            "delete to force a rebuild."
        ),
    )

    # Evaluation args
    parser.add_argument(
        "--metrics-k",
        type=int,
        nargs="+",
        default=[1, 3, 5, 10],
        help=(
            "K values for Acc@K (default includes 1,3,5 for file-level vs LocAgent Table 4)"
        ),
    )
    parser.add_argument(
        "--languages",
        type=str,
        nargs="+",
        default=["python"],
        help="Programming languages to index",
    )

    # Output args
    parser.add_argument(
        "--result-path",
        type=str,
        default="results/skill_eval.json",
        help="Path to save evaluation results",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Run evaluation
    report = run_evaluation(args)

    # Save results
    result_path = Path(args.result_path)
    result_path.parent.mkdir(parents=True, exist_ok=True)

    with open(result_path, "w") as f:
        json.dump(asdict(report), f, indent=2)

    logger.info(f"\n{'=' * 60}")
    logger.info("Evaluation complete!")
    logger.info(f"{'=' * 60}")
    logger.info(f"Results saved to: {result_path}")

    if report.total_usage:
        tu = report.total_usage
        cost_str = f"${tu['cost_usd']:.4f}" if tu.get("cost_usd") is not None else "n/a"
        logger.info(
            "\nTotal token usage: prompt=%d completion=%d total=%d cost=%s",
            tu.get("prompt_tokens", 0),
            tu.get("completion_tokens", 0),
            tu.get("total_tokens", 0),
            cost_str,
        )

    logger.info(f"\nAggregate Metrics:")

    def _metric_k_key(scope: str, k: int) -> Any:
        m = report.aggregate_metrics.get(scope, {})
        if k in m:
            return k
        if str(k) in m:
            return str(k)
        return None

    # Highlight file @1/@3/@5 (LocAgent Table 4 columns)
    fkeys = [_metric_k_key("files", k) for k in (1, 3, 5)]
    if any(fkeys):
        logger.info("\nFILES @1 / @3 / @5 (compare to LocAgent Table 4 BM25 file Acc):")
        for k, key in zip((1, 3, 5), fkeys, strict=False):
            if key is None:
                continue
            stats = report.aggregate_metrics["files"][key]
            logger.info(
                f"  @{k}: acc={stats['accuracy']:.4f} recall={stats['recall']:.4f} "
                f"prec={stats['precision']:.4f}"
            )

    for scope in ["files", "symbols"]:
        if scope not in report.aggregate_metrics:
            continue
        logger.info(f"\n{scope.upper()}:")
        for k, stats in sorted(
            report.aggregate_metrics[scope].items(),
            key=lambda item: int(item[0]),
        ):
            logger.info(f"  @{k}:")
            logger.info(f"    accuracy: {stats['accuracy']:.4f}")
            logger.info(f"    recall:   {stats['recall']:.4f}")
            logger.info(f"    precision:{stats['precision']:.4f}")


if __name__ == "__main__":
    main()
