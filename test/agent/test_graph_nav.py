# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared call-graph navigation engine (#133).

Covers find_callers / find_callees (neighbors) and trace, plus fuzzy
resolution and ambiguity handling.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codeminer.agent.skills import _graphnav
from codeminer.graph.code_graph import CodeGraph


class _FakeGraph:
    def __init__(self):
        self.name_to_vertex = {
            "pkg.a.doWatch": 0,
            "pkg.a.watchEffect": 1,
            "pkg.b.helper": 2,
            "pkg.c.notify": 3,  # ambiguous base "notify" with the next
            "pkg.d.notify": 4,
            "svc.HealthChecks": 5,
            "svc.NewReplacer": 6,
            "svc.globalDefaultReplacements": 7,
        }
        self._attr = {
            0: {
                "name": "pkg.a.doWatch",
                "type": "function",
                "file": "a.py",
                "start_line": 10,
            },
            1: {
                "name": "pkg.a.watchEffect",
                "type": "function",
                "file": "a.py",
                "start_line": 5,
            },
            2: {
                "name": "pkg.b.helper",
                "type": "function",
                "file": "b.py",
                "start_line": 3,
            },
            3: {
                "name": "pkg.c.notify",
                "type": "function",
                "file": "c.py",
                "start_line": 1,
            },
            4: {
                "name": "pkg.d.notify",
                "type": "function",
                "file": "d.py",
                "start_line": 1,
            },
            5: {
                "name": "svc.HealthChecks",
                "type": "function",
                "file": "health.go",
                "start_line": 20,
                "end_line": 60,
            },
            6: {
                "name": "svc.NewReplacer",
                "type": "function",
                "file": "replace.go",
                "start_line": 5,
                "end_line": 20,
            },
            7: {
                "name": "svc.globalDefaultReplacements",
                "type": "function",
                "file": "defaults.go",
                "start_line": 1,
                "end_line": 10,
            },
        }
        # watchEffect -> doWatch -> helper
        self._succ = {
            "pkg.a.watchEffect": [0],
            "pkg.a.doWatch": [2],
            "svc.NewReplacer": [7],
        }
        self._pred = {
            "pkg.a.doWatch": [1],
            "pkg.b.helper": [0],
            "svc.globalDefaultReplacements": [6],
        }
        self.graph = SimpleNamespace(get_shortest_paths=self._gsp)

    def _gsp(self, v, to=None, mode="out"):
        # watchEffect(1) -> doWatch(0) -> helper(2)
        chain = {(1, 0): [[1, 0]], (1, 2): [[1, 0, 2]], (0, 2): [[0, 2]]}
        return chain.get((v, to), [[]])

    def get_node_info_by_name(self, name):
        return self._attr.get(self.name_to_vertex.get(name))

    def get_node_info_by_id(self, vid):
        return self._attr.get(vid)

    def get_successors(self, name):
        return self._succ.get(name, [])

    def get_predecessors(self, name):
        return self._pred.get(name, [])


def test_resolve_unique_and_ambiguous():
    g = _FakeGraph()
    assert _graphnav.resolve(g, "doWatch")[0] == "pkg.a.doWatch"
    name, cands = _graphnav.resolve(g, "notify")  # two *.notify -> ambiguous
    assert name is None and len(cands) == 2


def test_find_callers_compact():
    g = _FakeGraph()
    res = _graphnav.neighbors(g, "doWatch", "callers")
    assert {n.node_name for n in res} == {"pkg.a.watchEffect"}
    n = res[0]
    assert n.content == "caller of pkg.a.doWatch"
    assert n.file == "a.py" and n.node_id == "a.py:pkg.a.watchEffect"
    assert n.content is not None  # marker only, no source body field populated


def test_find_callees_compact():
    g = _FakeGraph()
    res = _graphnav.neighbors(g, "doWatch", "callees")
    assert {n.node_name for n in res} == {"pkg.b.helper"}


def test_neighbors_unresolved_raises():
    g = _FakeGraph()
    with pytest.raises(ValueError, match="not found"):
        _graphnav.neighbors(g, "totally_absent_xyz", "callers")


def test_trace_shortest_path():
    g = _FakeGraph()
    path = _graphnav.trace(g, "watchEffect", "helper")
    names = [n.node_name for n in path]
    assert names == ["pkg.a.watchEffect", "pkg.a.doWatch", "pkg.b.helper"]
    assert path[0].content.startswith("hop 0")


def test_trace_ambiguous_endpoint_raises():
    g = _FakeGraph()
    with pytest.raises(ValueError, match="unresolved"):
        _graphnav.trace(g, "notify", "helper")


