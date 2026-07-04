# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from codeminer.agent import lsp_graph
from codeminer.agent.skills.loader import SkillLoader
from codeminer.graph.code_graph import CodeGraph
from codeminer.ops.expand import ExpandContext
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
    graph.graph.vs[graph.name_to_vertex["caller.run"]][
        "unified_name"
    ] = "caller.py:run()"
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


def test_lsp_definition_jumps_from_reference_anchor_to_definition():
    graph = _range_graph()

    results = lsp_graph.lsp_definition(graph, file_path="caller.py", line=1)

    assert [node.node_name for node in results] == ["callee.py:load_config()"]
    assert results[0].file == "callee.py"
    assert results[0].start_line == 4
    assert results[0].content == "definition of callee.py:load_config()"


def test_lsp_definition_accepts_symbol_seed():
    graph = _range_graph()

    results = lsp_graph.lsp_definition(graph, symbol="load_config")

    assert [node.node_name for node in results] == ["callee.py:load_config()"]


def test_lsp_references_returns_declaration_and_reference_site():
    graph = _range_graph()

    results = lsp_graph.lsp_references(
        graph, symbol="load_config", include_declaration=True
    )

    assert [(node.file, node.start_line, node.content) for node in results] == [
        ("callee.py", 4, "definition of callee.py:load_config()"),
        ("caller.py", 1, "reference to callee.py:load_config()"),
    ]
    assert results[1].node_id == "caller.py:2:ref:callee.py:load_config()"


class _RouteGraph:
    def __init__(self):
        self.name_to_vertex = {
            "svc.HandleRequest": 0,
            "svc.NewResolver": 1,
            "svc.DefaultConfig": 2,
            "svc.Config": 3,
        }
        self._attr = {
            0: {
                "name": "svc.HandleRequest",
                "type": "function",
                "file": "service.py",
                "start_line": 20,
                "end_line": 50,
            },
            1: {
                "name": "svc.NewResolver",
                "type": "function",
                "file": "resolver.py",
                "start_line": 5,
                "end_line": 15,
            },
            2: {
                "name": "svc.DefaultConfig",
                "type": "function",
                "file": "config.py",
                "start_line": 2,
                "end_line": 8,
            },
            3: {
                "name": "svc.Config",
                "type": "class",
                "file": "config.py",
                "start_line": 1,
                "end_line": 12,
            },
        }
        self._succ = {
            "svc.HandleRequest": [1],
            "svc.NewResolver": [2, 3],
        }
        self._pred = {}

    def get_node_info_by_name(self, name):
        return self._attr.get(self.name_to_vertex.get(name))

    def get_node_info_by_id(self, vertex_id):
        return self._attr.get(vertex_id)

    def get_successors(self, name):
        return self._succ.get(name, [])

    def get_predecessors(self, name):
        return self._pred.get(name, [])


def test_lsp_route_returns_general_roles_without_instance_hacks():
    route = lsp_graph.lsp_route(
        _RouteGraph(),
        symbols=["HandleRequest", "NewResolver"],
        query="handle request resolver default config",
        top_k=4,
    )

    assert [node.node_name for node in route] == [
        "svc.HandleRequest",
        "svc.NewResolver",
        "svc.DefaultConfig",
        "svc.Config",
    ]
    assert route[0].content == "route endpoint: direct seed HandleRequest"
    assert route[1].content == "route bridge: direct seed NewResolver"
    assert route[2].content == "route provider: successor via svc.NewResolver"
    assert route[3].content == "route type: successor via svc.NewResolver"


def test_lsp_route_skips_missing_symbols_but_uses_resolved_ones():
    route = lsp_graph.lsp_route(
        _RouteGraph(),
        symbols=["absent_symbol", "HandleRequest"],
        query="handle request",
        top_k=2,
    )

    assert [node.node_name for node in route] == [
        "svc.HandleRequest",
        "svc.NewResolver",
    ]


def test_lsp_skills_load_and_execute_against_expand_context():
    graph = _range_graph()
    context = {"expand": ExpandContext(code_graph=graph)}
    loader = SkillLoader()

    meta = loader.load_skill("codeminer/agent/skills/lsp_definition", context)

    assert meta is not None
    assert meta.executor_fn is not None
    results = meta.executor_fn(symbol="load_config")
    assert [node.node_name for node in results] == ["callee.py:load_config()"]
