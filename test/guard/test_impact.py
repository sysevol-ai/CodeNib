# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for constructor edges and the guard's impact analysis."""

from __future__ import annotations

import textwrap

from codenib.graph.constructor_edges import constructor_edges, short_name
from codenib.guard.impact import (
    ImpactGraph,
    base_changed_lines,
    base_rewritten_lines,
    pytest_id,
    symbol_id,
)

PRICING = textwrap.dedent(
    """\
    class Base:
        def __init__(self, items):
            self.items = list(items)


    class Cart(Base):
        def total(self):
            return sum(self.items)


    Basket = Cart
    """
)
USES = textwrap.dedent(
    """\
    from shop.pricing import Cart as C, Basket


    def build(items):
        return C(items)


    def kind(value):
        return isinstance(value, C)


    def basket():
        return Basket([])
    """
)
TESTS = textwrap.dedent(
    """\
    from shop.uses import build


    def test_build():
        assert build([1]).total() == 1


    class TestKind:
        def test_kind(self):
            assert kind(None) is False
    """
)

NODES = [
    ("shop/pricing.py", "file", None, None, None),
    ("shop/pricing.py:Base", "class", "shop/pricing.py", 0, 2),
    ("shop/pricing.py:Base.__init__()", "method", "shop/pricing.py", 1, 2),
    ("shop/pricing.py:Cart", "class", "shop/pricing.py", 5, 7),
    ("shop/pricing.py:Cart.total()", "method", "shop/pricing.py", 6, 7),
    ("shop/pricing.py:Basket", "field", "shop/pricing.py", 10, 10),
    ("shop/uses.py", "file", None, None, None),
    ("shop/uses.py:build()", "function", "shop/uses.py", 3, 4),
    ("shop/uses.py:kind()", "function", "shop/uses.py", 7, 8),
    ("shop/uses.py:basket()", "function", "shop/uses.py", 11, 12),
    ("tests/test_shop.py", "file", None, None, None),
    ("tests/test_shop.py:test_build()", "function", "tests/test_shop.py", 3, 4),
    ("tests/test_shop.py:TestKind", "class", "tests/test_shop.py", 7, 9),
    ("tests/test_shop.py:TestKind.test_kind()", "method", "tests/test_shop.py", 8, 9),
]
EDGES = [
    ("shop/pricing.py", "shop/pricing.py:Base", "contain", None),
    ("shop/pricing.py:Base", "shop/pricing.py:Base.__init__()", "contain", None),
    ("shop/pricing.py", "shop/pricing.py:Cart", "contain", None),
    ("shop/pricing.py:Cart", "shop/pricing.py:Cart.total()", "contain", None),
    ("shop/pricing.py:Cart", "shop/pricing.py:Base", "reference", 5),
    ("shop/pricing.py", "shop/pricing.py:Cart", "reference", 10),
    ("shop/uses.py:build()", "shop/pricing.py:Cart", "reference", 4),
    ("shop/uses.py:kind()", "shop/pricing.py:Cart", "reference", 8),
    ("shop/uses.py:basket()", "shop/pricing.py:Basket", "reference", 12),
    ("tests/test_shop.py:test_build()", "shop/uses.py:build()", "reference", 4),
    ("tests/test_shop.py:test_build()", "shop/pricing.py:Cart.total()", "reference", 4),
    ("tests/test_shop.py:TestKind.test_kind()", "shop/uses.py:kind()", "reference", 9),
]


def _repo(tmp_path, write_files):
    write_files(
        tmp_path,
        {"shop/pricing.py": PRICING, "shop/uses.py": USES, "tests/test_shop.py": TESTS},
    )
    return tmp_path


def test_short_name_drops_parentheses_and_disambiguators():
    assert short_name("a.py:C.m()") == "m"
    assert short_name("t.py:test_x().Form@codenib-l12-c4") == "Form"
    assert symbol_id("a.py:f().g()") == "a.py:f.g"
    assert pytest_id("tests/a.py:Suite.test_x()") == "tests/a.py::Suite::test_x"


def test_constructor_edges_follow_calls_aliases_and_bases(
    tmp_path, make_graph, write_files
):
    root = _repo(tmp_path, write_files)
    graph = make_graph(NODES, EDGES)
    edges = {
        (graph.vs[e.caller]["name"], graph.vs[e.constructor]["name"]): e.line
        for e in constructor_edges(graph, root)
    }
    init = "shop/pricing.py:Base.__init__()"
    # C(items) through an import alias runs the inherited Base.__init__.
    assert edges[("shop/uses.py:build()", init)] == 5
    # Basket([]) through a module-level alias of Cart.
    assert edges[("shop/uses.py:basket()", init)] == 13
    # isinstance(value, C) references Cart without calling it.
    assert ("shop/uses.py:kind()", init) not in edges
    # Cart's base list references Base without constructing it.
    assert ("shop/pricing.py:Cart", init) not in edges


def test_impact_reports_tests_with_paths_and_call_lines(
    tmp_path, make_graph, write_files
):
    root = _repo(tmp_path, write_files)
    graph = ImpactGraph(make_graph(NODES, EDGES), root)
    impact = graph.impact({"shop/pricing.py": {3}}, depth=3)
    assert [c.symbol for c in impact.changed] == ["shop/pricing.py:Base.__init__"]
    assert [t.test_id for t in impact.tests] == ["tests/test_shop.py::test_build"]
    test = impact.tests[0]
    assert test.depth == 2
    assert [(h.symbol, h.call_line) for h in test.path] == [
        ("tests/test_shop.py:test_build", 5),
        ("shop/uses.py:build", 5),
        ("shop/pricing.py:Base.__init__", None),
    ]
    assert impact.total_tests == 2


