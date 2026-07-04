# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Experiment configuration for agent-runner sweep evaluation.

A ``SweepConfig`` is one experiment arm: a model + dataset + a *harness*
(which skills the agent may use, whether the always-on file tools are present,
and the system prompt). Arms share almost everything except the harness, so a
config YAML may set ``base: <other.yaml>`` to inherit and override only the
deltas (see :meth:`SweepConfig.from_yaml`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class SweepConfig:
    """One cost-study arm: model + dataset + tool harness.

    The harness is the only thing that *should* vary across arms:

    * ``subsets`` — the index-backed skills the agent may call, keyed by an
      arm label (e.g. ``{"GRAPH": ["bm25_names", "find_callers", ...]}``).
    * ``include_default_tools`` — whether the always-on file tools
      (read / grep / glob / bash) are present at all.
    * ``default_tool_ids`` — restrict the always-on defaults to this subset
      (e.g. ``["file_read"]`` → read but no grep). ``None`` = all defaults.
    * ``system_prompt`` — override the runner's default localization prompt.
    """

    sweep_id: str
    subsets: Dict[str, List[str]]
    # Instance allowlist. Empty/omitted = the WHOLE dataset split (every
    # instance with prebuilt indexes) — the cost arms run on the full corpus,
    # so arms are directly comparable on the same instances.
    instances: List[str] = field(default_factory=list)
    model: Optional[str] = None
    vertex_location: Optional[str] = None
    vertex_project: Optional[str] = None
    reps: int = 2
    max_turns: int = 20
    temperature: float = 0.0
    max_tokens: int = 4096
    topk: int = 50
    num_retries: int = 8
    dataset: str = "fishmingyu/codeminer-base-dataset"
    split: str = "test"
    prebuilt_dir: str = "/mnt/data/codeminer"
    embedding_model: Optional[str] = None
    embedding_dimension: int = 1024
    metrics_k: List[int] = field(default_factory=lambda: [1, 3, 5, 10])
    gt_simplified_symbols: bool = True
    include_default_tools: bool = True
    # LocAgent-style harness: restrict the always-on defaults to this subset
    # (e.g. ["file_read"] → no grep). None = all of read + grep + glob + bash.
    default_tool_ids: Optional[List[str]] = None
    # Pre-load (context-assembly) recipes, keyed by arm label. An arm listed here
    # gets retrieval candidates computed UP FRONT and injected into its opening
    # prompt (retrieve-only — NO rerank; the agent reasons over them itself), so
    # retrieval value no longer depends on the agent choosing to call a tool.
    # Recipe: {retrievers: [embedding|bm25], top_k: int, level: l0|l2}. Arms NOT
    # listed run plain (no pre-injection). See
    # .claude/design/preload-vs-skill-architecture.md.
    preload: Dict[str, Any] = field(default_factory=dict)
    # Override the agent system prompt (e.g. a graph-primary prompt with no grep
    # guidance). None = runner's default localization prompt.
    system_prompt: Optional[str] = None
    # Force a tool call on the FIRST agent turn (e.g. "required"). Some models
    # answer one-shot under the default "auto" and never exercise the loop; this
    # makes them actually grep/read. None = leave "auto".
    first_turn_tool_choice: Optional[str] = None
    # Provider-specific raw-call body for the lightweight eager_gated classifier.
    # Keep quirks in config files instead of branching on model names in code.
    gate_llm_extra_body: Optional[Dict[str, Any]] = None

    @classmethod
    def from_yaml(cls, path: Path) -> "SweepConfig":
        """Load a config, resolving an optional ``base:`` overlay.

        If the YAML names ``base: <relative-or-absolute path>``, that file is
        loaded first and this file's keys are layered on top — so a per-arm
        config need only carry the harness deltas (``subsets`` / prompt /
        tools) while the shared environment + dataset live in one base file.
        """
        merged = cls._load_layered(Path(path))
        for req in ("sweep_id", "subsets", "model", "embedding_model"):
            if req not in merged:
                raise ValueError(f"{path}: {req!r} is required")
        if not isinstance(merged["subsets"], dict):
            raise ValueError(f"{path}: 'subsets' must be a mapping")
        return cls(**merged)

    @staticmethod
    def _load_layered(path: Path) -> Dict[str, Any]:
        with path.open("r", encoding="utf-8") as f:
            payload = yaml.safe_load(f) or {}
        base_ref = payload.pop("base", None)
        if not base_ref:
            return payload
        base_path = Path(base_ref)
        if not base_path.is_absolute():
            base_path = (path.parent / base_path).resolve()
        merged = SweepConfig._load_layered(base_path)
        merged.update(payload)  # this arm's keys win over the base
        return merged


# Backwards-compatible alias — this dataclass was ``SampleConfig`` while it
# lived inside run_agent_sweep.py. Kept so external callers keep importing.
SampleConfig = SweepConfig
