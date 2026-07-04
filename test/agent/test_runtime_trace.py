# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime trace contract tests for ``AgentRunner``."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from codeminer.agent.runner import AgentRunner
from codeminer.agent.runtime import AGENT_TRACE_SCHEMA_VERSION
from codeminer.agent.skills.core import (
    SkillInputSpec,
    SkillMetadata,
    SkillOutputSpec,
    SkillType,
)
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.llm.litellm_chat import LiteLLMChat


def _make_llm():
    return MagicMock(spec=LiteLLMChat)


def _make_response(content=None, tool_calls=None):
    msg = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _make_tool_call(tc_id, name, arguments_json):
    return SimpleNamespace(
        id=tc_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments_json),
    )


def _echo_registry() -> SkillRegistry:
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


def _events(result, kind):
    assert result.trace is not None
    return [event for event in result.trace.events if event.kind == kind]


def _context(result, source):
    assert result.trace is not None
    return [entry for entry in result.trace.context if entry.source == source]


def test_trace_records_direct_answer_contract():
    llm = _make_llm()
    llm._call_raw.return_value = _make_response(content="done")

    result = AgentRunner(
        llm,
        SkillRegistry(),
        force_localization_contract=False,
    ).run("answer directly")

    assert result.trace is not None
    assert result.trace.to_dict()["schema_version"] == AGENT_TRACE_SCHEMA_VERSION
    assert result.trace.to_dict()["context"] == []
    assert [event.kind for event in result.trace.events] == [
        "run_start",
        "llm_call",
        "llm_response",
        "final_answer",
        "run_end",
    ]
    assert _events(result, "final_answer")[0].data == {
        "source": "assistant",
        "answer_chars": 4,
        "has_contract": False,
    }


def test_trace_summarizes_successful_tool_call():
    llm = _make_llm()
    tc = _make_tool_call("call_1", "echo", '{"text": "hello"}')
    llm._call_raw.side_effect = [
        _make_response(tool_calls=[tc]),
        _make_response(content="done"),
    ]

    result = AgentRunner(
        llm,
        _echo_registry(),
        force_localization_contract=False,
    ).run("echo")

    tool_event = _events(result, "tool_call")[0]
    assert tool_event.turn == 1
    assert tool_event.data["tool_call_id"] == "call_1"
    assert tool_event.data["tool"] == "echo"
    assert tool_event.data["arguments"] == {"text": "hello"}
    assert tool_event.data["status"] == "ok"
    assert tool_event.data["result_type"] == "str"
    assert tool_event.data["result_chars"] == len("echo: hello")

    entry = _context(result, "echo")[0]
    assert entry.state == "offered"
    assert entry.tool_call_id == "call_1"
    assert entry.summary == "echo returned str (11 chars)"
    assert "echo: hello" not in entry.summary
    assert entry.provenance == {
        "kind": "tool_result",
        "tool": "echo",
        "tool_call_id": "call_1",
    }
    assert entry.freshness == "fresh"
    assert entry.token_estimate == 3
    assert entry.metadata["arguments"] == {"text": "hello"}


def test_trace_records_default_read_events(tmp_path):
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    read_call = _make_tool_call(
        "call_read",
        "read",
        json.dumps({"file_path": str(target), "offset": 1, "limit": 3}),
    )
    llm = _make_llm()
    llm._call_raw.side_effect = [
        _make_response(tool_calls=[read_call]),
        _make_response(content="done"),
    ]

    result = AgentRunner(
        llm,
        SkillRegistry(),
        force_localization_contract=False,
    ).run("read file")

    read_event = _events(result, "read")[0]
    assert read_event.data == {
        "tool_call_id": "call_read",
        "path": str(target),
        "offset": 1,
        "limit": 3,
        "status": "ok",
    }
    assert _events(result, "tool_call")[0].data["tool"] == "read"
    entry = _context(result, "read")[0]
    assert entry.state == "read"
    assert entry.path == str(target)
    assert entry.summary.startswith(f"read {target}")
    assert entry.provenance["kind"] == "tool_result"
    assert entry.freshness == "fresh"
    assert entry.token_estimate > 0
    assert entry.metadata["arguments"] == {
        "file_path": str(target),
        "offset": 1,
        "limit": 3,
    }


def test_trace_records_tool_errors():
    llm = _make_llm()
    missing = _make_tool_call("call_missing", "missing_tool", "{}")
    llm._call_raw.side_effect = [
        _make_response(tool_calls=[missing]),
        _make_response(content="done"),
    ]

    result = AgentRunner(
        llm,
        SkillRegistry(),
        force_localization_contract=False,
    ).run("call missing tool")

    tool_event = _events(result, "tool_call")[0]
    assert tool_event.data["status"] == "error"
    assert tool_event.data["result_type"] == "error"
    assert "not available" in tool_event.data["error"]
    entry = _context(result, "missing_tool")[0]
    assert entry.state == "rejected"
    assert "not available" in entry.summary


def test_trace_records_context_compaction(tmp_path):
    target = tmp_path / "target.py"
    target.write_text("secret_value = 7\n", encoding="utf-8")
    read_call = _make_tool_call(
        "call_read",
        "read",
        json.dumps({"file_path": str(target)}),
    )
    llm = _make_llm()
    llm._call_raw.side_effect = [
        _make_response(tool_calls=[read_call]),
        _make_response(content="done"),
    ]

    result = AgentRunner(
        llm,
        SkillRegistry(),
        compact_after_read=True,
        compact_keep_reads=1,
        force_localization_contract=False,
    ).run("locate value")

    compaction = _events(result, "context_compacted")[0]
    assert compaction.data["read_paths"] == [str(target)]
    assert compaction.data["keep_reads"] == 1
    read_entry = _context(result, "read")[0]
    assert read_entry.state == "read"
    compact_entry = _context(result, "runtime.compaction")[0]
    assert compact_entry.state == "summarized"
    assert compact_entry.metadata["read_paths"] == [str(target)]
