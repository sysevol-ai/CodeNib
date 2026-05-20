# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared loop + I/O for agentic localization baseline runners (#150).

Used by ``examples/claude_loc_agent.py`` and ``examples/codex_loc_agent.py``.
Holds:

- ``BUILTIN_DATASET_CHOICES`` and ``build_dataset(args)`` — same shape as
  the existing retrieval-baseline scripts (e.g. embedding_retrieve_baseline.py)
- ``already_done_ids(path)`` — skim a JSONL result file for completed IDs
- ``build_result_entry(...)`` — per-instance record with ``metric_k_files`` /
  ``metric_k_node_ids`` and per-scope/per-k ``metrics``, so it joins cleanly
  against the internal harness (see retrieve_rerank.py)
- ``run_agent_baseline(args, agent_factory, agent_name)`` — async driver
  that iterates the dataset, calls the agent, scores against GT, and
  appends JSONL incrementally; logs aggregate file/symbol@k at the end
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

from codeminer.dataset.codeminer_base import CodeMinerBaseDataset
from codeminer.dataset.locbench import LocbenchDataset
from codeminer.dataset.swebench import SwebenchDataset
from codeminer.eval.retrieval_eval import (
    aggregate_metrics,
    average_metrics,
    collect_targets,
    compute_metrics,
    normalize_file_path,
)
from codeminer.log_utils import get_logger

logger = get_logger(__name__)


BUILTIN_DATASET_CHOICES = ["codeminer_base", "swebench_lite", "locbench_v1"]
DEFAULT_METRICS_K = [1, 3, 5, 10]


def build_dataset(args):
    """Mirror of embedding_retrieve_baseline._build_dataset()."""
    if args.dataset == "codeminer_base":
        return CodeMinerBaseDataset(
            dataset="fishmingyu/codeminer-base-dataset",
            split=args.split,
            filter_instance=args.filter_instance,
            repo_root=os.path.expanduser(args.repo_cache_dir),
        )
    if args.dataset == "swebench_lite":
        return SwebenchDataset(
            dataset="princeton-nlp/SWE-bench_Lite",
            split=args.split,
            filter_instance=args.filter_instance,
            repo_root=os.path.expanduser(args.repo_cache_dir),
        )
    if args.dataset == "locbench_v1":
        return LocbenchDataset(
            dataset="czlll/Loc-Bench_V1",
            split=args.split,
            filter_instance=args.filter_instance,
            repo_root=os.path.expanduser(args.repo_cache_dir),
        )
    raise ValueError(f"Unsupported dataset: {args.dataset}")


def already_done_ids(result_path: Path) -> Set[str]:
    """Return instance_ids already present in an existing JSONL result file."""
    if not result_path.exists():
        return set()
    done: Set[str] = set()
    with result_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            iid = obj.get("instance_id")
            if iid:
                done.add(iid)
    return done


def _unique_files(locations) -> List[str]:
    seen = set()
    out: List[str] = []
    for sym in locations:
        if sym.file_path and sym.file_path not in seen:
            out.append(sym.file_path)
            seen.add(sym.file_path)
    return out


def _agent_symbol_id(file_path: str, name: str) -> Optional[str]:
    """Compose an agent ``(file_path, name)`` into the GT ``file:symbol`` form.

    GT (from ``codeminer/code_chunking/``) is exactly one of:
        ``<file>:bare_function()``
        ``<file>:Class.method()``
        ``<file>:ClassName``

    The agents' ``name`` field is constrained to this same canonical shape by
    the JSON-Schema ``pattern`` on ``LOC_OUTPUT_SCHEMA`` (at most one ``.``,
    no ``::``, no signatures) plus the system-prompt format rules. So scoring
    is just exact string match against GT — no candidate generation, no FP
    risk from over-permissive normalization.
    """
    if not name or not file_path:
        return None
    file_norm = normalize_file_path(file_path)
    if not file_norm:
        return None
    return f"{file_norm}:{name}"


def _agent_symbol_ids(locations) -> List[str]:
    """Build the predicted symbol id list in agent-output order."""
    out: List[str] = []
    for sym in locations:
        sid = _agent_symbol_id(sym.file_path, sym.name)
        if sid is not None:
            out.append(sid)
    return out


