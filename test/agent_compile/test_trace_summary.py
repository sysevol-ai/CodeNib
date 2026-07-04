import json

from scripts.agent_compile.lib.trace_summary import (
    build_cell_replay,
    link_answer_spans_to_trace,
    load_cell_replay,
    load_cell_trace_summary,
    render_replay_markdown,
    render_summary_markdown,
    summarize_answer_evidence,
    summarize_cell_trace,
    summarize_replay_readiness,
    summarize_trace,
)
from scripts.agent_compile.summarize_trace import main as summarize_main


def _cell():
    return {
        "cell_id": "repo-1__q1__arm__rep1",
        "instance_id": "repo-1",
        "query_id": "q1",
        "category": "traversal",
        "subset_id": "preinj_graph_scheduled",
        "success": True,
        "format_failed": False,
        "answer_span_count": 3,
        "answer_spans": [
            {"file": "src/a.py", "start": 12, "end": 15, "source": "answer"},
            {"file": "src/b.py", "start": 1, "end": 5, "source": "answer"},
            {"file": "src/d.py", "start": 1, "end": 5, "source": "answer"},
        ],
        "total_turns": 3,
        "total_tokens": 1234,
        "metrics": {"answer_blocks": {"5": {"recall": 1.0}}},
        "trace": {
            "schema_version": 1,
            "events": [
                {
                    "kind": "run_start",
                    "turn": 0,
                    "data": {
                        "max_turns": 12,
                        "tool_count": 4,
                        "query_chars": 100,
                        "preload_candidates": 2,
                    },
                },
                {
                    "kind": "assistant",
                    "turn": 1,
                    "data": {"content_chars": 20, "tool_calls": ["read"]},
                },
                {
                    "kind": "tool_call",
                    "turn": 1,
                    "data": {
                        "tool": "read",
                        "tool_call_id": "call_1",
                        "arguments": {
                            "file_path": "src/a.py",
                            "offset": 10,
                            "limit": 20,
                        },
                        "error": None,
                        "duplicate": False,
                        "duration_ms": 1.5,
                        "result_chars": 400,
                    },
                },
                {
                    "kind": "compaction",
                    "turn": 1,
                    "data": {
                        "seed_chars": 200,
                        "protected_tail_chars": 50,
                        "original_tool_result_chars": 400,
                        "pruned_tool_result_chars": 100,
                    },
                },
                {
                    "kind": "scheduled_context",
                    "turn": 2,
                    "data": {
                        "operation": "lsp_route",
                        "reason": "read_symbols",
                        "node_count": 3,
                        "route_read_confirmed_core_count": 2,
                        "route_unread_core_count": 1,
                        "route_roles": ["endpoint", "bridge", "provider"],
                        "locations": [
                            {
                                "file": "src/b.py",
                                "start_line": 1,
                                "end_line": 5,
                                "role": "provider",
                                "name": "src/b.py:Provider",
                                "source": "route_neighbor",
                            }
                        ],
                    },
                },
                {
                    "kind": "scheduled_top_anchor_audit",
                    "turn": 0,
                    "data": {"missing_count": 1},
                },
                {
                    "kind": "run_end",
                    "turn": 3,
                    "data": {
                        "answer_chars": 80,
                        "tool_calls": 1,
                        "duration_ms": 250.0,
                    },
                },
            ],
            "context": [
                {
                    "source": "preload",
                    "state": "verified",
                    "path": "src/a.py",
                    "tool_call_id": "call_1",
                    "metadata": {"rank": 1, "start_line": 12, "end_line": 15},
                },
                {
                    "source": "scheduled_graph_lsp",
                    "state": "offered",
                    "path": None,
                    "metadata": {"node_count": 3},
                },
                {
                    "source": "read",
                    "state": "read",
                    "path": "src/a.py",
                    "tool_call_id": "call_1",
                    "metadata": {"start_line": 10, "end_line": 29},
                },
            ],
        },
    }


