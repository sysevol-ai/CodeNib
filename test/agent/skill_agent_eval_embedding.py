#!/usr/bin/env python3
"""
Skill Agent Eval — embedding_search via AgentRunner on SWE-bench.

Registers only the ``embedding_search`` skill, builds a per-instance FAISS
vector index over the checked-out repo, runs :class:`AgentRunner` with
LiteLLM (default: Vertex Gemini), collects ``QueriedNode`` results from tool
calls, and evaluates against SWE-bench ground-truth file/symbol targets.

If you see ``PermissionError`` on ``/mnt/conda/huggingface``, either export
``HF_HOME=$HOME/.cache/huggingface`` before running, or rely on the script's
automatic fallback (see ``_ensure_user_writable_hf_cache``).

Run from the repository root::

    # Single instance (Vertex Gemini, default)
    python test/agent/skill_agent_eval_embedding.py \\
        --dataset swebench_lite \\
        --filter-instance "^(astropy__astropy-12907)$" \\
        --result-path results/skill_eval_embedding_single.json

    # Local vLLM
    python test/agent/skill_agent_eval_embedding.py \\
        --dataset swebench_lite \\
        --model openai/Qwen/Qwen2.5-Coder-7B-Instruct \\
        --api-base http://localhost:9000/v1 \\
        --filter-instance "^(astropy__astropy-12907)$" \\
        --result-path results/skill_eval_embedding_vllm.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# test/agent/ → repo root is three levels up
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _ensure_user_writable_hf_cache() -> None:
    """Redirect HuggingFace caches when HF_HOME (or HF_DATASETS_CACHE) is not writable.

    On shared machines, ``HF_HOME`` may point to e.g. ``/mnt/conda/huggingface``;
    ``datasets`` then fails with PermissionError when creating lock files under that path.
    Must run before importing ``codeminer.dataset`` (which pulls in ``datasets``).
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
                # The configured cache path may be invalid or not writable on shared hosts;
                # ignore and fall back to a user-writable cache directory below.
                pass
        os.environ[env_key] = str(fallback)
        fallback.mkdir(parents=True, exist_ok=True)

    _use_fallback_if_unwritable("HF_HOME", home_hf)
    _use_fallback_if_unwritable("HF_DATASETS_CACHE", home_hf / "datasets")


_ensure_user_writable_hf_cache()

from codeminer.agent.runner import AgentRunner
from codeminer.agent.skills.loader import SkillLoader
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.compiler.params import SessionContext
from codeminer.dataset.swebench import SwebenchDataset
from codeminer.eval.retrieval_eval import (
    aggregate_metrics,
    average_metrics,
    collect_targets,
    evaluate_predictions,
    extract_predictions,
)
from codeminer.index.embedding import CodeVectorStore, build_hierarchical_vector_store
from codeminer.llm.litellm_chat import LiteLLMChat
from codeminer.log_utils import get_logger
from codeminer.ops.retrieve import RetrieveContext
from codeminer.types import QueriedNode

logger = get_logger(__name__)

_SKILLS_DIR = os.path.join(_PROJECT_ROOT, "codeminer", "agent", "skills")
_EMBEDDING_SKILL_DIR = os.path.join(_SKILLS_DIR, "embedding_search")

_DATASET_MAP = {
    "swebench_lite": "princeton-nlp/SWE-bench_Lite",
    "swebench_verified": "princeton-nlp/SWE-bench_Verified",
}

# Steer the LLM toward the only registered tool (embedding_search).
_EMBEDDING_ONLY_SYSTEM = """\
You are a code search agent working on a single repository at a fixed commit.
You have exactly one tool: embedding_search — semantic (vector) search over \
the codebase. Use short, focused queries derived from the problem statement. \
You may call embedding_search multiple times with different queries, then \
give a brief final summary when you have enough context."""


def _safe_index_subdir(instance_id: str) -> str:
    """Convert instance_id to a safe directory name by replacing special chars."""
    return instance_id.replace("/", "__").replace(":", "_")


def _node_to_location(node: QueriedNode) -> dict:
    """Convert a QueriedNode to a location dictionary for the output report."""
    return {
        "name": node.node_name,
        "type": node.type or "unknown",
        "file_path": node.file or "",
        "line_start": node.start_line or 0,
        "line_end": node.end_line or 0,
        "action": "modify",
        "description": f"score={node.score:.4f}" if node.score is not None else "",
    }


def _loc_result_dict(
    *,
    success: bool,
    repo_path: str,
    nodes: List[QueriedNode],
    execution_log: List[str],
    error_message: Optional[str] = None,
) -> dict:
    """Build a location result dictionary for the evaluation output."""
    locations = [_node_to_location(node) for node in nodes]
    return {
        "success": success,
        "repo_path": repo_path,
        "locations": locations,
        "error_message": error_message,
        "execution_log": execution_log,
    }


