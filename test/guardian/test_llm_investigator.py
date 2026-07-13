# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Slow tests for the LLM investigation step (mocked litellm, no API calls)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codeminer.guardian.llm_investigator import build_test_failure_context, investigate_signal
from codeminer.llm.litellm_chat import LiteLLMChat

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_call(call_id: str, query: str) -> MagicMock:
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = "search_code"
    tc.function.arguments = json.dumps({"query": query})
    return tc


def _make_response(content: str, tool_calls=None) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


class _FakeRetriever:
    def __init__(self):
        self.queries: list = []

    def query(self, query: str, top_k=None) -> list:
        self.queries.append(query)
        node = SimpleNamespace(
            file="result.py",
            node_name="my_func",
            type="function",
            start_line=10,
            end_line=20,
            score=0.9,
        )
        return [node]


# ---------------------------------------------------------------------------
# investigate_signal + build_test_failure_context
# ---------------------------------------------------------------------------


def test_build_test_failure_context_includes_all_sections():
    """Context string must contain nodeid, error, test source, and source file."""
    ctx = build_test_failure_context(
        "test/guardian/test_cycle.py::test_foo",
        "AssertionError: expected 1 got 0",
        test_content="def test_foo():\n    assert run() == 1\n",
        source_content="def run():\n    return 0\n",
        source_path="codeminer/guardian/cycle.py",
    )
    assert "test/guardian/test_cycle.py::test_foo" in ctx
    assert "AssertionError" in ctx
    assert "def test_foo" in ctx
    assert "def run" in ctx
    assert "codeminer/guardian/cycle.py" in ctx


def test_build_test_failure_context_no_source():
    """Context must degrade gracefully when source file can't be inferred."""
    ctx = build_test_failure_context(
        "test/foo.py::test_bar",
        "SomeError",
        test_content="def test_bar(): pass\n",
    )
    assert "test/foo.py::test_bar" in ctx
    assert "def test_bar" in ctx


@pytest.mark.slow
def test_investigate_signal_passes_context_to_llm():
    """investigate_signal must send the caller-supplied context as the user message."""
    llm = LiteLLMChat(model="claude-opus-4-8")
    retriever = _FakeRetriever()
    ctx = "Failing test: test/foo.py::test_bar\n\nError: AssertionError\n"

    final_response = _make_response("Root cause is a missing return statement.")

    with patch("litellm.completion", return_value=final_response) as mock_completion:
        narrative = investigate_signal(ctx, retriever, llm=llm, max_tool_rounds=0)

    assert "return" in narrative
    first_call = mock_completion.call_args_list[0]
    messages = first_call[1]["messages"]
    user_msg = next(m for m in messages if m["role"] == "user")
    assert "test/foo.py::test_bar" in user_msg["content"]


@pytest.mark.slow
def test_investigate_signal_tool_round():
    """investigate_signal must run the same tool-use loop as investigate_with_llm."""
    llm = LiteLLMChat(model="claude-opus-4-8")
    retriever = _FakeRetriever()
    tc = _make_tool_call("call_t1", "run function caller")
    tool_response = _make_response("", tool_calls=[tc])
    final_response = _make_response("The run() function is missing a return value.")

    with patch(
        "litellm.completion", side_effect=[tool_response, final_response]
    ) as mock_completion:
        narrative = investigate_signal(
            "Failing test: test/foo.py::test_bar\n",
            retriever,
            llm=llm,
            max_tool_rounds=3,
        )

    assert "run" in narrative
    assert mock_completion.call_count == 2
    assert retriever.queries == ["run function caller"]
