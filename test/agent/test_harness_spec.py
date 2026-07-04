# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the declarative agent harness contract."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codeminer.agent.harness import AgentHarnessSpec
from codeminer.agent.runner import AgentRunner
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.llm.litellm_chat import LiteLLMChat


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
    assert runner.system_prompt.startswith("Subagent prompt")
    assert spec.max_turns == 6
