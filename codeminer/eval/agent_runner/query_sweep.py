# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reusable per-query sweep execution over prebuilt instance indexes.

This module owns the generic lifecycle for datasets with many localization
queries per repository: group rows by ``instance_id``, load the repo contexts
once, run every query across configured arms/reps, score each cell, and persist
resume-friendly JSON records. Dataset-specific scripts are responsible only for
loading and normalizing rows into the common fields consumed here.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .sweep_config import SweepConfig


def language_key_for_query_row(row: Mapping[str, Any]) -> str:
    """Return the agent language key for a normalized query row."""
    from codeminer.eval.agent_runner.sweep import LANG_GROUP_TO_KEY

    explicit = str(row.get("language_key") or "").strip()
    if explicit:
        return explicit
    language_group = row.get("language_group")
    if language_group:
        return LANG_GROUP_TO_KEY.get(str(language_group), "python")
    source_config = str(row.get("source_config") or "")
    return LANG_GROUP_TO_KEY.get(source_config.replace("_", "/"), "python")


def query_targets(
    row: Mapping[str, Any], *, simplified_symbols: bool
) -> Tuple[List[str], List[str]]:
    """Extract normalized target files and symbols from a query row."""
    from codeminer.eval.retrieval_eval import (
        normalize_file_path,
        normalize_symbol_identifier,
    )

    files = [normalize_file_path(path) for path in (row.get("gt_files") or [])]
    symbols_raw = list(row.get("gt_symbols") or [])
    if simplified_symbols:
        symbols_raw = [symbol for symbol in symbols_raw if "(" in symbol]
    symbols = [normalize_symbol_identifier(symbol) for symbol in symbols_raw]
    return [path for path in files if path], [symbol for symbol in symbols if symbol]


def filter_query_rows(
    rows: Iterable[Mapping[str, Any]], categories: Optional[Set[str]] = None
) -> List[Dict[str, Any]]:
    """Return normalized dict rows, optionally restricted by category."""
    selected = [dict(row) for row in rows]
    if categories:
        selected = [row for row in selected if row.get("category") in categories]
    return selected


