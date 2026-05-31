# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``examples/experiment_runner.py``.

Covers the three pieces #148 adds on top of ``skill_agent_eval``:

* multi-model dispatch (short name -> litellm string, pass-through, overrides),
* the per-axis aggregation rule (mean-of-3 accuracy, min-of-3 cost/latency/turns),
* the :class:`MetricRow` logging schema (prompt/completion split, derived
  ``$/resolved instance``, wall-time P50/P95, ``cap_hit``).

Pure logic only -- no LLM / dataset imports, so this runs under the default
(unit) marker. The real ``(model, subset, instance)`` end-to-end path is a
``slow`` concern and is not exercised here.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

# Load examples/experiment_runner.py by path (``examples/`` is not a package).
# The module must be registered in ``sys.modules`` *before* ``exec_module`` so
# that ``@dataclass`` can resolve its own module namespace during processing.
_MODULE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "examples" / "experiment_runner.py"
)
_spec = importlib.util.spec_from_file_location("experiment_runner", _MODULE_PATH)
assert _spec and _spec.loader
experiment_runner = importlib.util.module_from_spec(_spec)
sys.modules["experiment_runner"] = experiment_runner
_spec.loader.exec_module(experiment_runner)

MODEL_REGISTRY = experiment_runner.MODEL_REGISTRY
MetricRow = experiment_runner.MetricRow
RepResult = experiment_runner.RepResult
aggregate_reps = experiment_runner.aggregate_reps
rep_from_agent_usage = experiment_runner.rep_from_agent_usage
resolve_model = experiment_runner.resolve_model


# ---------------------------------------------------------------------------
# 1. Multi-model dispatch
# ---------------------------------------------------------------------------


class TestModelDispatch:
    def test_registry_covers_six_required_models(self):
        """All six RFC models have a short name -> litellm mapping."""
        required = {"haiku", "opus", "gpt", "gemini", "qwen-32b", "qwen-14b"}
        assert required.issubset(MODEL_REGISTRY.keys())
        # Not hardcoded to a single model.
        assert len(set(MODEL_REGISTRY.values())) == len(MODEL_REGISTRY)

    def test_resolve_short_name(self):
        assert resolve_model("haiku") == MODEL_REGISTRY["haiku"]
        assert resolve_model("qwen-32b") == MODEL_REGISTRY["qwen-32b"]

    def test_resolve_passthrough_litellm_string(self):
        """A fully-qualified litellm string resolves to itself."""
        assert (
            resolve_model("vertex_ai/claude-haiku-4-5") == "vertex_ai/claude-haiku-4-5"
        )
        assert resolve_model("openai/gpt-4o") == "openai/gpt-4o"

    def test_resolve_overrides(self):
        """Per-call overrides win over the registry without mutating it."""
        before = dict(MODEL_REGISTRY)
        got = resolve_model(
            "qwen-32b", overrides={"qwen-32b": "hosted_vllm/local-qwen"}
        )
        assert got == "hosted_vllm/local-qwen"
        # Registry untouched.
        assert MODEL_REGISTRY == before

    def test_resolve_unknown_raises(self):
        with pytest.raises(KeyError):
            resolve_model("totally-unknown-bare-name")


# ---------------------------------------------------------------------------
# 2. Aggregation rule
# ---------------------------------------------------------------------------


def _rep(resolved, prompt, completion, cost, wall, turns, cap=False):
    return RepResult(
        resolved=resolved,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=cost,
        wall_time_s=wall,
        turn_count=turns,
        cap_hit=cap,
    )