def test_summarize_trace_counts_tools_context_and_scheduled_route():
    summary = summarize_cell_trace(_cell())

    assert summary["trace_schema_version"] == 1
    assert summary["has_run_bounds"] is True
    assert summary["cell"]["answer_rec_at_5"] == 1.0
    assert summary["events"]["kinds"]["tool_call"] == 1
    assert summary["tools"]["by_name"] == {"read": 1}
    assert summary["reads"]["unique_paths"] == ["src/a.py"]
    assert summary["context"]["states"] == {"offered": 1, "read": 1, "verified": 1}
    assert summary["context"]["source_states"] == {
        "preload:verified": 1,
        "read:read": 1,
        "scheduled_graph_lsp:offered": 1,
    }
    assert summary["compaction"]["pruned_tool_result_chars"] == 100
    assert summary["scheduled_context"]["lsp_route_count"] == 1
    assert summary["scheduled_context"]["route_roles"] == {
        "bridge": 1,
        "endpoint": 1,
        "provider": 1,
    }
    assert summary["audits"]["scheduled_top_anchor_audit"] == 1
    assert summary["answer_evidence"]["span_count"] == 3
    assert summary["answer_evidence"]["trace_evidenced_count"] == 2
    assert summary["answer_evidence"]["trace_evidenced_ratio"] == 2 / 3
    assert summary["answer_evidence"]["read_confirmed_count"] == 1
    assert summary["answer_evidence"]["read_confirmed_ratio"] == 1 / 3
    assert summary["answer_evidence"]["unconfirmed_spans"] == [
        {"file": "src/d.py", "start": 1, "end": 5}
    ]
    readiness = summary["replay_readiness"]
    assert readiness["event_replayable"] is True
    assert readiness["exact_message_replayable"] is False
    assert readiness["blocking_gaps"] == [
        "missing_message_transcript",
        "assistant_content_not_persisted",
        "tool_result_content_not_persisted",
        "tool_envelope_incomplete",
    ]


def test_summarize_answer_evidence_ignores_answer_symbol_spans():
    summary = summarize_answer_evidence(
        {
            "answer_spans": [
                {"file": "src/a.py", "start": 1, "end": 5, "source": "answer"},
                {
                    "file": "src/a.py",
                    "start": 100,
                    "end": 101,
                    "source": "answer_symbol",
                },
            ],
            "answer_span_evidence": [
                {
                    "answer_index": 1,
                    "span": {"file": "src/a.py", "start": 1, "end": 5},
                    "trace_evidenced": True,
                    "read_confirmed": True,
                },
                {
                    "answer_index": 2,
                    "span": {"file": "src/a.py", "start": 100, "end": 101},
                    "trace_evidenced": False,
                    "read_confirmed": False,
                },
            ],
        }
    )

    assert summary["span_count"] == 1
    assert summary["trace_evidenced_count"] == 1
    assert summary["trace_evidenced_ratio"] == 1.0
    assert summary["unconfirmed_spans"] == []
    assert summary["ignored_symbol_span_count"] == 1


def test_trace_summary_loads_cell_json_and_renders_markdown(tmp_path, capsys):
    path = tmp_path / "cell.json"
    path.write_text(json.dumps(_cell()), encoding="utf-8")

    summary = load_cell_trace_summary(path)
    assert summary["cell"]["cell_id"] == "repo-1__q1__arm__rep1"
    assert summary["reads"]["count"] == 1

    markdown = render_summary_markdown(summary)
    assert "Agent Trace Summary" in markdown
    assert "lsp_route=1" in markdown
    assert "2/3 trace-evidenced" in markdown
    assert "1/3 read-confirmed" in markdown
    assert "replay_readiness" in markdown

    assert summarize_main([str(path), "--format", "markdown"]) == 0
    out = capsys.readouterr().out
    assert "repo-1__q1__arm__rep1" in out
    assert "answer_evidence" in out


