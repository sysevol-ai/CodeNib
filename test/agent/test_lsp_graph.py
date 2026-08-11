# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codenib.agent import lsp_graph
from codenib.agent.lsp_provider import STATIC_LSP_PROVIDER, lsp_result_metadata
from codenib.agent.skills.loader import SkillLoader
from codenib.agent.tool_schema import skill_to_tool_schema
from codenib.graph.code_graph import CodeGraph
from codenib.ops.expand import ExpandContext
from codenib.types import NODE_TYPE_FUNCTION


def _range_graph(project_root=None) -> CodeGraph:
    graph = CodeGraph(project_root=str(project_root) if project_root else None)
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


def test_lsp_definition_character_must_hit_reference_token(tmp_path):
    (tmp_path / "caller.py").write_text(
        "def run():\n    return load_config()\n", encoding="utf-8"
    )
    graph = _range_graph(tmp_path)

    results = lsp_graph.lsp_definition(
        graph, file_path="caller.py", line=1, character=15
    )

    assert [node.node_name for node in results] == ["callee.py:load_config()"]
    with pytest.raises(ValueError, match="no indexed definition token"):
        lsp_graph.lsp_definition(graph, file_path="caller.py", line=1, character=4)


def test_lsp_definition_character_considers_declaration_and_same_line_reference(
    tmp_path,
):
    (tmp_path / "reader.go").write_text(
        "func (r Reader) Render() {}\n", encoding="utf-8"
    )
    graph = CodeGraph(project_root=str(tmp_path))
    graph.add_file_node("reader.go")
    graph.add_symbol_node(
        "Reader.Render",
        line=0,
        scope_start_line=0,
        scope_end_line=0,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.add_symbol_reference(
        "Reader",
        module_path="reader.go",
        anchor_file="reader.go",
        anchor_line=0,
    )
    graph.build_range_indexes()

    result = lsp_graph.lsp_definition(
        graph, file_path="reader.go", line=0, character=20
    )

    assert [node.node_name for node in result] == ["Reader.Render"]


def test_lsp_references_character_must_hit_reference_token(tmp_path):
    (tmp_path / "caller.py").write_text(
        "def run():\n    return load_config()\n", encoding="utf-8"
    )
    graph = _range_graph(tmp_path)

    with pytest.raises(ValueError, match="no indexed definition token"):
        lsp_graph.lsp_references(graph, file_path="caller.py", line=1, character=4)


def test_lsp_definition_accepts_symbol_seed():
    graph = _range_graph()

    results = lsp_graph.lsp_definition(graph, symbol="load_config")

    assert [node.node_name for node in results] == ["callee.py:load_config()"]


def test_lsp_definition_rejects_reference_only_symbol_with_legacy_range():
    graph = CodeGraph()
    graph.add_file_node("caller.py")
    graph.add_symbol_reference(
        "missing.Target",
        module_path="missing.py",
        anchor_file="caller.py",
        anchor_line=2,
    )
    target = graph.graph.vs[graph.name_to_vertex["missing.Target"]]
    target["start_line"] = 0
    target["end_line"] = 3
    graph.build_range_indexes()

    with pytest.raises(ValueError, match="no indexed definition"):
        lsp_graph.lsp_definition(graph, symbol="missing.Target")
    with pytest.raises(ValueError, match="no indexed definition"):
        lsp_graph.lsp_definition(graph, file_path="caller.py", line=2)


def test_lsp_definition_uses_declaration_line_not_scope_start():
    graph = CodeGraph()
    graph.add_file_node("decorated.py")
    graph.add_symbol_node(
        "decorated.Handler",
        line=5,
        scope_start_line=3,
        scope_end_line=9,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.graph.vs[graph.name_to_vertex["decorated.Handler"]][
        "unified_name"
    ] = "decorated.py:Handler()"

    result = lsp_graph.lsp_definition(graph, symbol="decorated.Handler")

    assert result[0].start_line == 5
    assert result[0].end_line == 5
    info = graph.get_node_info_by_name("decorated.Handler")
    assert info["start_line"] == 3
    assert info["end_line"] == 9
    assert info["selection_line"] == 5


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


def test_lsp_route_can_fallback_to_query_seed_candidates():
    route = lsp_graph.lsp_route(
        _RouteGraph(),
        symbols=[],
        query="fix default config behavior",
        top_k=2,
    )

    assert [node.node_name for node in route] == ["svc.DefaultConfig"]
    assert route[0].content == "route provider: query match config, default"


class _QueryBudgetGraph:
    def __init__(self, names):
        self.names = list(names)
        self.name_to_vertex = {name: index for index, name in enumerate(self.names)}
        self.seen_names = []
        self.successors = {}

    def get_node_info_by_name(self, name):
        self.seen_names.append(name)
        if name not in self.name_to_vertex:
            return None
        return {
            "name": name,
            "unified_name": name,
            "type": "function",
            "file": "routes.py",
            "start_line": self.name_to_vertex[name],
            "end_line": self.name_to_vertex[name] + 1,
        }

    def get_successors(self, name):
        return self.successors.get(name, [])

    def get_predecessors(self, name):
        return []

    def get_node_info_by_id(self, vertex_id):
        if not 0 <= vertex_id < len(self.names):
            return None
        return self.get_node_info_by_name(self.names[vertex_id])


def test_lsp_route_query_fallback_stops_at_scan_budget(monkeypatch):
    graph = _QueryBudgetGraph(["NoiseOne", "NoiseTwo", "NoiseThree", "TargetRoute"])
    monkeypatch.setattr(lsp_graph, "_QUERY_SEED_SCAN_BUDGET", 3)
    monkeypatch.setattr(lsp_graph.time, "monotonic", lambda: 0.0)

    route = lsp_graph.lsp_route(
        graph,
        symbols=[],
        query="target route",
        top_k=4,
        include_neighbors=False,
    )

    assert route == []
    assert "TargetRoute" not in graph.seen_names


def test_lsp_route_query_fallback_stops_at_match_budget(monkeypatch):
    graph = _QueryBudgetGraph(["TargetRouteOne", "TargetRouteTwo", "TargetRouteThree"])
    monkeypatch.setattr(lsp_graph, "_QUERY_SEED_MATCH_BUDGET", 2)

    route = lsp_graph.lsp_route(
        graph,
        symbols=[],
        query="target route",
        top_k=4,
        include_neighbors=False,
    )

    assert [node.node_name for node in route] == ["TargetRouteOne", "TargetRouteTwo"]
    assert "TargetRouteThree" not in graph.seen_names


def test_lsp_route_query_fallback_bounds_expanded_candidates(monkeypatch):
    graph = _QueryBudgetGraph(
        ["TargetRoute", "EndpointOne", "EndpointTwo", "EndpointThree"]
    )
    graph.successors["TargetRoute"] = [1, 2, 3]
    monkeypatch.setattr(lsp_graph, "_QUERY_SEED_CANDIDATE_BUDGET", 3)

    route = lsp_graph.lsp_route(
        graph,
        symbols=[],
        query="target route",
        top_k=4,
        include_neighbors=True,
    )

    assert [node.node_name for node in route] == [
        "TargetRoute",
        "EndpointOne",
        "EndpointTwo",
    ]


def test_lsp_route_query_fallback_stops_at_deadline(monkeypatch):
    graph = _QueryBudgetGraph(["NoiseOne", "TargetRoute"])
    clock = iter([0.0, 2.0])
    monkeypatch.setattr(lsp_graph, "_QUERY_SEED_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(lsp_graph.time, "monotonic", lambda: next(clock))

    route = lsp_graph.lsp_route(
        graph,
        symbols=[],
        query="target route",
        top_k=4,
        include_neighbors=False,
    )

    assert route == []
    assert "TargetRoute" not in graph.seen_names


def test_lsp_skills_load_and_execute_against_expand_context(tmp_path):
    (tmp_path / "caller.py").write_text(
        "def run():\n    return load_config()\n", encoding="utf-8"
    )
    graph = _range_graph(tmp_path)
    context = {"expand": ExpandContext(code_graph=graph)}
    loader = SkillLoader()

    meta = loader.load_skill("codenib/agent/skills/lsp_definition", context)

    assert meta is not None
    assert meta.executor_fn is not None
    results = meta.executor_fn(file_path="caller.py", line=1, character=15)
    assert [node.node_name for node in results] == ["callee.py:5"]
    assert lsp_result_metadata(results)["provider"] == STATIC_LSP_PROVIDER
    assert lsp_result_metadata(results)["capability"] == "definition"
    schema = skill_to_tool_schema(meta)["function"]["parameters"]
    assert schema["required"] == ["file_path", "line", "character"]
    assert "symbol" not in schema["properties"]


def test_lsp_skills_use_injected_provider_without_a_graph():
    class Provider:
        def definition(self, **kwargs):
            return [kwargs]

    context = {"expand": ExpandContext(lsp_provider=Provider())}
    meta = SkillLoader().load_skill(
        "codenib/agent/skills/lsp_definition",
        context,
    )

    assert meta.executor_fn(file_path="caller.py", line=3, character=4) == [
        {
            "file_path": "caller.py",
            "line": 3,
            "character": 4,
            "top_k": 8,
        }
    ]


def test_lsp_route_skill_exposes_static_graph_tool_contract():
    graph = _RouteGraph()
    context = {"expand": ExpandContext(code_graph=graph)}
    loader = SkillLoader()

    meta = loader.load_skill("codenib/agent/skills/lsp_route", context)

    assert meta is not None
    assert meta.skill_id == "lsp_route"
    assert meta.operator == "graph.lsp.route"
    assert meta.defaults == {"top_k": 12, "include_neighbors": True}
    assert [req.index_type for req in meta.index_requirements] == ["symbol_graph"]
    assert "Static-index semantic route map" in meta.skill_doc

    schema = skill_to_tool_schema(meta)
    props = schema["function"]["parameters"]["properties"]
    assert schema["function"]["parameters"]["required"] == ["symbols"]
    assert props["symbols"]["type"] == "array"
    assert props["symbols"]["items"]["type"] == "string"
    assert props["include_neighbors"]["type"] == "boolean"

    results = meta.executor_fn(
        symbols=["HandleRequest", "NewResolver"],
        query="handle request resolver default config",
        top_k=3,
    )
    assert [node.node_name for node in results] == [
        "svc.HandleRequest",
        "svc.NewResolver",
        "svc.DefaultConfig",
    ]
    assert results[0].content == "route endpoint: direct seed HandleRequest"
    assert lsp_result_metadata(results)["provider"] == STATIC_LSP_PROVIDER
    assert lsp_result_metadata(results)["capability"] == "route"


def test_lsp_route_skill_uses_injected_provider_without_graph():
    class Provider:
        def route(self, **kwargs):
            return [kwargs]

    context = {"expand": ExpandContext(lsp_provider=Provider())}
    meta = SkillLoader().load_skill("codenib/agent/skills/lsp_route", context)

    assert meta.executor_fn(symbols=["demo"], query="find demo") == [
        {
            "symbols": ["demo"],
            "query": "find demo",
            "top_k": 12,
            "include_neighbors": True,
        }
    ]