class TestAggregation:
    def test_mean_of_three_accuracy(self):
        """Accuracy is the mean of ``resolved`` (2/3 here)."""
        reps = [
            _rep(True, 100, 50, 0.01, 5.0, 3),
            _rep(False, 120, 40, 0.02, 7.0, 4),
            _rep(True, 110, 60, 0.015, 6.0, 3),
        ]
        row = aggregate_reps(
            model="haiku", skill_subset=["bm25_search"], instance_id="x__1", reps=reps
        )
        assert row.accuracy == pytest.approx(2.0 / 3.0)
        assert row.rep_count == 3

    def test_min_of_three_cost_latency_turns(self):
        """Cost / tokens / turns take the minimum across reps (noise floor)."""
        reps = [
            _rep(True, 100, 50, 0.030, 9.0, 5),
            _rep(True, 90, 40, 0.020, 6.0, 3),
            _rep(True, 110, 70, 0.040, 8.0, 7),
        ]
        row = aggregate_reps(
            model="opus", skill_subset=["bm25_search"], instance_id="x__2", reps=reps
        )
        assert row.cost_usd == pytest.approx(0.020)
        assert row.prompt_tokens == 90
        assert row.completion_tokens == 40
        assert row.total_tokens == 130  # min of 150/130/180
        assert row.turn_count == 3

    def test_prompt_and_completion_reported_separately(self):
        """The min prompt and min completion are tracked independently."""
        reps = [
            _rep(True, 200, 10, 0.01, 1.0, 2),  # min prompt? no
            _rep(True, 50, 90, 0.02, 1.0, 2),  # min prompt = 50
        ]
        row = aggregate_reps(
            model="haiku", skill_subset=["a"], instance_id="i", reps=reps
        )
        assert row.prompt_tokens == 50  # not collapsed into completion
        assert row.completion_tokens == 10
        # They are distinct fields, not a single total.
        assert hasattr(row, "prompt_tokens") and hasattr(row, "completion_tokens")

    def test_single_rep_mean_and_min_collapse(self):
        """rep_count == 1 -> mean and min both equal the single observation."""
        reps = [_rep(True, 77, 33, 0.05, 4.2, 6, cap=True)]
        row = aggregate_reps(
            model="gemini", skill_subset=["a", "b"], instance_id="i", reps=reps
        )
        assert row.accuracy == 1.0
        assert row.prompt_tokens == 77
        assert row.completion_tokens == 33
        assert row.cost_usd == pytest.approx(0.05)
        assert row.turn_count == 6
        assert row.wall_time_p50 == pytest.approx(4.2)
        assert row.wall_time_p95 == pytest.approx(4.2)
        assert row.cap_hit is True

    def test_empty_reps_raises(self):
        with pytest.raises(ValueError):
            aggregate_reps(model="haiku", skill_subset=[], instance_id="i", reps=[])

    def test_cost_none_when_all_unpriced(self):
        reps = [
            _rep(True, 10, 5, None, 1.0, 2),
            _rep(False, 12, 6, None, 1.0, 2),
        ]
        row = aggregate_reps(
            model="qwen-14b", skill_subset=["a"], instance_id="i", reps=reps
        )
        assert row.cost_usd is None
        assert row.cost_per_resolved_instance is None

    def test_min_cost_ignores_unpriced_reps(self):
        """A None cost on one rep must not zero out the min."""
        reps = [
            _rep(True, 10, 5, None, 1.0, 2),
            _rep(True, 10, 5, 0.03, 1.0, 2),
        ]
        row = aggregate_reps(
            model="gpt", skill_subset=["a"], instance_id="i", reps=reps
        )
        assert row.cost_usd == pytest.approx(0.03)


class TestDerivedAndDistribution:
    def test_cost_per_resolved_instance(self):
        """$/resolved = min_cost / number of resolved reps."""
        reps = [
            _rep(True, 10, 5, 0.040, 1.0, 2),
            _rep(True, 10, 5, 0.020, 1.0, 2),
            _rep(False, 10, 5, 0.030, 1.0, 2),
        ]
        row = aggregate_reps(
            model="haiku", skill_subset=["a"], instance_id="i", reps=reps
        )
        # min cost = 0.020, resolved count = 2 -> 0.010
        assert row.cost_per_resolved_instance == pytest.approx(0.010)

    def test_cost_per_resolved_inf_when_none_resolved(self):
        reps = [
            _rep(False, 10, 5, 0.040, 1.0, 2),
            _rep(False, 10, 5, 0.020, 1.0, 2),
        ]
        row = aggregate_reps(
            model="haiku", skill_subset=["a"], instance_id="i", reps=reps
        )
        assert math.isinf(row.cost_per_resolved_instance)

    def test_wall_time_p50_p95(self):
        """P50 / P95 follow linear-interpolation percentiles of the reps."""
        reps = [
            _rep(True, 1, 1, 0.0, 2.0, 1),
            _rep(True, 1, 1, 0.0, 4.0, 1),
            _rep(True, 1, 1, 0.0, 6.0, 1),
            _rep(True, 1, 1, 0.0, 8.0, 1),
            _rep(True, 1, 1, 0.0, 10.0, 1),
        ]
        row = aggregate_reps(
            model="haiku", skill_subset=["a"], instance_id="i", reps=reps
        )
        # sorted [2,4,6,8,10]: p50 = 6, p95 = rank 3.8 -> 8 + 0.8*(10-8) = 9.6
        assert row.wall_time_p50 == pytest.approx(6.0)
        assert row.wall_time_p95 == pytest.approx(9.6)

    def test_cap_hit_rate(self):
        """cap_hit_rate = fraction of reps that hit the cap; cap_hit = any."""
        reps = [
            _rep(True, 1, 1, 0.0, 1.0, 1, cap=True),
            _rep(True, 1, 1, 0.0, 1.0, 1, cap=False),
            _rep(True, 1, 1, 0.0, 1.0, 1, cap=False),
        ]
        row = aggregate_reps(
            model="haiku", skill_subset=["a"], instance_id="i", reps=reps
        )
        assert row.cap_hit is True
        assert row.cap_hit_rate == pytest.approx(1.0 / 3.0)


# ---------------------------------------------------------------------------
# 3. Logging schema
# ---------------------------------------------------------------------------


