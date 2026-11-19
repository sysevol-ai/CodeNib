# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for reaching a special method from code that uses its class."""

from __future__ import annotations

import textwrap

from codenib.graph.operator_edges import operator_edges
from codenib.guard.impact import ImpactGraph

NUMBERS = textwrap.dedent(
    """\
    class Number:
        def __init__(self, value):
            self.value = value

        def __eq__(self, other):
            return self.value == other.value

        def __getattr__(self, name):
            return None


    class Float(Number):
        def __hash__(self):
            return 0
    """
)
NODES = [
    ("num/numbers.py", "file", None, None, None),
    ("num/numbers.py:Number", "class", "num/numbers.py", 0, 8),
    ("num/numbers.py:Number.__init__()", "method", "num/numbers.py", 1, 2),
    ("num/numbers.py:Number.__eq__()", "method", "num/numbers.py", 4, 5),
    ("num/numbers.py:Number.__getattr__()", "method", "num/numbers.py", 7, 8),
    ("num/numbers.py:Float", "class", "num/numbers.py", 11, 13),
    ("num/numbers.py:Float.__hash__()", "method", "num/numbers.py", 12, 13),
    ("num/compare.py:same()", "function", "num/compare.py", 0, 1),
    ("tests/test_compare.py:test_same()", "function", "tests/test_compare.py", 0, 1),
]
EDGES = [
    ("num/numbers.py:Number", "num/numbers.py:Number.__init__()", "contain", None),
    ("num/numbers.py:Number", "num/numbers.py:Number.__eq__()", "contain", None),
    ("num/numbers.py:Number", "num/numbers.py:Number.__getattr__()", "contain", None),
    ("num/numbers.py:Float", "num/numbers.py:Float.__hash__()", "contain", None),
    # ``class Float(Number)``.
    ("num/numbers.py:Float", "num/numbers.py:Number", "reference", 11),
    # ``same`` returns ``Float(1) == Float(1)``.
    ("num/compare.py:same()", "num/numbers.py:Float", "reference", 1),
    ("tests/test_compare.py:test_same()", "num/compare.py:same()", "reference", 1),
]


def _graph(make_graph, tmp_path, write_files):
    write_files(tmp_path, {"num/numbers.py": NUMBERS})
    graph = make_graph(NODES, EDGES)
    graph.es["anchor_file"] = [
        "num/numbers.py" if line == 11 else "num/compare.py" if line == 1 else None
        for _, _, _, line in EDGES
    ]
    return graph


def test_a_class_user_reaches_its_own_and_inherited_operators(
    make_graph, tmp_path, write_files
):
    graph = _graph(make_graph, tmp_path, write_files)

    edges = operator_edges(graph, tmp_path)

    # ``__init__`` belongs to constructor edges and ``__getattr__`` is
    # attribute access, not an operator.
    assert sorted(
        (graph.vs[e.user]["name"], graph.vs[e.method]["name"], e.line) for e in edges
    ) == [
        ("num/compare.py:same()", "num/numbers.py:Float.__hash__()", 2),
        ("num/compare.py:same()", "num/numbers.py:Number.__eq__()", 2),
    ]


def test_a_change_in_an_inherited_operator_reaches_tests(
    make_graph, tmp_path, write_files
):
    graph = ImpactGraph(_graph(make_graph, tmp_path, write_files), tmp_path)

    # Line 6 is inside ``Number.__eq__``; ``same`` compares two ``Float``.
    impact = graph.impact({"num/numbers.py": {6}})

    assert [(t.test_id, t.depth) for t in impact.tests] == [
        ("tests/test_compare.py::test_same", 2)
    ]
    assert [hop.symbol for hop in impact.tests[0].path] == [
        "tests/test_compare.py:test_same",
        "num/compare.py:same",
        "num/numbers.py:Number.__eq__",
    ]


def test_a_class_nobody_uses_adds_no_edges(make_graph, tmp_path, write_files):
    write_files(tmp_path, {"num/numbers.py": NUMBERS})
    graph = make_graph(NODES[:7], EDGES[:5])
    graph.es["anchor_file"] = [
        "num/numbers.py" if l == 11 else None for *_, l in EDGES[:5]
    ]

    assert operator_edges(graph, tmp_path) == []
