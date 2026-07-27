# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the demo API response mapping (pure logic, no indexes)."""

from codenib.agent.agent_types import AgentResult, ToolCallRecord
from codenib.types import QueriedNode
from codenib.web.schemas import agent_result_to_response


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
                result=[_node("a.py", 0, 9), _node("b.py", 19, 29)],
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
    assert resp.citations[0].end_line == 10


def test_citations_are_deduplicated_across_tool_calls():
    same = _node("a.py", 0, 9)
    result = AgentResult(
        answer="ans",
        tool_calls=[
            ToolCallRecord("1", "bm25_search", {}, result=[same]),
            ToolCallRecord("2", "embedding_search", {}, result=[_node("a.py", 0, 9)]),
        ],
    )
    resp = agent_result_to_response(result)
    assert len(resp.citations) == 1
    assert len(resp.tool_calls) == 2


def test_citations_prefer_files_and_symbols_named_in_the_answer():
    result = AgentResult(
        answer=("`AuthService` is defined in `src/auth.py` and owns authentication."),
        tool_calls=[
            ToolCallRecord(
                "1",
                "bm25_search",
                {},
                result=[
                    _node("src/bootstrap.py", 0, 9, "bootstrap"),
                    _node("src/auth.py", 10, 30, "AuthService.login"),
                    _node("src/config.py", 0, 9, "Config"),
                ],
            )
        ],
    )

    response = agent_result_to_response(result)

    assert [citation.file for citation in response.citations] == ["src/auth.py"]


def test_citations_bound_unnamed_retrieval_candidates():
    result = AgentResult(
        answer="The repository has several relevant components.",
        tool_calls=[
            ToolCallRecord(
                "1",
                "bm25_search",
                {},
                result=[_node(f"src/{index}.py", 0, 9) for index in range(10)],
            )
        ],
    )

    response = agent_result_to_response(result)

    assert len(response.citations) == 5
    assert [citation.file for citation in response.citations] == [
        f"src/{index}.py" for index in range(5)
    ]


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
    assert resp.citations[0].start_line == 6
    assert resp.citations[0].end_line == 10


class TestWindowStatsOnRepoInfo:
    """RepoInfo carries commit-window figures for the landing page."""

    def test_absent_by_default(self):
        from codenib.web.schemas import RepoInfo

        assert RepoInfo(id="x", name="x").incremental is None

    def test_round_trips(self):
        from codenib.web.schemas import RepoInfo, WindowStats

        info = RepoInfo(
            id="x",
            name="x",
            incremental=WindowStats(
                commit_count=5,
                patched_count=4,
                cold_seconds=96.5,
                mean_patch_seconds=4.0,
                speedup=24.1,
            ),
        )
        dumped = info.model_dump()
        assert dumped["incremental"]["speedup"] == 24.1
        assert dumped["incremental"]["commit_count"] == 5

    def test_speedup_may_be_null(self):
        """No defensible ratio must be representable as null, not 0 or NaN."""
        from codenib.web.schemas import WindowStats

        s = WindowStats(commit_count=1, patched_count=0)
        assert s.speedup is None
        assert s.cold_seconds is None


def test_repo_info_graph_coverage_is_optional_and_structured():
    from codenib.web.schemas import GraphCoverage, RepoInfo

    assert RepoInfo(id="x", name="x").graph_coverage is None
    info = RepoInfo(
        id="x",
        name="x",
        graph_coverage=GraphCoverage(
            available_languages=["python", "ts"],
            unavailable_languages=["cpp"],
            partial=True,
        ),
    )

    assert info.model_dump()["graph_coverage"] == {
        "available_languages": ["python", "ts"],
        "unavailable_languages": ["cpp"],
        "partial": True,
    }
