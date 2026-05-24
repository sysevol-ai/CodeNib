# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the agent runner (codeminer.agent.runner)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codeminer.agent.agent_types import AgentResult
from codeminer.agent.runner import AgentRunner, _serialize_result
from codeminer.agent.skills.core import (
    SkillInputSpec,
    SkillMetadata,
    SkillOutputSpec,
    SkillType,
)
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.llm.litellm_chat import LiteLLMChat

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm():
    """Return a mock LiteLLMChat."""
    return MagicMock(spec=LiteLLMChat)


def _make_response(content=None, tool_calls=None):
    """Build a fake litellm response object."""
    msg = SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


def _make_tool_call(tc_id, name, arguments_json):
    """Build a fake tool call object."""
    return SimpleNamespace(
        id=tc_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments_json),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


@pytest.fixture()
def echo_registry():
    """Registry with a simple echo skill."""
    reg = SkillRegistry()
    reg.register(
        SkillMetadata(
            skill_id="echo",
            skill_type=SkillType.CUSTOM,
            inputs=[SkillInputSpec(name="text", type_hint="str", required=True)],
            outputs=SkillOutputSpec(type_hint="str"),
            executor_fn=lambda text: f"echo: {text}",
        )
    )
    return reg


# ---------------------------------------------------------------------------
# Runner tests
# ---------------------------------------------------------------------------


class TestAgentRunner:
    def test_direct_answer_no_tools(self):
        """LLM answers directly without tool calls → 1 turn, no tool records."""
        llm = _make_llm()
        llm._call_raw.return_value = _make_response(content="The answer is 42.")

        runner = AgentRunner(llm, SkillRegistry())
        result = runner.run("What is the answer?")

        assert isinstance(result, AgentResult)
        assert result.answer == "The answer is 42."
        assert result.total_turns == 1
        assert result.tool_calls == []
        llm._call_raw.assert_called_once()

    def test_single_tool_call(self, echo_registry):
        """LLM calls one tool, then gives a final answer."""
        llm = _make_llm()

        # Turn 1: LLM requests tool call
        tc = _make_tool_call("call_1", "echo", '{"text": "hello"}')
        # Turn 2: LLM gives final answer
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="Done. The echo said hello."),
        ]

        runner = AgentRunner(llm, echo_registry)
        result = runner.run("Echo hello")

        assert result.answer == "Done. The echo said hello."
        assert result.total_turns == 2
        assert len(result.tool_calls) == 1

        record = result.tool_calls[0]
        assert record.skill_id == "echo"
        assert record.arguments == {"text": "hello"}
        assert record.result == "echo: hello"
        assert record.error is None

    def test_multiple_tool_calls_in_one_response(self, echo_registry):
        """LLM returns multiple tool calls in a single response."""
        llm = _make_llm()

        tc1 = _make_tool_call("call_1", "echo", '{"text": "a"}')
        tc2 = _make_tool_call("call_2", "echo", '{"text": "b"}')
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc1, tc2]),
            _make_response(content="Got both."),
        ]

        runner = AgentRunner(llm, echo_registry)
        result = runner.run("Echo a and b")

        assert len(result.tool_calls) == 2
        assert result.tool_calls[0].result == "echo: a"
        assert result.tool_calls[1].result == "echo: b"

    def test_max_turns_limit(self, echo_registry):
        """Runner stops after max_turns even if LLM keeps calling tools."""
        llm = _make_llm()
        tc = _make_tool_call("call_x", "echo", '{"text": "loop"}')
        # Always return a tool call, never a final answer
        llm._call_raw.return_value = _make_response(tool_calls=[tc])

        runner = AgentRunner(llm, echo_registry, max_turns=3)
        result = runner.run("loop forever")

        assert result.total_turns == 3
        assert len(result.tool_calls) == 3

    def test_unknown_skill_returns_error(self):
        """Tool call for unregistered skill records an error."""
        llm = _make_llm()
        tc = _make_tool_call("call_1", "nonexistent", "{}")
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="Failed."),
        ]

        runner = AgentRunner(llm, SkillRegistry())
        result = runner.run("test")

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].error is not None
        assert "not available" in result.tool_calls[0].error

    def test_executor_exception_handled(self):
        """Executor that raises returns error in tool response."""
        reg = SkillRegistry()
        reg.register(
            SkillMetadata(
                skill_id="fail",
                skill_type=SkillType.CUSTOM,
                inputs=[],
                executor_fn=lambda: (_ for _ in ()).throw(ValueError("boom")),
            )
        )

        llm = _make_llm()
        tc = _make_tool_call("call_1", "fail", "{}")
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="It failed."),
        ]

        runner = AgentRunner(llm, reg)
        result = runner.run("fail")

        assert result.tool_calls[0].error is not None
        assert "boom" in result.tool_calls[0].error

    def test_tool_messages_appended(self, echo_registry):
        """Verify tool-role messages are appended to conversation."""
        llm = _make_llm()
        tc = _make_tool_call("call_1", "echo", '{"text": "hi"}')
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="Done."),
        ]

        runner = AgentRunner(llm, echo_registry)
        result = runner.run("hi")

        tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_1"
        assert "echo: hi" in tool_msgs[0]["content"]

    def test_exclude_skills(self, echo_registry):
        """Excluded sweep-variable skills should not appear in tool schemas.

        Note: default skills (DEFAULT_SKILL_IDS) are NEVER excluded — the
        runner strips them from the exclude set before building the tool list.
        """
        from codeminer.agent.skills.defaults import DEFAULT_SKILL_IDS

        llm = _make_llm()
        llm._call_raw.return_value = _make_response(content="ok")

        runner = AgentRunner(llm, echo_registry, exclude_skills={"echo"})
        tool_names = {t["function"]["name"] for t in runner.tools}
        # echo is excluded
        assert "echo" not in tool_names
        # defaults are always present
        assert DEFAULT_SKILL_IDS.issubset(tool_names)


# ---------------------------------------------------------------------------
# Serialization tests
# ---------------------------------------------------------------------------


class TestSerializeResult:
    def test_string_passthrough(self):
        assert _serialize_result("hello") == "hello"

    def test_list_of_dicts(self):
        result = _serialize_result([{"a": 1}, {"b": 2}])
        assert '"a": 1' in result

    def test_pydantic_model(self):
        from pydantic import BaseModel

        class Foo(BaseModel):
            x: int = 1

        result = _serialize_result(Foo())
        assert '"x": 1' in result

    def test_list_of_pydantic(self):
        from pydantic import BaseModel

        class Bar(BaseModel):
            v: str = "ok"

        result = _serialize_result([Bar(), Bar()])
        assert '"v": "ok"' in result

    def test_truncation(self):
        long = "x" * 20000
        result = _serialize_result(long)
        assert len(result) <= 16100  # _MAX_RESULT_CHARS + small overhead
        assert "truncated" in result
