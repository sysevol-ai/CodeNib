# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for edges through a module-level alias such as ``name = obj.name``."""

from __future__ import annotations

import textwrap

from codenib.graph.alias_edges import alias_edges
from codenib.guard.impact import ImpactGraph

INIT = textwrap.dedent(
    """\
    from shop.widgets import Widgets

    _main = Widgets()
    date_input = _main.date_input
    typed: object = _main.slider
    clock = _main.now()
    other = _main.unrelated
    """
)
NODES = [
    ("shop/__init__.py", "file", None, None, None),
    ("shop.py:_main", "field", "shop/__init__.py", 2, 2),
    ("shop.py:date_input", "field", "shop/__init__.py", 3, 3),
    ("shop.py:typed", "field", "shop/__init__.py", 4, 4),
    ("shop.py:clock", "field", "shop/__init__.py", 5, 5),
    ("shop.py:other", "field", "shop/__init__.py", 6, 6),
    ("shop/widgets.py:Widgets", "class", "shop/widgets.py", 0, 12),
    ("shop/widgets.py:Widgets.date_input()", "method", "shop/widgets.py", 1, 2),
    ("shop/widgets.py:Widgets.slider()", "method", "shop/widgets.py", 4, 5),
    ("shop/widgets.py:Widgets.now()", "method", "shop/widgets.py", 7, 8),
    ("shop/widgets.py:Widgets.renamed()", "method", "shop/widgets.py", 10, 11),
    ("tests/test_shop.py:test_date()", "function", "tests/test_shop.py", 3, 4),
    ("tests/test_shop.py:test_slider()", "function", "tests/test_shop.py", 6, 7),
    ("tests/test_shop.py:test_clock()", "function", "tests/test_shop.py", 9, 10),
    ("tests/test_shop.py:test_other()", "function", "tests/test_shop.py", 12, 13),
]
EDGES = [
    # The module refers to each method on the line of its assignment.
    ("shop/__init__.py", "shop/widgets.py:Widgets.date_input()", "reference", 3),
    ("shop/__init__.py", "shop/widgets.py:Widgets.slider()", "reference", 4),
    ("shop/__init__.py", "shop/widgets.py:Widgets.now()", "reference", 5),
    ("shop/__init__.py", "shop/widgets.py:Widgets.renamed()", "reference", 6),
    # Tests use the published names.
    ("tests/test_shop.py:test_date()", "shop.py:date_input", "reference", 4),
    ("tests/test_shop.py:test_slider()", "shop.py:typed", "reference", 7),
    ("tests/test_shop.py:test_clock()", "shop.py:clock", "reference", 10),
    ("tests/test_shop.py:test_other()", "shop.py:other", "reference", 13),
]


def _named(graph, edges):
    return sorted(
        (graph.vs[e.user]["name"], graph.vs[e.target]["name"], e.line) for e in edges
    )


def test_user_of_a_published_name_reaches_the_method(make_graph, tmp_path, write_files):
    write_files(tmp_path, {"shop/__init__.py": INIT})
    graph = make_graph(NODES, EDGES)

    assert _named(graph, alias_edges(graph, tmp_path)) == [
        # ``clock = _main.now()`` is a value and ``other = _main.unrelated``
        # does not name the method the module refers to: neither is an alias.
        ("tests/test_shop.py:test_date()", "shop/widgets.py:Widgets.date_input()", 5),
        ("tests/test_shop.py:test_slider()", "shop/widgets.py:Widgets.slider()", 8),
    ]


def test_change_behind_an_alias_puts_its_users_at_risk(
    make_graph, tmp_path, write_files
):
    write_files(tmp_path, {"shop/__init__.py": INIT})
    graph = ImpactGraph(make_graph(NODES, EDGES), tmp_path)

    impact = graph.impact({"shop/widgets.py": {2}})

    assert [(t.test_id, t.depth) for t in impact.tests] == [
        ("tests/test_shop.py::test_date", 1)
    ]


def test_unreadable_source_yields_no_alias(make_graph, tmp_path):
    assert alias_edges(make_graph(NODES, EDGES), tmp_path) == []
