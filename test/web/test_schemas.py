# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for demo API schemas and response mapping."""

import pytest
from pydantic import ValidationError

from codenib.agent.agent_types import AgentResult, ToolCallRecord
from codenib.types import QueriedNode
from codenib.web.schemas import (
    ChatRequest,
    EdgeEndpoint,
    EdgeLabelRequest,
    agent_result_to_response,
)


def test_chat_request_bounds_message_count_and_total_context() -> None:
    with pytest.raises(ValidationError, match="at most 128"):
        ChatRequest(
            repo_id="repo",
            messages=[{"role": "user", "content": "x"}] * 129,
        )

    with pytest.raises(ValidationError, match="chat context limit"):
        ChatRequest(
            repo_id="repo",
            messages=[{"role": "user", "content": "x" * 60_000}] * 5,
        )


def test_chat_request_bounds_each_message() -> None:
    with pytest.raises(ValidationError, match="at most 65536"):
        ChatRequest(
            repo_id="repo",
            messages=[{"role": "user", "content": "x" * 65_537}],
        )


def test_chat_request_preserves_bounded_follow_up_history() -> None:
    request = ChatRequest(
        repo_id="repo",
        messages=[
            {"role": "user", "content": "Where is indexing configured?"},
            {"role": "assistant", "content": "See codenib/compiler."},
            {"role": "user", "content": "What calls it?"},
        ],
    )

    assert [message.role for message in request.messages] == [
        "user",
        "assistant",
        "user",
    ]


def test_edge_label_request_bounds_locations_and_anchor_count() -> None:
    endpoint = EdgeEndpoint(file="src/runtime.py", line=1)

    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        EdgeEndpoint(file="src/runtime.py", line=0)
    with pytest.raises(ValidationError, match="at most 32"):
        EdgeLabelRequest(
            source=endpoint,
            target=endpoint,
            anchors=[{"file": "src/runtime.py", "line": 1}] * 33,
        )


def test_edge_label_request_bounds_text_fields() -> None:
    endpoint = EdgeEndpoint(file="src/runtime.py", line=1)

    with pytest.raises(ValidationError, match="at most 4096"):
        EdgeEndpoint(file="f" * 4097, line=1)
    with pytest.raises(ValidationError, match="at most 1024"):
        EdgeEndpoint(file="src/runtime.py", line=1, label="n" * 1025)
    with pytest.raises(ValidationError, match="at most 128"):
        EdgeLabelRequest(
            source=endpoint,
            target=endpoint,
            commit="c" * 129,
        )
    with pytest.raises(ValidationError, match="at most 4096"):
        EdgeLabelRequest(
            source=endpoint,
            target=endpoint,
            anchors=[{"file": "a" * 4097, "line": 1}],
        )


