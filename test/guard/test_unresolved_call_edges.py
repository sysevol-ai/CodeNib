# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for linking method calls on unknown receivers to their candidates."""

from __future__ import annotations

import textwrap

from codenib.graph.unresolved_call_edges import unresolved_call_edges
from codenib.guard.impact import ImpactGraph

COMPILER = textwrap.dedent(
    """\
    def compile_node(node, compiler):
        return node.as_sql(compiler)


    def render(item):
        return item.render()


    def known(lookup):
        return lookup.as_sql(None)
    """
)
NODES = [
    ("orm/compiler.py", "file", None, None, None),
    ("orm/compiler.py:compile_node()", "function", "orm/compiler.py", 0, 1),
    ("orm/compiler.py:render()", "function", "orm/compiler.py", 4, 5),
    ("orm/compiler.py:known()", "function", "orm/compiler.py", 8, 9),
    ("orm/lookups.py:Exact", "class", "orm/lookups.py", 0, 2),
    ("orm/lookups.py:Exact.as_sql()", "method", "orm/lookups.py", 1, 2),
    ("orm/where.py:WhereNode", "class", "orm/where.py", 0, 2),
    ("orm/where.py:WhereNode.as_sql()", "method", "orm/where.py", 1, 2),
    ("tests/test_lookups.py:Fake", "class", "tests/test_lookups.py", 0, 2),
    ("tests/test_lookups.py:Fake.as_sql()", "method", "tests/test_lookups.py", 1, 2),
    ("tests/test_lookups.py:test_compile()", "function", "tests/test_lookups.py", 4, 5),
] + [
    (f"orm/widgets.py:W{i}.render()", "method", "orm/widgets.py", 3 * i, 3 * i + 1)
    for i in range(4)
]
EDGES = [
    ("orm/lookups.py:Exact", "orm/lookups.py:Exact.as_sql()", "contain", None),
    ("orm/where.py:WhereNode", "orm/where.py:WhereNode.as_sql()", "contain", None),
    (
        "tests/test_lookups.py:Fake",
        "tests/test_lookups.py:Fake.as_sql()",
        "contain",
        None,
    ),
    # ``known`` annotates its receiver, so its call was resolved.
    ("orm/compiler.py:known()", "orm/lookups.py:Exact.as_sql()", "reference", 9),
    (
        "tests/test_lookups.py:test_compile()",
        "orm/compiler.py:compile_node()",
        "reference",
        5,
    ),
]


def _graph(make_graph, tmp_path, write_files):
    write_files(tmp_path, {"orm/compiler.py": COMPILER})
    return make_graph(NODES, EDGES)


def test_an_unresolved_call_reaches_each_project_method_of_its_name(
    make_graph, tmp_path, write_files
):
    graph = _graph(make_graph, tmp_path, write_files)

    edges = unresolved_call_edges(graph, tmp_path)

    # ``render`` has four definitions, more than the default of three; the
    # test file's ``Fake.as_sql`` is not a target; ``known`` was resolved.
    assert [
        (graph.vs[e.caller]["name"], graph.vs[e.method]["name"], e.line) for e in edges
    ] == [
        ("orm/compiler.py:compile_node()", "orm/lookups.py:Exact.as_sql()", 2),
        ("orm/compiler.py:compile_node()", "orm/where.py:WhereNode.as_sql()", 2),
    ]


def test_the_limit_on_definitions_is_a_parameter(make_graph, tmp_path, write_files):
    graph = _graph(make_graph, tmp_path, write_files)

    edges = unresolved_call_edges(graph, tmp_path, max_targets=4)

    assert (
        sum(graph.vs[e.caller]["name"] == "orm/compiler.py:render()" for e in edges)
        == 4
    )


def test_a_change_in_a_candidate_reaches_tests(make_graph, tmp_path, write_files):
    graph = ImpactGraph(_graph(make_graph, tmp_path, write_files), tmp_path)

    # Line 2 is inside ``WhereNode.as_sql``.
    impact = graph.impact({"orm/where.py": {2}})

    assert [(t.test_id, t.depth) for t in impact.tests] == [
        ("tests/test_lookups.py::test_compile", 2)
    ]
    assert impact.tests[0].path[1].call_line == 2


def test_the_text_check_keeps_calls_split_across_lines(tmp_path):
    from codenib.graph.unresolved_call_edges import _attribute_calls

    path = tmp_path / "split.py"
    path.write_text(
        "def run(node):\n"
        "    return (node.as_sql  # the compiled form\n"
        "            (None))\n"
        "\n"
        "\n"
        "def other(node):\n"
        "    return node \\\n"
        "        .as_sql()\n",
        encoding="utf-8",
    )

    assert sorted(_attribute_calls(path, {"as_sql"})) == [(2, "as_sql"), (7, "as_sql")]
    # A file with no call of a wanted name is not parsed at all.
    assert _attribute_calls(path, {"render"}) == []
