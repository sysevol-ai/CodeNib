# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Per-instance experiment harness for the agent-compile cost study.

Given a :class:`~scripts.agent_compile.lib.config.SweepConfig`, this module
loads the dataset rows, builds the *full* (union-of-all-subsets) skill context
once per instance from prebuilt indexes, classifies the scenario, and runs a
single agent cell (``run_cell``) for a given skill subset.

These functions were private helpers inside ``run_agent_sweep.py``; they are
promoted here so the sweep runner *and* the offline retrieval ablations share
one definition of "load this instance + register skills" instead of importing
underscore-prefixed names from a sibling script.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .config import SweepConfig

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

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
    return seen


def load_full_contexts(cfg: SweepConfig, repo_path: str, cache_dir: str):
    """Build the full (union) context dict + register every skill once."""
    from codeminer.agent.skills.loader import SkillLoader
    from codeminer.agent.skills.registry import SkillRegistry
    from codeminer.compiler import build_skill_contexts

    skills_dir = os.path.join(_PROJECT_ROOT, "codeminer", "agent", "skills")
    union = all_index_skill_ids(cfg)

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


def _leaf_symbol(name: Any) -> str:
    """Return the final answer/GT leaf from graph or display symbol names."""
    s = str(name or "").strip()
    if not s:
        return ""
    if ":" in s:
        s = s.rsplit(":", 1)[1]
    s = s.replace("#", ".")
    s = s.split("(", 1)[0]
    return s.split("/")[-1].split(".")[-1].strip()


def build_symbol_span_index(prebuilt_dir: str, instance_id: str) -> Dict[Any, Any]:
    """``{(norm_file, leaf_name): (start_1based, end_1based)}`` from the prebuilt
    graph, so committed scoring can resolve a named symbol to its line span.

    Graph vertices are 0-based (tree-sitter); shifted +1 here to match the
    1-based ground-truth blocks. Returns an empty dict when no graph is present.
    """
    import pickle

    from codeminer.eval.retrieval_eval import normalize_file_path

    path = os.path.join(prebuilt_dir, instance_id, "graph.pkl")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as f:
            graph = pickle.load(f)
    except Exception:  # noqa: BLE001 — a missing/corrupt graph just disables this
        return {}
    g = graph.get("graph") if isinstance(graph, dict) else None
    if g is None:
        return {}
    index: Dict[Any, Any] = {}
    for v in g.vs:
        attrs = v.attributes()
        file, start, end = (
            attrs.get("file"),
            attrs.get("start_line"),
            attrs.get("end_line"),
        )
        name = attrs.get("name")
        unified_name = attrs.get("unified_name")
        if file is None or start is None or end is None or not (name or unified_name):
            continue
        nf = normalize_file_path(file)
        for label in (name, unified_name):
            leaf = _leaf_symbol(label)
            if not leaf:
                continue
            key = (nf, leaf)
            if key not in index:  # first definition wins
                index[key] = (int(start) + 1, int(end) + 1)
    return index


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
    from codeminer.agent.runner import AgentRunner
    from codeminer.agent.skills.registry import SkillRegistry
    from codeminer.compiler.params import SessionContext
    from scripts.agent_compile.lib.preload import assemble_preload

    preload_candidates: List[Dict[str, Any]] = []
    effective_query = query
    if preload_spec:
        preamble, preload_candidates = assemble_preload(
            contexts, query, recipe=preload_spec
        )
        if preamble:
            effective_query = f"{query}\n\n{preamble}"

    sctx = SessionContext(
        repo_path=repo_path, repo_size=1000, primary_language=language_key
    )
    runner = AgentRunner(
        llm=llm,
        registry=SkillRegistry(),
        max_turns=cfg.max_turns,
        allow_skills=set(skills),
        session_ctx=sctx,
        include_default_tools=cfg.include_default_tools,
        default_tool_ids=(
            set(cfg.default_tool_ids) if cfg.default_tool_ids is not None else None
        ),
        system_prompt=cfg.system_prompt,
    )
    # The always-on default tools (file_read / file_search) resolve relative
    # paths against the process cwd, so run the agent from the instance repo.
    # The sweep is sequential, so chdir is safe here.
    prev_cwd = os.getcwd()
    runs_usage: List[Dict[str, Any]] = []
    runs_turns: List[int] = []

    def _do_run(q: str):
        try:
            os.chdir(repo_path)
            r = runner.run(q)
        finally:
            os.chdir(prev_cwd)
        u = r.usage.to_dict() if r.usage else {}
        runs_usage.append(u.get("token_usage") or u or {})
        if r.total_turns is not None:
            runs_turns.append(r.total_turns)
        return r

    result = _do_run(effective_query)

    # Verify-expand (Layer 4): if the committed answer is not anchored to a real
    # graph symbol, inject 1-hop neighbours of the best seeds and answer once more.
    verify_triggered = False
    verify_resolved: Optional[int] = None
    if verify and nav is not None:
        from scripts.agent_compile.lib.verify_expand import (
            expansion_seeds_from_candidates,
            graph_verify,
            render_expansion,
        )

        verdict = graph_verify(result.answer or "", nav)
        verify_resolved = verdict.n_resolved
        if not verdict.ok:
            seeds = verdict.seeds or expansion_seeds_from_candidates(preload_candidates)
            extra = render_expansion(nav.neighbors(seeds, max_nodes=10))
            if extra:
                verify_triggered = True
                result = _do_run(f"{effective_query}\n\n{extra}")

    nodes: List[Any] = []
    tool_calls: List[Dict[str, Any]] = []
    file_read_paths: List[str] = []
    file_reads: List[Dict[str, Any]] = []
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
        if tc.skill_id == "read" and not tc.error:
            args = tc.arguments or {}
            p = args.get("file_path")
            if p:
                file_read_paths.append(str(p))
                # Capture the read window (1-based offset/limit) for audit (which
                # line range the agent inspected); not scored.
                file_reads.append(
                    {
                        "file_path": str(p),
                        "offset": args.get("offset"),
                        "limit": args.get("limit"),
                    }
                )

    # Sum token usage across the (1 or 2) runs so the verify arm is charged for
    # the closed loop, not just its final turn.
    def _sum(key: str) -> Optional[float]:
        vals = [u.get(key) for u in runs_usage if u.get(key) is not None]
        return sum(vals) if vals else None

    return {
        "nodes": nodes,
        "tool_calls": tool_calls,
        "file_read_paths": file_read_paths,
        "file_reads": file_reads,
        "preload_candidates": preload_candidates,
        "answer": result.answer or "",
        "total_turns": sum(runs_turns) if runs_turns else result.total_turns,
        "total_duration_ms": result.total_duration_ms,
        "tool_call_count": len(result.tool_calls),
        "verify_triggered": verify_triggered,
        "verify_resolved": verify_resolved,
        "prompt_tokens": _sum("prompt_tokens"),
        "completion_tokens": _sum("completion_tokens"),
        "total_tokens": _sum("total_tokens"),
        "cost_usd": _sum("cost_usd"),
        "cache_read_input_tokens": _sum("cache_read_input_tokens"),
        "cache_creation_input_tokens": _sum("cache_creation_input_tokens"),
    }
