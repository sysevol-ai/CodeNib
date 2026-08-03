# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for agent runtime trace summaries."""

from __future__ import annotations

from types import SimpleNamespace

from codenib.agent.runtime import AGENT_TRACE_SCHEMA_VERSION, AgentRunTrace
from codenib.eval.agent_runner.trace_summary import summarize_agent_trace


def _trace_with_runtime_signals() -> AgentRunTrace:
    trace = AgentRunTrace()
    trace.add("run_start", 0)
    trace.add(
        "lsp_route_context",
        0,
        status="skipped",
        reason="no_symbol_seeds",
        seed_policy="specific",
    )
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
    assert summary.event_count == 10
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
    assert summary.lsp_route_context["status"] == "skipped"
    assert summary.lsp_route_context["reason"] == "no_symbol_seeds"
    assert summary.lsp_route_context["seed_policy"] == "specific"
    assert isinstance(summary.lsp_route_context["event_ms"], float)
    assert summary.lsp_route_tool_calls == []
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


def test_summarize_agent_trace_preserves_lsp_route_preview():
    trace = AgentRunTrace()
    preview = [
        {
            "location": "svc.py:3-5",
            "symbol": "svc.DefaultConfig",
            "type": "function",
            "relation": "route provider: query match config, default",
        }
    ]
    trace.add(
        "lsp_route_context",
        0,
        status="offered",
        seeds=[],
        seed_source="query",
        seed_policy="specific",
        route_count=1,
        context_chars=120,
        duration_ms=12.5,
        route_preview=preview,
    )

    summary = summarize_agent_trace(trace)

    expected_context = {
        "status": "offered",
        "seeds": [],
        "seed_source": "query",
        "seed_policy": "specific",
        "route_count": 1,
        "context_chars": 120,
        "duration_ms": 12.5,
        "route_preview": preview,
        "visible_turn": 0,
        "route_visible_ms": 12.5,
    }
    for key, value in expected_context.items():
        assert summary.lsp_route_context[key] == value
    assert isinstance(summary.lsp_route_context["event_ms"], float)


def test_summarize_agent_trace_preserves_dynamic_lsp_route_calls():
    trace = AgentRunTrace()
    trace.add("llm_call", 1)
    trace.add(
        "tool_call",
        1,
        tool="lsp_route",
        status="ok",
        arguments={
            "symbols": ["F523"],
            "query": "Fix F523 format call",
            "top_k": 5,
            "include_neighbors": True,
            "ignored": "not persisted",
        },
        route_args={
            "symbols": ["F523"],
            "query": "Fix F523 format call",
            "top_k": 5,
            "include_neighbors": True,
        },
        lsp_provider={
            "provider": "codenib_static_index",
            "capability": "route",
            "status": "ok",
            "lsp_method": "codenib/lspRoute",
        },
        lsp_result_fingerprint="abc123",
        lsp_result_preview=[
            {
                "location": "rules.py:10-12",
                "symbol": "rules.F523",
                "type": "function",
            }
        ],
        route_fingerprint="abc123",
        result_count=5,
        duration_ms=123,
    )
    trace.add("llm_call", 2)

    summary = summarize_agent_trace(trace)

    call = summary.lsp_route_tool_calls[0]
    expected_call = {
        "turn": 1,
        "status": "ok",
        "result_count": 5,
        "duration_ms": 123,
        "route_args": {
            "symbols": ["F523"],
            "query": "Fix F523 format call",
            "top_k": 5,
            "include_neighbors": True,
        },
        "lsp_provider": {
            "provider": "codenib_static_index",
            "capability": "route",
            "status": "ok",
            "lsp_method": "codenib/lspRoute",
        },
        "lsp_result_fingerprint": "abc123",
        "lsp_result_preview": [
            {
                "location": "rules.py:10-12",
                "symbol": "rules.F523",
                "type": "function",
            }
        ],
        "route_fingerprint": "abc123",
        "model_can_use_turn": 2,
        "extra_model_round_trips": 1,
        "arguments": {
            "symbols": ["F523"],
            "query": "Fix F523 format call",
            "top_k": 5,
            "include_neighbors": True,
        },
    }
    for key, value in expected_call.items():
        assert call[key] == value
    assert isinstance(call["tool_result_ms"], float)
    assert isinstance(call["model_can_use_ms"], float)


def test_summarize_agent_trace_handles_missing_trace():
    summary = summarize_agent_trace(None)

    assert summary.schema_version is None
    assert summary.event_count == 0
    assert summary.tool_call_count == 0
    assert summary.lsp_route_context == {}
    assert summary.lsp_route_tool_calls == []
    assert summary.failure_categories == []