class _HashNamedGraph:
    """Mimics a clang/SCIP-indexed C repo: canonical ``name`` is a content
    hash, the readable identifier lives in ``unified_name``.

    ``popGenericCommand`` (caller) -> ``addReplyNull`` (callee).
    """

    def __init__(self):
        self.name_to_vertex = {"deadbeef00": 0, "cafef00d11": 1}
        self._attr = {
            0: {
                "name": "deadbeef00",
                "unified_name": "src/t_list.c:popGenericCommand()",
                "type": "function",
                "file": "src/t_list.c",
                "start_line": 488,
            },
            1: {
                "name": "cafef00d11",
                "unified_name": "src/networking.c:addReplyNull()",
                "type": "function",
                "file": "src/networking.c",
                "start_line": 700,
            },
        }
        self._unified_to_names = {
            "src/t_list.c:popGenericCommand()": ["deadbeef00"],
            "src/networking.c:addReplyNull()": ["cafef00d11"],
        }
        self._succ = {"deadbeef00": [1]}
        self._pred = {"cafef00d11": [0]}

    def get_node_info_by_name(self, name):
        return self._attr.get(self.name_to_vertex.get(name))

    def get_node_info_by_id(self, vid):
        return self._attr.get(vid)

    def get_successors(self, name):
        return self._succ.get(name, [])

    def get_predecessors(self, name):
        return self._pred.get(name, [])


def test_display_name_prefers_unified():
    g = _HashNamedGraph()
    assert _graphnav.display_name(g, "deadbeef00") == "src/t_list.c:popGenericCommand()"


def test_resolve_bare_symbol_to_hash_canonical():
    """Agent re-seeds with a readable bare symbol; resolution returns the
    canonical hash so graph ops still work, with a readable candidate."""
    g = _HashNamedGraph()
    name, cands = _graphnav.resolve(g, "popGenericCommand")
    assert name == "deadbeef00"  # canonical identity for get_successors
    assert cands == ["src/t_list.c:popGenericCommand()"]  # readable display


def test_resolve_accepts_full_unified_name():
    g = _HashNamedGraph()
    name, _ = _graphnav.resolve(g, "src/networking.c:addReplyNull()")
    assert name == "cafef00d11"


def test_resolves_when_unified_dict_empty_rebuilds_from_vertices():
    """The prebuilt graph often ships an EMPTY ``_unified_to_names`` even though
    vertices carry ``unified_name``. Resolution must rebuild the index from
    vertex attributes — otherwise readable embedding seeds never expand
    (the redis graph-LSP bug)."""
    g = _HashNamedGraph()
    g._unified_to_names = {}  # simulate the un-serialized prebuilt dict
    name, _ = _graphnav.resolve(g, "popGenericCommand")
    assert name == "deadbeef00"
    callees = _graphnav.neighbors(g, "popGenericCommand", "callees")
    assert {n.node_name for n in callees} == {"src/networking.c:addReplyNull()"}


def test_neighbors_emit_readable_names_for_hash_graph():
    g = _HashNamedGraph()
    callees = _graphnav.neighbors(g, "popGenericCommand", "callees")
    assert {n.node_name for n in callees} == {"src/networking.c:addReplyNull()"}
    assert callees[0].content == "callee of src/t_list.c:popGenericCommand()"
    # no raw content hash leaks into what the agent reads
    assert "deadbeef00" not in callees[0].node_id
    assert "cafef00d11" not in callees[0].node_id
    assert callees[0].node_id == "src/networking.c:addReplyNull()"


def _range_graph():
    graph = CodeGraph()
    graph.add_file_node("a.py")
    graph.add_symbol_node(
        "a.foo", line=0, scope_start_line=0, scope_end_line=3, symbol_type="function"
    )
    graph.graph.vs[graph.name_to_vertex["a.foo"]]["unified_name"] = "a.py:foo()"
    graph.update_current_scope("a.foo", start_line=0, end_line=3)
    graph.add_symbol_reference(
        "b.bar",
        module_path="b.py",
        symbol_type="function",
        anchor_file="a.py",
        anchor_line=1,
    )

    graph.add_file_node("b.py")
    graph.add_symbol_node(
        "b.bar", line=4, scope_start_line=4, scope_end_line=6, symbol_type="function"
    )
    graph.graph.vs[graph.name_to_vertex["b.bar"]]["unified_name"] = "b.py:bar()"
    graph.build_range_indexes()
    return graph


def test_lsp_definition_jumps_from_reference_anchor_to_definition():
    graph = _range_graph()
    defs = _graphnav.lsp_definition(graph, file_path="a.py", line=1)
    assert [node.node_name for node in defs] == ["b.py:bar()"]
    assert defs[0].file == "b.py"
    assert defs[0].start_line == 4
    assert defs[0].content == "definition of b.py:bar()"


def test_lsp_definition_accepts_symbol_seed():
    graph = _range_graph()
    defs = _graphnav.lsp_definition(graph, symbol="bar")
    assert [node.node_name for node in defs] == ["b.py:bar()"]


def test_lsp_references_returns_definition_and_anchor_sites():
    graph = _range_graph()
    refs = _graphnav.lsp_references(graph, symbol="bar", include_declaration=True)
    assert [(node.file, node.start_line, node.content) for node in refs] == [
        ("b.py", 4, "definition of b.py:bar()"),
        ("a.py", 1, "reference to b.py:bar()"),
    ]
    assert refs[1].node_id == "a.py:2:ref:b.py:bar()"


