# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for LSP-compatible static provider metadata."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from codeminer.agent.lsp_provider import (
    STATIC_LSP_PROVIDER,
    LSPProviderNodes,
    StaticLSPProvider,
)
from codeminer.agent.runner import AgentRunner
from codeminer.agent.skills.core import (
    SkillInputSpec,
    SkillMetadata,
    SkillOutputSpec,
    SkillType,
)
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.graph.code_graph import CodeGraph
from codeminer.llm.litellm_chat import LiteLLMChat
from codeminer.types import NODE_TYPE_FUNCTION


def _range_graph() -> CodeGraph:
    graph = CodeGraph()
    graph.add_file_node("caller.py")
    graph.add_symbol_node(
        "caller.run",
        line=0,
        scope_start_line=0,
        scope_end_line=3,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.update_current_scope("caller.run", start_line=0, end_line=3)
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


def _make_response(content=None, tool_calls=None):
    msg = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _make_tool_call(tc_id, name, arguments_json):
    return SimpleNamespace(
        id=tc_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments_json),
    )


def test_static_lsp_provider_returns_list_with_metadata():
    result = StaticLSPProvider(_range_graph(), snapshot_id="graph:demo").definition(
        symbol="load_config"
    )

    assert isinstance(result, list)
    assert isinstance(result, LSPProviderNodes)
    assert [node.node_name for node in result] == ["callee.py:load_config()"]
    metadata = result.provider_metadata_dict()
    assert metadata["provider"] == STATIC_LSP_PROVIDER
    assert metadata["capability"] == "definition"
    assert metadata["status"] == "ok"
    assert metadata["lsp_method"] == "textDocument/definition"
    assert metadata["index_snapshot"] == "graph:demo"


def test_static_lsp_provider_reports_fallback_reason_without_graph():
    decision = StaticLSPProvider(None).can_serve("textDocument/definition")

    assert decision.status == "unavailable"
    assert decision.fallback_reason == "symbol_graph_unavailable"
    assert decision.provider == STATIC_LSP_PROVIDER


def test_runner_traces_static_lsp_provider_for_dynamic_tool_call():
    graph = _range_graph()

    def executor(**kwargs):
        return StaticLSPProvider(graph, snapshot_id="graph:trace").definition(**kwargs)

    registry = SkillRegistry()
    registry.register(
        SkillMetadata(
            skill_id="lsp_definition",
            skill_type=SkillType.EXPAND,
            inputs=[
                SkillInputSpec(name="symbol", type_hint="str", required=False),
                SkillInputSpec(name="top_k", type_hint="int", required=False),
            ],
            outputs=SkillOutputSpec(type_hint="List[QueriedNode]"),
            executor_fn=executor,
        )
    )

    llm = MagicMock(spec=LiteLLMChat)
    llm._call_raw.side_effect = [
        _make_response(
            tool_calls=[
                _make_tool_call(
                    "call_1",
                    "lsp_definition",
                    '{"symbol": "load_config", "top_k": 2}',
                )
            ]
        ),
        _make_response(content="done"),
    ]

    result = AgentRunner(
        llm,
        registry,
        include_default_tools=False,
        force_localization_contract=False,
    ).run("jump to load_config")

    tool_event = next(
        event for event in result.trace.events if event.kind == "tool_call"
    )
    assert tool_event.data["tool"] == "lsp_definition"
    assert tool_event.data["result_count"] == 1
    assert tool_event.data["lsp_provider"] == {
        "provider": STATIC_LSP_PROVIDER,
        "capability": "definition",
        "status": "ok",
        "lsp_method": "textDocument/definition",
        "behavior_contract": "static_graph_lsp_v1",
        "position_granularity": "line",
        "index_snapshot": "graph:trace",
    }
    assert len(tool_event.data["lsp_result_fingerprint"]) == 64
    assert tool_event.data["lsp_result_preview"][0]["location"] == "callee.py:5-9"
