# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dynamic native-LSP arms for the CodeNib Base agent ablation."""

from __future__ import annotations

import hashlib
import json
import os
import random
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from codenib.agent.harness import AgentHarnessSpec, run_agent_in_directory
from codenib.agent.lsp_provider import (
    CAPABILITY_DEFINITION,
    CAPABILITY_REFERENCES,
    fingerprint_lsp_result,
    lsp_result_metadata,
)
from codenib.agent.runner import LOCALIZATION_SCHEMA
from codenib.agent.skills.loader import SkillLoader
from codenib.agent.skills.registry import SkillRegistry
from codenib.compiler.params import SessionContext
from codenib.compiler.snapshot_store import SourceSnapshot
from codenib.eval.experiments.lsp_agent_study_policy import (
    DEFAULT_BASE_DATASET,
    DEFAULT_BASE_REVISION,
    DEFAULT_DEVELOPMENT_REPOSITORIES,
    DEFAULT_MODEL,
    LANGUAGE_GROUPS,
    STATIC_NATIVE_BACKENDS,
)
from codenib.eval.retrieval_eval import collect_target_blocks, collect_targets
from codenib.ops.expand import ExpandContext

from .lsp_provider_validation import LSPProviderRequest
from .results import summarize_agent_result
from .scoring import evaluate_agent_localization

FILESYSTEM_ARM = "filesystem"
LIVE_LSP_ARM = "live_lsp"
CODENIB_LSP_ARM = "codenib_lsp"
DEFAULT_ARMS = (FILESYSTEM_ARM, LIVE_LSP_ARM, CODENIB_LSP_ARM)
NATIVE_LSP_SKILLS = ("lsp_definition", "lsp_references")

_SYSTEM_PROMPT = f"""\
You are a code localization agent. Find the repository files, symbols, and
line ranges that implement the requested behavior.

Use the available filesystem tools to inspect the repository. When native LSP
definition or references tools are available, call them only with a file and
position obtained from repository evidence such as read or grep output. LSP
locations are navigation hints, not proof: read a returned location before
citing it. Do not assume an optional tool is available unless it appears in the
tool schema.

End with these three repo-relative lines:
{LOCALIZATION_SCHEMA}
"""


@dataclass(frozen=True)
class LSPAgentStudySpec:
    """Pre-registered controls for the CodeNib Base provider ablation."""

    dataset: str = DEFAULT_BASE_DATASET
    dataset_revision: str = DEFAULT_BASE_REVISION
    split: str = "test"
    language_groups: tuple[str, ...] = tuple(LANGUAGE_GROUPS)
    development_repositories: tuple[str, ...] = DEFAULT_DEVELOPMENT_REPOSITORIES
    arms: tuple[str, ...] = DEFAULT_ARMS
    model: str = DEFAULT_MODEL
    reps: int = 3
    max_turns: int = 20
    max_tokens: int = 4096
    temperature: float = 0.0
    randomization_seed: str = "codenib-lsp-agent-v1"
    static_native_backend: str = "per_language_v1"
    static_fallback_backend: str = "symbol_graph_v1"
    primary_quality_metric: str = "answer_blocks.recall@5"
    noninferiority_margin: float = 0.05
    cluster_key: str = "repo"
    prompt_cache_enabled: bool = False
    bootstrap_samples: int = 5000
    bootstrap_seed: int = 0

    def __post_init__(self) -> None:
        if not self.dataset_revision.strip():
            raise ValueError("dataset_revision must pin an immutable revision")
        if self.reps < 1 or self.max_turns < 1 or self.max_tokens < 1:
            raise ValueError("reps, max_turns, and max_tokens must be positive")
        if self.bootstrap_samples < 1:
            raise ValueError("bootstrap_samples must be positive")
        if not self.model.strip():
            raise ValueError("model must be non-empty")
        if (
            not self.static_native_backend.strip()
            or not self.static_fallback_backend.strip()
        ):
            raise ValueError("static LSP backend identities must be non-empty")
        if not 0 <= self.noninferiority_margin < 1:
            raise ValueError("noninferiority_margin must be in [0, 1)")
        unknown_groups = set(self.language_groups) - set(LANGUAGE_GROUPS)
        if unknown_groups:
            raise ValueError(f"unsupported language groups: {sorted(unknown_groups)}")
        if tuple(self.arms) != DEFAULT_ARMS:
            raise ValueError(f"study arms must be {DEFAULT_ARMS!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "arms": list(self.arms),
            "cluster_key": self.cluster_key,
            "bootstrap_samples": self.bootstrap_samples,
            "bootstrap_seed": self.bootstrap_seed,
            "development_repositories": list(self.development_repositories),
            "language_groups": list(self.language_groups),
            "max_turns": self.max_turns,
            "max_tokens": self.max_tokens,
            "model": self.model,
            "noninferiority_margin": self.noninferiority_margin,
            "primary_comparison": [CODENIB_LSP_ARM, LIVE_LSP_ARM],
            "primary_latency_endpoint": "frozen_live_trace_request_replay",
            "primary_quality_metric": self.primary_quality_metric,
            "prompt_cache_enabled": self.prompt_cache_enabled,
            "randomization": "deterministic crossover within instance and rep",
            "randomization_seed": self.randomization_seed,
            "reps": self.reps,
            "request_admission": "all observed native LSP calls",
            "static_fallback_backend": self.static_fallback_backend,
            "static_native_backend": self.static_native_backend,
            "static_native_backend_by_language": {
                LANGUAGE_GROUPS[group]: STATIC_NATIVE_BACKENDS[LANGUAGE_GROUPS[group]]
                for group in self.language_groups
            },
            "task_admission": "all supported-language dataset rows",
            "temperature": self.temperature,
        }