def _score_predictions(
    predicted_files: Sequence[str],
    predicted_symbols: Sequence[str],
    target_files: Sequence[str],
    target_symbols: Sequence[str],
    ks: Sequence[int],
) -> Dict[str, Dict[int, Dict[str, float]]]:
    """``{files, symbols}`` × k metric dict via retrieval_eval.compute_metrics."""
    metrics: Dict[str, Dict[int, Dict[str, float]]] = {"files": {}, "symbols": {}}
    for k in ks:
        metrics["files"][k] = compute_metrics(list(predicted_files[:k]), target_files)
        metrics["symbols"][k] = compute_metrics(
            list(predicted_symbols[:k]), target_symbols
        )
    return metrics


def build_result_entry(
    *,
    instance_id: str,
    agent_name: str,
    model: Optional[str],
    result,
    target_files: Sequence[str],
    target_symbols: Sequence[str],
    metrics_k: Sequence[int],
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """One JSONL record: predictions + GT + per-scope/per-k metrics."""
    base = {
        "instance_id": instance_id,
        "agent": agent_name,
        "model": model,
        "target_files": list(target_files),
        "target_symbols": list(target_symbols),
    }
    if error or result is None or not result.success:
        base.update(
            {
                "success": False,
                "error": error or (result.error_message if result else "no result"),
                "metric_k_files": [],
                "metric_k_node_ids": [],
                "locations": [],
                "usage": None,
                "metrics": None,
            }
        )
        return base

    locations_payload = [
        {
            "name": sym.name,
            "type": sym.type,
            "file_path": sym.file_path,
            "line_start": sym.line_start,
            "line_end": sym.line_end,
            "action": sym.action,
            "description": sym.description,
        }
        for sym in result.locations
    ]
    predicted_files = _unique_files(result.locations)
    predicted_symbols = _agent_symbol_ids(result.locations)
    metrics = _score_predictions(
        predicted_files,
        predicted_symbols,
        target_files,
        target_symbols,
        metrics_k,
    )

    base.update(
        {
            "success": True,
            "metric_k_files": predicted_files,
            "metric_k_node_ids": predicted_symbols,
            "locations": locations_payload,
            "usage": result.usage,
            "metrics": metrics,
            "error": None,
        }
    )
    return base


def _empty_aggregate(ks: Sequence[int]) -> Dict[str, Dict[int, Dict[str, float]]]:
    """Pre-seed the running aggregate so `aggregate_metrics` accumulates in place."""
    return {
        scope: {
            k: {"accuracy": 0.0, "precision": 0.0, "recall": 0.0, "hits": 0.0}
            for k in ks
        }
        for scope in ("files", "symbols")
    }


def _log_aggregate_table(
    averaged: Mapping[str, Mapping[int, Mapping[str, float]]],
    eval_count: int,
) -> None:
    """Pretty-print the aggregated files@k / symbols@k table to the logger."""
    if eval_count == 0:
        logger.info("No instances were scored (eval_count=0).")
        return
    logger.info("Aggregate metrics over %d scored instance(s):", eval_count)
    for scope in ("files", "symbols"):
        per_k = averaged.get(scope, {})
        if not per_k:
            continue
        for k in sorted(per_k):
            stats = per_k[k]
            logger.info(
                "  [%s] k=%-2d  accuracy=%.3f  precision=%.3f  recall=%.3f  "
                "avg_hits=%.2f",
                scope,
                k,
                stats["accuracy"],
                stats["precision"],
                stats["recall"],
                stats["avg_hits"],
            )


async def run_agent_baseline(
    args,
    *,
    agent_factory: Callable[[], Any],
    agent_name: str,
    model_label: Optional[str] = None,
) -> int:
    """Iterate dataset, call agent.locate_code on each instance, append JSONL.

    Per-instance: score against GT (``target_files`` + ``target_symbols``)
    via ``codeminer.eval.retrieval_eval.compute_metrics`` for the configured
    ``--metrics-k`` cutoffs and embed the result in the JSONL row. At the
    end, aggregate + log the dataset-level table.
    """
    dataset_obj = build_dataset(args)
    instances = dataset_obj.load()
    if len(instances) == 0:
        raise ValueError(
            f"No instances matched filter {args.filter_instance!r} "
            f"on dataset {args.dataset!r}"
        )
    if getattr(args, "limit", None):
        instances = instances.select(range(min(args.limit, len(instances))))
    logger.info("Loaded %d instance(s) from %s", len(instances), args.dataset)

    eval_metadata = dataset_obj.load_eval_metadata(args.eval_instances or "")
    simplified_symbols = getattr(dataset_obj, "simplified_symbols", True)
    metrics_k = list(args.metrics_k)

    result_path = Path(args.result_path).expanduser().resolve()
    result_path.parent.mkdir(parents=True, exist_ok=True)

    done = already_done_ids(result_path) if args.resume else set()
    if done:
        logger.info(
            "Resuming — %d instance(s) already in %s, will skip",
            len(done),
            result_path,
        )

    agent = agent_factory()

    aggregate = _empty_aggregate(metrics_k)
    scored_count = 0
    n_success = 0
    n_failed = 0
    n_skipped = 0

    with result_path.open("a", encoding="utf-8") as out:
        for i, inst in enumerate(instances):
            instance_id = inst["instance_id"]
            if instance_id in done:
                n_skipped += 1
                logger.info(
                    "[%d/%d] %s — skipped (resume)",
                    i + 1,
                    len(instances),
                    instance_id,
                )
                continue

            metadata = eval_metadata.get(instance_id)
            if metadata is None:
                target_files: List[str] = []
                target_symbols: List[str] = []
            else:
                target_files, target_symbols = collect_targets(
                    metadata, simplified_symbols=simplified_symbols
                )

            logger.info("[%d/%d] %s", i + 1, len(instances), instance_id)
            try:
                dataset_obj.process_instance(inst)
                repo = dataset_obj.get_repo_path(inst)
                context = {
                    "issue_title": f"[{instance_id}] {inst['repo']}",
                    "issue_body": inst["problem_statement"],
                }
                if inst.get("hints_text"):
                    context["hints"] = inst["hints_text"]
                result = await agent.locate_code(
                    inst["problem_statement"], repo, context=context
                )
                entry = build_result_entry(
                    instance_id=instance_id,
                    agent_name=agent_name,
                    model=model_label,
                    result=result,
                    target_files=target_files,
                    target_symbols=target_symbols,
                    metrics_k=metrics_k,
                )
                if entry["success"]:
                    n_success += 1
                else:
                    n_failed += 1
            except Exception as e:  # noqa: BLE001 — record + continue
                logger.exception("[%s] failed", instance_id)
                entry = build_result_entry(
                    instance_id=instance_id,
                    agent_name=agent_name,
                    model=model_label,
                    result=None,
                    target_files=target_files,
                    target_symbols=target_symbols,
                    metrics_k=metrics_k,
                    error=str(e),
                )
                n_failed += 1

            if entry["success"] and entry["metrics"] is not None:
                aggregate_metrics(aggregate, entry["metrics"])
                scored_count += 1
                fk = entry["metrics"]["files"][metrics_k[0]]
                sk = entry["metrics"]["symbols"][metrics_k[0]]
                logger.info(
                    "  files@%d acc=%.2f rec=%.2f | symbols@%d acc=%.2f rec=%.2f",
                    metrics_k[0],
                    fk["accuracy"],
                    fk["recall"],
                    metrics_k[0],
                    sk["accuracy"],
                    sk["recall"],
                )

            out.write(json.dumps(entry, ensure_ascii=False) + "\n")
            out.flush()

    averaged = average_metrics(aggregate, scored_count)
    _log_aggregate_table(averaged, scored_count)

    logger.info(
        "Done. success=%d failed=%d skipped=%d scored=%d total=%d result=%s",
        n_success,
        n_failed,
        n_skipped,
        scored_count,
        len(instances),
        result_path,
    )
    return 0 if n_failed == 0 else 1


def add_common_args(parser) -> None:
    """Attach the shared dataset / output / resume / scoring flags."""
    parser.add_argument(
        "--dataset",
        type=str,
        required=True,
        choices=BUILTIN_DATASET_CHOICES,
    )
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument("--filter-instance", type=str, default=".*")
    parser.add_argument(
        "--repo-cache-dir",
        type=str,
        default="~/.codeminer/",
    )
    parser.add_argument(
        "--result-path",
        type=str,
        required=True,
        help="Path to JSONL result file (one instance per line, appended).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip instance_ids already present in --result-path.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap number of instances to process (post-filter).",
    )
    parser.add_argument(
        "--eval-instances",
        type=str,
        default="",
        help=(
            "Path to JSON file with eval annotations (target_files, symbols_*). "
            "Leave empty for codeminer_base — GT is projected from dataset columns."
        ),
    )
    parser.add_argument(
        "--metrics-k",
        type=int,
        nargs="+",
        default=DEFAULT_METRICS_K,
        help="Cutoffs for files@k / symbols@k reporting.",
    )
