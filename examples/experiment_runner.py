#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Experiment runner: ``(model, skill_subset, instance, rep_count)`` -> metric row.

The unified Phase 0 / Phase 2 driver for the Agent Router RFC (#133, sub-issue
#148). ``examples/skill_agent_eval.py`` already runs ``AgentRunner`` over an
arbitrary skill subset with a ``UsageTracker`` that keeps *prompt* and
*completion* tokens separate. This module adds the three pieces that were
missing:

1. **Multi-model dispatch** -- :data:`MODEL_REGISTRY` maps short, stable names
   (``haiku``, ``opus``, ``gpt``, ``gemini``, ``qwen-32b``, ``qwen-14b``) to
   litellm model strings, so a sweep cell can say ``--model haiku`` instead of
   hardcoding a provider path. :func:`resolve_model` also accepts a raw
   litellm string (anything containing ``/`` or starting with a known prefix)
   and passes it through untouched.

2. **Aggregation per metric axis** (per #133 v2 Table 4):

   * *accuracy* -- **mean-of-3** across repetitions (or 1 run at ``temp=0``).
   * *cost / latency / turns* -- **min-of-3**: the noise on these axes is
     one-sided (a slow/expensive run can only ever add overhead), so the
     minimum across reps approximates the noise floor.

   ``rep_count`` is configurable; with ``rep_count == 1`` mean and min both
   collapse to the single observed value.

3. **Logging schema** -- :class:`MetricRow` reports ``prompt_tokens`` and
   ``completion_tokens`` **separately** (the RFC's core motivation: a router
   that trades completion tokens for prompt tokens is invisible if the two are
   collapsed), the derived ``cost_per_resolved_instance``, ``wall_time`` P50 /
   P95, ``turn_count``, and a ``cap_hit`` boolean (did the run exhaust
   ``max_turns``?).

This module deliberately stays small and dependency-light: the aggregation and
schema logic are pure functions of per-rep records, unit-tested without any LLM
call. The real ``(model, subset, instance)`` execution path reuses
``skill_agent_eval.evaluate_instance`` and is exercised only under the ``slow``
marker.

Out of scope (per #148): the CAR runtime and the third-party baseline harness.
CAR is just one more ``subset`` value once it lands; the baseline harness is a
separate sub-issue.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# 1. Multi-model dispatch
# ---------------------------------------------------------------------------

# Short, stable experiment names -> litellm model strings. The values follow
# the provider conventions already used across the repo (``vertex_ai/...`` for
# Anthropic + Gemini on Vertex, ``openai/...`` for OpenAI, an ``openai/``-shaped
# path against a local vLLM ``--api-base`` for the open Qwen weights). Override
# any single entry via :func:`resolve_model`'s ``overrides`` argument or extend
# the dict for a new provider -- the harness never assumes a single model.
MODEL_REGISTRY: Dict[str, str] = {
    "haiku": "vertex_ai/claude-haiku-4-5",
    "opus": "vertex_ai/claude-opus-4-1",
    "gpt": "openai/gpt-5.5",
    "gemini": "vertex_ai/gemini-3-pro",
    "qwen-32b": "openai/Qwen/Qwen2.5-Coder-32B-Instruct",
    "qwen-14b": "openai/Qwen/Qwen2.5-Coder-14B-Instruct",
}

# litellm provider prefixes that mark a value as already being a fully-qualified
# model string (so it is passed through verbatim rather than treated as a short
# name to look up).
_LITELLM_PREFIXES = (
    "vertex_ai/",
    "openai/",
    "anthropic/",
    "gemini/",
    "azure/",
    "bedrock/",
    "hosted_vllm/",
    "ollama/",
)


def resolve_model(
    model: str,
    overrides: Optional[Dict[str, str]] = None,
) -> str:
    """Resolve a short experiment name to a litellm model string.

    A value that already looks like a litellm model string (contains ``/`` or
    starts with a known provider prefix) is returned unchanged, so callers can
    pass either ``"haiku"`` or ``"vertex_ai/claude-haiku-4-5"``.

    Args:
        model: Short registry key (``haiku`` / ``opus`` / ...) or a raw
            litellm model string.
        overrides: Optional per-call overrides layered on top of
            :data:`MODEL_REGISTRY` (e.g. to point ``qwen-32b`` at a specific
            local deployment without mutating the module-level dict).

    Returns:
        A litellm-ready model string.

    Raises:
        KeyError: If ``model`` is neither a known short name nor a recognizable
            litellm string.
    """
    table = dict(MODEL_REGISTRY)
    if overrides:
        table.update(overrides)

    if model in table:
        return table[model]

    # Already a fully-qualified litellm string -> pass through.
    if "/" in model or model.startswith(_LITELLM_PREFIXES):
        return model

    raise KeyError(
        f"Unknown model {model!r}. Known short names: {sorted(table)}; "
        "or pass a litellm model string (e.g. 'vertex_ai/claude-haiku-4-5')."
    )


# ---------------------------------------------------------------------------
# 2. Per-rep record + aggregation
# ---------------------------------------------------------------------------


@dataclass
class RepResult:
    """One repetition of ``(model, subset, instance)``.

    The raw, *per-run* observation that aggregation reduces over. ``resolved``
    is the binary outcome whose mean across reps is the accuracy axis;
    everything else is a cost/latency/turn observation whose minimum across reps
    approximates the floor.
    """

    resolved: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Optional[float] = None
    wall_time_s: float = 0.0
    turn_count: int = 0
    cap_hit: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _min(values: Sequence[float]) -> float:
    return min(values) if values else 0.0


def _percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile (``pct`` in ``[0, 100]``).

    NumPy-free so the harness's pure-logic layer carries no heavy import. For a
    single observation, returns that observation; matches ``numpy.percentile``
    on the tested fixtures.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


# ---------------------------------------------------------------------------
# 3. Logging schema
# ---------------------------------------------------------------------------


@dataclass
class MetricRow:
    """One logged row for a ``(model, subset, instance)`` cell.

    Token counts stay split (``prompt_tokens`` vs ``completion_tokens``) -- the
    RFC's whole point is that a good router shifts work between the two, which a
    collapsed ``total`` would hide. Accuracy is mean-of-rep; cost / latency /
    turns are min-of-rep; ``cap_hit_rate`` is the fraction of reps that
    exhausted ``max_turns``.
    """

    model: str
    model_resolved: str
    skill_subset: List[str]
    instance_id: str
    rep_count: int

    # accuracy axis: mean across reps
    accuracy: float = 0.0

    # cost / latency / turn axes: min across reps (noise floor)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Optional[float] = None

    # derived: cost amortized over resolved instances (inf when none resolved)
    cost_per_resolved_instance: Optional[float] = None

    # wall-time distribution across reps
    wall_time_p50: float = 0.0
    wall_time_p95: float = 0.0

    turn_count: int = 0
    cap_hit: bool = False
    cap_hit_rate: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def aggregate_reps(
    *,
    model: str,
    skill_subset: Sequence[str],
    instance_id: str,
    reps: Sequence[RepResult],
    model_resolved: Optional[str] = None,
) -> MetricRow:
    """Reduce per-rep records into a single :class:`MetricRow`.

    Aggregation rule (per #133 v2 Table 4):

    * **accuracy** = mean of ``resolved`` over reps.
    * **cost / latency / turns** = min over reps (one-sided noise -> the min
      approximates the floor of the distribution).
    * **wall_time** keeps the P50 / P95 of the per-rep wall times so the tail is
      visible even though the headline latency is the min.
    * **cost_per_resolved_instance** divides the (min) cost by the number of
      resolved reps; ``None`` when cost is unpriced, and ``inf`` when the cell
      resolved nothing (so a never-solving cell is not silently free).

    Args:
        model: The experiment-level model name (short or litellm string).
        skill_subset: The skill allowlist this cell ran with.
        instance_id: SWE-bench instance identifier.
        reps: One or more :class:`RepResult`. With a single rep, mean and min
            both collapse to that rep's value.
        model_resolved: Optional already-resolved litellm string (defaults to
            :func:`resolve_model` applied to ``model``).

    Returns:
        A populated :class:`MetricRow`.

    Raises:
        ValueError: If ``reps`` is empty.
    """
    if not reps:
        raise ValueError("aggregate_reps requires at least one RepResult")

    rep_count = len(reps)

    # accuracy: mean-of-N
    accuracy = _mean([1.0 if r.resolved else 0.0 for r in reps])

    # cost / latency / turns: min-of-N (noise floor)
    min_prompt = int(_min([r.prompt_tokens for r in reps]))
    min_completion = int(_min([r.completion_tokens for r in reps]))
    min_total = int(_min([r.total_tokens for r in reps]))
    min_turns = int(_min([r.turn_count for r in reps]))

    costed = [r.cost_usd for r in reps if r.cost_usd is not None]
    min_cost: Optional[float] = min(costed) if costed else None

    # wall-time distribution
    wall_times = [r.wall_time_s for r in reps]
    wt_p50 = _percentile(wall_times, 50.0)
    wt_p95 = _percentile(wall_times, 95.0)

    # cap_hit: True if any rep hit the cap; cap_hit_rate is the fraction.
    cap_hits = [r.cap_hit for r in reps]
    cap_hit_rate = _mean([1.0 if c else 0.0 for c in cap_hits])
    cap_hit = any(cap_hits)

    # derived $ / resolved instance
    resolved_count = sum(1 for r in reps if r.resolved)
    cost_per_resolved: Optional[float]
    if min_cost is None:
        cost_per_resolved = None
    elif resolved_count == 0:
        cost_per_resolved = float("inf")
    else:
        cost_per_resolved = min_cost / resolved_count

    return MetricRow(
        model=model,
        model_resolved=(
            model_resolved if model_resolved is not None else _safe_resolve(model)
        ),
        skill_subset=list(skill_subset),
        instance_id=instance_id,
        rep_count=rep_count,
        accuracy=accuracy,
        prompt_tokens=min_prompt,
        completion_tokens=min_completion,
        total_tokens=min_total,
        cost_usd=min_cost,
        cost_per_resolved_instance=cost_per_resolved,
        wall_time_p50=wt_p50,
        wall_time_p95=wt_p95,
        turn_count=min_turns,
        cap_hit=cap_hit,
        cap_hit_rate=cap_hit_rate,
    )


def _safe_resolve(model: str) -> str:
    """``resolve_model`` that never raises (falls back to the raw name)."""
    try:
        return resolve_model(model)
    except KeyError:
        return model


# ---------------------------------------------------------------------------
# Bridging the agent run -> a RepResult
# ---------------------------------------------------------------------------


def rep_from_agent_usage(
    *,
    resolved: bool,
    usage: Optional[Dict[str, Any]],
    max_turns: int,
) -> RepResult:
    """Build a :class:`RepResult` from one ``skill_agent_eval`` usage dict.

    ``usage`` is the ``usage_stats`` dict that
    ``skill_agent_eval.run_agent_with_skills`` returns: ``total_turns``,
    ``total_duration_ms`` and a nested ``token_usage`` (the
    :meth:`TokenUsage.to_dict` shape). ``cap_hit`` is derived by comparing
    ``total_turns`` to ``max_turns`` -- the agent loop reports
    ``total_turns == max_turns`` exactly when the budget was exhausted.

    Kept import-light (no LLM / dataset imports) so the slow end-to-end caller
    can construct rows without pulling this module into the unit-test path.
    """
    usage = usage or {}
    token_usage = usage.get("token_usage") or {}
    total_turns = int(usage.get("total_turns") or 0)
    duration_ms = float(usage.get("total_duration_ms") or 0.0)

    return RepResult(
        resolved=resolved,
        prompt_tokens=int(token_usage.get("prompt_tokens") or 0),
        completion_tokens=int(token_usage.get("completion_tokens") or 0),
        cost_usd=token_usage.get("cost_usd"),
        wall_time_s=duration_ms / 1000.0,
        turn_count=total_turns,
        cap_hit=total_turns >= max_turns > 0,
    )


@dataclass
class ExperimentReport:
    """Container for a sweep's worth of :class:`MetricRow` records."""

    rows: List[MetricRow] = field(default_factory=list)

    def add(self, row: MetricRow) -> None:
        self.rows.append(row)

    def to_dict(self) -> Dict[str, Any]:
        return {"rows": [r.to_dict() for r in self.rows]}
