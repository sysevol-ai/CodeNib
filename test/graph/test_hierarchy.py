# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the repo-level hierarchical code graph model."""

from codenib.graph.code_graph import CodeGraph
from codenib.graph.hierarchy import (
    attach_edge_routes,
    build_hierarchical_code_graph,
    hierarchy_for_view,
)


def _compound_graph() -> CodeGraph:
    graph = CodeGraph()
    graph._add_vertex(
        "src/pkg/model.py:Widget",
        {
            "type": "class",
            "file": "src/pkg/model.py",
            "start_line": 0,
            "end_line": 20,
            "unified_name": "src/pkg/model.py:Widget",
        },
    )
    graph._add_vertex(
        "src/pkg/model.py:Widget.run()",
        {
            "type": "method",
            "file": "src/pkg/model.py",
            "start_line": 4,
            "end_line": 10,
            "unified_name": "src/pkg/model.py:Widget.run()",
        },
    )
    graph._add_vertex(
        "src/pkg/helper.py:load()",
        {
            "type": "function",
            "file": "src/pkg/helper.py",
            "start_line": 30,
            "end_line": 36,
            "unified_name": "src/pkg/helper.py:load()",
        },
    )
    graph._add_vertex(
        "setup.py:main()",
        {
            "type": "function",
            "file": "setup.py",
            "start_line": 0,
            "end_line": 2,
            "unified_name": "setup.py:main()",
        },
    )
    graph._add_vertex(
        "docs/conf.py:configure()",
        {
            "type": "function",
            "file": "docs/conf.py",
            "start_line": 0,
            "end_line": 3,
            "unified_name": "docs/conf.py:configure()",
        },
    )
    graph._add_edge(
        "src/pkg/model.py:Widget",
        "src/pkg/model.py:Widget.run()",
        "contain",
    )
    for line in (5, 8):
        graph._add_edge(
            "src/pkg/model.py:Widget.run()",
            "src/pkg/helper.py:load()",
            "reference",
            anchor_file="src/pkg/model.py",
            anchor_line=line,
        )
    return graph


def test_hierarchical_code_graph_keeps_containment_and_dependency_layers():
    hgraph = build_hierarchical_code_graph(_compound_graph())
    nodes = {node.id: node for node in hgraph.containment}

    assert hgraph.source_root == "src"
    assert nodes["hier::dir::pkg"].parent == "hier::root"
    assert nodes["hier::file::src/pkg/model.py"].parent == "hier::dir::pkg"
    assert (
        nodes["hier::symbol::src/pkg/model.py:Widget.run()"].parent
        == "hier::symbol::src/pkg/model.py:Widget"
    )
    assert nodes["hier::symbol::src/pkg/model.py:Widget"].symbol_count == 2

    assert len(hgraph.dependencies) == 1
    dep = hgraph.dependencies[0]
    assert dep.source == "src/pkg/model.py:Widget.run()"
    assert dep.target == "src/pkg/helper.py:load()"
    assert dep.weight == 2
    assert dep.anchors == [
        {"file": "src/pkg/model.py", "line": 6},
        {"file": "src/pkg/model.py", "line": 9},
    ]


def test_hierarchical_code_graph_infers_scope_when_containment_edges_are_absent():
    graph = CodeGraph()
    graph._add_vertex(
        "src/pkg/model.py:Widget",
        {
            "type": "class",
            "file": "src/pkg/model.py",
            "start_line": 0,
            "end_line": 20,
            "unified_name": "src/pkg/model.py:Widget",
        },
    )
    graph._add_vertex(
        "src/pkg/model.py:Widget.run()",
        {
            "type": "method",
            "file": "src/pkg/model.py",
            "start_line": 4,
            "end_line": 10,
            "unified_name": "src/pkg/model.py:Widget.run()",
        },
    )
    graph._add_vertex(
        "src/pkg/model.py:WidgetHelper.run()",
        {
            "type": "function",
            "file": "src/pkg/model.py",
            "start_line": 25,
            "end_line": 28,
            "unified_name": "src/pkg/model.py:WidgetHelper.run()",
        },
    )

    hgraph = build_hierarchical_code_graph(graph)
    nodes = {node.id: node for node in hgraph.containment}

    assert (
        nodes["hier::symbol::src/pkg/model.py:Widget.run()"].parent
        == "hier::symbol::src/pkg/model.py:Widget"
    )
    assert (
        nodes["hier::symbol::src/pkg/model.py:WidgetHelper.run()"].parent
        == "hier::file::src/pkg/model.py"
    )