def group_query_rows_by_instance(
    rows: Iterable[Mapping[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Group query rows by ``instance_id`` while preserving input order."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        instance_id = str(row.get("instance_id") or "")
        if not instance_id:
            continue
        grouped.setdefault(instance_id, []).append(dict(row))
    return grouped


def run_query_sweep(
    cfg: SweepConfig,
    output_dir: Path,
    rows: Sequence[Mapping[str, Any]],
    *,
    categories: Optional[Set[str]] = None,
    max_queries: Optional[int] = None,
    resume: bool = True,
    summary_filename: str = "query_sweep_summary.json",
) -> Dict[str, Any]:
    """Run a per-query sweep against already-normalized query rows."""
    from codeminer.eval.agent_runner.prebuilt import (
        has_full_indexes,
        repo_path_for,
        stage_prebuilt_indexes,
    )
    from codeminer.eval.agent_runner.scoring import evaluate_agent_localization
    from codeminer.eval.agent_runner.sweep import load_full_contexts, run_cell, slug
    from codeminer.eval.agent_runner.symbols import build_prebuilt_symbol_span_index
    from codeminer.eval.agent_runner.verify_expand import load_graph_nav
    from codeminer.eval.retrieval_eval import collect_target_blocks
    from codeminer.llm.litellm_chat import LiteLLMChat

    filtered_rows = filter_query_rows(rows, categories)
    needs_verify = any(
        (recipe or {}).get("verify") for recipe in (cfg.preload or {}).values()
    )
    cells_dir = output_dir / "cells"
    cells_dir.mkdir(parents=True, exist_ok=True)

    vertex_extra: Dict[str, Any] = {}
    if cfg.vertex_project:
        vertex_extra["vertex_project"] = cfg.vertex_project
    if cfg.vertex_location:
        vertex_extra["vertex_location"] = cfg.vertex_location
    vertex_extra["num_retries"] = cfg.num_retries
    llm = LiteLLMChat(
        model=cfg.model,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        extra_kwargs=vertex_extra,
    )
    summary: Dict[str, Any] = {"completed": [], "skipped": [], "failed": []}

    for instance_id, instance_rows in group_query_rows_by_instance(
        filtered_rows
    ).items():
        if max_queries:
            instance_rows = instance_rows[:max_queries]
        if not has_full_indexes(cfg.prebuilt_dir, instance_id, cfg.embedding_model):
            print(f"query-sweep: WARN {instance_id} missing prebuilt; skipping")
            summary["skipped"].append(
                {"instance_id": instance_id, "reason": "no-prebuilt"}
            )
            continue

        plan = []
        for row in instance_rows:
            for subset_id, skills in cfg.subsets.items():
                for rep in range(1, cfg.reps + 1):
                    cell_id = f"{row['query_id']}__{subset_id}__rep{rep}"
                    cell_path = cells_dir / f"{slug(cell_id)}.json"
                    if resume and cell_path.exists():
                        continue
                    plan.append((row, subset_id, list(skills), rep, cell_id, cell_path))
        if not plan:
            source = instance_rows[0].get("source_config") if instance_rows else ""
            print(f"query-sweep: {instance_id} ({source}) fully cached; skipping")
            continue

        repo_path = repo_path_for(cfg.prebuilt_dir, instance_id)
        cache_dir = repo_path
        source = instance_rows[0].get("source_config") if instance_rows else ""
        started = time.time()
        print(
            f"query-sweep: {instance_id} ({source}) staging + loading contexts "
            f"({len(plan)} cells over {len(instance_rows)} queries)"
        )
        try:
            stage_prebuilt_indexes(cfg.prebuilt_dir, instance_id, cache_dir)
            contexts = load_full_contexts(cfg, repo_path, cache_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"query-sweep: FAIL load {instance_id}: {exc}", file=sys.stderr)
            summary["failed"].append(
                {"instance_id": instance_id, "reason": f"load:{exc}"}
            )
            continue
        symbol_span_index = build_prebuilt_symbol_span_index(
            cfg.prebuilt_dir, instance_id
        )
        nav = load_graph_nav(cfg.prebuilt_dir, instance_id) if needs_verify else None
        print(f"query-sweep:   contexts ready in {time.time() - started:.1f}s")

        for row, subset_id, skills, rep, cell_id, cell_path in plan:
            cell_started = time.time()
            gt_blocks = collect_target_blocks(row)
            target_files, target_symbols = query_targets(
                row, simplified_symbols=cfg.gt_simplified_symbols
            )
            try:
                preload_spec = (cfg.preload or {}).get(subset_id)
                out = run_cell(
                    cfg,
                    contexts=contexts,
                    llm=llm,
                    repo_path=repo_path,
                    language_key=language_key_for_query_row(row),
                    query=row["query"],
                    subset_id=subset_id,
                    skills=skills,
                    preload_spec=preload_spec,
                    verify=bool((preload_spec or {}).get("verify")),
                    nav=nav,
                )
                evaluation = evaluate_agent_localization(
                    answer=out["answer"],
                    file_read_paths=out["file_read_paths"],
                    nodes=out["nodes"],
                    target_files=target_files,
                    target_symbols=target_symbols,
                    gt_blocks=gt_blocks,
                    metrics_k=cfg.metrics_k,
                    repo_path=repo_path,
                    symbol_span_index=symbol_span_index,
                    preload_candidates=out.get("preload_candidates") or [],
                    metrics_meaningful=bool(target_files or gt_blocks),
                )
                record = {
                    "cell_id": cell_id,
                    "instance_id": instance_id,
                    "query_id": row["query_id"],
                    "category": row.get("category"),
                    "length_variant": row.get("length_variant"),
                    "source_config": row.get("source_config"),
                    "subset_id": subset_id,
                    "skills": skills,
                    "model": cfg.model,
                    "rep": rep,
                    "language": language_key_for_query_row(row),
                    "success": True,
                    **evaluation.to_record_fields(),
                    "query": row["query"],
                    "preload_candidates": out.get("preload_candidates") or [],
                    "verify_triggered": out.get("verify_triggered"),
                    "verify_resolved": out.get("verify_resolved"),
                    "tool_calls": out["tool_calls"],
                    "file_read_paths": out["file_read_paths"],
                    "answer": out["answer"],
                    "total_turns": out["total_turns"],
                    "total_tokens": out["total_tokens"],
                    "cost_usd": out["cost_usd"],
                    "cache_read_input_tokens": out["cache_read_input_tokens"],
                    "elapsed_seconds": time.time() - cell_started,
                    "error": None,
                }
                summary["completed"].append(cell_id)
                recall_at_5 = (
                    evaluation.metrics.get("answer_blocks", {}).get(5, {}).get("recall")
                )
                print(
                    f"query-sweep:   done {cell_id} [{row.get('category')}] "
                    f"in {time.time() - cell_started:.1f}s turns={out['total_turns']} "
                    f"answer_recall_at_5={recall_at_5}"
                )
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                summary["failed"].append({"cell_id": cell_id, "reason": str(exc)})
                print(f"query-sweep:   FAIL {cell_id}: {exc}", file=sys.stderr)
                if any(
                    fragment in str(exc).lower()
                    for fragment in (
                        "ratelimit",
                        "rate limit",
                        "429",
                        "quota",
                        "resourceexhausted",
                    )
                ):
                    print(f"query-sweep:   (transient; not persisting {cell_id})")
                    continue
                record = {
                    "cell_id": cell_id,
                    "instance_id": instance_id,
                    "query_id": row["query_id"],
                    "category": row.get("category"),
                    "subset_id": subset_id,
                    "success": False,
                    "metrics": {},
                    "error": str(exc),
                    "elapsed_seconds": time.time() - cell_started,
                }
            with cell_path.open("w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2, ensure_ascii=False)

    with (output_dir / summary_filename).open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
    print(
        f"query-sweep done: completed={len(summary['completed'])} "
        f"skipped={len(summary['skipped'])} failed={len(summary['failed'])}"
    )
    return summary