def _node(file, start, end, name="fn", score=1.0, content="def fn(): ..."):
    return QueriedNode(
        node_name=name,
        type="function",
        file=file,
        start_line=start,
        end_line=end,
        score=score,
        content=content,
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


def test_citations_prefer_symbols_named_in_the_final_answer():
    result = AgentResult(
        answer=(
            "`index_repository()` delegates compilation to "
            "`IndexCompiler.compile_repo()`."
        ),
        tool_calls=[
            ToolCallRecord(
                "1",
                "repository_search",
                {},
                result=[
                    _node(
                        "codenib/cli.py",
                        92,
                        112,
                        "codenib/cli.py:resolve_manifest_path()",
                    ),
                    _node(
                        "codenib/cli.py",
                        137,
                        179,
                        "codenib/cli.py:index_repository()",
                    ),
                    _node(
                        "codenib/compiler/index_compiler.py",
                        84,
                        108,
                        (
                            "codenib/compiler/index_compiler.py:"
                            "IndexCompiler.compile_repo()"
                        ),
                    ),
                    _node(
                        "codenib/scip_interface/scip_indexer_php.py",
                        235,
                        239,
                        "SCIPPHPIndexer._build_index_command()",
                    ),
                ],
            )
        ],
    )

    resp = agent_result_to_response(result)

    assert [citation.node_name for citation in resp.citations] == [
        "codenib/cli.py:index_repository()",
        "codenib/compiler/index_compiler.py:IndexCompiler.compile_repo()",
    ]


def test_citations_keep_one_location_for_each_named_file():
    result = AgentResult(
        answer="The path starts in `a.py` and persists state in `b.py`.",
        tool_calls=[
            ToolCallRecord(
                "1",
                "repository_search",
                {},
                result=[
                    _node("a.py", 0, 9, "first"),
                    _node("a.py", 10, 19, "second"),
                    _node("b.py", 20, 29, "third"),
                    _node("noise.py", 30, 39, "noise"),
                ],
            )
        ],
    )

    resp = agent_result_to_response(result)

    assert [citation.file for citation in resp.citations] == ["a.py", "b.py"]


def test_citations_fall_back_to_five_retrieval_results():
    result = AgentResult(
        answer="The implementation follows this path.",
        tool_calls=[
            ToolCallRecord(
                "1",
                "repository_search",
                {},
                result=[
                    _node(f"{index}.py", index, index + 1, f"fn_{index}")
                    for index in range(7)
                ],
            )
        ],
    )

    resp = agent_result_to_response(result)

    assert [citation.file for citation in resp.citations] == [
        "0.py",
        "1.py",
        "2.py",
        "3.py",
        "4.py",
    ]


def test_citations_embed_exact_live_source_and_drop_empty_tabs(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "runtime.py").write_text(
        "# heading\ndef load_runtime():\n    return 'ready'\n",
        encoding="utf-8",
    )
    result = AgentResult(
        answer=(
            "`load_runtime()` is implemented in `src/runtime.py`; "
            "`missing_runtime()` is not in this checkout."
        ),
        tool_calls=[
            ToolCallRecord(
                "1",
                "repository_search",
                {},
                result=[
                    _node(
                        "src/runtime.py",
                        1,
                        2,
                        "src/runtime.py:load_runtime()",
                    ),
                    _node(
                        "src/missing.py",
                        1,
                        2,
                        "src/missing.py:missing_runtime()",
                    ),
                ],
            )
        ],
    )

    response = agent_result_to_response(result, repo_path=str(tmp_path))

    assert len(response.citations) == 1
    assert response.citations[0].file == "src/runtime.py"
    assert response.citations[0].start_line == 2
    assert response.citations[0].end_line == 3
    assert response.citations[0].content == (
        "def load_runtime():\n    return 'ready'\n"
    )


def test_citations_read_from_the_indexed_commit_instead_of_live_source(
    monkeypatch,
):
    commit = "a" * 40
    calls = []

    def snapshot_slice(repo_path, repo_commit, file, start, end):
        calls.append((repo_path, repo_commit, file, start, end))
        return {
            "file": file,
            "start_line": start,
            "end_line": end,
            "content": "def indexed_runtime():\n    return 'snapshot'\n",
        }

    def reject_live_read(*_args, **_kwargs):
        raise AssertionError("chat citation read from the mutable checkout")

    monkeypatch.setattr(
        "codenib.web.schemas.git_source_slice",
        snapshot_slice,
    )
    monkeypatch.setattr(
        "codenib.web.schemas.live_source_slice",
        reject_live_read,
    )
    result = AgentResult(
        answer="`indexed_runtime()` is implemented in `src/runtime.py`.",
        tool_calls=[
            ToolCallRecord(
                "1",
                "repository_search",
                {},
                result=[
                    _node(
                        "src/runtime.py",
                        1,
                        2,
                        "src/runtime.py:indexed_runtime()",
                    )
                ],
            )
        ],
    )

    response = agent_result_to_response(
        result,
        repo_path="/served/checkout",
        repo_commit=commit,
    )

    assert calls == [("/served/checkout", commit, "src/runtime.py", 2, 3)]
    assert response.citations[0].content == (
        "def indexed_runtime():\n    return 'snapshot'\n"
    )


def test_citations_keep_indexed_content_for_non_git_prebuilt_snapshot(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "runtime.py").write_text(
        "def mutable_runtime():\n    return 'live'\n",
        encoding="utf-8",
    )
    result = AgentResult(
        answer="`indexed_runtime()` is implemented in `src/runtime.py`.",
        tool_calls=[
            ToolCallRecord(
                "1",
                "repository_search",
                {},
                result=[
                    _node(
                        "src/runtime.py",
                        1,
                        2,
                        "src/runtime.py:indexed_runtime()",
                        content="def indexed_runtime():\n    return 'snapshot'\n",
                    )
                ],
            )
        ],
    )

    response = agent_result_to_response(
        result,
        repo_path=str(tmp_path),
        repo_commit="a" * 40,
    )

    assert len(response.citations) == 1
    assert response.citations[0].content == (
        "def indexed_runtime():\n    return 'snapshot'\n"
    )
    assert "mutable_runtime" not in response.citations[0].content


def test_plain_prose_does_not_match_a_generic_main_symbol():
    result = AgentResult(
        answer="The main entry point delegates to `index_repository()`.",
        tool_calls=[
            ToolCallRecord(
                "1",
                "repository_search",
                {},
                result=[
                    _node("scripts/index_repo.py", 1, 9, "main()"),
                    _node("codenib/cli.py", 10, 20, "index_repository()"),
                ],
            )
        ],
    )

    resp = agent_result_to_response(result)

    assert [citation.node_name for citation in resp.citations] == ["index_repository()"]


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
