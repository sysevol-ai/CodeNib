# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the demo API response mapping (pure logic, no indexes)."""

from codeminer.agent.agent_types import AgentResult, ToolCallRecord
from codeminer.types import QueriedNode
from codeminer.web.schemas import agent_result_to_response


def _node(file, start, end, name="fn", score=1.0):
    return QueriedNode(
        node_name=name,
        type="function",
        file=file,
        start_line=start,
        end_line=end,
        score=score,
        content="def fn(): ...",
    )


def test_maps_tool_calls_and_citations():
    result = AgentResult(
        answer="It works like X.",
        tool_calls=[
            ToolCallRecord(
                tool_call_id="1",
                skill_id="bm25_search",
                arguments={"query": "auth", "top_k": 5},
                result=[_node("a.py", 1, 10), _node("b.py", 20, 30)],
                duration_ms=12.5,
            )
        ],
        total_turns=2,
        total_duration_ms=99.0,
    )

    resp = agent_result_to_response(result)

    assert resp.answer == "It works like X."
    assert resp.total_turns == 2
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].skill_id == "bm25_search"
    assert resp.tool_calls[0].result_count == 2
    assert len(resp.citations) == 2
    assert resp.citations[0].file == "a.py"
    assert resp.citations[0].start_line == 1


def test_citations_are_deduplicated_across_tool_calls():
    same = _node("a.py", 1, 10)
    result = AgentResult(
        answer="ans",
        tool_calls=[
            ToolCallRecord("1", "bm25_search", {}, result=[same]),
            ToolCallRecord("2", "embedding_search", {}, result=[_node("a.py", 1, 10)]),
        ],
    )
    resp = agent_result_to_response(result)
    assert len(resp.citations) == 1
    assert len(resp.tool_calls) == 2


def test_error_tool_call_has_no_citations():
    result = AgentResult(
        answer="ans",
        tool_calls=[ToolCallRecord("1", "bm25_search", {}, error="index missing")],
    )
    resp = agent_result_to_response(result)
    assert resp.citations == []
    assert resp.tool_calls[0].error == "index missing"
    assert resp.tool_calls[0].result_count == 0


def test_handles_dict_results():
    result = AgentResult(
        answer="ans",
        tool_calls=[
            ToolCallRecord(
                "1",
                "search_zoekt",
                {},
                result=[{"file": "c.py", "start_line": 5, "end_line": 9, "name": "g"}],
            )
        ],
    )
    resp = agent_result_to_response(result)
    assert len(resp.citations) == 1
    assert resp.citations[0].file == "c.py"
    assert resp.citations[0].node_name == "g"
