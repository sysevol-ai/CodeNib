# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for LSP-compatible static provider metadata."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codenib.agent.lsp_provider import (
    STATIC_LSP_PROVIDER,
    LSPPositionFallback,
    LSPProviderNodes,
    NativeOccurrenceQueryAdapter,
    StaticLSPProvider,
    normalize_native_lsp_nodes,
    select_checkout_lsp_provider,
)
from codenib.agent.runner import AgentRunner
from codenib.agent.skills.core import (
    SkillInputSpec,
    SkillMetadata,
    SkillOutputSpec,
    SkillType,
)
from codenib.agent.skills.registry import SkillRegistry
from codenib.graph.code_graph import CodeGraph
from codenib.llm.litellm_chat import LiteLLMChat
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.scip_interface.lsp_occurrence_index import (
    SCIPOccurrence,
    SCIPOccurrenceIndex,
)
from codenib.types import NODE_TYPE_FUNCTION


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
    assert [node.node_name for node in result] == ["callee.py:5"]
    assert result[0].type == "definition"
    assert result[0].content == "lsp definition"
    metadata = result.provider_metadata_dict()
    assert metadata["provider"] == STATIC_LSP_PROVIDER
    assert metadata["capability"] == "definition"
    assert metadata["status"] == "ok"
    assert metadata["lsp_method"] == "textDocument/definition"
    assert metadata["index_snapshot"] == "graph:demo"


def test_static_lsp_provider_prefers_a_content_bound_graph_snapshot():
    graph = _range_graph()
    legacy_snapshot = StaticLSPProvider(graph).snapshot_id
    graph.snapshot_id = "clangd_fact_query:sha256:content"

    assert legacy_snapshot.startswith("symbol_graph:")
    assert StaticLSPProvider(graph).snapshot_id == graph.snapshot_id


def test_static_lsp_provider_uses_exact_scip_occurrences_for_native_positions():
    index = SCIPOccurrenceIndex(
        [
            SCIPOccurrence("caller.py", 1, 4, 1, 10, "local 0", 1),
            SCIPOccurrence("caller.py", 2, 8, 2, 14, "local 0", 8),
        ]
    )
    provider = StaticLSPProvider(
        _range_graph(),
        snapshot_id="snapshot:demo",
        occurrence_index=index,
    )

    definition = provider.definition(file_path="caller.py", line=2, character=9)
    references = provider.references(file_path="caller.py", line=1, character=5)

    assert [(node.file, node.start_line) for node in definition] == [("caller.py", 1)]
    assert [(node.file, node.start_line) for node in references] == [
        ("caller.py", 1),
        ("caller.py", 2),
    ]
    assert definition.provider_metadata_dict()["behavior_contract"] == (
        "static_scip_occurrence_lsp_v1"
    )
    assert definition.provider_metadata_dict()["position_granularity"] == ("character")


def test_graph_position_backend_records_character_contract(tmp_path):
    (tmp_path / "caller.py").write_text(
        "def run():\n    load_config()\n", encoding="utf-8"
    )
    graph = _range_graph()
    graph.project_root = str(tmp_path)
    provider = StaticLSPProvider(graph, snapshot_id="snapshot:graph-position")

    definition = provider.definition(
        file_path="caller.py", line=1, character=6, top_k=8
    )

    assert [(node.file, node.start_line) for node in definition] == [("callee.py", 4)]
    assert definition.provider_metadata_dict()["behavior_contract"] == (
        "static_symbol_graph_position_lsp_v1"
    )
    assert definition.provider_metadata_dict()["position_granularity"] == "character"


@pytest.mark.parametrize(
    ("position_encoding", "character"),
    [("UTF8", 5), ("UTF16", 3), ("UTF32", 2)],
)
def test_native_occurrence_adapter_normalizes_unicode_character_units(
    tmp_path, position_encoding, character
):
    (tmp_path / "caller.cpp").write_text("😀 target();\n", encoding="utf-8")

    class QueryIndex:
        project_root = str(tmp_path)
        occurrence_count = 1

        def __init__(self):
            self.position_encoding = position_encoding

        @staticmethod
        def position_definitions(**_arguments):
            return {
                "served": True,
                "fallback_reason": None,
                "locations": [
                    {
                        "file_path": "target.cpp",
                        "start_line": 8,
                        "start_character": 4,
                        "end_line": 8,
                        "end_character": 10,
                    }
                ],
                "targets": [0],
            }

        @staticmethod
        def get_node_info_by_id(_target):
            return {"name": "canonical", "unified_name": "target.cpp:target()"}

    adapter = NativeOccurrenceQueryAdapter(QueryIndex())

    assert (
        adapter.definitions(file_path="caller.cpp", line=0, character=character)[0][
            "file_path"
        ]
        == "target.cpp"
    )
    with pytest.raises(
        LSPPositionFallback, match="native_position_token_requires_legacy_graph"
    ):
        adapter.definitions(file_path="caller.cpp", line=0, character=0)


