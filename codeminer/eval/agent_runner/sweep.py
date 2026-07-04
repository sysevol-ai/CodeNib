# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Per-instance harness helpers for agent-runner sweep evaluation.

Given a :class:`~codeminer.eval.agent_runner.sweep_config.SweepConfig`, this module
loads the dataset rows, builds the *full* (union-of-all-subsets) skill context
once per instance from prebuilt indexes, classifies the scenario, and runs a
single agent cell (``run_cell``) for a given skill subset.

These helpers intentionally stay independent from ``scripts/`` so experiments
can provide thin CLIs without owning reusable runner semantics.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .sweep_config import SweepConfig

# language_group (HF column) -> classify() language key
LANG_GROUP_TO_KEY = {
    "Python": "python",
    "Go": "go",
    "Rust": "rust",
    "C++/C": "cpp",
    "TypeScript/JavaScript": "typescript",
}


def slug(value: str) -> str:
    """Filesystem-safe slug for cell ids."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")


# ---------------------------------------------------------------------------
# Dataset + scenario
# ---------------------------------------------------------------------------


def load_dataset_rows(cfg: SweepConfig):
    """Return ``(rows_by_id, eval_lookup)`` for the configured instances.

    An empty ``cfg.instances`` means the *whole* split — every instance is
    returned (the runner then skips any without prebuilt indexes).
    """
    from codeminer.dataset.codeminer_base import CodeMinerBaseDataset

    ds = CodeMinerBaseDataset(dataset=cfg.dataset, split=cfg.split)
    data = ds.load()
    if cfg.instances:
        wanted = set(cfg.instances)
        rows_by_id = {r["instance_id"]: r for r in data if r["instance_id"] in wanted}
    else:
        rows_by_id = {r["instance_id"]: r for r in data}
    eval_lookup = ds.load_eval_metadata()
    return rows_by_id, eval_lookup


def scenario_for(query: str, language_key: str) -> str:
    """Deterministic ``language:has_stacktrace`` scenario key for a query."""
    from codeminer.agent.compile import classify
    from codeminer.compiler.params import SessionContext

    sctx = SessionContext(repo_path=".", repo_size=1, primary_language=language_key)
    return classify(query, sctx).key()


# ---------------------------------------------------------------------------
# Per-instance index loading (full set, once) + per-cell agent run
# ---------------------------------------------------------------------------


# Pre-load recipes name retrievers ("embedding"/"bm25"), not skills; map them to
# the skill that backs each index so the contexts get built even when no ARM
# offers that retriever as a tool (pre-load arms only carry default tools).
_PRELOAD_RETRIEVER_SKILL = {
    "embedding": "embedding_search",
    "bm25": "bm25_search",
    # graph = the codeminer_context composer (search seeds + call-graph expand);
    # pulling it into the union builds the "expand" (symbol_graph) context.
    "graph": "codeminer_context",
}


def all_index_skill_ids(cfg: SweepConfig) -> List[str]:
    """Union of skills across all subsets — used to load the full context once.

    Also includes the skills implied by any ``preload`` recipe, so a pre-load
    arm whose subset is just default tools still gets its retrieval index built.
    """
    seen: List[str] = []
    for skills in cfg.subsets.values():
        for s in skills:
            if s not in seen:
                seen.append(s)
    for recipe in (cfg.preload or {}).values():
        for retriever in recipe.get("retrievers") or []:
            sk = _PRELOAD_RETRIEVER_SKILL.get(retriever)
            if sk and sk not in seen:
                seen.append(sk)
    if (
        cfg.enable_lsp_route_context or cfg.lsp_route_context
    ) and "lsp_route" not in seen:
        seen.append("lsp_route")
    return seen


def lsp_route_context_for_subset(
    cfg: SweepConfig,
    subset_id: str,
) -> Optional[Dict[str, Any]]:
    """Return the route-context policy for one arm, or ``None`` when disabled."""

    raw = (cfg.lsp_route_context or {}).get(subset_id)
    if raw is not None:
        if raw is True:
            return {}
        if isinstance(raw, Mapping):
            return dict(raw)
        raise ValueError(
            f"{cfg.sweep_id}/{subset_id}: lsp_route_context must be a mapping "
            "or true when present"
        )
    if cfg.enable_lsp_route_context:
        return {}
    return None


def validate_sweep_harness(cfg: SweepConfig) -> None:
    """Fail loudly on tool/skill ids that would be silently dropped.

    ``default_tool_ids`` is intersected with the registered default tools, and
    ``allow_skills`` with the registry. An unknown name (for example the legacy
    ``file_read`` instead of ``read``) otherwise vanishes before an expensive
    sweep starts.
    """
    from codeminer.agent.tools.defaults import DEFAULT_TOOL_IDS
    from codeminer.eval.agent_runner.contexts import default_agent_skills_dir

    defaults = set(DEFAULT_TOOL_IDS)
    skills_dir = default_agent_skills_dir()
    index_skills = {
        path.name for path in skills_dir.iterdir() if (path / "config.yaml").exists()
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
        bad = [skill_id for skill_id in skills if skill_id not in known]
        if bad:
            raise ValueError(
                f"{cfg.sweep_id}/{arm}: unknown skills {bad} — not a default tool "
                f"{sorted(defaults)} nor a package under codeminer/agent/skills/."
            )
    route_arms = set((cfg.lsp_route_context or {}).keys())
    unknown_route_arms = sorted(route_arms - set(cfg.subsets))
    if unknown_route_arms:
        raise ValueError(
            f"{cfg.sweep_id}: lsp_route_context references unknown subset(s) "
            f"{unknown_route_arms}; known subsets are {sorted(cfg.subsets)}."
        )
    for arm in route_arms:
        lsp_route_context_for_subset(cfg, arm)


def load_full_contexts(cfg: SweepConfig, repo_path: str, cache_dir: str):
    """Build the full (union) context dict + register every skill once."""
    from codeminer.eval.agent_runner.contexts import (
        AgentSkillContextSpec,
        load_agent_skill_contexts,
    )

    return load_agent_skill_contexts(
        repo_path=repo_path,
        cache_dir=cache_dir,
        spec=AgentSkillContextSpec(
            skill_ids=all_index_skill_ids(cfg),
            # bm25 builds from the prebuilt graph; lang-agnostic.
            languages=["python"],
            embedding_model=cfg.embedding_model,
            embedding_dimension=cfg.embedding_dimension,
            top_k=cfg.topk,
            default_level="l2",
            rebuild=False,
        ),
    )


def run_cell(
    cfg: SweepConfig,
    *,
    contexts: Dict[str, Any],
    llm: Any,
    repo_path: str,
    language_key: str,
    query: str,
    subset_id: str,
    skills: Sequence[str],
    preload_spec: Optional[Dict[str, Any]] = None,
    verify: bool = False,
    nav: Any = None,
) -> Dict[str, Any]:
    """Run one (subset) agent cell against already-loaded contexts.

    When ``preload_spec`` is given, retrieval candidates are computed up front
    and injected into the agent's opening prompt (the pre-load architecture);
    the returned ``preload_candidates`` lists the injected ``(file, span)``s for
    contribution attribution.

    When ``verify`` and a ``nav`` (GraphNav) are given, the committed answer is
    checked against the graph (deterministic); if it does not resolve to a real
    symbol, the 1-hop neighbours of the best seeds are injected and the agent
    answers once more. Token/turn costs of BOTH runs are summed so the verify
    arm is charged for the closed loop.
    """
    from codeminer.agent.harness import (
        AgentHarnessSpec,
        AgentRunAccumulator,
        run_agent_in_directory,
    )
    from codeminer.agent.skills.registry import SkillRegistry
    from codeminer.compiler.params import SessionContext
    from codeminer.eval.agent_runner.orchestrator import scatter_gather_localize
    from codeminer.eval.agent_runner.preload import prepare_preload_query

    accounting = AgentRunAccumulator()
    preload_candidates: List[Dict[str, Any]] = []
    effective_query = query
    scatter_mode = False
    mode = (preload_spec or {}).get("mode")
    if preload_spec:
        gate_call = None
        if mode == "eager_gated":

            def _gate_call(msgs):
                extra = {}
                if cfg.gate_llm_extra_body:
                    extra["extra_body"] = cfg.gate_llm_extra_body
                resp = llm._call_raw(msgs, **extra)
                u = getattr(resp, "usage", None)
                if u is not None:
                    accounting.add_usage(
                        {
                            "prompt_tokens": getattr(u, "prompt_tokens", 0),
                            "completion_tokens": getattr(u, "completion_tokens", 0),
                            "total_tokens": getattr(u, "total_tokens", 0),
                        }
                    )
                return resp.choices[0].message.content

            gate_call = _gate_call

        preload_plan = prepare_preload_query(
            contexts,
            query,
            recipe=preload_spec,
            gate_call=gate_call,
        )
        preload_candidates = preload_plan.candidates
        effective_query = preload_plan.query
        scatter_mode = preload_plan.scatter

    sctx = SessionContext(
        repo_path=repo_path, repo_size=1000, primary_language=language_key
    )
    # Seed richness is experiment policy. Recipes that use eager_compact should
    # set compact_keep_reads explicitly; absence means the terse default.
    _keep_reads = int((preload_spec or {}).get("compact_keep_reads") or 0)
    route_context_spec = lsp_route_context_for_subset(cfg, subset_id)
    route_context_enabled = route_context_spec is not None
    route_context_spec = route_context_spec or {}
    harness_spec = AgentHarnessSpec(
        max_turns=cfg.max_turns,
        allow_skills=set(skills),
        include_default_tools=cfg.include_default_tools,
        default_tool_ids=(
            set(cfg.default_tool_ids) if cfg.default_tool_ids is not None else None
        ),
        system_prompt=cfg.system_prompt,
        first_turn_tool_choice=getattr(cfg, "first_turn_tool_choice", None) or None,
        force_localization_contract=cfg.force_localization_contract,
        compact_after_read=(mode == "eager_compact"),
        compact_keep_reads=(_keep_reads if mode == "eager_compact" else 0),
        enable_lsp_route_context=route_context_enabled,
        lsp_route_seed_limit=int(
            route_context_spec.get("seed_limit", cfg.lsp_route_seed_limit)
        ),
        lsp_route_top_k=int(route_context_spec.get("top_k", cfg.lsp_route_top_k)),
        lsp_route_include_neighbors=bool(
            route_context_spec.get(
                "include_neighbors",
                cfg.lsp_route_include_neighbors,
            )
        ),
    )
    runner = harness_spec.create_runner(
        llm=llm,
        registry=SkillRegistry(),
        session_ctx=sctx,
    )

    def _do_run(q: str):
        r = run_agent_in_directory(runner, q, repo_path)
        accounting.add_result(r)
        return r

    if scatter_mode:

        def _run_subagent(sys_prompt, q, mt):
            sub = harness_spec.create_runner(
                llm=llm,
                registry=SkillRegistry(),
                max_turns=mt,
                session_ctx=sctx,
                system_prompt=sys_prompt or cfg.system_prompt,
            )
            r = run_agent_in_directory(sub, q, repo_path)
            accounting.add_result(r)
            return r

        result = scatter_gather_localize(
            query, preload_candidates, _run_subagent, explore_turns=cfg.max_turns
        )
    else:
        result = _do_run(effective_query)

    from codeminer.eval.agent_runner.verify_expand import run_verify_expand

    verify_run = run_verify_expand(
        result,
        query=effective_query,
        nav=nav,
        preload_candidates=preload_candidates,
        rerun=_do_run,
        enabled=verify,
    )
    result = verify_run.result

    from codeminer.eval.agent_runner.results import summarize_agent_result

    observations = summarize_agent_result(result)

    # Sum token usage across every run so verify/scatter arms are charged for
    # the closed loop, not just their final turn.
    usage_totals = accounting.usage_totals()

    return {
        "nodes": observations["nodes"],
        "tool_calls": observations["tool_calls"],
        "file_read_paths": observations["file_read_paths"],
        "file_reads": observations["file_reads"],
        "trace_summary": observations["trace_summary"],
        "preload_candidates": preload_candidates,
        "answer": observations["answer"],
        "total_turns": accounting.total_turns(fallback=observations["total_turns"]),
        "total_duration_ms": observations["total_duration_ms"],
        "tool_call_count": observations["tool_call_count"],
        "verify_triggered": verify_run.triggered,
        "verify_resolved": verify_run.resolved,
        "prompt_tokens": usage_totals["prompt_tokens"],
        "completion_tokens": usage_totals["completion_tokens"],
        "total_tokens": usage_totals["total_tokens"],
        "cost_usd": usage_totals["cost_usd"],
        "cache_read_input_tokens": usage_totals["cache_read_input_tokens"],
        "cache_creation_input_tokens": usage_totals["cache_creation_input_tokens"],
    }


def run_sweep(cfg: SweepConfig, output_dir: Path, *, resume: bool = True) -> Dict:
    """Run an agent-runner sweep and write per-cell JSON records.

    The CLI in ``scripts/agent_compile`` owns argument parsing and experiment
    config files; the reusable execution semantics live here so other
    experiments do not need to copy resume, prebuilt index loading, scoring, or
    transient failure handling.
    """
    from codeminer.eval.agent_runner.prebuilt import (
        has_full_indexes,
        repo_path_for,
        stage_prebuilt_indexes,
    )
    from codeminer.eval.agent_runner.scoring import evaluate_agent_localization
    from codeminer.eval.agent_runner.symbols import build_prebuilt_symbol_span_index
    from codeminer.eval.retrieval_eval import collect_target_blocks, collect_targets
    from codeminer.llm.litellm_chat import LiteLLMChat

    validate_sweep_harness(cfg)
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
        # 1-based ground-truth line spans for overlap-based localization scoring
        # (read from the raw row, which carries ``gt_code_blocks``).
        gt_blocks = collect_target_blocks(row)
        # {(file, leaf): (start, end)} so committed scoring can resolve a named
        # symbol to its span when the agent didn't emit an explicit range.
        symbol_span_index = build_prebuilt_symbol_span_index(
            cfg.prebuilt_dir, instance_id
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
                    preload_spec=(cfg.preload or {}).get(subset_id),
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
                    metrics_meaningful=gt_meaningful,
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
                    **evaluation.to_record_fields(),
                    "preload_candidates": out.get("preload_candidates") or [],
                    "tool_calls": out["tool_calls"],
                    "file_read_paths": out["file_read_paths"],
                    "file_reads": out.get("file_reads", []),
                    "trace_summary": out.get("trace_summary"),
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
                    "file_accuracy_at_5="
                    f"{evaluation.metrics.get('files', {}).get(5, {}).get('accuracy')}"
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
                    fragment in str(exc).lower()
                    for fragment in (
                        "ratelimit",
                        "rate limit",
                        "429",
                        "quota",
                        "resourceexhausted",
                    )
                ):
                    print(f"sweep:   (transient; not persisting {cell_id})")
                    continue

            with cell_path.open("w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2, ensure_ascii=False)

    summary_path = output_dir / "sweep_summary.json"
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
    return summary
