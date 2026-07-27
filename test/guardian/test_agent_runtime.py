# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Tests for mechanics shared by Guardian's cycle and investigation agents."""

import json
from types import SimpleNamespace

from codeminer.guardian.agent import ToolAgentRuntime, execution_batches


def _call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class _Session:
    def __init__(self):
        self.closed = False
        self.reset_count = 0

    def _call_raw(self, messages, **kwargs):
        del messages, kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        tool_calls=[
                            _call("one", "read_code", json.dumps({"path": "a.py"})),
                            _call("two", "read_code", "{"),
                            _call("three", "update", "{}"),
                        ],
                    )
                )
            ]
        )

    def reset(self):
        self.reset_count += 1

    def close(self):
        self.closed = True


class _LLM:
    def __init__(self):
        self.session = _Session()

    def start_agent_loop(self):
        return self.session


def test_runtime_normalizes_calls_and_owns_transcript_and_session():
    llm = _LLM()
    with ToolAgentRuntime(llm, [{"role": "system", "content": "frame"}]) as runtime:
        turn = runtime.request(tools=[{"type": "function"}])

        assert turn.tool_calls[0].arguments == {"path": "a.py"}
        assert turn.tool_calls[1].arguments == {}
        assert turn.tool_calls[1].argument_error.startswith("malformed tool arguments:")
        runtime.observe(turn.tool_calls[0], "source")
        runtime.replace_messages(runtime.messages)
        assert [item["role"] for item in runtime.messages] == [
            "system",
            "assistant",
            "tool",
        ]
        assert llm.session.reset_count == 1

    assert llm.session.closed is True


def test_execution_batches_only_groups_contiguous_safe_calls():
    llm = _LLM()
    with ToolAgentRuntime(llm, []) as runtime:
        calls = runtime.request(tools=[]).tool_calls

    batches = execution_batches(calls, {"read_code"})

    assert [[call.id for call in batch] for batch in batches] == [
        ["one", "two"],
        ["three"],
    ]
