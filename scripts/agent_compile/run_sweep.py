#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent-compile cost-arm sweep on ``codeminer_base`` with prebuilt indexes.

Sweeps ``{subsets} × {instances} × {reps}`` for one model, reusing the
offline-built per-instance indexes under ``--prebuilt-dir`` (see
``lib/prebuilt.py``) instead of cloning + reindexing.

Per instance the *full* (union-of-subsets) index set is loaded once and all
skills are registered; each subset cell then runs ``AgentRunner`` with
``allow_skills=<subset>`` so the only thing that varies across subsets is the
tool allowlist. The vector store is therefore loaded once per instance, not
once per cell.

Each cell is written as ``<output-dir>/cells/<cell_id>.json`` in the schema
``aggregate.py`` consumes, plus ``scenario`` / ``tool_calls`` fields for the
invocation histogram and per-scenario reporting.

Usage::

    python scripts/agent_compile/run_sweep.py \\
        --config scripts/agent_compile/configs/harness_grep.yaml \\
        --output-dir results/agent_compile/grep
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.agent_compile.lib.config import SweepConfig  # noqa: E402
from scripts.agent_compile.lib.harness import (  # noqa: E402
    LANG_GROUP_TO_KEY,
    load_dataset_rows,
    load_full_contexts,
    run_cell,
    scenario_for,
    slug,
)
from scripts.agent_compile.lib.prebuilt import (  # noqa: E402
    has_full_indexes,
    repo_path_for,
    stage_prebuilt_indexes,
)


def _validate_harness(cfg: SweepConfig) -> None:
    """Fail loudly on tool/skill ids that would be silently dropped.

    ``default_tool_ids`` is intersected with the registered default tools, and
    ``allow_skills`` with the registry — an unknown name (e.g. the legacy
    ``file_read`` instead of ``read``) just vanishes, which once zeroed out a
    whole arm's filesystem access. Catch it before paying for a sweep.
    """
    from codeminer.agent.tools.defaults import DEFAULT_TOOL_IDS

    defaults = set(DEFAULT_TOOL_IDS)
    skills_dir = _PROJECT_ROOT / "codeminer" / "agent" / "skills"
    index_skills = {
        p.name for p in skills_dir.iterdir() if (p / "config.yaml").exists()
    }
    known = defaults | index_skills

    if cfg.default_tool_ids:
        bad = sorted(set(cfg.default_tool_ids) - defaults)
        if bad:
            raise ValueError(
                f"{cfg.sweep_id}: default_tool_ids {bad} are not registered default "
                f"tools {sorted(defaults)}; they would be silently dropped, leaving "
                f"the arm with no default tools. Use the real ids (read/grep/glob/bash)."
            )
    for arm, skills in cfg.subsets.items():
        bad = [s for s in skills if s not in known]
        if bad:
            raise ValueError(
                f"{cfg.sweep_id}/{arm}: unknown skills {bad} — not a default tool "
                f"{sorted(defaults)} nor a package under codeminer/agent/skills/."
            )


