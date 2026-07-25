# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the declarative agent harness contract."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codenib.agent.agent_types import AgentResult
from codenib.agent.harness import (
    AgentHarnessSpec,
    AgentRunAccumulator,
    run_agent_in_directory,
)
from codenib.agent.runner import AgentRunner
from codenib.agent.skills.registry import SkillRegistry
from codenib.llm.litellm_chat import LiteLLMChat
from codenib.llm.usage import TokenUsage


@pytest.fixture(autouse=True)
def _reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


def test_harness_spec_normalizes_runner_collections():
    spec = AgentHarnessSpec(
        allow_skills=[" embedding_search ", "bm25_search", "bm25_search"],
        exclude_skills=[],
        default_tool_ids=[],
    )

    assert spec.allow_skills == frozenset({"embedding_search", "bm25_search"})
    assert spec.exclude_skills is None
    assert spec.default_tool_ids is None


def test_harness_spec_preserves_empty_allow_set_for_runner_warning():
    spec = AgentHarnessSpec(allow_skills=[])

    assert spec.allow_skills == frozenset()
    assert spec.to_runner_kwargs()["allow_skills"] == set()


def test_harness_spec_does_not_force_localization_contract_by_default():
    spec = AgentHarnessSpec()

    assert not spec.force_localization_contract
    assert not spec.to_runner_kwargs()["force_localization_contract"]


def test_runner_kwargs_are_copied_from_immutable_spec():
    spec = AgentHarnessSpec(
        allow_skills=["bm25_search"],
        default_tool_ids=["read"],
    )

    kwargs = spec.to_runner_kwargs()
    kwargs["allow_skills"].add("embedding_search")
    kwargs["default_tool_ids"].add("grep")

    assert spec.allow_skills == frozenset({"bm25_search"})
    assert spec.default_tool_ids == frozenset({"read"})


def test_with_overrides_revalidates_and_rejects_unknown_options():
    spec = AgentHarnessSpec(max_turns=4)

    assert spec.with_overrides(max_turns=2).max_turns == 2
    with pytest.raises(ValueError, match="max_turns"):
        spec.with_overrides(max_turns=0)
    with pytest.raises(ValueError, match="lsp_route_top_k"):
        spec.with_overrides(lsp_route_top_k=0)
    with pytest.raises(ValueError, match="seed_policy"):
        spec.with_overrides(lsp_route_seed_policy="benchmark_magic")
    with pytest.raises(TypeError, match="Unknown harness option"):
        spec.with_overrides(experiment_label="baseline")


def test_create_runner_uses_spec_without_mutating_it():
    llm = MagicMock(spec=LiteLLMChat)
    session_ctx = SimpleNamespace(repo_path=None)
    spec = AgentHarnessSpec(
        max_turns=6,
        allow_skills=["echo"],
        include_default_tools=False,
        first_turn_tool_choice="required",
        compact_after_read=True,
        compact_keep_reads=1,
        enable_lsp_route_context=True,
        lsp_route_seed_limit=4,
        lsp_route_seed_policy="specific",
        lsp_route_query_fallback=True,
        lsp_route_top_k=9,
        lsp_route_include_neighbors=False,
    )

    runner = spec.create_runner(
        llm=llm,
        registry=SkillRegistry(),
        session_ctx=session_ctx,
        max_turns=2,
        system_prompt="Subagent prompt",
    )

    assert isinstance(runner, AgentRunner)
    assert runner.max_turns == 2
    assert runner._base_allow == {"echo"}
    assert runner._include_defaults is False
    assert runner.first_turn_tool_choice == "required"
    assert runner._compact_after_read is True
    assert runner._compact_keep_reads == 1
    assert runner._enable_lsp_route_context is True
    assert runner._lsp_route_seed_limit == 4
    assert runner._lsp_route_seed_policy == "specific"
    assert runner._lsp_route_query_fallback is True
    assert runner._lsp_route_top_k == 9
    assert runner._lsp_route_include_neighbors is False
    assert runner.system_prompt.startswith("Subagent prompt")
    assert spec.max_turns == 6


def test_run_agent_in_directory_scopes_and_restores_cwd(tmp_path):
    class CwdRunner:
        def run(self, prompt):
            return prompt, Path.cwd()

    previous = Path.cwd()

    result = run_agent_in_directory(CwdRunner(), "locate auth", tmp_path)

    assert result == ("locate auth", tmp_path)
    assert Path.cwd() == previous


def test_run_accumulator_sums_usage_mappings_and_turns():
    acc = AgentRunAccumulator()

    acc.add_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "cache_read_input_tokens": 3,
        }
    )
    acc.add_usage(
        {
            "token_usage": {
                "prompt_tokens": 5,
                "completion_tokens": 1,
                "total_tokens": 6,
                "cost_usd": 0.02,
            }
        }
    )
    acc.add_turns(2)
    acc.add_turns(3)

    assert acc.usage_totals() == {
        "prompt_tokens": 15,
        "completion_tokens": 5,
        "total_tokens": 20,
        "cost_usd": 0.02,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": None,
    }
    assert acc.total_turns(fallback=99) == 5


def test_run_accumulator_adds_agent_result_usage():
    acc = AgentRunAccumulator()
    result = AgentResult(
        answer="done",
        total_turns=4,
        usage=TokenUsage(
            prompt_tokens=12,
            completion_tokens=8,
            total_tokens=20,
            cost_usd=0.05,
            cache_creation_input_tokens=2,
        ),
    )

    acc.add_result(result)

    totals = acc.usage_totals()
    assert totals["prompt_tokens"] == 12
    assert totals["completion_tokens"] == 8
    assert totals["total_tokens"] == 20
    assert totals["cost_usd"] == 0.05
    assert totals["cache_creation_input_tokens"] == 2
    assert acc.total_turns() == 4


def test_empty_run_accumulator_reports_missing_usage():
    acc = AgentRunAccumulator()

    assert acc.usage_totals() == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
    }
    assert acc.total_turns(fallback=7) == 7
