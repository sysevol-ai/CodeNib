# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for agent runtime trace summaries."""

from __future__ import annotations

from types import SimpleNamespace

from codeminer.agent.runtime import AGENT_TRACE_SCHEMA_VERSION, AgentRunTrace
from codeminer.eval.agent_runner.trace_summary import summarize_agent_trace


def _trace_with_runtime_signals() -> AgentRunTrace:
    trace = AgentRunTrace()
    trace.add("run_start", 0)
    trace.add(
        "tool_call",
        1,
        tool="grep",
        status="ok",
    )
    trace.add(
        "tool_call",
        2,
        tool="read",
        status="ok",
    )
    trace.add("read", 2, path="pkg/a.py", status="ok")
    trace.add(
        "tool_call",
        3,
        tool="embedding_search",
        status="error",
    )
    trace.add("context_compacted", 3)
    trace.add("final_answer_forced", 4)
    trace.add("max_turns_exhausted", 4)
    trace.add("final_answer", 4, source="forced_schema", has_contract=False)
    trace.add_context(
        "grep",
        "offered",
        1,
        token_estimate=10,
    )
    trace.add_context(
        "read",
        "read",
        2,
        path="pkg/a.py",
        token_estimate=20,
    )
    return trace


def test_summarize_agent_trace_counts_runtime_signals():
    summary = summarize_agent_trace(_trace_with_runtime_signals())

    assert summary.schema_version == AGENT_TRACE_SCHEMA_VERSION
    assert summary.event_count == 9
    assert summary.max_turn == 4
    assert summary.tool_call_count == 3
    assert summary.tool_error_count == 1
    assert summary.tools == {"embedding_search": 1, "grep": 1, "read": 1}
    assert summary.tool_errors == {"embedding_search": 1}
    assert summary.read_count == 1
    assert summary.read_paths == ["pkg/a.py"]
    assert summary.context_entry_count == 2
    assert summary.context_tokens_estimate == 30
    assert summary.context_by_state == {"offered": 1, "read": 1}
    assert summary.context_sources == {"grep": 1, "read": 1}
    assert summary.compaction_count == 1
    assert summary.max_turns_exhausted is True
    assert summary.forced_final_answer is True
    assert summary.answer_contract_present is False
    assert summary.final_answer_source == "forced_schema"
    assert summary.failure_categories == [
        "tool_error",
        "turn_budget_exhausted",
        "answer_contract_missing",
    ]


def test_summarize_agent_trace_accepts_result_or_dict_shapes():
    trace = _trace_with_runtime_signals()

    from_result = summarize_agent_trace(SimpleNamespace(trace=trace))
    from_dict = summarize_agent_trace(trace.to_dict())

    assert from_result.to_dict() == from_dict.to_dict()


def test_summarize_agent_trace_handles_missing_trace():
    summary = summarize_agent_trace(None)

    assert summary.schema_version is None
    assert summary.event_count == 0
    assert summary.tool_call_count == 0
    assert summary.failure_categories == []