def count_files(repo_path: str) -> int:
    """Recursively count all files in the repository."""
    total = 0
    for _, _, files in os.walk(repo_path):
        total += len(files)
    return total


def build_or_load_vector_store(
    repo_path: str,
    index_path: str,
    languages: List[str],
    embedding_model: str,
    embedding_provider: str,
    embedding_dimension: int,
    *,
    rebuild: bool,
    batch_size: int = 4,
) -> CodeVectorStore:
    """Build or load a hierarchical FAISS index for the given repository.

    Args:
        repo_path: Path to the repository root.
        index_path: Path where the index should be stored.
        languages: List of programming languages to index.
        embedding_model: Name of the embedding model to use.
        embedding_provider: Provider for the embedding model (e.g., 'huggingface').
        embedding_dimension: Dimensionality of the embedding vectors.
        rebuild: If True, delete existing index and rebuild from scratch.

    Returns:
        CodeVectorStore with the loaded or newly built index.
    """
    path = Path(index_path).expanduser().resolve()
    l0 = path / "l0"
    l2 = path / "l2"

    emb_kw = _get_embedding_kwargs(embedding_provider, batch_size)

    # Rebuild: delete existing index
    if rebuild and path.exists():
        shutil.rmtree(path, ignore_errors=True)

    # Load from cache if available
    if not rebuild and l0.exists() and l2.exists():
        logger.info("Loading cached vector index from %s", path)
        vs = CodeVectorStore(
            embedding_model=embedding_model,
            embedding_provider=embedding_provider,
            dimension=embedding_dimension,
            store_path=str(path),
            **emb_kw,
        )
        vs.load(str(path))
        return vs

    # Build new index
    path.mkdir(parents=True, exist_ok=True)
    logger.info("Building vector index at %s (this may take a while)", path)
    return build_hierarchical_vector_store(
        repo_path=repo_path,
        index_path=str(path),
        languages=languages,
        embedding_model=embedding_model,
        embedding_provider=embedding_provider,
        embedding_dimension=embedding_dimension,
        embedding_kwargs=emb_kw if emb_kw else None,
    )


def _get_embedding_kwargs(
    embedding_provider: str, batch_size: int = 4
) -> Dict[str, Any]:
    """Build embedding model kwargs for GPU optimization.

    Args:
        embedding_provider: The embedding provider (e.g., 'huggingface').
        batch_size: Batch size for encoding (default: 4).

    Returns:
        Dictionary of kwargs to pass to CodeVectorStore.
    """
    if embedding_provider.lower() != "huggingface":
        return {}

    return {
        "model_kwargs": {
            "trust_remote_code": True,
            "device": "cuda",
            "model_kwargs": {"torch_dtype": "float16"},  # Use FP16 to reduce GPU memory
        },
        "encode_kwargs": {
            "batch_size": batch_size,  # Adjustable batch size to avoid OOM
            "normalize_embeddings": True,
        },
    }


def load_embedding_skill(retrieve_ctx: RetrieveContext) -> SkillRegistry:
    """Reset the singleton registry and load only embedding_search."""
    registry = SkillRegistry()
    registry.reset()
    loader = SkillLoader()
    metadata = loader.load_skill(
        _EMBEDDING_SKILL_DIR, contexts={"retrieve": retrieve_ctx}
    )
    if metadata is None:
        raise RuntimeError(
            f"Failed to load embedding_search skill from {_EMBEDDING_SKILL_DIR}"
        )
    registry.register(metadata)
    return registry


def _collect_retrieved_nodes(
    agent_result,
    execution_log: List[str],
) -> List[QueriedNode]:
    """Extract all QueriedNodes from embedding_search tool calls in agent result.

    Args:
        agent_result: The AgentResult object from runner.run().
        execution_log: List to append execution details to.

    Returns:
        List of QueriedNode objects retrieved from all successful tool calls.
    """
    retrieved_nodes: List[QueriedNode] = []
    for tc in agent_result.tool_calls:
        if tc.skill_id != "embedding_search":
            continue
        if tc.error is not None:
            execution_log.append(
                f"embedding_search error: {tc.error} args={tc.arguments}"
            )
            continue
        if isinstance(tc.result, list):
            retrieved_nodes.extend(tc.result)
            execution_log.append(
                f"embedding_search({tc.arguments}) -> {len(tc.result)} nodes"
            )
    return retrieved_nodes