class TestMetricRowSchema:
    def test_row_to_dict_has_required_fields(self):
        reps = [_rep(True, 100, 50, 0.01, 5.0, 3, cap=False)]
        row = aggregate_reps(
            model="haiku",
            skill_subset=["bm25_search", "graph_expand"],
            instance_id="astropy__astropy-12907",
            reps=reps,
        )
        d = row.to_dict()
        required = {
            "model",
            "model_resolved",
            "skill_subset",
            "instance_id",
            "rep_count",
            "accuracy",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cost_usd",
            "cost_per_resolved_instance",
            "wall_time_p50",
            "wall_time_p95",
            "turn_count",
            "cap_hit",
            "cap_hit_rate",
        }
        assert required.issubset(d.keys())
        # prompt and completion are not collapsed.
        assert d["prompt_tokens"] == 100
        assert d["completion_tokens"] == 50
        assert "tokens" not in d or d.get("total_tokens") == 150
        assert d["model_resolved"] == MODEL_REGISTRY["haiku"]
        assert isinstance(d["cap_hit"], bool)

    def test_model_resolved_override_passthrough(self):
        reps = [_rep(True, 1, 1, None, 1.0, 1)]
        row = aggregate_reps(
            model="custom-name",
            skill_subset=["a"],
            instance_id="i",
            reps=reps,
            model_resolved="vertex_ai/some-model",
        )
        assert row.model_resolved == "vertex_ai/some-model"

    def test_unknown_model_resolved_falls_back_to_raw(self):
        """An unknown bare model name does not crash row building."""
        reps = [_rep(True, 1, 1, None, 1.0, 1)]
        row = aggregate_reps(
            model="mystery", skill_subset=["a"], instance_id="i", reps=reps
        )
        assert row.model_resolved == "mystery"


# ---------------------------------------------------------------------------
# Bridge from skill_agent_eval usage dict
# ---------------------------------------------------------------------------


class TestRepFromAgentUsage:
    def test_extracts_separated_tokens_and_wall_time(self):
        usage = {
            "total_turns": 4,
            "total_duration_ms": 2500.0,
            "token_usage": {
                "prompt_tokens": 800,
                "completion_tokens": 120,
                "total_tokens": 920,
                "cost_usd": 0.0042,
            },
        }
        rep = rep_from_agent_usage(resolved=True, usage=usage, max_turns=10)
        assert rep.prompt_tokens == 800
        assert rep.completion_tokens == 120
        assert rep.cost_usd == pytest.approx(0.0042)
        assert rep.wall_time_s == pytest.approx(2.5)
        assert rep.turn_count == 4
        assert rep.cap_hit is False

    def test_cap_hit_true_when_turns_reach_max(self):
        usage = {"total_turns": 10, "total_duration_ms": 100.0, "token_usage": {}}
        rep = rep_from_agent_usage(resolved=False, usage=usage, max_turns=10)
        assert rep.cap_hit is True

    def test_handles_missing_usage(self):
        rep = rep_from_agent_usage(resolved=False, usage=None, max_turns=10)
        assert rep.prompt_tokens == 0
        assert rep.completion_tokens == 0
        assert rep.cost_usd is None
        assert rep.cap_hit is False


# ---------------------------------------------------------------------------
# End-to-end (real LLM) -- gated behind the ``slow`` marker per #148.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_real_model_dispatch_end_to_end():
    """Resolve a model, run one rep against a real provider, build a row.

    Skipped unless Vertex AI credentials are configured. This is the only path
    that issues a real LLM call; the aggregation / schema logic above is fully
    covered without one.
    """
    import os

    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        pytest.skip(
            "Vertex AI not configured (GOOGLE_APPLICATION_CREDENTIALS unset); "
            "real-LLM dispatch test requires credentials."
        )

    from codeminer.llm.litellm_chat import LiteLLMChat

    resolved = resolve_model("haiku")
    llm = LiteLLMChat(model=resolved, temperature=0.0, max_tokens=64)

    # One trivial single-rep run: no tools, deterministic at temp=0.
    from codeminer.agent.runner import AgentRunner
    from codeminer.agent.skills.registry import SkillRegistry

    SkillRegistry.reset()
    result = AgentRunner(llm=llm, registry=SkillRegistry(), max_turns=1).run(
        "Reply with the single word OK."
    )
    usage = {
        "total_turns": result.total_turns,
        "total_duration_ms": result.total_duration_ms,
        "token_usage": result.usage.to_dict() if result.usage else None,
    }
    rep = rep_from_agent_usage(resolved=True, usage=usage, max_turns=1)
    row = aggregate_reps(
        model="haiku",
        skill_subset=[],
        instance_id="smoke__1",
        reps=[rep],
    )
    assert row.model_resolved == resolved
    assert row.prompt_tokens > 0
    assert row.completion_tokens >= 0
    # prompt and completion stay separate.
    assert row.total_tokens == row.prompt_tokens + row.completion_tokens