def run_sweep(cfg: SweepConfig, output_dir: Path, *, resume: bool = True) -> Dict:
    from codeminer.eval.retrieval_eval import collect_targets, score_agent_localization
    from codeminer.llm.litellm_chat import LiteLLMChat

    _validate_harness(cfg)
    cells_dir = output_dir / "cells"
    cells_dir.mkdir(parents=True, exist_ok=True)

    vertex_extra: Dict[str, Any] = {}
    if cfg.vertex_project:
        vertex_extra["vertex_project"] = cfg.vertex_project
    if cfg.vertex_location:
        vertex_extra["vertex_location"] = cfg.vertex_location
    # The default-tool agent makes many LLM calls (grep/read turns), so vertex
    # rate limits are common; let litellm retry with exponential backoff.
    vertex_extra["num_retries"] = cfg.num_retries
    llm = LiteLLMChat(
        model=cfg.model,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        extra_kwargs=vertex_extra,
    )

    rows_by_id, eval_lookup = load_dataset_rows(cfg)
    # Empty cfg.instances => the whole split (sorted for deterministic order).
    instance_ids = cfg.instances or sorted(rows_by_id)
    summary: Dict[str, Any] = {
        "sweep_id": cfg.sweep_id,
        "model": cfg.model,
        "instance_count": len(instance_ids),
        "completed": [],
        "skipped": [],
        "failed": [],
    }

    for instance_id in instance_ids:
        row = rows_by_id.get(instance_id)
        if row is None:
            print(f"sweep: WARN {instance_id} not in dataset; skipping")
            summary["failed"].append(
                {"instance_id": instance_id, "reason": "not-in-dataset"}
            )
            continue
        if not has_full_indexes(cfg.prebuilt_dir, instance_id, cfg.embedding_model):
            print(f"sweep: WARN {instance_id} missing prebuilt indexes; skipping")
            summary["failed"].append(
                {"instance_id": instance_id, "reason": "no-prebuilt"}
            )
            continue

        language_key = LANG_GROUP_TO_KEY.get(row.get("language_group"), "python")
        query = row["problem_statement"]
        scenario = scenario_for(query, language_key)
        repo_path = repo_path_for(cfg.prebuilt_dir, instance_id)

        meta = eval_lookup.get(instance_id, row)
        target_files, target_symbols = collect_targets(
            meta, simplified_symbols=cfg.gt_simplified_symbols
        )
        gt_meaningful = bool(target_files or target_symbols)

        cache_dir = os.path.join(
            os.environ.get("CLAUDE_JOB_DIR", str(output_dir)),
            "cache",
            instance_id,
        )

        # Decide which cells still need running before paying the load cost.
        pending = []
        for subset_id, skills in cfg.subsets.items():
            for rep in range(1, cfg.reps + 1):
                cell_id = f"{slug(instance_id)}__{subset_id}__rep{rep}"
                cell_path = cells_dir / f"{cell_id}.json"
                if resume and cell_path.exists():
                    summary["skipped"].append(cell_id)
                    continue
                pending.append((subset_id, list(skills), rep, cell_id, cell_path))
        if not pending:
            print(f"sweep: {instance_id} fully cached; skipping load")
            continue

        print(
            f"sweep: {instance_id} [{scenario}] staging + loading full contexts "
            f"({len(pending)} cells pending)"
        )
        t0 = time.time()
        try:
            stage_prebuilt_indexes(cfg.prebuilt_dir, instance_id, cache_dir)
            contexts = load_full_contexts(cfg, repo_path, cache_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"sweep: FAIL load {instance_id}: {exc}", file=sys.stderr)
            traceback.print_exc()
            summary["failed"].append(
                {"instance_id": instance_id, "reason": f"load:{exc}"}
            )
            continue
        print(
            f"sweep:   contexts ready in {time.time() - t0:.1f}s, keys={sorted(contexts)}"
        )

        for subset_id, skills, rep, cell_id, cell_path in pending:
            t = time.time()
            try:
                out = run_cell(
                    cfg,
                    contexts=contexts,
                    llm=llm,
                    repo_path=repo_path,
                    language_key=language_key,
                    query=query,
                    subset_id=subset_id,
                    skills=skills,
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
                record = {
                    "cell_id": cell_id,
                    "instance_id": instance_id,
                    "subset_id": subset_id,
                    "skills": skills,
                    "model": cfg.model,
                    "rep": rep,
                    "scenario": scenario,
                    "language": language_key,
                    "success": True,
                    "metrics": metrics,
                    "metrics_meaningful": gt_meaningful,
                    "target_files": target_files,
                    "target_symbols": target_symbols,
                    "tool_calls": out["tool_calls"],
                    "file_read_paths": out["file_read_paths"],
                    "answer": out["answer"],
                    "total_turns": out["total_turns"],
                    "total_duration_ms": out["total_duration_ms"],
                    "tool_call_count": out["tool_call_count"],
                    "prompt_tokens": out["prompt_tokens"],
                    "completion_tokens": out["completion_tokens"],
                    "total_tokens": out["total_tokens"],
                    "cost_usd": out["cost_usd"],
                    "cache_read_input_tokens": out["cache_read_input_tokens"],
                    "cache_creation_input_tokens": out["cache_creation_input_tokens"],
                    "elapsed_seconds": time.time() - t,
                    "error": None,
                }
                summary["completed"].append(cell_id)
                print(
                    f"sweep:   done {cell_id} in {time.time() - t:.1f}s "
                    f"turns={out['total_turns']} tokens={out['total_tokens']} "
                    f"files@5={metrics.get('files', {}).get(5, {}).get('accuracy')}"
                )
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                record = {
                    "cell_id": cell_id,
                    "instance_id": instance_id,
                    "subset_id": subset_id,
                    "skills": skills,
                    "model": cfg.model,
                    "rep": rep,
                    "scenario": scenario,
                    "language": language_key,
                    "success": False,
                    "metrics": {},
                    "metrics_meaningful": gt_meaningful,
                    "tool_calls": [],
                    "error": str(exc),
                    "elapsed_seconds": time.time() - t,
                }
                summary["failed"].append({"cell_id": cell_id, "reason": str(exc)})
                print(f"sweep:   FAIL {cell_id}: {exc}", file=sys.stderr)
                # Transient (rate-limit/quota) failures must not poison resume:
                # skip persisting so a later run retries this cell.
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
                    print(f"sweep:   (transient; not persisting {cell_id})")
                    continue

            with cell_path.open("w", encoding="utf-8") as f:
                json.dump(record, f, indent=2, ensure_ascii=False)

    summary_path = output_dir / "sweep_summary.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Agent-compile cost-arm sweep on codeminer_base (prebuilt indexes).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--reps", type=int, default=None, help="Override config reps.")
    parser.add_argument(
        "--instances", nargs="+", default=None, help="Override config instance list."
    )
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--model", default=None, help="Override config model.")
    parser.add_argument(
        "--vertex-location", default=None, help="Override config vertex_location."
    )
    args = parser.parse_args(argv)

    cfg = SweepConfig.from_yaml(args.config)
    if args.reps is not None:
        cfg.reps = args.reps
    if args.instances:
        cfg.instances = args.instances
    if args.model:
        cfg.model = args.model
    if args.vertex_location:
        cfg.vertex_location = args.vertex_location

    summary = run_sweep(cfg, args.output_dir, resume=not args.no_resume)
    print(
        "sweep done: completed={c} skipped={s} failed={f} cells={d}".format(
            c=len(summary["completed"]),
            s=len(summary["skipped"]),
            f=len(summary["failed"]),
            d=args.output_dir / "cells",
        )
    )
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