@dataclass(frozen=True)
class LSPAgentStudyTask:
    """One CodeNib Base task and its scoring labels."""

    instance_id: str
    repo: str
    base_commit: str
    language: str
    query: str
    repo_path: str
    role: str = "unspecified"
    target_files: tuple[str, ...] = ()
    target_symbols: tuple[str, ...] = ()
    gt_code_blocks: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_base_row(
        cls,
        row: Mapping[str, Any],
        *,
        repo_path: str | Path,
        role: str = "unspecified",
    ) -> "LSPAgentStudyTask":
        language_group = str(row.get("language_group") or "")
        if language_group not in LANGUAGE_GROUPS:
            raise ValueError(f"unsupported language group: {language_group!r}")
        metadata = {
            "target_files": list(row.get("gt_target_files") or []),
            "symbols_modified": list(row.get("gt_symbols_modified") or []),
            "symbols_deleted": list(row.get("gt_symbols_deleted") or []),
            "symbols_added": [],
        }
        target_files, target_symbols = collect_targets(metadata)
        return cls(
            instance_id=str(row["instance_id"]),
            repo=str(row["repo"]),
            base_commit=str(row["base_commit"]),
            language=LANGUAGE_GROUPS[language_group],
            query=str(row.get("problem_statement") or ""),
            repo_path=str(Path(repo_path).resolve()),
            role=role,
            target_files=tuple(target_files),
            target_symbols=tuple(target_symbols),
            gt_code_blocks=tuple(collect_target_blocks(row)),
        )


def run_lsp_agent_study_arm(
    task: LSPAgentStudyTask,
    *,
    arm: str,
    graph: Any,
    llm: Any,
    provider: Any = None,
    rep: int = 1,
    order: int = 1,
    max_turns: int = 20,
    prompt_cache_enabled: bool = False,
    symbol_span_index: Optional[Mapping[tuple[str, str], tuple[int, int]]] = None,
) -> dict[str, Any]:
    """Run one dynamic agent cell; only the provider differs across LSP arms."""

    if arm not in DEFAULT_ARMS:
        raise ValueError(f"unsupported study arm: {arm!r}")
    uses_lsp = arm != FILESYSTEM_ARM
    if uses_lsp != (provider is not None):
        raise ValueError(f"arm {arm!r} provider configuration is inconsistent")

    SkillRegistry.reset()
    try:
        registry = SkillRegistry()
        if uses_lsp:
            context = {"expand": ExpandContext(code_graph=graph, lsp_provider=provider)}
            skills_root = Path(__file__).resolve().parents[2] / "agent" / "skills"
            for skill_id in NATIVE_LSP_SKILLS:
                metadata = SkillLoader().load_skill(
                    str(skills_root / skill_id), context
                )
                if metadata is None:
                    raise RuntimeError(f"could not load agent skill: {skill_id}")
                registry.register(metadata)

        harness = AgentHarnessSpec(
            max_turns=max_turns,
            allow_skills=(set(NATIVE_LSP_SKILLS) if uses_lsp else None),
            include_default_tools=True,
            system_prompt=_SYSTEM_PROMPT,
            force_localization_contract=True,
        )
        runner = harness.create_runner(
            llm=llm,
            registry=registry,
            session_ctx=SessionContext(
                repo_path=task.repo_path,
                primary_language=task.language,
            ),
        )
        tool_schema_sha256 = _sha256_json(runner.tools)
        system_prompt_sha256 = _sha256_text(runner.system_prompt)
        with _prompt_cache_policy(enabled=prompt_cache_enabled):
            result = run_agent_in_directory(runner, task.query, task.repo_path)
    finally:
        SkillRegistry.reset()

    observations = summarize_agent_result(result)
    evaluation = evaluate_agent_localization(
        answer=observations["answer"],
        file_read_paths=observations["file_read_paths"],
        nodes=observations["nodes"],
        target_files=task.target_files,
        target_symbols=task.target_symbols,
        gt_blocks=task.gt_code_blocks,
        metrics_k=(1, 3, 5, 10),
        repo_path=task.repo_path,
        symbol_span_index=symbol_span_index,
        metrics_meaningful=True,
    )
    usage = getattr(result, "usage", None)
    usage_payload = usage.to_dict() if hasattr(usage, "to_dict") else {}
    tool_calls = _tool_call_rows(result, task=task, rep=rep)
    return {
        "schema_version": 1,
        "benchmark": "codenib_base_lsp_agent_ablation",
        "instance_id": task.instance_id,
        "repo": task.repo,
        "base_commit": task.base_commit,
        "snapshot_id": SourceSnapshot(task.repo, task.base_commit).snapshot_id,
        "language": task.language,
        "role": task.role,
        "arm": arm,
        "rep": int(rep),
        "order": int(order),
        "query_sha256": _sha256_text(task.query),
        "model": getattr(llm, "model", None),
        "model_config_sha256": _model_config_sha256(llm),
        "prompt_cache_enabled": bool(prompt_cache_enabled),
        "system_prompt_sha256": system_prompt_sha256,
        "tool_schema_sha256": tool_schema_sha256,
        "answer": observations["answer"],
        "total_turns": observations["total_turns"],
        "total_duration_ms": observations["total_duration_ms"],
        "usage": usage_payload,
        "tool_calls": tool_calls,
        "lsp_requests": [
            row["replay_request"]
            for row in tool_calls
            if row.get("replay_request") is not None
        ],
        "lsp_call_count": sum(
            row.get("skill_id") in NATIVE_LSP_SKILLS for row in tool_calls
        ),
        "file_read_paths": observations["file_read_paths"],
        "trace_summary": observations["trace_summary"],
        **evaluation.to_record_fields(),
    }