def test_lsp_route_returns_direct_roles_and_provider_neighbor():
    g = _FakeGraph()
    route = _graphnav.lsp_route(
        g,
        symbols=["HealthChecks", "NewReplacer"],
        query="health checks replacement defaults",
        top_k=3,
    )

    assert [node.node_name for node in route] == [
        "svc.HealthChecks",
        "svc.NewReplacer",
        "svc.globalDefaultReplacements",
    ]
    assert route[0].content == "route endpoint: direct seed HealthChecks"
    assert route[1].content == "route bridge: direct seed NewReplacer"
    assert route[2].content == "route provider: successor via svc.NewReplacer"


def test_lsp_route_skips_missing_symbols_but_uses_resolved_ones():
    g = _FakeGraph()
    route = _graphnav.lsp_route(
        g,
        symbols=["totally_absent_xyz", "HealthChecks"],
        query="health checks",
        top_k=3,
    )

    assert [node.node_name for node in route] == ["svc.HealthChecks"]
    assert route[0].content == "route endpoint: direct seed HealthChecks"


class _OneLineRouteGraph:
    def __init__(self):
        self.name_to_vertex = {
            "svc.Handler#activeHealthCheckPort": 0,
            "svc.Handler#doActiveHealthCheckForAllHosts": 1,
        }
        self._attr = {
            0: {
                "name": "svc.Handler#activeHealthCheckPort",
                "type": "field",
                "file": "healthchecks.go",
                "start_line": 183,
                "end_line": 183,
            },
            1: {
                "name": "svc.Handler#doActiveHealthCheckForAllHosts",
                "type": "method",
                "file": "healthchecks.go",
                "start_line": 243,
                "end_line": 299,
            },
        }
        self._succ = {}
        self._pred = {}

    def get_node_info_by_name(self, name):
        return self._attr.get(self.name_to_vertex.get(name))

    def get_node_info_by_id(self, vid):
        return self._attr.get(vid)

    def get_successors(self, name):
        return self._succ.get(name, [])

    def get_predecessors(self, name):
        return self._pred.get(name, [])


def test_lsp_route_demotes_one_line_hash_named_fields():
    g = _OneLineRouteGraph()
    route = _graphnav.lsp_route(
        g,
        symbols=[
            "svc.Handler#activeHealthCheckPort",
            "svc.Handler#doActiveHealthCheckForAllHosts",
        ],
        query="active health check loop host",
        top_k=2,
    )

    assert [node.node_name for node in route] == [
        "svc.Handler#doActiveHealthCheckForAllHosts",
        "svc.Handler#activeHealthCheckPort",
    ]
    assert route[0].content.startswith("route endpoint:")
    assert route[1].content.startswith("route support:")


class _TypeHeavyGraph:
    def __init__(self):
        self.name_to_vertex = {
            "ruff.ExprCall": 0,
            "ruff.ExprName": 1,
            "ruff.ExprList": 2,
            "ruff.match_iteration_target": 3,
        }
        self._attr = {
            0: {
                "name": "ruff.ExprCall",
                "type": "class",
                "file": "crates/ruff_python_ast/src/nodes.rs",
                "start_line": 565,
                "end_line": 575,
            },
            1: {
                "name": "ruff.ExprName",
                "type": "class",
                "file": "crates/ruff_python_ast/src/nodes.rs",
                "start_line": 576,
                "end_line": 586,
            },
            2: {
                "name": "ruff.ExprList",
                "type": "class",
                "file": "crates/ruff_python_ast/src/nodes.rs",
                "start_line": 587,
                "end_line": 597,
            },
            3: {
                "name": "ruff.match_iteration_target",
                "type": "function",
                "file": "crates/ruff_python_ast/src/helpers.rs",
                "start_line": 10,
                "end_line": 40,
            },
        }
        self._succ = {"ruff.ExprCall": [3]}
        self._pred = {"ruff.match_iteration_target": [0]}

    def get_node_info_by_name(self, name):
        return self._attr.get(self.name_to_vertex.get(name))

    def get_node_info_by_id(self, vid):
        return self._attr.get(vid)

    def get_successors(self, name):
        return self._succ.get(name, [])

    def get_predecessors(self, name):
        return self._pred.get(name, [])


def test_lsp_route_keeps_type_family_before_neighbors():
    g = _TypeHeavyGraph()
    route = _graphnav.lsp_route(
        g,
        symbols=["ExprCall", "ExprName", "ExprList"],
        query="Expr variants AST node traversal",
        top_k=4,
    )

    assert [node.node_name for node in route] == [
        "ruff.ExprCall",
        "ruff.ExprName",
        "ruff.ExprList",
    ]
    assert all(node.content.startswith("route type: direct seed") for node in route)
