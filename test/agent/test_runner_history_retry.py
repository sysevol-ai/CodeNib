# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""AgentRunner wiring for the #109 retry + context-budget knobs.

Verifies the runner threads ``retry`` into the LiteLLMChat it builds and
selects a TokenBudgetedChatHistory when ``max_context_tokens`` is set.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codeminer.agent.history import PlainChatHistory, TokenBudgetedChatHistory
from codeminer.agent.runner import AgentRunner
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.llm.litellm_chat import LiteLLMChat, RetryConfig


def _make_response(content=None, tool_calls=None):
    msg = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


@pytest.fixture(autouse=True)
def _reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


def test_retry_threaded_into_built_llm():
    """When constructed via model=, the runner passes retry into LiteLLMChat."""
    retry = RetryConfig(max_retries=5, base_delay=0.25)
    runner = AgentRunner(model="gpt-4o", registry=SkillRegistry(), retry=retry)
    assert isinstance(runner.llm, LiteLLMChat)
    assert runner.llm.retry is retry


def test_default_retry_present_when_built_from_model():
    runner = AgentRunner(model="gpt-4o", registry=SkillRegistry())
    assert isinstance(runner.llm.retry, RetryConfig)


def test_new_history_plain_by_default():
    llm = MagicMock(spec=LiteLLMChat)
    runner = AgentRunner(llm, SkillRegistry())
    assert isinstance(runner._new_history(), PlainChatHistory)


def test_new_history_budgeted_when_max_context_tokens_set():
    llm = MagicMock(spec=LiteLLMChat)
    llm.model = "gpt-4o"
    runner = AgentRunner(llm, SkillRegistry(), max_context_tokens=5000)
    hist = runner._new_history()
    assert isinstance(hist, TokenBudgetedChatHistory)
    assert hist.max_tokens == 5000
    assert hist.model == "gpt-4o"


def test_run_uses_budgeted_history_and_still_answers():
    """A budgeted run still returns the final answer (smoke through run())."""
    llm = MagicMock(spec=LiteLLMChat)
    llm.model = "gpt-4o"
    llm._call_raw.return_value = _make_response(content="final answer")

    runner = AgentRunner(llm, SkillRegistry(), max_context_tokens=10_000)
    result = runner.run("hello")

    assert result.answer == "final answer"
    # system + user + assistant present (budget high enough to keep all).
    roles = [m["role"] for m in result.messages]
    assert roles[0] == "system"
    assert "user" in roles