def test_native_lsp_result_normalization_is_stable_and_deduplicated():
    result = normalize_native_lsp_nodes(
        [
            {"file": "z.py", "start_line": 8},
            {"file_path": "a.py", "start_line": "2"},
            {"file": "z.py", "start_line": 8},
        ],
        capability="textDocument/references",
    )

    assert [node.model_dump() for node in result] == [
        {
            "node_name": "a.py:3",
            "type": "references",
            "file": "a.py",
            "node_id": "a.py:3:references",
            "start_line": 2,
            "end_line": 2,
            "score": 1.0,
            "content": "lsp references",
        },
        {
            "node_name": "z.py:9",
            "type": "references",
            "file": "z.py",
            "node_id": "z.py:9:references",
            "start_line": 8,
            "end_line": 8,
            "score": 1.0,
            "content": "lsp references",
        },
    ]


def test_static_lsp_provider_reports_fallback_reason_without_graph():
    decision = StaticLSPProvider(None).can_serve("textDocument/definition")

    assert decision.status == "unavailable"
    assert decision.fallback_reason == "symbol_graph_unavailable"
    assert decision.provider == STATIC_LSP_PROVIDER


def test_persisted_provider_fallback_is_explicit_in_result_metadata():
    provider, selection = select_checkout_lsp_provider(
        project_root="/portable/repo",
        languages=["cpp"],
        symbol_graph=_range_graph(),
        allow_native=False,
        native_disabled_reason="portable_artifact_uses_persisted_graph",
    )

    result = provider.definition(symbol="load_config")
    metadata = result.provider_metadata_dict()

    assert selection["backend"] == "persisted-symbol-graph-v1"
    assert selection["fallback_reason"] == ("portable_artifact_uses_persisted_graph")
    assert metadata["backend"] == "persisted-symbol-graph-v1"
    assert metadata["fallback_reason"] == ("portable_artifact_uses_persisted_graph")


def test_mixed_language_checkout_does_not_hide_graph_symbols():
    graph = _range_graph()
    provider, selection = select_checkout_lsp_provider(
        project_root="/mixed/repo",
        languages=["cpp", "python"],
        symbol_graph=graph,
    )

    assert provider.graph is graph
    assert selection["backend"] == "persisted-symbol-graph-v1"
    assert selection["fallback_reason"] == ("mixed_language_requires_persisted_graph")
    assert selection["capabilities"] == {
        "definition": True,
        "references": True,
        "route": True,
    }


def test_unknown_language_alongside_cpp_forces_graph_fallback():
    graph = _range_graph()
    provider, selection = select_checkout_lsp_provider(
        project_root="/mixed/repo",
        languages=["cpp", "unregistered-language"],
        symbol_graph=graph,
    )

    assert provider.graph is graph
    assert selection["backend"] == "persisted-symbol-graph-v1"
    assert selection["fallback_reason"] == ("mixed_language_requires_persisted_graph")


@pytest.mark.parametrize(
    "selection_policy",
    (
        RepositorySourceSelection(),
        RepositorySourceSelection(("private[1]",)),
    ),
)
def test_manifest_source_policy_disables_unproved_native_cpp_provider(
    selection_policy,
):
    graph = _range_graph()

    with patch("codenib.ls_router.LSIndexer") as indexer:
        provider, selection = select_checkout_lsp_provider(
            project_root="/local/repo",
            languages=["cpp"],
            symbol_graph=graph,
            source_selection=selection_policy,
        )

    indexer.assert_not_called()
    assert provider.graph is graph
    assert selection["backend"] == "persisted-symbol-graph-v1"
    assert selection["fallback_reason"] == (
        "repository_source_policy_requires_persisted_graph"
    )


def test_runtime_native_off_switch_uses_persisted_provider(monkeypatch):
    graph = _range_graph()
    monkeypatch.setenv("CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX", "off")

    with patch("codenib.ls_router.LSIndexer") as indexer:
        provider, selection = select_checkout_lsp_provider(
            project_root="/local/repo",
            languages=["cpp"],
            symbol_graph=graph,
        )

    indexer.assert_not_called()
    assert provider.graph is graph
    assert selection["backend"] == "persisted-symbol-graph-v1"
    assert selection["fallback_reason"] == "native_clangd_provider_disabled"


def test_native_checkout_selection_requires_native_query_provider():
    native_provider = SimpleNamespace(
        provider=STATIC_LSP_PROVIDER,
        provider_backend="native-clangd-fact-query-v1",
        snapshot_id="clangd_fact_query:sha256:test",
    )

    with patch("codenib.ls_router.LSIndexer") as indexer:
        indexer.return_value.process_query_provider.return_value = native_provider
        provider, selection = select_checkout_lsp_provider(
            project_root="/local/repo",
            languages=["cpp"],
            symbol_graph=_range_graph(),
        )

    assert provider is native_provider
    indexer.return_value.process_query_provider.assert_called_once_with(
        require_native=True
    )
    assert selection == {
        "provider": STATIC_LSP_PROVIDER,
        "backend": "native-clangd-fact-query-v1",
        "status": "ok",
        "index_snapshot": "clangd_fact_query:sha256:test",
        "capabilities": {
            "definition": True,
            "references": True,
            "route": True,
        },
    }


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
    assert tool_event.data["lsp_result_preview"][0]["location"] == "callee.py:5-5"
