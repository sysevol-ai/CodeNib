# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for reaching an override from a call made through its base class."""

from __future__ import annotations

import textwrap

from codenib.graph.override_edges import class_bases, override_pairs
from codenib.guard.impact import ImpactGraph

SHAPES = textwrap.dedent(
    """\
    class Shape:
        def area(self):
            return 0

        def __eq__(self, other):
            return True


    class Square(Shape):
        def area(self):
            return 4

        def __eq__(self, other):
            return False


    class Tiny(Square):
        def area(self):
            return 1
    """
)
NODES = [
    ("shop/shapes.py", "file", None, None, None),
    ("shop/shapes.py:Shape", "class", "shop/shapes.py", 0, 5),
    ("shop/shapes.py:Shape.area()", "method", "shop/shapes.py", 1, 2),
    ("shop/shapes.py:Shape.__eq__()", "method", "shop/shapes.py", 4, 5),
    ("shop/shapes.py:Square", "class", "shop/shapes.py", 8, 13),
    ("shop/shapes.py:Square.area()", "method", "shop/shapes.py", 9, 10),
    ("shop/shapes.py:Square.__eq__()", "method", "shop/shapes.py", 12, 13),
    ("shop/shapes.py:Tiny", "class", "shop/shapes.py", 16, 18),
    ("shop/shapes.py:Tiny.area()", "method", "shop/shapes.py", 17, 18),
    ("shop/draw.py:total()", "function", "shop/draw.py", 0, 2),
    ("tests/test_draw.py:test_total()", "function", "tests/test_draw.py", 0, 2),
    ("tests/test_draw.py:test_same()", "function", "tests/test_draw.py", 4, 6),
]
EDGES = [
    ("shop/shapes.py:Shape", "shop/shapes.py:Shape.area()", "contain", None),
    ("shop/shapes.py:Shape", "shop/shapes.py:Shape.__eq__()", "contain", None),
    ("shop/shapes.py:Square", "shop/shapes.py:Square.area()", "contain", None),
    ("shop/shapes.py:Square", "shop/shapes.py:Square.__eq__()", "contain", None),
    ("shop/shapes.py:Tiny", "shop/shapes.py:Tiny.area()", "contain", None),
    # ``class Square(Shape)`` and ``class Tiny(Square)``.
    ("shop/shapes.py:Square", "shop/shapes.py:Shape", "reference", 8),
    ("shop/shapes.py:Tiny", "shop/shapes.py:Square", "reference", 16),
    # ``total`` calls ``shape.area()``, resolved to the base class.
    ("shop/draw.py:total()", "shop/shapes.py:Shape.area()", "reference", 1),
    ("tests/test_draw.py:test_total()", "shop/draw.py:total()", "reference", 1),
    ("tests/test_draw.py:test_same()", "shop/shapes.py:Shape.__eq__()", "reference", 5),
]


def _graph(make_graph, tmp_path, write_files):
    write_files(tmp_path, {"shop/shapes.py": SHAPES})
    graph = make_graph(NODES, EDGES)
    graph.es["anchor_file"] = [
        "shop/shapes.py" if line in (8, 16) else None for _, _, _, line in EDGES
    ]
    return graph


def _named(graph, pairs):
    return sorted((graph.vs[a]["name"], graph.vs[b]["name"]) for a, b in pairs)


def test_each_override_is_paired_with_every_ancestor_s_method(
    make_graph, tmp_path, write_files
):
    graph = _graph(make_graph, tmp_path, write_files)

    assert {
        graph.vs[cls]["name"]: sorted(graph.vs[b]["name"] for b in bases)
        for cls, bases in class_bases(graph, tmp_path).items()
    } == {
        "shop/shapes.py:Square": ["shop/shapes.py:Shape"],
        "shop/shapes.py:Tiny": ["shop/shapes.py:Square"],
    }
    # ``__eq__`` is an operator, not a call the graph records: left out.
    assert _named(graph, override_pairs(graph, tmp_path)) == [
        ("shop/shapes.py:Square.area()", "shop/shapes.py:Shape.area()"),
        ("shop/shapes.py:Tiny.area()", "shop/shapes.py:Shape.area()"),
        ("shop/shapes.py:Tiny.area()", "shop/shapes.py:Square.area()"),
    ]


def test_change_in_an_override_reaches_callers_through_the_base(
    make_graph, tmp_path, write_files
):
    graph = ImpactGraph(_graph(make_graph, tmp_path, write_files), tmp_path)

    # ``Tiny.area`` (line 19) is two classes below the method that is called.
    impact = graph.impact({"shop/shapes.py": {19}})

    assert [(t.test_id, t.depth) for t in impact.tests] == [
        ("tests/test_draw.py::test_total", 2)
    ]
    assert [hop.symbol for hop in impact.tests[0].path] == [
        "tests/test_draw.py:test_total",
        "shop/draw.py:total",
        "shop/shapes.py:Tiny.area",
    ]


def test_change_in_a_special_method_gains_no_callers(make_graph, tmp_path, write_files):
    graph = ImpactGraph(_graph(make_graph, tmp_path, write_files), tmp_path)

    assert graph.impact({"shop/shapes.py": {14}}).tests == []
