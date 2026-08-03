# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for agent-runner result observation helpers."""

from __future__ import annotations

from codenib.agent.agent_types import AgentResult, ToolCallRecord
from codenib.eval.agent_runner.results import summarize_agent_result


def test_summarize_agent_result_collects_tool_and_read_observations():
    node_a = {"file": "pkg/a.py", "name": "pkg.a.foo"}
    node_b = {"file": "pkg/b.py", "name": "pkg.b.bar"}
    result = AgentResult(
        answer="Locations: pkg/a.py:1-3",
        tool_calls=[
            ToolCallRecord(
                "search-1",
                "bm25_search",
                {"query": "foo"},
                result=[node_a, node_b],
            ),
            ToolCallRecord(
                "read-1",
                "read",
                {"file_path": "pkg/a.py", "offset": 1, "limit": 20},
                result="1 | def foo():",
            ),
            ToolCallRecord(
                "read-2",
                "read",
                {"file_path": "missing.py"},
                error="file not found",
            ),
        ],
        total_turns=3,
        total_duration_ms=42.5,
    )

    summary = summarize_agent_result(result)

    assert summary["nodes"] == [node_a, node_b]
    assert summary["tool_calls"] == [
        {"skill_id": "bm25_search", "n_results": 2, "error": False},
        {"skill_id": "read", "n_results": 0, "error": False},
        {"skill_id": "read", "n_results": 0, "error": True},
    ]
    assert summary["file_read_paths"] == ["pkg/a.py"]
    assert summary["file_reads"] == [
        {"file_path": "pkg/a.py", "offset": 1, "limit": 20}
    ]
    assert summary["answer"] == "Locations: pkg/a.py:1-3"
    assert summary["total_turns"] == 3
    assert summary["total_duration_ms"] == 42.5
    assert summary["tool_call_count"] == 3
    assert summary["trace_summary"]["event_count"] == 0


def test_summarize_agent_result_handles_empty_or_partial_result_shape():
    class PartialResult:
        answer = None
        tool_calls = None

    summary = summarize_agent_result(PartialResult())

    assert summary["answer"] == ""
    assert summary["nodes"] == []
    assert summary["tool_calls"] == []
    assert summary["file_read_paths"] == []
    assert summary["file_reads"] == []
    assert summary["tool_call_count"] == 0
    assert summary["trace_summary"]["schema_version"] is None
