# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from codeminer.graph.code_graph import CodeGraph
from codeminer.types import EDGE_TYPE_CONTAIN, NODE_TYPE_FUNCTION


def test_merge_from_combines_vertices_edges_and_range_indexes():
    python_graph = CodeGraph("repo")
    python_graph.add_file_node("a.py")
    python_graph.add_symbol_node(
        "a.py:foo()",
        line=0,
        scope_start_line=0,
        scope_end_line=3,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    python_graph.add_containment_edge("a.py:foo()")
    python_graph.update_current_scope("a.py:foo()", start_line=0, end_line=3)
    python_graph.add_symbol_reference(
        "b.go:bar()",
        anchor_file="a.py",
        anchor_line=1,
    )
    python_graph.build_range_indexes()

    go_graph = CodeGraph("repo")
    go_graph.add_file_node("b.go")
    go_graph.add_symbol_node(
        "b.go:bar()",
        line=4,
        scope_start_line=4,
        scope_end_line=6,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    go_graph.add_containment_edge("b.go:bar()")
    go_graph.build_range_indexes()

    merged = CodeGraph("repo")
    merged.merge_from(python_graph)
    merged.merge_from(go_graph)
    merged.merge_from(go_graph)

    assert set(merged.name_to_vertex) == {
        "a.py",
        "a.py:foo()",
        "b.go",
        "b.go:bar()",
    }
    assert merged.graph.vcount() == 4
    assert merged.graph.ecount() == 3

    contain_edges = [
        edge
        for edge in merged.graph.es
        if edge.attributes().get("type") == EDGE_TYPE_CONTAIN
    ]
    assert len(contain_edges) == 2

    outgoing = merged.query_range("a.py", 1, 1).outgoing
    assert len(outgoing) == 1
    [reference] = outgoing
    assert reference.anchor_file == "a.py"
    assert reference.anchor_line == 1
    assert merged.graph.vs[reference.source_vid]["name"] == "a.py:foo()"
    assert merged.graph.vs[reference.target_vid]["name"] == "b.go:bar()"

    defined = merged.query_range("b.go", 4, 4).defined
    assert [node.name for node in defined] == ["b.go:bar()"]
