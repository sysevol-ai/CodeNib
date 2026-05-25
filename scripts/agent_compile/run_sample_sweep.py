#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent-compile sample sweep on ``codeminer_base`` with prebuilt indexes.

This is the Phase-2 (#133) subset sweep specialised for a small sample run:
it sweeps ``{subsets} × {instances} × {reps}`` for one model, reusing the
offline-built per-instance indexes under ``--prebuilt-dir`` (see
``prebuilt.py``) instead of cloning + reindexing.

Per instance the *full* (A6) index set is loaded once and all skills are
registered; each subset cell then runs ``AgentRunner`` with
``allow_skills=<subset>`` so the only thing that varies across subsets is the
tool allowlist — exactly the runtime contract (full registry available, CAR
narrows). The Qwen3 vector store is therefore loaded once per instance, not
once per cell.

Each cell is written as ``<output-dir>/cells/<cell_id>.json`` in the schema
that ``aggregate.py`` (Phase 0) and ``aggregate_phase2.py`` consume, plus
``scenario`` / ``tool_calls`` fields for the invocation histogram and
per-scenario ``compile_table`` derivation.

Usage::

    python scripts/agent_compile/run_sample_sweep.py \\
        --config configs/agent_compile/sample.yaml \\
        --output-dir results/agent_compile/sample
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.agent_compile.prebuilt import (  # noqa: E402
    has_full_indexes,
    repo_path_for,
    stage_prebuilt_indexes,
)

# language_group (HF column) -> classify() language key
_LANG_GROUP_TO_KEY = {
    "Python": "python",
    "Go": "go",
    "Rust": "rust",
    "C++/C": "cpp",
    "TypeScript/JavaScript": "typescript",
}


@dataclass
class SampleConfig:
    sweep_id: str
    subsets: Dict[str, List[str]]
    instances: List[str]
    model: str = "vertex_ai/claude-haiku-4-5"
    vertex_location: Optional[str] = "us-east5"
    vertex_project: Optional[str] = None
    reps: int = 2
    max_turns: int = 20
    temperature: float = 0.0
    max_tokens: int = 4096
    topk: int = 50
    dataset: str = "fishmingyu/codeminer-base-dataset"
    split: str = "test"
    prebuilt_dir: str = "/mnt/data/codeminer"
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_dimension: int = 1024
    metrics_k: List[int] = field(default_factory=lambda: [1, 3, 5, 10])
    gt_simplified_symbols: bool = True

    @classmethod
    def from_yaml(cls, path: Path) -> "SampleConfig":
        with path.open("r", encoding="utf-8") as f:
            payload = yaml.safe_load(f) or {}
        for req in ("sweep_id", "subsets", "instances"):
            if req not in payload:
                raise ValueError(f"{path}: {req!r} is required")
        if not isinstance(payload["subsets"], dict):
            raise ValueError(f"{path}: 'subsets' must be a mapping")
        return cls(**payload)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")


# ---------------------------------------------------------------------------
# Dataset + scenario
# ---------------------------------------------------------------------------


def _load_dataset_rows(cfg: SampleConfig):
    """Return (rows_by_id, eval_lookup) for the configured instances."""
    from codeminer.dataset.codeminer_base import CodeMinerBaseDataset

    ds = CodeMinerBaseDataset(dataset=cfg.dataset, split=cfg.split)
    data = ds.load()
    wanted = set(cfg.instances)
    rows_by_id = {r["instance_id"]: r for r in data if r["instance_id"] in wanted}
    eval_lookup = ds.load_eval_metadata()
    return rows_by_id, eval_lookup


def _scenario_for(query: str, language_key: str) -> str:
    from codeminer.agent.compile import classify
    from codeminer.compiler.params import SessionContext

    sctx = SessionContext(repo_path=".", repo_size=1, primary_language=language_key)
    return classify(query, sctx).key()


# ---------------------------------------------------------------------------
# Per-instance index loading (full set, once) + per-cell agent run
# ---------------------------------------------------------------------------


def _all_index_skill_ids(cfg: SampleConfig) -> List[str]:
    """Union of skills across all subsets — used to load the full context once."""
    seen: List[str] = []
    for skills in cfg.subsets.values():
        for s in skills:
            if s not in seen:
                seen.append(s)
    return seen


def _load_full_contexts(cfg: SampleConfig, repo_path: str, cache_dir: str):
    """Build the full (union) context dict + register every skill once."""
    from codeminer.agent.skills.loader import SkillLoader
    from codeminer.agent.skills.registry import SkillRegistry
    from codeminer.compiler import build_skill_contexts

    skills_dir = os.path.join(_PROJECT_ROOT, "codeminer", "agent", "skills")
    union = _all_index_skill_ids(cfg)

    # 1) metadata-only load so build_skill_contexts can read index_requirements
    SkillRegistry().reset()
    SkillLoader().load_all(skills_dir, contexts=None)

    contexts = build_skill_contexts(
        repo_path=repo_path,
        skill_ids=union,
        languages=["python"],  # bm25 builds from the prebuilt graph; lang-agnostic
        cache_dir=cache_dir,
        embedding_model=cfg.embedding_model,
        embedding_dimension=cfg.embedding_dimension,
        default_top_k=cfg.topk,
        default_level="l2",
        rebuild=False,
    )

    # 2) re-load with contexts so executors are wired
    SkillRegistry().reset()
    SkillLoader().load_all(skills_dir, contexts=contexts)
    return contexts


def _run_cell(
    cfg: SampleConfig,
    *,
    contexts: Dict[str, Any],
    llm: Any,
    repo_path: str,
    language_key: str,
    query: str,
    subset_id: str,
    skills: Sequence[str],
) -> Dict[str, Any]:
    """Run one (subset) agent cell against already-loaded contexts."""
    from codeminer.agent.runner import AgentRunner
    from codeminer.agent.skills.registry import SkillRegistry
    from codeminer.compiler.params import SessionContext

    sctx = SessionContext(
        repo_path=repo_path, repo_size=1000, primary_language=language_key
    )
    runner = AgentRunner(
        llm=llm,
        registry=SkillRegistry(),
        max_turns=cfg.max_turns,
        allow_skills=set(skills),
        session_ctx=sctx,
    )
    # The always-on default tools (file_read / file_search) resolve relative
    # paths against the process cwd, so run the agent from the instance repo.
    # The sweep is sequential, so chdir is safe here.
    prev_cwd = os.getcwd()
    try:
        os.chdir(repo_path)
        result = runner.run(query)
    finally:
        os.chdir(prev_cwd)

    nodes: List[Any] = []
    tool_calls: List[Dict[str, Any]] = []
    file_read_paths: List[str] = []
    for tc in result.tool_calls:
        n = len(tc.result) if isinstance(tc.result, list) else 0
        tool_calls.append(
            {
                "skill_id": tc.skill_id,
                "n_results": n,
                "error": bool(tc.error),
            }
        )
        if isinstance(tc.result, list):
            nodes.extend(tc.result)
        if tc.skill_id == "file_read" and not tc.error:
            p = (tc.arguments or {}).get("path")
            if p:
                file_read_paths.append(str(p))

    usage = result.usage.to_dict() if result.usage else {}
    token_usage = usage.get("token_usage") or usage or {}
    return {
        "nodes": nodes,
        "tool_calls": tool_calls,
        "file_read_paths": file_read_paths,
        "answer": result.answer or "",
        "total_turns": result.total_turns,
        "total_duration_ms": result.total_duration_ms,
        "tool_call_count": len(result.tool_calls),
        "prompt_tokens": token_usage.get("prompt_tokens"),
        "completion_tokens": token_usage.get("completion_tokens"),
        "total_tokens": token_usage.get("total_tokens"),
        "cost_usd": token_usage.get("cost_usd"),
    }


# ---------------------------------------------------------------------------
# Localization scoring (agent answer + file_read targets + retrieval nodes)
# ---------------------------------------------------------------------------

_FILES_LINE = re.compile(r"(?im)^\s*files?\s*[:=]\s*(.+)$")
_SYMBOLS_LINE = re.compile(r"(?im)^\s*symbols?\s*[:=]\s*(.+)$")
_PATH_TOKEN = re.compile(r"[\w./\\-]+\.[A-Za-z0-9_]+")


def _rel_norm(path: str, repo_path: str):
    """Normalize a path to the repo-relative posix form used by the GT."""
    from codeminer.eval.retrieval_eval import normalize_file_path

    p = (path or "").strip().strip("`'\"")
    if not p:
        return None
    if repo_path and os.path.isabs(p):
        try:
            p = os.path.relpath(p, repo_path)
        except ValueError:
            pass
    return normalize_file_path(p)


def _dedup(seq):
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _answer_files(answer: str) -> List[str]:
    """Files named in the answer: explicit ``Files:`` line, else path tokens."""
    explicit: List[str] = []
    for m in _FILES_LINE.finditer(answer or ""):
        explicit.extend(t for t in re.split(r"[,\s]+", m.group(1).strip()) if t)
    if explicit:
        return explicit
    return _PATH_TOKEN.findall(answer or "")  # soft fallback


def _answer_symbols(answer: str) -> List[str]:
    syms: List[str] = []
    for m in _SYMBOLS_LINE.finditer(answer or ""):
        syms.extend(t for t in re.split(r"[,\s]+", m.group(1).strip()) if t)
    return syms


def score_localization(
    out: Dict[str, Any],
    target_files: Sequence[str],
    target_symbols: Sequence[str],
    ks: Sequence[int],
    repo_path: str,
):
    """files@k / symbols@k from the agent's answer + file_read + skill nodes.

    Predicted files are an ordered union of (1) the answer's Files: line /
    path tokens, (2) the paths the agent file_read, (3) retrieval-skill node
    files — reflecting how the agent actually localized rather than only the
    retrieval-skill output. Same output shape as ``evaluate_predictions``.
    """
    from codeminer.eval.retrieval_eval import (
        compute_metrics,
        extract_predictions,
        normalize_symbol_identifier,
    )

    node_files, node_symbols = extract_predictions(out.get("nodes") or [])
    answer = out.get("answer") or ""
    pred_files = _dedup(
        [_rel_norm(f, repo_path) for f in _answer_files(answer)]
        + [_rel_norm(p, repo_path) for p in (out.get("file_read_paths") or [])]
        + list(node_files)
    )
    pred_symbols = _dedup(
        [normalize_symbol_identifier(s) for s in _answer_symbols(answer)]
        + list(node_symbols)
    )
    metrics: Dict[str, Dict[int, Dict[str, float]]] = {"files": {}, "symbols": {}}
    for k in ks:
        metrics["files"][k] = compute_metrics(pred_files[:k], target_files)
        metrics["symbols"][k] = compute_metrics(pred_symbols[:k], target_symbols)
    return metrics


# ---------------------------------------------------------------------------
# Sweep loop
# ---------------------------------------------------------------------------


def run_sweep(cfg: SampleConfig, output_dir: Path, *, resume: bool = True) -> Dict:
    from codeminer.eval.retrieval_eval import collect_targets
    from codeminer.llm.litellm_chat import LiteLLMChat

    cells_dir = output_dir / "cells"
    cells_dir.mkdir(parents=True, exist_ok=True)

    vertex_extra: Dict[str, Any] = {}
    if cfg.vertex_project:
        vertex_extra["vertex_project"] = cfg.vertex_project
    if cfg.vertex_location:
        vertex_extra["vertex_location"] = cfg.vertex_location
    llm = LiteLLMChat(
        model=cfg.model,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        extra_kwargs=vertex_extra,
    )

    rows_by_id, eval_lookup = _load_dataset_rows(cfg)
    summary: Dict[str, Any] = {
        "sweep_id": cfg.sweep_id,
        "model": cfg.model,
        "completed": [],
        "skipped": [],
        "failed": [],
    }

    for instance_id in cfg.instances:
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

        language_key = _LANG_GROUP_TO_KEY.get(row.get("language_group"), "python")
        query = row["problem_statement"]
        scenario = _scenario_for(query, language_key)
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
                cell_id = f"{_slug(instance_id)}__{subset_id}__rep{rep}"
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
            contexts = _load_full_contexts(cfg, repo_path, cache_dir)
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
                out = _run_cell(
                    cfg,
                    contexts=contexts,
                    llm=llm,
                    repo_path=repo_path,
                    language_key=language_key,
                    query=query,
                    subset_id=subset_id,
                    skills=skills,
                )
                metrics = score_localization(
                    out,
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

            with cell_path.open("w", encoding="utf-8") as f:
                json.dump(record, f, indent=2, ensure_ascii=False)

    summary_path = output_dir / "sample_sweep_summary.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Agent-compile sample sweep on codeminer_base (prebuilt indexes).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--reps", type=int, default=None, help="Override config reps.")
    parser.add_argument(
        "--instances", nargs="+", default=None, help="Override config instance list."
    )
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args(argv)

    cfg = SampleConfig.from_yaml(args.config)
    if args.reps is not None:
        cfg.reps = args.reps
    if args.instances:
        cfg.instances = args.instances

    summary = run_sweep(cfg, args.output_dir, resume=not args.no_resume)
    print(
        "sample sweep done: completed={c} skipped={s} failed={f} cells={d}".format(
            c=len(summary["completed"]),
            s=len(summary["skipped"]),
            f=len(summary["failed"]),
            d=args.output_dir / "cells",
        )
    )
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