def _process_single_instance(
    instance: dict,
    eval_metadata: dict,
    dataset_obj: SwebenchDataset,
    args: argparse.Namespace,
    llm: LiteLLMChat,
    index_cache_base: Path,
    metrics_k: List[int],
) -> dict:
    """Process a single SWE-bench instance and return evaluation result.

    Args:
        instance: The dataset instance to process.
        eval_metadata: Ground truth metadata for evaluation.
        dataset_obj: The SwebenchDataset object.
        args: Command-line arguments.
        llm: The LLM client for agent execution.
        index_cache_base: Base directory for index caching.
        metrics_k: List of k values for metrics evaluation.

    Returns:
        Dictionary containing instance results and metrics.
    """
    instance_id = instance["instance_id"]
    execution_log: List[str] = []

    # Get ground truth targets
    metadata = eval_metadata.get(instance_id)
    if not metadata:
        logger.info("Skipping %s — no eval metadata", instance_id)
        return None
    target_files, target_symbols = collect_targets(metadata)
    if not target_symbols:
        logger.info("Skipping %s — no valid target symbols", instance_id)
        return None

    t0 = time.time()

    # Prepare repository
    dataset_obj.process_instance(instance)
    repo_path = dataset_obj.get_repo_path(instance)
    execution_log.append(f"repo_path={repo_path}")

    # Build or load vector index
    instance_index_dir = index_cache_base / _safe_index_subdir(instance_id)
    vector_store = build_or_load_vector_store(
        repo_path,
        str(instance_index_dir),
        list(args.languages),
        args.embedding_model,
        args.embedding_provider,
        args.embedding_dimension,
        rebuild=args.rebuild_index,
        batch_size=args.embedding_batch_size,
    )

    # Load skill and create agent
    retrieve_ctx = RetrieveContext(
        vector_store=vector_store,
        default_top_k=args.topk,
        default_level="l2",
    )
    registry = load_embedding_skill(retrieve_ctx)

    session_ctx = SessionContext(
        repo_path=repo_path,
        repo_size=count_files(repo_path),
        primary_language=args.languages[0],
    )

    runner = AgentRunner(
        llm=llm,
        registry=registry,
        max_turns=args.max_turns,
        session_ctx=session_ctx,
        system_prompt=_EMBEDDING_ONLY_SYSTEM,
    )

    # Run agent
    agent_result = runner.run(instance["problem_statement"])
    elapsed = time.time() - t0

    execution_log.append(
        f"turns={agent_result.total_turns} "
        f"tool_calls={len(agent_result.tool_calls)}"
    )

    # Collect retrieved nodes
    retrieved_nodes = _collect_retrieved_nodes(agent_result, execution_log)

    # Build location result
    loc_result = _loc_result_dict(
        success=True,
        repo_path=repo_path,
        nodes=retrieved_nodes,
        execution_log=execution_log,
    )

    # Evaluate predictions
    metrics = evaluate_predictions(
        nodes=retrieved_nodes,
        target_files=target_files,
        target_symbols=target_symbols,
        ks=metrics_k,
    )

    logger.info(
        "[%s] Done in %.1fs (%d retrieved nodes)",
        instance_id,
        elapsed,
        len(retrieved_nodes),
    )

    # Build result dictionary
    unique_files, normalized_symbols = extract_predictions(retrieved_nodes)
    metric_max_k = max(metrics_k)
    return {
        "instance_id": instance_id,
        "method": "skill_agent_embedding",
        "model": args.model,
        "turns": agent_result.total_turns,
        "num_tool_calls": len(agent_result.tool_calls),
        "num_retrieved_nodes": len(retrieved_nodes),
        "elapsed_s": elapsed,
        "metric_k_files": unique_files[:metric_max_k],
        "metric_k_node_ids": normalized_symbols[:metric_max_k],
        "metrics": metrics,
        "loc_result": loc_result,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Skill agent eval: embedding_search via AgentRunner on SWE-bench.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default="swebench_lite",
        choices=list(_DATASET_MAP.keys()),
    )
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument("--filter-instance", type=str, default=".*")
    parser.add_argument(
        "--model",
        type=str,
        default="vertex_ai/gemini-2.5-flash",
        help="LiteLLM model id (e.g. vertex_ai/gemini-2.5-flash)",
    )
    parser.add_argument(
        "--api-base",
        type=str,
        default=None,
        help="Optional api_base for local OpenAI-compatible servers (vLLM)",
    )
    parser.add_argument("--max-turns", type=int, default=3)
    parser.add_argument("--metrics-k", type=int, nargs="+", default=[1, 3, 5, 10])
    parser.add_argument(
        "--topk",
        type=int,
        default=50,
        help="Default top_k for RetrieveContext / skill defaults scaling",
    )
    parser.add_argument("--languages", type=str, nargs="+", default=["python"])
    parser.add_argument(
        "--embedding-model",
        type=str,
        default="nomic-ai/CodeRankEmbed",
    )
    parser.add_argument(
        "--embedding-provider",
        type=str,
        default="huggingface",
    )
    parser.add_argument(
        "--embedding-dimension",
        type=int,
        default=768,
    )
    parser.add_argument(
        "--embedding-batch-size",
        type=int,
        default=4,
        help="Batch size for embedding computation (lower = less GPU memory)",
    )
    parser.add_argument(
        "--index-cache-dir",
        type=str,
        default="~/.codeminer/embedding_eval_index",
        help="Per-instance FAISS indices are stored under <dir>/<instance_id>/",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="Delete cached index for each instance and rebuild",
    )
    parser.add_argument(
        "--eval-instances",
        type=str,
        default=None,
        help="Path to eval annotations JSON; auto-generated if missing",
    )
    parser.add_argument(
        "--repo-cache-dir",
        type=str,
        default="~/.codeminer/repos",
    )
    parser.add_argument("--result-path", type=str, default=None)
    return parser.parse_args()


