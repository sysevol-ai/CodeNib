#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Per-query sweep over the ``sysevol-ai/codeminer-synthesis`` dataset.

Unlike ``run_sweep.py`` (one query per SWE-bench instance), synthesis has many
queries (50-80) per repo, varying by ``category`` (behavioral / symbol_hint /
traversal / ...). That is exactly the setting where retrieval should pay off:

* category DISCRIMINATES grep vs retrieval — ``behavioral`` queries name no
  identifiers (grep must explore the whole repo), ``symbol_hint`` names the
  symbol (grep wins), ``traversal`` needs call-graph navigation.
* index REUSE — the repo index is built ONCE and amortized across all its
  queries (this loop loads contexts once per instance, then runs every query),
  whereas grep re-searches per query.

Scoring is the same span-overlap harness as ``run_sweep.py`` (answer + retrieval
scopes); each cell additionally records ``category`` and ``query_id`` so the
aggregator can break ``answer_rec@k`` down per category.

Usage::

    python scripts/agent_compile/run_synthesis_sweep.py \
        --config scripts/agent_compile/configs/preload_probe.yaml \
        --output-dir results/agent_compile/synthesis \
        --synthesis-configs Python,Go,Rust,TypeScript_JavaScript
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.agent_compile.lib.config import SweepConfig  # noqa: E402
from scripts.agent_compile.lib.harness import (  # noqa: E402
    build_symbol_span_index,
    load_full_contexts,
    run_cell,
    slug,
)
from scripts.agent_compile.lib.prebuilt import (  # noqa: E402
    has_full_indexes,
    repo_path_for,
    stage_prebuilt_indexes,
)

# language_group / config -> SessionContext primary_language key.
_LANG_KEY = {
    "Python": "python",
    "Go": "go",
    "Rust": "rust",
    "TypeScript_JavaScript": "typescript",
    "C++_C": "cpp",
}
_ALL_CONFIGS = list(_LANG_KEY)


def _load_normalized(config_name: str) -> List[Dict[str, Any]]:
    """Load + normalize one synthesis config into common CodeMiner rows."""
    from datasets import load_dataset

    from codeminer.dataset.codeminer_synthesis import normalize_synthesis_record

    ds = load_dataset("sysevol-ai/codeminer-synthesis", config_name, split="test")
    return [normalize_synthesis_record(r, config_name) for r in ds]


def _targets(row: Dict[str, Any], simplified: bool):
    """target_files / target_symbols from the synthesis gt_files / gt_symbols."""
    from codeminer.eval.retrieval_eval import (
        normalize_file_path,
        normalize_symbol_identifier,
    )

    files = [normalize_file_path(f) for f in (row.get("gt_files") or [])]
    syms_raw = row.get("gt_symbols") or []
    if simplified:  # function-like only (mirrors collect_targets)
        syms_raw = [s for s in syms_raw if "(" in s]
    syms = [normalize_symbol_identifier(s) for s in syms_raw]
    return [f for f in files if f], [s for s in syms if s]


