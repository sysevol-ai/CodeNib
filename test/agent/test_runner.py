# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the agent runner (codeminer.agent.runner)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codeminer.agent.agent_types import AgentResult
from codeminer.agent.runner import (
    LOCALIZATION_SCHEMA_INSTRUCTIONS,
    AgentRunner,
    _serialize_result,
)
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

    def test_default_prompt_carries_structured_location_ordering_contract(self):
        """The base runner prompt should encode the strict deliverable shape."""
        llm = _make_llm()
        llm._call_raw.return_value = _make_response(content="The answer is 42.")

        runner = AgentRunner(llm, SkillRegistry())
        runner.run("What is the answer?")

        first_messages = llm._call_raw.call_args.args[0]
        system = first_messages[0]["content"]
        assert LOCALIZATION_SCHEMA_INSTRUCTIONS in system
        assert "Put at most five entries in Locations" in system
        assert "endpoint/caller first" in system

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
        assert record.envelope is not None
        assert record.envelope.status == "ok"
        assert record.envelope.result_type == "text"
        assert record.envelope.result_chars == len("echo: hello")
        tool_events = [e for e in result.trace.events if e.kind == "tool_call"]
        assert tool_events[0].data["envelope"]["status"] == "ok"
        assert tool_events[0].data["envelope"]["result_type"] == "text"

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

        runner = AgentRunner(llm, echo_registry)
        result = runner.run("Echo hello")

        assert result.total_turns == 2
        assert result.answer.endswith("Locations: src/a.py:10-20")
        assert llm._call_raw.call_count == 3
        assert llm._call_raw.call_args.kwargs.get("tool_choice") == "none"
        forced_messages = llm._call_raw.call_args.args[0]
        forced_user_messages = [
            m.get("content", "") for m in forced_messages if m.get("role") == "user"
        ]
        assert any(
            LOCALIZATION_SCHEMA_INSTRUCTIONS in content
            for content in forced_user_messages
        )

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

    def test_duplicate_tool_call_guard_skips_third_identical_call(self):
        """Repeated identical deterministic calls should not run forever."""
        calls = []
        reg = SkillRegistry()
        reg.register(
            SkillMetadata(
                skill_id="echo",
                skill_type=SkillType.CUSTOM,
                inputs=[SkillInputSpec(name="text", type_hint="str", required=True)],
                outputs=SkillOutputSpec(type_hint="str"),
                executor_fn=lambda text: calls.append(text) or f"echo: {text}",
            )
        )
        llm = _make_llm()
        llm._call_raw.side_effect = [
            _make_response(
                tool_calls=[_make_tool_call("call_1", "echo", '{"text":"x"}')]
            ),
            _make_response(
                tool_calls=[_make_tool_call("call_2", "echo", '{"text":"x"}')]
            ),
            _make_response(
                tool_calls=[_make_tool_call("call_3", "echo", '{"text":"x"}')]
            ),
            _make_response(content="Done."),
        ]

        runner = AgentRunner(llm, reg, force_localization_contract=False)
        result = runner.run("repeat")

        assert calls == ["x", "x"]
        assert len(result.tool_calls) == 3
        assert "Duplicate tool call skipped" in (result.tool_calls[2].error or "")
        assert result.trace is not None
        duplicate_events = [
            e
            for e in result.trace.events
            if e.kind == "tool_call" and e.data.get("duplicate")
        ]
        assert len(duplicate_events) == 1
        assert duplicate_events[0].data["tool"] == "echo"
        assert duplicate_events[0].data["envelope"]["status"] == "skipped"
        assert duplicate_events[0].data["envelope"]["error_kind"] == "duplicate"

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

        runner = AgentRunner(llm, echo_registry, max_turns=3)
        result = runner.run("loop forever")

        assert result.total_turns == 3
        assert len(result.tool_calls) == 3  # forced turn calls no tools
        assert result.answer == "Best-effort summary."
        # 3 loop turns + 1 forced summary turn.
        assert llm._call_raw.call_count == 4

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
        assert result.tool_calls[0].envelope is not None
        assert result.tool_calls[0].envelope.status == "error"
        assert result.tool_calls[0].envelope.error_kind == "unavailable"

    def test_invalid_tool_args_have_typed_envelope(self, echo_registry):
        llm = _make_llm()
        tc = _make_tool_call("call_1", "echo", "{}")
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="Invalid."),
        ]

        runner = AgentRunner(llm, echo_registry)
        result = runner.run("test")

        record = result.tool_calls[0]
        assert record.error is not None
        assert record.envelope is not None
        assert record.envelope.status == "error"
        assert record.envelope.error_kind == "invalid_args"
        tool_event = next(e for e in result.trace.events if e.kind == "tool_call")
        assert tool_event.data["envelope"]["error_kind"] == "invalid_args"
        ledger_entry = next(c for c in result.trace.context if c.source == "echo")
        assert ledger_entry.state == "rejected"
        assert ledger_entry.metadata["tool_result"]["error_kind"] == "invalid_args"

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
        assert result.tool_calls[0].envelope.error_kind == "exception"

    def test_tool_result_envelope_records_truncation(self):
        reg = SkillRegistry()
        reg.register(
            SkillMetadata(
                skill_id="large",
                skill_type=SkillType.CUSTOM,
                inputs=[],
                outputs=SkillOutputSpec(type_hint="str"),
                executor_fn=lambda: "x" * 30000,
            )
        )
        llm = _make_llm()
        tc = _make_tool_call("call_1", "large", "{}")
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="Done."),
        ]

        runner = AgentRunner(llm, reg)
        result = runner.run("large")

        record = result.tool_calls[0]
        assert record.envelope is not None
        assert record.envelope.status == "ok"
        assert record.envelope.truncated is True
        assert record.envelope.result_chars < 30000
        tool_event = next(e for e in result.trace.events if e.kind == "tool_call")
        assert tool_event.data["envelope"]["truncated"] is True
        ledger_entry = next(c for c in result.trace.context if c.source == "large")
        assert ledger_entry.metadata["truncated"] is True

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
        assert result.trace is not None
        trace_dict = result.trace.to_dict()
        assert trace_dict["events"][0]["kind"] == "run_start"
        assert trace_dict["events"][-1]["kind"] == "run_end"
        assert any(e.kind == "compaction" for e in result.trace.events)
        read_entries = [c for c in result.trace.context if c.source == "read"]
        assert read_entries
        assert read_entries[0].state == "read"
        assert read_entries[0].path == str(target)
        assert read_entries[0].metadata["start_line"] == 1
        assert read_entries[0].metadata["end_line"] == 200

    def test_preload_candidates_are_traced_and_verified_by_read(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text("secret_value = 7\n", encoding="utf-8")
        other = tmp_path / "other.py"
        other.write_text("other_value = 3\n", encoding="utf-8")
        tc = _make_tool_call(
            "call_read",
            "read",
            json.dumps({"file_path": str(target)}),
        )
        llm = _make_llm()

        def _call_raw(messages, **kwargs):
            if any(m.get("role") == "tool" for m in messages):
                return _make_response(content="Done.")
            return _make_response(tool_calls=[tc])

        llm._call_raw.side_effect = _call_raw
        runner = AgentRunner(llm, SkillRegistry(), force_localization_contract=False)

        result = runner.run(
            "Locate the value",
            preload_candidates=[
                {"file": str(target), "start": 1, "end": 1},
                {"file": str(other), "start": 1, "end": 1},
            ],
        )

        assert result.trace is not None
        run_start = result.trace.events[0]
        assert run_start.kind == "run_start"
        assert run_start.data["preload_candidates"] == 2
        preload_entries = [c for c in result.trace.context if c.source == "preload"]
        assert [c.path for c in preload_entries] == [str(target), str(other)]
        assert preload_entries[0].state == "verified"
        assert preload_entries[0].tool_call_id == "call_read"
        assert preload_entries[0].metadata["rank"] == 1
        assert preload_entries[0].metadata["read_confirmed"] is True
        assert preload_entries[1].state == "offered"
        assert preload_entries[1].metadata["read_confirmed"] is False

    def test_context_scheduler_injects_live_loop_context(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text("secret_value = 7\n", encoding="utf-8")
        tc = _make_tool_call(
            "call_read",
            "read",
            json.dumps({"file_path": str(target)}),
        )
        llm = _make_llm()
        seen_messages = []
        scheduler_calls = []

        def _scheduler(state):
            scheduler_calls.append(dict(state))
            assert state["turn"] == 1
            assert state["has_read"] is True
            assert [tc.skill_id for tc in state["tool_calls"]] == ["read"]
            assert state["read_outputs"][0]["path"] == str(target)
            assert "secret_value" in state["read_outputs"][0]["content"]
            return {
                "source": "scheduled_graph_lsp",
                "content": "Related static graph/LSP context: target.py:1-1",
                "metadata": {"node_count": 1, "reason": "unit_test"},
            }

        def _call_raw(messages, **kwargs):
            seen_messages.append([dict(m) for m in messages])
            if len(seen_messages) == 1:
                return _make_response(tool_calls=[tc])
            seed = "\n".join(str(m.get("content") or "") for m in messages)
            assert "Related static graph/LSP context" in seed
            return _make_response(content="Done.")

        llm._call_raw.side_effect = _call_raw
        runner = AgentRunner(
            llm,
            SkillRegistry(),
            force_localization_contract=False,
            context_scheduler=_scheduler,
        )

        result = runner.run("Locate the value")

        assert result.answer == "Done."
        assert len(scheduler_calls) == 1
        assert result.trace is not None
        scheduled_events = [
            e for e in result.trace.events if e.kind == "scheduled_context"
        ]
        assert len(scheduled_events) == 1
        assert scheduled_events[0].data["source"] == "scheduled_graph_lsp"
        assert scheduled_events[0].data["node_count"] == 1
        scheduled_entries = [
            c for c in result.trace.context if c.source == "scheduled_graph_lsp"
        ]
        assert len(scheduled_entries) == 1
        assert scheduled_entries[0].state == "offered"

    def test_unverified_preload_triggers_fallback_before_compact(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text("secret_value = 7\n", encoding="utf-8")
        other = tmp_path / "other.py"
        other.write_text("other_value = 3\n", encoding="utf-8")
        tc = _make_tool_call(
            "call_read",
            "read",
            json.dumps({"file_path": str(target), "offset": 1, "limit": 1}),
        )
        llm = _make_llm()
        seen_messages = []

        def _call_raw(messages, **kwargs):
            seen_messages.append([dict(m) for m in messages])
            if len(seen_messages) == 1:
                return _make_response(tool_calls=[tc])
            combined = "\n".join(str(m.get("content") or "") for m in messages)
            assert "preloaded candidate spans have not been confirmed" in combined
            assert "Triage complete" not in combined
            return _make_response(content="Done.")

        llm._call_raw.side_effect = _call_raw
        runner = AgentRunner(
            llm,
            SkillRegistry(),
            compact_after_read=True,
            force_localization_contract=False,
        )

        result = runner.run(
            "Locate the value",
            preload_candidates=[{"file": str(other), "start": 1, "end": 1}],
        )

        assert result.answer == "Done."
        assert result.trace is not None
        assert any(e.kind == "fallback" for e in result.trace.events)
        assert not any(e.kind == "compaction" for e in result.trace.events)
        preload_entries = [c for c in result.trace.context if c.source == "preload"]
        assert preload_entries[0].state == "offered"
        assert preload_entries[0].metadata["read_confirmed"] is False

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
                return _make_response(
                    content="I will inspect the target file and compare grep hits.",
                    tool_calls=[read_tc, grep_tc],
                )
            seed = "\n".join(str(m.get("content") or "") for m in messages)
            assert "read_marker = 1" in seed
            assert "grep_marker = 1" not in seed
            assert "# Exploration summary" in seed
            assert "Confirmed reads:" in seed
            assert "Preload candidates offered: 2; read-confirmed:" in seed
            assert "Other tool context already offered: grep x1" in seed
            assert "# Recent working tail" in seed
            return _make_response(content="Done.")

        llm._call_raw.side_effect = _call_raw
        runner = AgentRunner(
            llm,
            SkillRegistry(),
            compact_after_read=True,
            compact_keep_reads=1,
            force_localization_contract=False,
        )

        result = runner.run(
            "Locate the value",
            preload_candidates=[
                {"file": str(target), "start": 1, "end": 1},
                {"file": str(other), "start": 1, "end": 1},
            ],
        )

        assert result.answer == "Done."
        assert result.trace is not None
        assert any(e.kind == "compaction" for e in result.trace.events)
        read_entries = [c for c in result.trace.context if c.source == "read"]
        assert read_entries
        assert read_entries[0].state == "read"
        assert read_entries[0].path == str(target)
        assert read_entries[0].metadata["start_line"] == 1
        assert read_entries[0].metadata["end_line"] == 200
        grep_entries = [c for c in result.trace.context if c.source == "grep"]
        assert grep_entries
        assert grep_entries[0].state == "offered"
        summary_entries = [
            c
            for c in result.trace.context
            if c.source == "compaction" and c.state == "summarized"
        ]
        assert summary_entries
        assert summary_entries[0].metadata["summary_reads"] == 1
        assert summary_entries[0].metadata["summary_offered_sources"] == 1
        assert summary_entries[0].metadata["summary_preload_candidates"] == 2
        assert summary_entries[0].metadata["summary_preload_verified"] == 1
        assert summary_entries[0].metadata["original_tool_result_chars"] > 0
        assert summary_entries[0].metadata["pruned_tool_result_chars"] > 0
        assert summary_entries[0].metadata["protected_tail_chars"] > 0

    def test_trace_records_replay_messages_across_compaction(self, tmp_path):
        """Trace replay keeps the original tool result after live history reset."""
        target = tmp_path / "target.py"
        target.write_text("value = 1\n", encoding="utf-8")
        read_tc = _make_tool_call(
            "call_read",
            "read",
            json.dumps({"file_path": str(target), "offset": 1, "limit": 1}),
        )
        llm = _make_llm()

        def _call_raw(messages, **kwargs):
            if llm._call_raw.call_count == 1:
                return _make_response(
                    content="I will inspect the target.",
                    tool_calls=[read_tc],
                )
            combined = "\n".join(str(m.get("content") or "") for m in messages)
            assert "Triage complete" in combined
            assert "value = 1" in combined
            return _make_response(content="Files: target.py\nLocations: target.py:1-1")

        llm._call_raw.side_effect = _call_raw
        runner = AgentRunner(
            llm,
            SkillRegistry(),
            compact_after_read=True,
            compact_keep_reads=1,
            force_localization_contract=False,
        )

        result = runner.run(
            "Locate the value",
            preload_candidates=[{"file": str(target), "start": 1, "end": 1}],
        )

        assert result.trace is not None
        assert [m["role"] for m in result.messages] == [
            "system",
            "user",
            "assistant",
        ]
        assert all(m.get("role") != "tool" for m in result.messages)

        trace_payload = result.trace.to_dict()
        assert trace_payload["schema_version"] == 2
        replay = trace_payload["message_replay"]
        assert [m["role"] for m in replay] == [
            "system",
            "user",
            "assistant",
            "tool",
            "system",
            "user",
            "assistant",
        ]
        assert replay[2]["tool_calls"][0]["function"]["name"] == "read"
        assert "value = 1" in replay[3]["content"]
        assert "Triage complete" in replay[5]["content"]
        tool_schema_names = [
            schema["function"]["name"]
            for schema in trace_payload["tool_schema_replay"]
            if schema.get("function")
        ]
        assert "read" in tool_schema_names

        llm_calls = [e for e in result.trace.events if e.kind == "llm_call"]
        assert [e.data["message_replay_indexes"] for e in llm_calls] == [
            [0, 1],
            [4, 5],
        ]
        assert all(
            len(e.data["tool_schema_indexes"]) == e.data["tool_count"]
            for e in llm_calls
        )

    def test_compact_tail_chars_limits_recent_working_tail(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text("value = 1\n", encoding="utf-8")
        read_tc = _make_tool_call(
            "call_read",
            "read",
            json.dumps({"file_path": str(target), "offset": 1, "limit": 1}),
        )
        llm = _make_llm()

        def _call_raw(messages, **kwargs):
            if llm._call_raw.call_count == 1:
                return _make_response(
                    content="abcdefghijklmnopqrstuvwxyz",
                    tool_calls=[read_tc],
                )
            seed = "\n".join(str(m.get("content") or "") for m in messages)
            assert "# Recent working tail\nabcdefg" in seed
            assert "abcdefgh" not in seed
            return _make_response(content="Files: target.py\nLocations: target.py:1-1")

        llm._call_raw.side_effect = _call_raw
        runner = AgentRunner(
            llm,
            SkillRegistry(),
            compact_after_read=True,
            compact_keep_reads=1,
            compact_tail_chars=7,
            force_localization_contract=False,
        )

        result = runner.run(
            "Locate the value",
            preload_candidates=[{"file": str(target), "start": 1, "end": 1}],
        )

        summary_entry = next(
            c
            for c in result.trace.context
            if c.source == "compaction" and c.state == "summarized"
        )
        assert summary_entry.metadata["compact_tail_chars"] == 7
        assert summary_entry.metadata["protected_tail_chars"] == 7

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
        assert result.trace is not None
        assert not any(e.kind == "compaction" for e in result.trace.events)
        read_entries = [c for c in result.trace.context if c.source == "read"]
        assert read_entries
        assert read_entries[0].state == "rejected"
        assert read_entries[0].path == str(missing)
        assert read_entries[0].metadata["tool_result"]["status"] == "error"
        assert (
            read_entries[0].metadata["tool_result"]["error_kind"] == "tool_result_error"
        )
        tool_event = next(e for e in result.trace.events if e.kind == "tool_call")
        assert tool_event.data["envelope"]["error_kind"] == "tool_result_error"

    def test_read_envelope_records_permission_metadata(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text("value = 1\n", encoding="utf-8")
        tc = _make_tool_call(
            "call_read",
            "read",
            json.dumps({"file_path": str(target), "offset": 1, "limit": 1}),
        )
        llm = _make_llm()
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="Done."),
        ]
        runner = AgentRunner(
            llm,
            SkillRegistry(),
            force_localization_contract=False,
        )

        result = runner.run("Read target")

        record = result.tool_calls[0]
        metadata = record.envelope.metadata
        assert metadata["permission_boundary"] == "process_read_permissions"
        assert metadata["sandbox"] == "caller_managed"
        assert metadata["path_jail"] == "none"
        assert metadata["side_effects_possible"] is False
        file_scope = metadata["paths"]["file_path"]
        assert file_scope["value"] == str(target)
        assert file_scope["is_absolute"] is True
        assert "outside_cwd" in file_scope

        tool_event = next(e for e in result.trace.events if e.kind == "tool_call")
        event_meta = tool_event.data["envelope"]["metadata"]
        assert event_meta["permission_boundary"] == "process_read_permissions"
        read_entry = next(c for c in result.trace.context if c.source == "read")
        ledger_meta = read_entry.metadata["tool_result"]["metadata"]
        assert ledger_meta["path_jail"] == "none"

    def test_bash_envelope_records_shell_permission_metadata(self, tmp_path):
        tc = _make_tool_call(
            "call_bash",
            "bash",
            json.dumps({"command": "printf hi", "cwd": str(tmp_path), "timeout": 1000}),
        )
        llm = _make_llm()
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="Done."),
        ]
        runner = AgentRunner(
            llm,
            SkillRegistry(),
            force_localization_contract=False,
        )

        result = runner.run("Run command")

        record = result.tool_calls[0]
        metadata = record.envelope.metadata
        assert metadata["permission_boundary"] == "process_exec_permissions"
        assert metadata["sandbox"] == "caller_managed"
        assert metadata["path_jail"] == "none"
        assert metadata["shell"] is True
        assert metadata["side_effects_possible"] is True
        assert metadata["command_chars"] == len("printf hi")
        assert metadata["timeout_ms"] == 1000
        assert metadata["cwd"]["value"] == str(tmp_path)

        bash_entry = next(c for c in result.trace.context if c.source == "bash")
        ledger_meta = bash_entry.metadata["tool_result"]["metadata"]
        assert ledger_meta["permission_boundary"] == "process_exec_permissions"

    def test_bash_nonzero_exit_is_typed_error(self):
        tc = _make_tool_call("call_bash", "bash", json.dumps({"command": "false"}))
        llm = _make_llm()
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="Done."),
        ]
        runner = AgentRunner(
            llm,
            SkillRegistry(),
            force_localization_contract=False,
        )

        result = runner.run("Run command")

        record = result.tool_calls[0]
        assert record.error is None
        assert record.envelope.status == "error"
        assert record.envelope.error_kind == "exit_nonzero"
        assert record.envelope.metadata["exit_code"] == 1
        bash_entry = next(c for c in result.trace.context if c.source == "bash")
        assert bash_entry.state == "rejected"
        assert bash_entry.metadata["tool_result"]["error_kind"] == "exit_nonzero"
        tool_event = next(e for e in result.trace.events if e.kind == "tool_call")
        assert tool_event.data["envelope"]["metadata"]["exit_code"] == 1

    def test_bash_spawn_error_is_typed_error(self, tmp_path):
        missing_cwd = tmp_path / "missing"
        tc = _make_tool_call(
            "call_bash",
            "bash",
            json.dumps({"command": "printf hi", "cwd": str(missing_cwd)}),
        )
        llm = _make_llm()
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="Done."),
        ]
        runner = AgentRunner(
            llm,
            SkillRegistry(),
            force_localization_contract=False,
        )

        result = runner.run("Run command")

        record = result.tool_calls[0]
        assert record.envelope.status == "error"
        assert record.envelope.error_kind == "spawn_error"
        assert record.envelope.metadata["cwd"]["value"] == str(missing_cwd)

    def test_bash_timeout_is_typed_error(self):
        tc = _make_tool_call(
            "call_bash",
            "bash",
            json.dumps({"command": "sleep 2", "timeout": 1000}),
        )
        llm = _make_llm()
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="Done."),
        ]
        runner = AgentRunner(
            llm,
            SkillRegistry(),
            force_localization_contract=False,
        )

        result = runner.run("Run command")

        record = result.tool_calls[0]
        assert record.envelope.status == "error"
        assert record.envelope.error_kind == "timeout"
        assert record.envelope.metadata["timeout_ms"] == 1000

    def test_bash_output_truncation_is_typed(self):
        tc = _make_tool_call(
            "call_bash",
            "bash",
            json.dumps({"command": "yes x | head -c 20000"}),
        )
        llm = _make_llm()
        llm._call_raw.side_effect = [
            _make_response(tool_calls=[tc]),
            _make_response(content="Done."),
        ]
        runner = AgentRunner(
            llm,
            SkillRegistry(),
            force_localization_contract=False,
        )

        result = runner.run("Run command")

        record = result.tool_calls[0]
        assert record.envelope.status == "ok"
        assert record.envelope.truncated is True
        assert record.envelope.metadata["exit_code"] == 0
        assert record.envelope.metadata["output_truncated"] is True

    def test_exclude_skills(self, echo_registry):
        """Excluded sweep-variable skills should not appear in tool schemas.

        Note: default TOOLS (DEFAULT_TOOL_IDS) are NEVER excluded — they live in
        a separate ToolRegistry, outside the skill exclude/allow funnel.
        """
        from codeminer.agent.tools.defaults import DEFAULT_TOOL_IDS

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