def study_arm_order(
    instance_id: str,
    rep: int,
    *,
    seed: str = "codenib-lsp-agent-v1",
    arms: Sequence[str] = DEFAULT_ARMS,
) -> tuple[str, ...]:
    """Return a reproducible within-task crossover order."""

    if set(arms) != set(DEFAULT_ARMS) or len(arms) != len(DEFAULT_ARMS):
        raise ValueError(f"arms must contain exactly {DEFAULT_ARMS!r}")
    digest = hashlib.sha256(f"{seed}:{instance_id}:{rep}".encode()).digest()
    ordered = list(arms)
    random.Random(int.from_bytes(digest[:8], "big")).shuffle(ordered)
    return tuple(ordered)


def _tool_call_rows(
    result: Any, *, task: LSPAgentStudyTask, rep: int
) -> list[dict[str, Any]]:
    rows = []
    lsp_ordinal = 0
    for call in list(getattr(result, "tool_calls", []) or []):
        skill_id = str(getattr(call, "skill_id", "") or "")
        raw_result = getattr(call, "result", None)
        row = {
            "skill_id": skill_id,
            "arguments": dict(getattr(call, "arguments", None) or {}),
            "resolved_arguments": dict(getattr(call, "resolved_arguments", None) or {}),
            "duration_ms": float(getattr(call, "duration_ms", 0.0) or 0.0),
            "error": getattr(call, "error", None),
            "result_count": len(raw_result) if isinstance(raw_result, list) else None,
            "provider": lsp_result_metadata(raw_result),
            "result_fingerprint": (
                fingerprint_lsp_result(raw_result)
                if isinstance(raw_result, (list, tuple))
                and skill_id in NATIVE_LSP_SKILLS
                else None
            ),
        }
        if skill_id in NATIVE_LSP_SKILLS and row["resolved_arguments"]:
            lsp_ordinal += 1
            request = LSPProviderRequest(
                capability=(
                    CAPABILITY_DEFINITION
                    if skill_id == "lsp_definition"
                    else CAPABILITY_REFERENCES
                ),
                arguments=row["resolved_arguments"],
                request_id=(
                    f"{task.instance_id}:rep{rep}:call{lsp_ordinal}:{skill_id}"
                ),
            )
            row["replay_request"] = request.to_dict()
        else:
            row["replay_request"] = None
        rows.append(row)
    return rows


def _model_config_sha256(llm: Any) -> str:
    return _sha256_json(
        {
            "max_tokens": getattr(llm, "max_tokens", None),
            "model": getattr(llm, "model", None),
            "temperature": getattr(llm, "temperature", None),
        }
    )


@contextmanager
def _prompt_cache_policy(*, enabled: bool):
    key = "CODENIB_DISABLE_PROMPT_CACHE"
    previous = os.environ.get(key)
    if not enabled:
        os.environ[key] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


__all__ = [
    "CODENIB_LSP_ARM",
    "DEFAULT_ARMS",
    "DEFAULT_BASE_DATASET",
    "DEFAULT_BASE_REVISION",
    "FILESYSTEM_ARM",
    "LANGUAGE_GROUPS",
    "LIVE_LSP_ARM",
    "LSPAgentStudySpec",
    "LSPAgentStudyTask",
    "NATIVE_LSP_SKILLS",
    "run_lsp_agent_study_arm",
    "study_arm_order",
]
