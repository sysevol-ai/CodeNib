# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for controlled static-vs-live LSP agent A/B evaluation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from codeminer.agent.lsp_provider import StaticLSPProvider
from codeminer.eval.agent_runner.lsp_agent_ab import (
    exact_lsp_result_guard,
    lsp_agent_prompt,
    run_lsp_agent_arm,
    summarize_lsp_agent_ab,
)
from codeminer.eval.agent_runner.lsp_provider_validation import LSPProviderRequest
from codeminer.graph.code_graph import CodeGraph
from codeminer.llm.litellm_chat import LiteLLMChat
from codeminer.types import NODE_TYPE_FUNCTION


def _graph(tmp_path):
    (tmp_path / "caller.py").write_text(
        "def run():\n    return load_config()\n", encoding="utf-8"
    )
    graph = CodeGraph(project_root=str(tmp_path))
    graph.add_file_node("caller.py")
    graph.add_symbol_node(
        "caller.run",
        line=0,
        scope_start_line=0,
        scope_end_line=1,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.update_current_scope("caller.run", start_line=0, end_line=1)
    graph.add_symbol_reference(
        "callee.load_config",
        module_path="callee.py",
        symbol_type=NODE_TYPE_FUNCTION,
        anchor_file="caller.py",
        anchor_line=1,
    )
    graph.add_file_node("callee.py")
    graph.add_symbol_node(
        "callee.load_config",
        line=4,
        scope_start_line=4,
        scope_end_line=8,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.graph.vs[graph.name_to_vertex["callee.load_config"]][
        "unified_name"
    ] = "callee.py:load_config()"
    graph.build_range_indexes()
    return graph


def _request():
    return LSPProviderRequest(
        capability="textDocument/definition",
        arguments={
            "file_path": "caller.py",
            "line": 1,
            "character": 15,
            "top_k": 8,
        },
        request_id="definition:caller.py:1:15:load_config",
    )


def _response(*, content=None, tool_calls=None):
    message = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _tool_call():
    return SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(
            name="lsp_definition",
            arguments=(
                '{"file_path":"caller.py","line":2,' '"character":15,"top_k":8}'
            ),
        ),
    )


def test_lsp_agent_prompt_converts_internal_line_to_agent_line():
    prompt = lsp_agent_prompt(_request())

    assert '"line": 2' in prompt
    assert "lsp_definition" in prompt


def test_exact_guard_ignores_provider_metadata(tmp_path):
    provider = StaticLSPProvider(_graph(tmp_path), snapshot_id="test")

    guard = exact_lsp_result_guard(
        _request(), static_provider=provider, live_provider=provider
    )

    assert guard["exact_match"] is True
    assert guard["static_result_sha256"] == guard["live_result_sha256"]


def test_run_lsp_agent_arm_records_provider_and_protocol(tmp_path):
    graph = _graph(tmp_path)
    provider = StaticLSPProvider(graph, snapshot_id="test")
    llm = MagicMock(spec=LiteLLMChat)
    llm.model = "fake-haiku"
    llm._call_raw.side_effect = [
        _response(tool_calls=[_tool_call()]),
        _response(content="callee.py:5"),
    ]

    row = run_lsp_agent_arm(
        _request(),
        arm="static",
        provider=provider,
        graph=graph,
        llm=llm,
        repo_path=tmp_path,
    )

    assert row["protocol_ok"] is True
    assert row["provider"] == "codeminer_static_index"
    assert row["resolved_arguments"]["line"] == 1
    assert row["expected_locations"] == ["callee.py:5"]
    assert row["answer_contains_all_locations"] is True
    assert row["total_turns"] == 2


def test_summary_keeps_agent_wall_time_separate_from_tool_latency():
    payload = {
        "cells": [
            {
                "request_id": "r1",
                "rep": 1,
                "arm": "static",
                "protocol_ok": True,
                "answer_contains_all_locations": True,
                "tool_result_sha256": "same",
                "answer": "a.py:1",
                "tool_duration_ms": 1.0,
                "total_duration_ms": 1000.0,
                "total_turns": 2,
                "usage": {"total_tokens": 100},
            },
            {
                "request_id": "r1",
                "rep": 1,
                "arm": "live",
                "protocol_ok": True,
                "answer_contains_all_locations": True,
                "tool_result_sha256": "same",
                "answer": "a.py:1",
                "tool_duration_ms": 5.0,
                "total_duration_ms": 900.0,
                "total_turns": 2,
                "usage": {"total_tokens": 100},
            },
        ]
    }

    summary = summarize_lsp_agent_ab(payload)

    assert summary["tool_result_equal_pair_count"] == 1
    assert summary["by_arm"]["static"]["tool_duration_ms_p50"] == 1.0
    assert summary["by_arm"]["static"]["agent_duration_ms_p50"] == 1000.0
