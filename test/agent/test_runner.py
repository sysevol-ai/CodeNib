# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the agent runner (codenib.agent.runner)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codenib.agent.agent_types import AgentResult
from codenib.agent.runner import AgentRunner, _serialize_result
from codenib.agent.skills.core import (
    SkillInputSpec,
    SkillMetadata,
    SkillOutputSpec,
    SkillType,
)
from codenib.agent.skills.registry import SkillRegistry
from codenib.llm.litellm_chat import LiteLLMChat

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

    def test_partial_contract_answer_forces_schema_turn(self, echo_registry):
        """A Files-only answer is not enough for span scoring; force schema."""
        llm = _make_llm()
        tc = _make_tool_call("call_1", "echo", '{"text": "hello"}')
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="Files: src/a.py"),
            _make_response(
                content=(
                    "Files: src/a.py\n"
                    "Symbols: src/a.py:foo\n"
                    "Locations: src/a.py:10-20"
                )
            ),
        ]

        runner = AgentRunner(
            llm,
            echo_registry,
            force_localization_contract=True,
        )
        result = runner.run("Echo hello")

        assert result.total_turns == 2
        assert result.answer.endswith("Locations: src/a.py:10-20")
        assert llm._call_raw.call_count == 3
        assert llm._call_raw.call_args.kwargs.get("tool_choice") == "none"

    def test_partial_contract_answer_is_not_forced_by_default(self, echo_registry):
        """Generic runner defaults do not repair localization formatting."""
        llm = _make_llm()
        tc = _make_tool_call("call_1", "echo", '{"text": "hello"}')
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="Files: src/a.py"),
        ]

        runner = AgentRunner(llm, echo_registry)
        result = runner.run("Echo hello")

        assert result.total_turns == 2
        assert result.answer == "Files: src/a.py"
        assert llm._call_raw.call_count == 2

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

    def test_max_turns_forces_final_answer(self, echo_registry):
        """On max_turns with no prose, force one tool-free summary turn.

        A run that spent every turn calling tools must still return its best
        answer, not an empty string.
        """
        llm = _make_llm()
        tc = _make_tool_call("call_x", "echo", '{"text": "loop"}')
        # 3 tool-only turns, then the forced tool-free turn returns prose.
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(tool_calls=[tc]),
            _make_response(tool_calls=[tc]),
            _make_response(content="Best-effort summary."),
        ]

        runner = AgentRunner(
            llm,
            echo_registry,
            max_turns=3,
            force_localization_contract=True,
        )
        result = runner.run("loop forever")

        assert result.total_turns == 3
        assert len(result.tool_calls) == 3  # forced turn calls no tools
        assert result.answer == "Best-effort summary."
        # 3 loop turns + 1 forced summary turn.
        assert llm._call_raw.call_count == 4

    def test_force_final_answer_on_max_turns(self, echo_registry):
        """Chat opt-in: exhausted budget ends in one tool-free prose turn."""
        llm = _make_llm()
        tc = _make_tool_call("call_x", "echo", '{"text": "loop"}')
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(tool_calls=[tc]),
            _make_response(content="Grounded final answer."),
        ]

        runner = AgentRunner(llm, echo_registry, max_turns=2, force_final_answer=True)
        result = runner.run("loop forever")

        assert result.answer == "Grounded final answer."
        assert llm._call_raw.call_count == 3
        forced_kwargs = llm._call_raw.call_args.kwargs
        assert forced_kwargs.get("tool_choice") == "none"
        assert forced_kwargs.get("usage_turn") == 3

    def test_force_final_answer_empty_result_raises(self, echo_registry):
        """An empty forced answer is an error, not a silent fallback."""
        llm = _make_llm()
        tc = _make_tool_call("call_x", "echo", '{"text": "loop"}')
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(tool_calls=[tc]),
            _make_response(content=None),
        ]

        runner = AgentRunner(llm, echo_registry, max_turns=2, force_final_answer=True)
        with pytest.raises(RuntimeError, match="max_turns"):
            runner.run("loop forever")

    def test_force_final_answer_leaves_normal_termination_alone(self, echo_registry):
        """A run that ends in prose on its own never gets an extra turn."""
        llm = _make_llm()
        tc = _make_tool_call("call_1", "echo", '{"text": "hello"}')
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="Done."),
        ]

        runner = AgentRunner(llm, echo_registry, max_turns=5, force_final_answer=True)
        result = runner.run("test")

        assert result.answer == "Done."
        assert llm._call_raw.call_count == 2

    def test_grounded_review_revises_first_prose_draft(self, echo_registry):
        """Opt-in QA review turns the first sourced prose answer into a draft."""
        llm = _make_llm()
        tc = _make_tool_call("call_1", "echo", '{"text": "evidence"}')
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="Unsupported draft."),
            _make_response(content="Corrected answer grounded in evidence."),
        ]

        result = AgentRunner(
            llm,
            echo_registry,
            max_turns=5,
            review_final_answer=True,
        ).run("Explain the mechanism")

        assert result.answer == "Corrected answer grounded in evidence."
        assert result.total_turns == 3
        assert llm._call_raw.call_count == 3
        review_messages = [
            message["content"]
            for message in result.messages
            if message.get("role") == "user"
            and "audit it against the retrieved implementation"
            in message.get("content", "")
        ]
        assert len(review_messages) == 1

    def test_grounded_review_can_search_for_missing_evidence(self, echo_registry):
        """The audit retains tools so it can repair evidence, not just rephrase."""
        llm = _make_llm()
        first = _make_tool_call("call_1", "echo", '{"text": "predicate"}')
        follow_up = _make_tool_call("call_2", "echo", '{"text": "caller"}')
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[first]),
            _make_response(content="Draft with an unverified call site."),
            _make_response(tool_calls=[follow_up]),
            _make_response(content="Verified predicate and caller."),
        ]

        result = AgentRunner(
            llm,
            echo_registry,
            max_turns=6,
            review_final_answer=True,
        ).run("Explain enforcement")

        assert result.answer == "Verified predicate and caller."
        assert result.total_turns == 4
        assert [record.arguments["text"] for record in result.tool_calls] == [
            "predicate",
            "caller",
        ]

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

    def test_compact_seed_includes_latest_read_for_keep0(self, tmp_path):
        """Immediate compaction must not hide the read result from the model."""
        target = tmp_path / "target.py"
        target.write_text("secret_value = 7\n", encoding="utf-8")
        tc = _make_tool_call(
            "call_read",
            "read",
            json.dumps({"file_path": str(target)}),
        )
        llm = _make_llm()
        seen_messages = []

        def _call_raw(messages, **kwargs):
            seen_messages.append([dict(m) for m in messages])
            if len(seen_messages) == 1:
                return _make_response(tool_calls=[tc])
            seed = "\n".join(str(m.get("content") or "") for m in messages)
            assert "secret_value = 7" in seed
            assert str(target) in seed
            return _make_response(content="Done.")

        llm._call_raw.side_effect = _call_raw
        runner = AgentRunner(
            llm,
            SkillRegistry(),
            compact_after_read=True,
            compact_keep_reads=0,
            force_localization_contract=False,
        )

        result = runner.run("Locate the value")

        assert result.answer == "Done."
        assert all(m.get("role") != "tool" for m in seen_messages[1])

    def test_compact_keep_reads_keeps_read_output_not_later_grep(self, tmp_path):
        """Seed-richness should retain successful read results, not any tool."""
        target = tmp_path / "target.py"
        target.write_text("read_marker = 1\n", encoding="utf-8")
        other = tmp_path / "other.py"
        other.write_text("grep_marker = 1\n", encoding="utf-8")
        read_tc = _make_tool_call(
            "call_read",
            "read",
            json.dumps({"file_path": str(target)}),
        )
        grep_tc = _make_tool_call(
            "call_grep",
            "grep",
            json.dumps({"pattern": "grep_marker", "path": str(tmp_path)}),
        )
        llm = _make_llm()
        seen_messages = []

        def _call_raw(messages, **kwargs):
            seen_messages.append([dict(m) for m in messages])
            if len(seen_messages) == 1:
                return _make_response(tool_calls=[read_tc, grep_tc])
            seed = "\n".join(str(m.get("content") or "") for m in messages)
            assert "read_marker = 1" in seed
            assert "grep_marker = 1" not in seed
            return _make_response(content="Done.")

        llm._call_raw.side_effect = _call_raw
        runner = AgentRunner(
            llm,
            SkillRegistry(),
            compact_after_read=True,
            compact_keep_reads=1,
            force_localization_contract=False,
        )

        result = runner.run("Locate the value")

        assert result.answer == "Done."

    def test_failed_read_does_not_trigger_compact(self, tmp_path):
        """A default-tool Error string is not a successful read anchor."""
        missing = tmp_path / "missing.py"
        tc = _make_tool_call(
            "call_read",
            "read",
            json.dumps({"file_path": str(missing)}),
        )
        llm = _make_llm()
        seen_messages = []

        def _call_raw(messages, **kwargs):
            seen_messages.append([dict(m) for m in messages])
            if len(seen_messages) == 1:
                return _make_response(tool_calls=[tc])
            assert any(m.get("role") == "tool" for m in messages)
            seed = "\n".join(str(m.get("content") or "") for m in messages)
            assert "Triage complete" not in seed
            assert "file not found" in seed
            return _make_response(content="Done.")

        llm._call_raw.side_effect = _call_raw
        runner = AgentRunner(
            llm,
            SkillRegistry(),
            compact_after_read=True,
            force_localization_contract=False,
        )

        result = runner.run("Locate the value")

        assert result.answer == "Done."

    def test_exclude_skills(self, echo_registry):
        """Excluded sweep-variable skills should not appear in tool schemas.

        Note: default TOOLS (DEFAULT_TOOL_IDS) are NEVER excluded — they live in
        a separate ToolRegistry, outside the skill exclude/allow funnel.
        """
        from codenib.agent.tools.defaults import DEFAULT_TOOL_IDS

        llm = _make_llm()
        llm._call_raw.return_value = _make_response(content="ok")

        runner = AgentRunner(llm, echo_registry, exclude_skills={"echo"})
        tool_names = {t["function"]["name"] for t in runner.tools}
        # echo is excluded
        assert "echo" not in tool_names
        # default tools are always present
        assert DEFAULT_TOOL_IDS.issubset(tool_names)


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
