# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the dependency_subgraph MCP tool impl (#133, #19)."""

from __future__ import annotations

from types import SimpleNamespace

import igraph as ig
import pytest

from codenib.graph.code_graph import CodeGraph
from codenib.mcp.tools.dependency import dependency_subgraph_impl


def _graph() -> CodeGraph:
    g = CodeGraph()
    gg = ig.Graph(directed=True)
    gg.add_vertices(3)
    gg.vs["name"] = ["hashA", "hashB", "hashC"]
    gg.vs["unified_name"] = ["a.c:caller()", "a.c:mid()", "b.c:leaf()"]
    gg.vs["type"] = ["function"] * 3
    gg.vs["file"] = ["a.c", "a.c", "b.c"]
    gg.vs["start_line"] = [0, 10, 5]
    gg.vs["end_line"] = [9, 20, 8]
    gg.add_edges([(0, 1), (1, 2)])  # caller -> mid -> leaf
    gg.es["type"] = ["reference", "reference"]
    g.graph = gg
    g.name_to_vertex = {"hashA": 0, "hashB": 1, "hashC": 2}
    g._unified_to_names = {}
    return g


def _dense_graph(size: int = 12) -> CodeGraph:
    g = CodeGraph()
    graph = ig.Graph(directed=True)
    graph.add_vertices(size)
    names = [f"hash{index}" for index in range(size)]
    graph.vs["name"] = names
    graph.vs["unified_name"] = [f"dense.py:node_{index}()" for index in range(size)]
    graph.vs["type"] = ["function"] * size
    graph.vs["file"] = ["dense.py"] * size
    graph.vs["start_line"] = list(range(size))
    graph.vs["end_line"] = list(range(size))
    graph.add_edges(
        (source, target)
        for source in range(size)
        for target in range(size)
        if source != target
    )
    graph.es["type"] = ["reference"] * graph.ecount()
    g.graph = graph
    g.name_to_vertex = {name: index for index, name in enumerate(names)}
    g._unified_to_names = {}
    return g


def test_impact_direction_returns_transitive_callers():
    ctx = SimpleNamespace(symbol_graph=_graph())
    out = dependency_subgraph_impl(ctx, "leaf", direction="impact", depth=3)
    assert out["root"] == "b.c:leaf()"
    assert {n["name"] for n in out["nodes"]} == {"a.c:mid()", "a.c:caller()"}
    assert {n["name"]: n["line"] for n in out["nodes"]} == {
        "a.c:caller()": 1,
        "a.c:mid()": 11,
    }
    assert all(set(e) == {"source", "target", "type"} for e in out["edges"])


def test_both_direction_neighborhood():
    ctx = SimpleNamespace(symbol_graph=_graph())
    out = dependency_subgraph_impl(ctx, "mid", direction="both", depth=1)
    assert {n["name"] for n in out["nodes"]} == {"a.c:caller()", "b.c:leaf()"}


def test_both_direction_honors_node_limit_without_dangling_edges():
    ctx = SimpleNamespace(symbol_graph=_graph())

    out = dependency_subgraph_impl(
        ctx,
        "mid",
        direction="both",
        depth=3,
        max_nodes=2,
    )

    names = {out["root"], *(node["name"] for node in out["nodes"])}
    assert len(out["nodes"]) == 1
    assert out["truncated"] is True
    assert all(
        edge["source"] in names and edge["target"] in names for edge in out["edges"]
    )


def test_dependency_tool_caps_dense_edge_output():
    out = dependency_subgraph_impl(
        SimpleNamespace(symbol_graph=_dense_graph()),
        "node_0",
        direction="both",
        depth=2,
        max_nodes=10,
    )

    assert len(out["nodes"]) <= 9
    assert len(out["edges"]) == 40
    assert out["truncated"] is True


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"symbol": ""}, "symbol must not be empty"),
        ({"symbol": "mid", "direction": "sideways"}, "direction must be"),
        ({"symbol": "mid", "depth": 9}, "depth must be between"),
        ({"symbol": "mid", "max_nodes": 101}, "max_nodes must be between"),
    ],
)
def test_dependency_tool_validates_bounded_request(kwargs, message):
    with pytest.raises(ValueError, match=message):
        dependency_subgraph_impl(SimpleNamespace(symbol_graph=_graph()), **kwargs)


def test_missing_graph_returns_error():
    out = dependency_subgraph_impl(SimpleNamespace(symbol_graph=None), "x")
    assert "error" in out
