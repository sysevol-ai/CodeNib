# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the connectivity figures a symbol graph build reports."""

from __future__ import annotations

import igraph as ig

from codenib.graph.health import graph_health


def _graph(nodes, edges) -> ig.Graph:
    """``nodes``: (name, type, file, start, end). ``edges``: (source, target)."""

    graph = ig.Graph(directed=True)
    graph.add_vertices(len(nodes))
    graph.vs["name"] = [n[0] for n in nodes]
    graph.vs["type"] = [n[1] for n in nodes]
    graph.vs["file"] = [n[2] for n in nodes]
    graph.vs["start_line"] = [n[3] for n in nodes]
    graph.vs["end_line"] = [n[4] for n in nodes]
    graph.vs["has_definition"] = [n[2] is not None for n in nodes]
    index = {n[0]: i for i, n in enumerate(nodes)}
    graph.add_edges([(index[s], index[t]) for s, t in edges])
    graph.es["type"] = ["reference"] * len(edges)
    return graph


def _tests(count):
    return [
        (f"tests/test_shop.py:test_{i}()", "function", "tests/test_shop.py", i, i)
        for i in range(count)
    ]


SOURCE = [("shop/cart.py:total()", "function", "shop/cart.py", 0, 3)]


def test_connected_graph_has_no_warning():
    tests = _tests(60)
    edges = [(name, "shop/cart.py:total()") for name, *_ in tests[:45]]
    health = graph_health(_graph(SOURCE + tests, edges))

    assert health["test_functions"] == 60
    assert health["tests_reaching_source"] == 0.75
    assert health["repeated_definitions"] == 0.0
    assert health["warnings"] == []


def test_tests_cut_off_from_source_are_reported():
    tests = _tests(60)
    # A test that only calls another test helper does not reach source.
    edges = [(name, "shop/cart.py:total()") for name, *_ in tests[:6]]
    edges += [(tests[10][0], tests[11][0])]
    health = graph_health(_graph(SOURCE + tests, edges))

    assert health["tests_reaching_source"] == 0.1
    [warning] = health["warnings"]
    assert "Only 10% of 60 test functions" in warning
    assert "may not resolve" in warning


def test_few_tests_are_too_few_to_judge():
    health = graph_health(_graph(SOURCE + _tests(5), []))

    assert health["tests_reaching_source"] == 0.0
    assert health["warnings"] == []
    assert graph_health(_graph(SOURCE, []))["tests_reaching_source"] is None


def test_definitions_repeated_at_one_place_are_reported():
    # The same file reached under three names, as through symbolic links.
    nodes = [
        (f"{prefix}shop/cart.py:f{i}()", "function", "shop/cart.py", i * 3, i * 3 + 2)
        for prefix in ("", "libs/a/", "libs/b/")
        for i in range(100)
    ]
    health = graph_health(_graph(nodes, []))

    assert health["definitions"] == 300
    assert health["repeated_definitions"] == 1.0
    [warning] = health["warnings"]
    assert "appear more than once" in warning