def test_impact_respects_depth_and_maps_class_level_lines(
    tmp_path, make_graph, write_files
):
    root = _repo(tmp_path, write_files)
    graph = ImpactGraph(make_graph(NODES, EDGES), root)
    assert graph.impact({"shop/pricing.py": {3}}, depth=1).tests == []
    # A line in Cart's body outside its methods maps to Cart and its methods.
    impact = graph.impact({"shop/pricing.py": {6}}, depth=3)
    assert impact.changed[0].unit == "class"
    assert {t.test_id for t in impact.tests} == {
        "tests/test_shop.py::test_build",
        "tests/test_shop.py::TestKind::test_kind",
    }


def test_unknown_python_files_are_reported_as_unmapped(
    tmp_path, make_graph, write_files
):
    root = _repo(tmp_path, write_files)
    graph = ImpactGraph(make_graph(NODES, EDGES), root)
    impact = graph.impact({"shop/other.py": {1}, "README.md": {1}}, depth=3)
    assert impact.unmapped_files == ["shop/other.py"]
    assert impact.changed == [] and impact.tests == []


def test_base_changed_lines_uses_base_side_numbers():
    diff = textwrap.dedent(
        """\
        diff --git a/shop/a.py b/shop/a.py
        --- a/shop/a.py
        +++ b/shop/a.py
        @@ -3,2 +3,3 @@ def f():
        @@ -10,0 +12,2 @@ def g():
        @@ -20 +22,0 @@ def h():
        diff --git a/shop/new.py b/shop/new.py
        new file mode 100644
        --- /dev/null
        +++ b/shop/new.py
        @@ -0,0 +1,2 @@
        diff --git a/shop/gone.py b/shop/gone.py
        deleted file mode 100644
        --- a/shop/gone.py
        +++ /dev/null
        @@ -1,4 +0,0 @@
        """
    )
    changed, added = base_changed_lines(diff)
    assert changed == {"shop/a.py": {3, 4, 10, 20}, "shop/gone.py": {1, 2, 3, 4}}
    assert added == ["shop/new.py"]


def test_tests_that_call_in_come_before_edited_tests(tmp_path, make_graph, write_files):
    root = _repo(tmp_path, write_files)
    graph = ImpactGraph(make_graph(NODES, EDGES), root)
    # The edit touches Base.__init__ and the test file's own test_build.
    impact = graph.impact({"shop/pricing.py": {3}, "tests/test_shop.py": {9}}, depth=3)
    assert [(t.test_id, t.depth) for t in impact.tests] == [
        ("tests/test_shop.py::test_build", 2),
        ("tests/test_shop.py::TestKind::test_kind", 0),
    ]


def test_base_rewritten_lines_ignore_pure_insertions():
    diff = textwrap.dedent(
        """\
        diff --git a/tests/test_shop.py b/tests/test_shop.py
        --- a/tests/test_shop.py
        +++ b/tests/test_shop.py
        @@ -3,2 +3,2 @@ def helper():
        -    old
        -    old
        +    new
        +    new
        @@ -10,0 +11,3 @@ def test_build():
        +def test_added():
        +    pass
        +
        @@ -20 +22,0 @@ class TestKind:
        -    gone
        diff --git a/tests/test_new.py b/tests/test_new.py
        --- /dev/null
        +++ b/tests/test_new.py
        @@ -0,0 +1 @@
        +def test_new(): pass
        """
    )
    assert base_rewritten_lines(diff) == {"tests/test_shop.py": {3, 4, 20}}
    changed, added = base_changed_lines(diff)  # the shared parser is unchanged
    assert changed == {"tests/test_shop.py": {3, 4, 10, 20}}
    assert added == ["tests/test_new.py"]


def test_edited_existing_tests_come_first(tmp_path, make_graph, write_files):
    root = _repo(tmp_path, write_files)
    graph = ImpactGraph(make_graph(NODES, EDGES), root)
    # Base.__init__ changes and test_kind's own assertion is rewritten.
    impact = graph.impact(
        {"shop/pricing.py": {3}, "tests/test_shop.py": {10}},
        depth=3,
        rewritten_lines={"shop/pricing.py": {3}, "tests/test_shop.py": {10}},
    )
    assert impact.edited_tests == ["tests/test_shop.py::TestKind::test_kind"]
    assert [(t.test_id, t.depth) for t in impact.tests] == [
        ("tests/test_shop.py::TestKind::test_kind", 0),
        ("tests/test_shop.py::test_build", 2),
    ]


def test_code_added_after_a_test_leaves_it_unedited(tmp_path, make_graph, write_files):
    root = _repo(tmp_path, write_files)
    graph = ImpactGraph(make_graph(NODES, EDGES), root)
    # A new test inserted after test_build counts as a change to test_build's
    # last line, but rewrites none of its lines.
    impact = graph.impact(
        {"tests/test_shop.py": {5}},
        depth=3,
        rewritten_lines={"tests/test_shop.py": set()},
    )
    assert [t.test_id for t in impact.tests] == ["tests/test_shop.py::test_build"]
    assert impact.edited_tests == []
