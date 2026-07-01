# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Slow tests for the LLM investigation step (mocked litellm, no API calls)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codeminer.guardian.llm_investigator import (
    build_test_failure_context,
    investigate_signal,
    investigate_with_llm,
)
from codeminer.guardian.signals import Hotspot
from codeminer.llm.litellm_chat import LiteLLMChat

_FAKE_FILE = "def run():\n    pass\n"
# Patch _read_hotspot_file so tests don't need real files on disk.
_PATCH_READ = patch(
    "codeminer.guardian.llm_investigator._read_hotspot_file", return_value=_FAKE_FILE
)

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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_investigate_with_llm_no_tool_calls():
    """When the model returns a final answer immediately, no tool loop runs."""
    llm = LiteLLMChat(model="claude-opus-4-8")
    retriever = _FakeRetriever()
    hotspot = Hotspot(path="codeminer/agent/runner.py", commit_count=42)

    final_response = _make_response(
        "This file is risky because it is the central dispatch hub."
    )

    with _PATCH_READ, patch(
        "litellm.completion", return_value=final_response
    ) as mock_completion:
        narrative = investigate_with_llm(
            hotspot,
            retriever,
            since="90 days ago",
            llm=llm,
            max_tool_rounds=3,
        )

    assert "risky" in narrative
    assert mock_completion.call_count == 1
    assert retriever.queries == []


@pytest.mark.slow
def test_investigate_with_llm_one_tool_round():
    """Model issues one search_code call, then writes the narrative."""
    llm = LiteLLMChat(model="claude-opus-4-8")
    retriever = _FakeRetriever()
    hotspot = Hotspot(path="codeminer/guardian/cycle.py", commit_count=15)

    tc = _make_tool_call("call_abc", "Guardian cycle orchestrator")
    tool_response = _make_response("", tool_calls=[tc])
    final_response = _make_response(
        "The cycle.py file couples index building and reporting."
    )

    with _PATCH_READ, patch(
        "litellm.completion", side_effect=[tool_response, final_response]
    ) as mock_completion:
        narrative = investigate_with_llm(
            hotspot,
            retriever,
            since="30 days ago",
            llm=llm,
            max_tool_rounds=3,
        )

    assert "cycle.py" in narrative or "couples" in narrative
    assert mock_completion.call_count == 2
    assert retriever.queries == ["Guardian cycle orchestrator"]

    # Second litellm call must include the tool result message
    second_call_messages = mock_completion.call_args_list[1][1]["messages"]
    tool_result_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_result_msgs) == 1
    assert tool_result_msgs[0]["tool_call_id"] == "call_abc"


@pytest.mark.slow
def test_investigate_with_llm_exhausts_rounds():
    """When max_tool_rounds is exhausted, a final no-tool call is forced."""
    llm = LiteLLMChat(model="claude-opus-4-8")
    retriever = _FakeRetriever()
    hotspot = Hotspot(path="codeminer/index/bm25.py", commit_count=7)

    tc = _make_tool_call("call_x", "bm25 index")
    tool_response = _make_response("", tool_calls=[tc])
    final_response = _make_response("BM25 index has stability concerns.")

    with _PATCH_READ, patch(
        "litellm.completion",
        side_effect=[tool_response, final_response],
    ):
        narrative = investigate_with_llm(
            hotspot,
            retriever,
            since="60 days ago",
            llm=llm,
            max_tool_rounds=1,
        )

    assert "BM25" in narrative


@pytest.mark.slow
def test_investigate_with_llm_retriever_error_does_not_crash():
    """A retriever that throws must not propagate — the tool result is an error message."""
    llm = LiteLLMChat(model="claude-opus-4-8")

    class _Boom:
        def query(self, query, top_k=None):
            raise RuntimeError("index gone")

    hotspot = Hotspot(path="a.py", commit_count=3)
    tc = _make_tool_call("call_y", "something")
    tool_response = _make_response("", tool_calls=[tc])
    final_response = _make_response("Found nothing due to index error.")

    with _PATCH_READ, patch(
        "litellm.completion", side_effect=[tool_response, final_response]
    ):
        narrative = investigate_with_llm(
            hotspot,
            _Boom(),
            since="7 days ago",
            llm=llm,
            max_tool_rounds=3,
        )

    assert narrative


@pytest.mark.slow
def test_investigate_with_llm_llm_error_returns_message():
    """An LLM API error on the first call should return a graceful error string."""
    llm = LiteLLMChat(model="claude-opus-4-8")
    retriever = _FakeRetriever()
    hotspot = Hotspot(path="b.py", commit_count=1)

    with _PATCH_READ, patch(
        "litellm.completion", side_effect=RuntimeError("network down")
    ):
        narrative = investigate_with_llm(
            hotspot,
            retriever,
            since="7 days ago",
            llm=llm,
        )

    assert "unavailable" in narrative.lower() or narrative == ""


@pytest.mark.slow
def test_investigate_with_llm_file_content_in_prompt():
    """File content must appear in the first user message sent to the LLM."""
    llm = LiteLLMChat(model="claude-opus-4-8")
    retriever = _FakeRetriever()
    hotspot = Hotspot(path="codeminer/languages.py", commit_count=5)

    final_response = _make_response("File analysis complete.")
    fake_content = "def chunker_languages():\n    return ['python']\n"

    with patch(
        "codeminer.guardian.llm_investigator._read_hotspot_file",
        return_value=fake_content,
    ), patch("litellm.completion", return_value=final_response) as mock_completion:
        investigate_with_llm(
            hotspot,
            retriever,
            repo_path="/repo",
            since="90 days ago",
            llm=llm,
            max_tool_rounds=0,
        )

    first_call = mock_completion.call_args_list[0]
    messages = first_call[1]["messages"]
    user_msg = next(m for m in messages if m["role"] == "user")
    assert "chunker_languages" in user_msg["content"]
    assert "```" in user_msg["content"]


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