def test_summarize_trace_requires_exact_replay_fails_with_blockers(tmp_path, capsys):
    path = tmp_path / "cell.json"
    path.write_text(json.dumps(_cell()), encoding="utf-8")

    assert summarize_main([str(path), "--require-exact-replay"]) == 2
    captured = capsys.readouterr()

    assert '"replay_readiness"' in captured.out
    assert "exact replay gate failed" in captured.err
    assert "missing_message_transcript" in captured.err
    assert "tool_envelope_incomplete" in captured.err


def test_summarize_trace_requires_exact_replay_passes(tmp_path, capsys):
    cell = _cell()
    cell["messages"] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "query"},
        {"role": "assistant", "content": "I will read.", "tool_calls": ["read"]},
        {"role": "tool", "tool_call_id": "call_1", "content": "file content"},
        {"role": "assistant", "content": "Files: src/a.py"},
    ]
    tool_event = next(
        event for event in cell["trace"]["events"] if event["kind"] == "tool_call"
    )
    tool_event["data"]["envelope"] = {
        "status": "ok",
        "result_type": "text",
        "result_chars": 12,
    }
    path = tmp_path / "cell.json"
    path.write_text(json.dumps(cell), encoding="utf-8")

    assert (
        summarize_main([str(path), "--format", "replay-json", "--require-exact-replay"])
        == 0
    )
    captured = capsys.readouterr()

    assert '"exact_message_replayable": true' in captured.out
    assert captured.err == ""


def test_build_cell_replay_reconstructs_turn_timeline(tmp_path, capsys):
    cell = _cell()
    cell["answer"] = "Files: src/a.py\nLocations: src/a.py:12-15"
    cell["trace"]["events"].append(
        {
            "kind": "answer_schema_salvage",
            "turn": 0,
            "data": {
                "reason": "read_confirmed_schema_salvage",
                "location_count": 1,
            },
        }
    )
    path = tmp_path / "cell.json"
    path.write_text(json.dumps(cell), encoding="utf-8")

    replay = load_cell_replay(path)

    assert replay["replay_version"] == 2
    assert replay["cell"]["cell_id"] == "repo-1__q1__arm__rep1"
    assert [item["kind"] for item in replay["timeline"][:3]] == [
        "run_start",
        "assistant",
        "tool_call",
    ]
    assert replay["timeline"][2]["summary"].startswith("tool read src/a.py:10+20")
    assert any("scheduled lsp_route" in item["summary"] for item in replay["timeline"])
    assert any(
        "answer_schema_salvage" in item["summary"] for item in replay["timeline"]
    )
    assert replay["answer"]["text"].startswith("Files:")
    assert replay["answer"]["evidence"][0]["read_confirmed"] is True
    assert replay["replay_readiness"]["exact_message_replayable"] is False

    markdown = render_replay_markdown(replay)
    assert "Agent Trace Replay" in markdown
    assert "`tool_call` tool read src/a.py:10+20" in markdown
    assert "## Answer Spans" in markdown
    assert "replay_readiness" in markdown

    assert summarize_main([str(path), "--format", "replay-markdown"]) == 0
    out = capsys.readouterr().out
    assert "Agent Trace Replay" in out
    assert "scheduled lsp_route" in out


def test_replay_readiness_accepts_full_message_transcript():
    cell = _cell()
    cell["messages"] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "query"},
        {"role": "assistant", "content": "I will read.", "tool_calls": ["read"]},
        {"role": "tool", "tool_call_id": "call_1", "content": "file content"},
        {"role": "assistant", "content": "Files: src/a.py"},
    ]
    tool_event = next(
        event for event in cell["trace"]["events"] if event["kind"] == "tool_call"
    )
    tool_event["data"]["envelope"] = {
        "status": "ok",
        "result_type": "text",
        "result_chars": 12,
    }

    readiness = summarize_replay_readiness(cell)

    assert readiness["has_message_transcript"] is True
    assert readiness["exact_message_replayable"] is True
    assert readiness["message_roles"] == {
        "assistant": 2,
        "system": 1,
        "tool": 1,
        "user": 1,
    }
    assert readiness["tool_envelope_coverage"] == 1.0
    assert readiness["blocking_gaps"] == []