def test_hierarchy_projection_remaps_visible_symbols_for_frontend_routes():
    hgraph = build_hierarchical_code_graph(_compound_graph())
    view_nodes = [
        {
            "id": "n0",
            "name": "src/pkg/model.py:Widget.run()",
            "file": "src/pkg/model.py",
            "depth": 0,
            "importance": 0.9,
            "is_root": True,
        },
        {
            "id": "n1",
            "name": "src/pkg/helper.py:load()",
            "file": "src/pkg/helper.py",
            "depth": 1,
            "importance": 0.4,
        },
    ]

    hierarchy = hierarchy_for_view(hgraph, view_nodes)
    nodes = {node["id"]: node for node in hierarchy["nodes"]}

    assert "hier::symbol::n0" in nodes
    assert "hier::symbol::n1" in nodes
    assert (
        nodes["hier::symbol::n0"]["parent"] == "hier::symbol::src/pkg/model.py:Widget"
    )
    assert (
        nodes["hier::symbol::src/pkg/model.py:Widget"]["parent"]
        == "hier::file::src/pkg/model.py"
    )
    assert hierarchy["open_files"][0] == "src/pkg/model.py"

    edge = {"source": "n0", "target": "n1"}
    attach_edge_routes([edge], hierarchy)
    assert edge["source_hierarchy"] == "hier::symbol::n0"
    assert edge["target_hierarchy"] == "hier::symbol::n1"
    assert edge["bundle_path"][0] == "hier::symbol::n0"
    assert edge["bundle_path"][-1] == "hier::symbol::n1"
    assert "hier::symbol::src/pkg/model.py:Widget" in edge["bundle_path"]
    assert edge["bundle_lca"] == "hier::dir::pkg"
    assert edge["bundle_lca_kind"] == "directory"
    assert edge["cross_file"] is True


def test_hierarchy_projection_doi_prefers_nearby_containment_context():
    graph = _compound_graph()
    graph._add_vertex(
        "src/pkg/model.py:Widget.close()",
        {
            "type": "method",
            "file": "src/pkg/model.py",
            "start_line": 12,
            "end_line": 18,
            "unified_name": "src/pkg/model.py:Widget.close()",
        },
    )
    graph._add_edge(
        "src/pkg/model.py:Widget",
        "src/pkg/model.py:Widget.close()",
        "contain",
    )
    hgraph = build_hierarchical_code_graph(graph)
    view_nodes = [
        {
            "id": "n0",
            "name": "src/pkg/model.py:Widget.run()",
            "file": "src/pkg/model.py",
            "depth": 0,
            "importance": 0.8,
            "is_root": True,
        },
        {
            "id": "n1",
            "name": "src/pkg/model.py:Widget.close()",
            "file": "src/pkg/model.py",
            "depth": 1,
            "importance": 0.6,
        },
        {
            "id": "n2",
            "name": "src/pkg/helper.py:load()",
            "file": "src/pkg/helper.py",
            "depth": 1,
            "importance": 0.6,
        },
    ]

    hierarchy = hierarchy_for_view(hgraph, view_nodes)
    nodes = {node["id"]: node for node in hierarchy["nodes"]}

    assert nodes["hier::symbol::n1"]["doi"] > nodes["hier::symbol::n2"]["doi"]