def run(
    cfg: SweepConfig,
    output_dir: Path,
    configs: Sequence[str],
    *,
    categories: Optional[set],
    max_queries: Optional[int],
    resume: bool,
) -> Dict:
    from codeminer.eval.retrieval_eval import (
        collect_target_blocks,
        dedup_spans,
        nodes_to_spans,
        parse_answer_spans,
        resolve_symbol_spans,
        score_agent_localization,
        score_localization_spans,
        spans_overlap,
    )
    from codeminer.llm.litellm_chat import LiteLLMChat
    from scripts.agent_compile.lib.verify_expand import load_graph_nav

    needs_verify = any((r or {}).get("verify") for r in (cfg.preload or {}).values())
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

    for config_name in configs:
        rows = _load_normalized(config_name)
        if categories:
            rows = [r for r in rows if r.get("category") in categories]
        # Group by instance_id so the repo index loads ONCE per instance.
        by_instance: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            by_instance.setdefault(r["instance_id"], []).append(r)

        for instance_id, inst_rows in by_instance.items():
            if max_queries:
                inst_rows = inst_rows[:max_queries]
            if not has_full_indexes(cfg.prebuilt_dir, instance_id, cfg.embedding_model):
                print(f"synth: WARN {instance_id} missing prebuilt; skipping")
                summary["skipped"].append(
                    {"instance_id": instance_id, "reason": "no-prebuilt"}
                )
                continue
            # Decide which cells still need running before paying the load cost.
            plan = []
            for row in inst_rows:
                for subset_id, skills in cfg.subsets.items():
                    for rep in range(1, cfg.reps + 1):
                        cid = f"{row['query_id']}__{subset_id}__rep{rep}"
                        cpath = cells_dir / f"{slug(cid)}.json"
                        if resume and cpath.exists():
                            continue
                        plan.append((row, subset_id, list(skills), rep, cid, cpath))
            if not plan:
                print(f"synth: {instance_id} ({config_name}) fully cached; skipping")
                continue

            repo_path = repo_path_for(cfg.prebuilt_dir, instance_id)
            cache_dir = repo_path  # prebuilt staged in place
            t0 = time.time()
            print(
                f"synth: {instance_id} ({config_name}) staging + loading contexts "
                f"({len(plan)} cells over {len(inst_rows)} queries)"
            )
            try:
                stage_prebuilt_indexes(cfg.prebuilt_dir, instance_id, cache_dir)
                contexts = load_full_contexts(cfg, repo_path, cache_dir)
            except Exception as exc:  # noqa: BLE001
                print(f"synth: FAIL load {instance_id}: {exc}", file=sys.stderr)
                summary["failed"].append(
                    {"instance_id": instance_id, "reason": f"load:{exc}"}
                )
                continue
            symbol_span_index = build_symbol_span_index(cfg.prebuilt_dir, instance_id)
            nav = (
                load_graph_nav(cfg.prebuilt_dir, instance_id) if needs_verify else None
            )
            print(f"synth:   contexts ready in {time.time() - t0:.1f}s")

            for row, subset_id, skills, rep, cid, cpath in plan:
                t = time.time()
                gt_blocks = collect_target_blocks(row)
                target_files, target_symbols = _targets(row, cfg.gt_simplified_symbols)
                try:
                    out = run_cell(
                        cfg,
                        contexts=contexts,
                        llm=llm,
                        repo_path=repo_path,
                        language_key=_LANG_KEY.get(config_name, "python"),
                        query=row["query"],
                        subset_id=subset_id,
                        skills=skills,
                        preload_spec=(cfg.preload or {}).get(subset_id),
                        verify=bool(
                            ((cfg.preload or {}).get(subset_id) or {}).get("verify")
                        ),
                        nav=nav,
                    )
                    metrics = score_agent_localization(
                        answer=out["answer"],
                        file_read_paths=out["file_read_paths"],
                        nodes=out["nodes"],
                        target_files=target_files,
                        target_symbols=target_symbols,
                        ks=cfg.metrics_k,
                        repo_path=repo_path,
                    )
                    answer_spans = parse_answer_spans(
                        out["answer"], repo_path
                    ) + resolve_symbol_spans(
                        out["answer"], symbol_span_index, repo_path
                    )
                    retrieval_spans = nodes_to_spans(out["nodes"])
                    metrics.update(
                        score_localization_spans(
                            answer_spans=answer_spans,
                            retrieval_spans=retrieval_spans,
                            gt_blocks=gt_blocks,
                            ks=cfg.metrics_k,
                        )
                    )
                    preload_candidates = out.get("preload_candidates") or []
                    ans_dedup = dedup_spans(answer_spans)
                    preload_contribution = (
                        sum(
                            1
                            for a in ans_dedup
                            if any(spans_overlap(a, c) for c in preload_candidates)
                        )
                        / len(ans_dedup)
                        if (ans_dedup and preload_candidates)
                        else None
                    )
                    from codeminer.agent.runner import _has_localization_contract

                    format_failed = (
                        not _has_localization_contract(out["answer"] or "")
                        and not answer_spans
                    )
                    record = {
                        "cell_id": cid,
                        "instance_id": instance_id,
                        "query_id": row["query_id"],
                        "category": row.get("category"),
                        "length_variant": row.get("length_variant"),
                        "source_config": config_name,
                        "subset_id": subset_id,
                        "skills": skills,
                        "model": cfg.model,
                        "rep": rep,
                        "language": _LANG_KEY.get(config_name, "python"),
                        "success": True,
                        "format_failed": format_failed,
                        "metrics": metrics,
                        "metrics_meaningful": bool(target_files or gt_blocks),
                        "query": row["query"],
                        "target_files": target_files,
                        "target_symbols": target_symbols,
                        "gt_code_blocks": gt_blocks,
                        "answer_spans": answer_spans,
                        "retrieval_spans": retrieval_spans,
                        "preload_candidates": preload_candidates,
                        "preload_contribution": preload_contribution,
                        "verify_triggered": out.get("verify_triggered"),
                        "verify_resolved": out.get("verify_resolved"),
                        "tool_calls": out["tool_calls"],
                        "file_read_paths": out["file_read_paths"],
                        "answer": out["answer"],
                        "total_turns": out["total_turns"],
                        "total_tokens": out["total_tokens"],
                        "cost_usd": out["cost_usd"],
                        "cache_read_input_tokens": out["cache_read_input_tokens"],
                        "elapsed_seconds": time.time() - t,
                        "error": None,
                    }
                    summary["completed"].append(cid)
                    ar = metrics.get("answer_blocks", {}).get(5, {}).get("recall")
                    print(
                        f"synth:   done {cid} [{row.get('category')}] "
                        f"in {time.time() - t:.1f}s turns={out['total_turns']} "
                        f"answer_rec@5={ar}"
                    )
                except Exception as exc:  # noqa: BLE001
                    traceback.print_exc()
                    summary["failed"].append({"cell_id": cid, "reason": str(exc)})
                    print(f"synth:   FAIL {cid}: {exc}", file=sys.stderr)
                    if any(
                        s in str(exc).lower()
                        for s in (
                            "ratelimit",
                            "rate limit",
                            "429",
                            "quota",
                            "resourceexhausted",
                        )
                    ):
                        print(f"synth:   (transient; not persisting {cid})")
                        continue
                    record = {
                        "cell_id": cid,
                        "instance_id": instance_id,
                        "query_id": row["query_id"],
                        "category": row.get("category"),
                        "subset_id": subset_id,
                        "success": False,
                        "metrics": {},
                        "error": str(exc),
                        "elapsed_seconds": time.time() - t,
                    }
                with cpath.open("w", encoding="utf-8") as f:
                    json.dump(record, f, indent=2, ensure_ascii=False)

    with (output_dir / "synthesis_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(
        f"synth done: completed={len(summary['completed'])} "
        f"skipped={len(summary['skipped'])} failed={len(summary['failed'])}"
    )
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument(
        "--synthesis-configs",
        default=",".join(_ALL_CONFIGS),
        help="comma list of HF configs (Python,Go,Rust,TypeScript_JavaScript,C++_C)",
    )
    p.add_argument(
        "--categories",
        default=None,
        help="comma list to filter (e.g. behavioral,symbol_hint)",
    )
    p.add_argument(
        "--max-queries", type=int, default=None, help="cap queries per instance (smoke)"
    )
    p.add_argument("--reps", type=int, default=None)
    p.add_argument("--no-resume", action="store_true")
    p.add_argument(
        "--model",
        default=None,
        help="Override config model (e.g. a local openai/ vLLM model).",
    )
    args = p.parse_args(argv)

    cfg = SweepConfig.from_yaml(args.config)
    if args.reps is not None:
        cfg.reps = args.reps
    if args.model:
        cfg.model = args.model
    configs = [c.strip() for c in args.synthesis_configs.split(",") if c.strip()]
    categories = (
        {c.strip() for c in args.categories.split(",")} if args.categories else None
    )
    run(
        cfg,
        args.output_dir,
        configs,
        categories=categories,
        max_queries=args.max_queries,
        resume=not args.no_resume,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