def _log_aggregate_metrics(averaged: Dict[str, Any], eval_count: int) -> None:
    """Log the aggregate metrics to console.

    Args:
        averaged: Dictionary of averaged metrics by scope and k.
        eval_count: Number of instances evaluated.
    """
    logger.info(
        "=== Skill Agent embedding_search aggregate (%d instances) ===",
        eval_count,
    )
    for scope, per_k in averaged.items():
        for k, stats in per_k.items():
            logger.info(
                "[%s] k=%s acc=%.3f prec=%.3f recall=%.3f avg_hits=%.3f",
                scope,
                k,
                stats["accuracy"],
                stats["precision"],
                stats["recall"],
                stats["avg_hits"],
            )


def run_eval(args: argparse.Namespace) -> None:
    """Run evaluation on SWE-bench dataset using embedding_search skill.

    Args:
        args: Command-line arguments containing dataset, model, and other configs.
    """
    # Load dataset
    hf_dataset = _DATASET_MAP[args.dataset]
    dataset_obj = SwebenchDataset(
        dataset=hf_dataset,
        split=args.split,
        filter_instance=args.filter_instance,
        repo_root=args.repo_cache_dir,
    )
    dataset_instances = dataset_obj.load()
    if len(dataset_instances) == 0:
        raise ValueError(f"No instances found for dataset={args.dataset}")

    logger.info("Loaded %d instance(s)", len(dataset_instances))

    # Load ground truth metadata
    eval_path = args.eval_instances or str(
        Path.home() / ".codeminer" / f"{args.dataset}_{args.split}_gt.json"
    )
    eval_metadata = dataset_obj.load_eval_metadata(eval_path)

    # Setup
    metrics_k = sorted(set(args.metrics_k))
    index_cache_base = Path(args.index_cache_dir).expanduser().resolve()
    llm = LiteLLMChat(
        model=args.model,
        temperature=0.0,
        max_tokens=1024,
        api_base=args.api_base,
    )

    # Process instances
    agg: Dict[str, Any] = {}
    eval_count = 0
    all_results: List[dict] = []

    for idx, instance in enumerate(dataset_instances):
        instance_id = instance["instance_id"]
        execution_log: List[str] = []

        try:
            result = _process_single_instance(
                instance=instance,
                eval_metadata=eval_metadata,
                dataset_obj=dataset_obj,
                args=args,
                llm=llm,
                index_cache_base=index_cache_base,
                metrics_k=metrics_k,
            )

            if result is None:
                continue

            aggregate_metrics(agg, result["metrics"])
            eval_count += 1
            all_results.append(result)

        except Exception:
            logger.exception("Error processing %s", instance_id)
            all_results.append(
                {
                    "instance_id": instance_id,
                    "method": "skill_agent_embedding",
                    "model": args.model,
                    "error": True,
                    "loc_result": _loc_result_dict(
                        success=False,
                        repo_path="",
                        nodes=[],
                        execution_log=execution_log,
                        error_message="exception during processing",
                    ),
                }
            )

    # Compute and log aggregate metrics
    averaged: Dict[str, Any] = {}
    if agg and eval_count:
        averaged = average_metrics(agg, eval_count)
        _log_aggregate_metrics(averaged, eval_count)

    # Build final report
    report = {
        "dataset": args.dataset,
        "model": args.model,
        "skill_ids": ["embedding_search"],
        "instance_count": eval_count,
        "results": all_results,
        "aggregate_metrics": averaged,
    }

    # Save results
    if args.result_path:
        result_path = Path(args.result_path).expanduser().resolve()
        result_path.parent.mkdir(parents=True, exist_ok=True)
        with open(result_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        logger.info("Results saved to %s", result_path)


def main() -> None:
    args = parse_args()
    logger.info("Dataset  : %s (%s)", args.dataset, args.split)
    logger.info("Model    : %s", args.model)
    logger.info("Index dir: %s", args.index_cache_dir)
    logger.info("Max turns: %d", args.max_turns)
    run_eval(args)


if __name__ == "__main__":
    main()
