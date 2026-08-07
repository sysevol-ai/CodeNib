# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for dependency/impact analysis + CodeGraph.resolve_symbol (#133).

Builds a tiny hash-named (clang/SCIP-style) call graph by hand:

    caller()  -->  mid()  -->  leaf()      (reference edges)

with canonical ``name`` = content hash and the readable name in
``unified_name`` — exactly the redis-style shape that broke earlier.
"""

from __future__ import annotations

import igraph as ig

from codenib.graph.code_graph import CodeGraph
from codenib.graph.dependency import DependencyAnalyzer


def _make_graph(unified_empty: bool = True) -> CodeGraph:
    g = CodeGraph()
    gg = ig.Graph(directed=True)
    gg.add_vertices(3)
    gg.vs["name"] = ["hashA", "hashB", "hashC"]
    gg.vs["unified_name"] = ["a.c:caller()", "a.c:mid()", "b.c:leaf()"]
    gg.vs["type"] = ["function", "function", "function"]
    gg.vs["file"] = ["a.c", "a.c", "b.c"]
    gg.vs["start_line"] = [1, 10, 5]
    gg.vs["end_line"] = [9, 20, 8]
    gg.add_edges([(0, 1), (1, 2)])  # caller -> mid -> leaf
    gg.es["type"] = ["reference", "reference"]
    g.graph = gg
    g.name_to_vertex = {"hashA": 0, "hashB": 1, "hashC": 2}
    # Simulate the prebuilt graph that ships an EMPTY reverse map.
    g._unified_to_names = (
        {}
        if unified_empty
        else {
            "a.c:caller()": ["hashA"],
            "a.c:mid()": ["hashB"],
            "b.c:leaf()": ["hashC"],
        }
    )
    return g


def _make_display_collision_graph() -> CodeGraph:
    g = CodeGraph()
    gg = ig.Graph(directed=True)
    gg.add_vertices(4)
    gg.vs["name"] = ["hashRoot", "hashLeft", "hashRight", "hashLeaf"]
    gg.vs["unified_name"] = [
        "root.c:root()",
        "shared.c:duplicate()",
        "shared.c:duplicate()",
        "leaf.c:leaf()",
    ]
    gg.vs["type"] = ["function"] * 4
    gg.vs["file"] = ["root.c", "left.c", "right.c", "leaf.c"]
    gg.vs["start_line"] = [1, 2, 3, 4]
    gg.vs["end_line"] = [1, 2, 3, 4]
    gg.add_edges([(0, 1), (0, 2), (2, 3)])
    gg.es["type"] = ["reference"] * 3
    g.graph = gg
    g.name_to_vertex = {
        "hashRoot": 0,
        "hashLeft": 1,
        "hashRight": 2,
        "hashLeaf": 3,
    }
    return g


def _make_fanout_graph(size: int) -> CodeGraph:
    g = CodeGraph()
    gg = ig.Graph(directed=True)
    gg.add_vertices(size + 1)
    gg.vs["name"] = ["root", *(f"child-{i}" for i in range(size))]
    gg.vs["unified_name"] = [
        "root.c:root()",
        *(f"child.c:child_{i}()" for i in range(size)),
    ]
    gg.vs["type"] = ["function"] * (size + 1)
    gg.vs["file"] = ["root.c", *("child.c" for _ in range(size))]
    gg.vs["start_line"] = list(range(size + 1))
    gg.vs["end_line"] = list(range(size + 1))
    gg.add_edges((0, i) for i in range(1, size + 1))
    gg.es["type"] = ["reference"] * size
    g.graph = gg
    g.name_to_vertex = {name: i for i, name in enumerate(gg.vs["name"])}
    return g


def test_resolve_symbol_bare_to_hash():
    g = _make_graph()
    name, cands = g.resolve_symbol("leaf")
    assert name == "hashC"
    assert cands == ["b.c:leaf()"]


def test_resolve_symbol_full_unified():
    g = _make_graph()
    name, _ = g.resolve_symbol("a.c:mid()")
    assert name == "hashB"


def test_resolve_symbol_missing():
    g = _make_graph()
    name, cands = g.resolve_symbol("does_not_exist")
    assert name is None and cands == []


def test_impact_is_transitive_callers():
    g = _make_graph()
    res = DependencyAnalyzer(g).impact("leaf", max_depth=3)
    assert res.root == "b.c:leaf()"
    names = {n.name for n in res.nodes}
    assert names == {"a.c:mid()", "a.c:caller()"}  # mid (1 hop) + caller (2 hops)
    depths = {n.name: n.depth for n in res.nodes}
    assert depths["a.c:mid()"] == 1 and depths["a.c:caller()"] == 2


def test_impact_depth_limit():
    g = _make_graph()
    res = DependencyAnalyzer(g).impact("leaf", max_depth=1)
    assert {n.name for n in res.nodes} == {"a.c:mid()"}  # only direct caller


def test_dependencies_is_transitive_callees():
    g = _make_graph()
    res = DependencyAnalyzer(g).dependencies("caller", max_depth=3)
    assert {n.name for n in res.nodes} == {"a.c:mid()", "b.c:leaf()"}


def test_call_path():
    g = _make_graph()
    res = DependencyAnalyzer(g).call_path("caller", "leaf")
    assert [n.name for n in res.nodes] == ["a.c:caller()", "a.c:mid()", "b.c:leaf()"]
    assert len(res.edges) == 2


def test_subgraph_frontend_json():
    g = _make_graph()
    res = DependencyAnalyzer(g).subgraph("mid", radius=1)
    d = res.to_dict()
    assert set(d) == {"root", "direction", "nodes", "edges", "truncated", "note"}
    # mid's neighborhood: caller (in) + leaf (out)
    assert {n["name"] for n in d["nodes"]} == {"a.c:caller()", "b.c:leaf()"}
    assert all({"source", "target", "type"} == set(e) for e in d["edges"])


def test_subgraph_respects_root_inclusive_node_budget():
    g = _make_graph()
    res = DependencyAnalyzer(g).subgraph("mid", radius=2, max_nodes=2)

    assert [n.name for n in res.nodes] == ["b.c:leaf()"]
    assert [e.to_dict() for e in res.edges] == [
        {"source": "a.c:mid()", "target": "b.c:leaf()", "type": "reference"}
    ]
    assert res.truncated is True
    included = {res.root, *(node.name for node in res.nodes)}
    assert all(
        edge.source in included and edge.target in included for edge in res.edges
    )


def test_subgraph_deduplicates_logical_edges():
    g = _make_graph()
    g.graph.add_edge(0, 1, type="reference")

    res = DependencyAnalyzer(g).subgraph("mid", radius=2)
    edge_keys = {(edge.source, edge.target, edge.type) for edge in res.edges}

    assert len(res.edges) == len(edge_keys) == 2


def test_dependencies_keep_canonical_nodes_when_display_names_collide():
    res = DependencyAnalyzer(_make_display_collision_graph()).dependencies(
        "hashRoot", max_depth=2
    )

    # The colliding nodes fall back to their canonical identities so nodes and
    # edges remain distinguishable in the serialized response.
    assert [node.name for node in res.nodes] == [
        "hashLeft",
        "hashRight",
        "leaf.c:leaf()",
    ]
    assert [edge.to_dict() for edge in res.edges] == [
        {"source": "root.c:root()", "target": "hashLeft", "type": "reference"},
        {"source": "root.c:root()", "target": "hashRight", "type": "reference"},
        {"source": "hashRight", "target": "leaf.c:leaf()", "type": "reference"},
    ]


def test_edge_budget_stops_consuming_high_fanout_neighbors():
    analyzer = DependencyAnalyzer(_make_fanout_graph(50))
    original = analyzer.searcher.iter_neighbors
    consumed = 0

    def counted_neighbors(*args, **kwargs):
        nonlocal consumed
        for neighbor in original(*args, **kwargs):
            consumed += 1
            yield neighbor

    analyzer.searcher.iter_neighbors = counted_neighbors
    res = analyzer.dependencies("root", max_depth=1, max_edges=2)

    assert len(res.edges) == 2
    assert res.truncated is True
    # One look-ahead edge establishes truncation; the other 47 edge tuples are
    # never allocated or inspected by the dependency traversal.
    assert consumed == 3


def test_edge_budget_stops_parallel_edges_before_later_unique_edge():
    graph = _make_fanout_graph(2)
    graph.graph.delete_edges(range(graph.graph.ecount()))
    graph.graph.add_edges([(0, 1)] * 50 + [(0, 2)])
    graph.graph.es["type"] = ["reference"] * 51
    analyzer = DependencyAnalyzer(graph)
    original = analyzer.searcher.iter_neighbors
    consumed = 0

    def counted_neighbors(*args, **kwargs):
        nonlocal consumed
        for neighbor in original(*args, **kwargs):
            consumed += 1
            yield neighbor

    analyzer.searcher.iter_neighbors = counted_neighbors
    res = analyzer.dependencies("root", max_depth=1, max_edges=1)

    assert len(res.edges) == 1
    assert res.truncated is True
    # Parallel anchors have distinct raw edge IDs. The bounded duplicate slack
    # stops the scan instead of consuming all 51 edges to find the later node.
    assert consumed == 3


def test_subgraph_respects_edge_budget_without_orphan_nodes():
    g = _make_graph()
    res = DependencyAnalyzer(g).subgraph("mid", radius=2, max_edges=1)

    assert [node.name for node in res.nodes] == ["b.c:leaf()"]
    assert [edge.to_dict() for edge in res.edges] == [
        {"source": "a.c:mid()", "target": "b.c:leaf()", "type": "reference"}
    ]
    assert res.truncated is True


def test_files_nearest_first():
    g = _make_graph()
    res = DependencyAnalyzer(g).impact("leaf", max_depth=3)
    assert res.files() == ["a.c"]  # both callers live in a.c


def test_unresolved_returns_note_not_crash():
    g = _make_graph()
    res = DependencyAnalyzer(g).impact("nope_xyz", max_depth=2)
    assert res.nodes == [] and "unresolved" in res.note


def test_works_with_prepopulated_unified_dict():
    g = _make_graph(unified_empty=False)
    res = DependencyAnalyzer(g).impact("leaf", max_depth=3)
    assert {n.name for n in res.nodes} == {"a.c:mid()", "a.c:caller()"}
