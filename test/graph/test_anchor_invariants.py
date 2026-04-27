"""Anchor invariants: CONTAIN edges carry no anchor; REFERENCE edges always do.

See `CodeGraph.query_range` docstring for the three anchor invariants.
"""

from codeminer.graph.code_graph import CodeGraph
from codeminer.types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE


def test_contain_edge_has_no_anchor():
    g = CodeGraph(project_root="/tmp/x")
    g.add_file_node("a.py")
    g._add_vertex(
        "Foo",
        {
            "type": "class",
            "file": "a.py",
            "start_line": 1,
            "end_line": 30,
            "unified_name": "a.py:Foo",
        },
    )
    g.update_current_scope("Foo", 1, 30)
    g.add_symbol_node(
        "Foo.bar",
        line=5,
        scope_start_line=5,
        scope_end_line=15,
        symbol_type="method",
    )
    g.add_containment_edge("Foo.bar")

    found = False
    for e in g.graph.es:
        if e["type"] == EDGE_TYPE_CONTAIN:
            attrs = e.attributes()
            assert (
                attrs.get("anchor_file") is None
            ), f"CONTAIN edges must not carry anchor_file; got {attrs.get('anchor_file')!r}"
            assert (
                attrs.get("anchor_line") is None
            ), f"CONTAIN edges must not carry anchor_line; got {attrs.get('anchor_line')!r}"
            found = True
    assert found, "test setup did not produce a CONTAIN edge"


def test_reference_edge_carries_anchor():
    g = CodeGraph(project_root="/tmp/x")
    g.add_file_node("a.py")
    g._add_vertex(
        "Foo",
        {
            "type": "function",
            "file": "a.py",
            "start_line": 1,
            "end_line": 10,
            "unified_name": "a.py:Foo",
        },
    )
    g.update_current_scope("Foo", 1, 10)
    g.add_symbol_reference("Bar", anchor_file="a.py", anchor_line=5)

    found = False
    for e in g.graph.es:
        if e["type"] == EDGE_TYPE_REFERENCE:
            attrs = e.attributes()
            assert attrs.get("anchor_file") == "a.py"
            assert attrs.get("anchor_line") == 5
            found = True
    assert found, "test setup did not produce a REFERENCE edge"