def test_replay_readiness_accepts_trace_message_replay():
    cell = _cell()
    cell["trace"]["tool_schema_replay"] = [
        {"type": "function", "function": {"name": "read"}}
    ]
    cell["trace"]["message_replay"] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "query"},
        {"role": "assistant", "tool_calls": ["read"]},
        {"role": "tool", "tool_call_id": "call_1", "content": "file content"},
        {"role": "assistant", "content": "Files: src/a.py"},
    ]
    cell["trace"]["events"].insert(
        1,
        {
            "kind": "llm_call",
            "turn": 1,
            "data": {
                "message_replay_indexes": [0, 1],
                "message_count": 2,
                "tool_count": 1,
                "tool_schema_indexes": [0],
            },
        },
    )
    cell["trace"]["events"].append(
        {
            "kind": "llm_call",
            "turn": 2,
            "data": {
                "message_replay_indexes": [0, 1, 2, 3],
                "message_count": 4,
                "tool_count": 1,
                "tool_schema_indexes": [0],
            },
        }
    )
    tool_event = next(
        event for event in cell["trace"]["events"] if event["kind"] == "tool_call"
    )
    tool_event["data"]["envelope"] = {
        "status": "ok",
        "result_type": "text",
        "result_chars": 12,
    }

    readiness = summarize_replay_readiness(cell)
    replay = build_cell_replay(cell)

    assert readiness["has_message_transcript"] is True
    assert readiness["exact_message_replayable"] is True
    assert readiness["llm_call_event_count"] == 2
    assert readiness["llm_call_message_index_coverage"] == 1.0
    assert readiness["llm_call_tool_schema_index_coverage"] == 1.0
    assert readiness["blocking_gaps"] == []
    assert len(replay["message_replay"]) == 5
    assert len(replay["tool_schema_replay"]) == 1


def test_replay_tool_summary_prefers_typed_envelope():
    cell = _cell()
    tool_event = next(
        event for event in cell["trace"]["events"] if event["kind"] == "tool_call"
    )
    tool_event["data"]["envelope"] = {
        "status": "error",
        "result_type": "error",
        "error_kind": "tool_result_error",
        "result_chars": 20000,
        "truncated": True,
        "metadata": {
            "permission_boundary": "process_read_permissions",
            "sandbox": "caller_managed",
            "side_effects_possible": False,
            "exit_code": 1,
        },
    }

    replay = build_cell_replay(cell)

    tool_item = next(item for item in replay["timeline"] if item["kind"] == "tool_call")
    assert "error_kind=tool_result_error" in tool_item["summary"]
    assert "truncated" in tool_item["summary"]
    assert "perm=process_read_permissions" in tool_item["summary"]
    assert "sandbox=caller_managed" in tool_item["summary"]
    assert "sidefx=false" in tool_item["summary"]
    assert "exit=1" in tool_item["summary"]


def test_link_answer_spans_to_trace_uses_read_confirmed_ledger_entries():
    links = link_answer_spans_to_trace(
        [
            {"file": "src/a.py", "start": 10, "end": 12},
            {"file": "src/c.py", "start": 1, "end": 2},
        ],
        _cell()["trace"],
    )

    assert links[0]["read_confirmed"] is True
    assert links[0]["evidence_count"] == 2
    assert {entry["state"] for entry in links[0]["evidence"]} == {
        "read",
        "verified",
    }
    assert links[1]["read_confirmed"] is False
    assert links[1]["evidence"] == []


def test_summarize_trace_tolerates_missing_trace():
    summary = summarize_trace(None)

    assert summary["has_trace"] is False
    assert summary["events"]["count"] == 0
    assert summary["tools"]["count"] == 0
    assert summary["reads"]["unique_paths"] == []
