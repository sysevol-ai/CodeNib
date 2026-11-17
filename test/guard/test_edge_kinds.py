# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for classifying reference edges as calls, bases, imports or reads."""

from __future__ import annotations

import textwrap

from codenib.graph.edge_kinds import file_mentions, reference_kinds

SOURCE = textwrap.dedent(
    """\
    import shop.pricing
    from shop.pricing import (
        Cart,
        total as price,
    )


    @register
    class Basket(Cart, metaclass=Meta):
        limit: Limit = default_limit()

        @cached(ttl)
        def add(self, item: "Item", extra: list[Extra] | None = EMPTY) -> Cart:
            handler = callback
            self.items.append(
                item
            ).save()
            return Cart(total(total))


    Alias = pricing.Cart
    """
)


def _mentions(tmp_path):
    path = tmp_path / "basket.py"
    path.write_text(SOURCE)
    return file_mentions(path)


def test_each_way_of_mentioning_a_name_gets_its_kind(tmp_path):
    mentions = _mentions(tmp_path)

    assert mentions[(1, "pricing")] == "import"
    assert mentions[(3, "Cart")] == "import"
    assert mentions[(4, "total")] == "import"
    assert mentions[(8, "register")] == "call"  # a bare decorator runs
    assert mentions[(9, "Cart")] == "inherit"
    assert mentions[(9, "Meta")] == "read"
    assert mentions[(10, "Limit")] == "annotation"
    assert mentions[(10, "default_limit")] == "call"
    assert mentions[(12, "cached")] == "call"
    assert mentions[(12, "ttl")] == "read"
    assert mentions[(13, "Extra")] == "annotation"
    assert mentions[(13, "list")] == "annotation"
    assert mentions[(13, "EMPTY")] == "read"
    assert mentions[(13, "Cart")] == "annotation"
    assert mentions[(14, "callback")] == "read"  # passed around, not called
    # ``append`` is named on the line where the attribute ends.
    assert mentions[(15, "append")] == "call"
    assert mentions[(17, "save")] == "call"
    assert mentions[(18, "Cart")] == "call"
    # ``total(total)`` mentions the name twice; the call wins.
    assert mentions[(18, "total")] == "call"
    assert mentions[(21, "Cart")] == "read"
    # A name inside a string annotation is not placed.
    assert (13, "Item") not in mentions


def test_edges_are_classified_by_their_anchor_line(make_graph, tmp_path):
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop/basket.py").write_text(SOURCE)
    nodes = [
        ("shop/basket.py", "file", None, None, None),
        ("shop/basket.py:Basket", "class", "shop/basket.py", 8, 17),
        ("shop/basket.py:Basket.add()", "method", "shop/basket.py", 11, 17),
        ("shop/pricing.py", "file", None, None, None),
        ("shop/pricing.py:Cart", "class", "shop/pricing.py", 0, 5),
        ("shop/pricing.py:total()", "function", "shop/pricing.py", 7, 8),
    ]
    edges = [
        ("shop/basket.py", "shop/basket.py:Basket", "contain", None),
        ("shop/basket.py", "shop/pricing.py", "reference", 0),
        ("shop/basket.py", "shop/pricing.py:Cart", "reference", 2),
        ("shop/basket.py:Basket", "shop/pricing.py:Cart", "reference", 8),
        ("shop/basket.py:Basket.add()", "shop/pricing.py:Cart", "reference", 12),
        ("shop/basket.py:Basket.add()", "shop/pricing.py:Cart", "reference", 17),
        ("shop/basket.py:Basket.add()", "shop/pricing.py:total()", "reference", 17),
        ("shop/basket.py", "shop/pricing.py:Cart", "reference", 20),
        # No mention of the name on this line: left unclassified.
        ("shop/basket.py:Basket.add()", "shop/pricing.py:total()", "reference", 13),
    ]
    graph = make_graph(nodes, edges)
    graph.es["anchor_file"] = [
        None if kind == "contain" else "shop/basket.py" for _, _, kind, _ in edges
    ]

    assert reference_kinds(graph, tmp_path) == [
        None,
        "import",
        "import",
        "inherit",
        "annotation",
        "call",
        "call",
        "read",
        None,
    ]


def test_unreadable_source_leaves_edges_unclassified(make_graph, tmp_path):
    graph = make_graph(
        [("a.py", "file", None, None, None), ("b.py:f()", "function", "b.py", 0, 1)],
        [("a.py", "b.py:f()", "reference", 0)],
    )
    graph.es["anchor_file"] = ["a.py"]

    assert reference_kinds(graph, tmp_path) == [None]
