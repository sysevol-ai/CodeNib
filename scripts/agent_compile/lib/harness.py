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
from typing import Any, Dict, List, Sequence

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


def all_index_skill_ids(cfg: SweepConfig) -> List[str]:
    """Union of skills across all subsets — used to load the full context once."""
    seen: List[str] = []
    for skills in cfg.subsets.values():
        for s in skills:
            if s not in seen:
                seen.append(s)
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
        if tc.skill_id == "read" and not tc.error:
            p = (tc.arguments or {}).get("file_path")
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
