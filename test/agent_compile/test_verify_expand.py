# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the deterministic verify-expand core (runtime Layer 4).

Builds a tiny in-memory igraph (no repo / no LLM) and checks that GraphNav
resolves symbols + returns 1-hop neighbours, and that graph_verify flags an
unanchored answer while accepting one that names a real symbol or a Locations:
span.
"""

import sys
from pathlib import Path

import igraph

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.agent_compile.lib.verify_expand import (  # noqa: E402
    GraphNav,
    expansion_seeds_from_candidates,
    graph_verify,
    render_expansion,
)


def _toy_graph() -> igraph.Graph:
    # 3 symbols in one file: foo calls bar; baz is separate.
    g = igraph.Graph(directed=True)
    g.add_vertices(3)
    g.vs["name"] = ["pkg/mod.go:foo", "pkg/mod.go:bar", "pkg/mod.go:baz"]
    g.vs["unified_name"] = g.vs["name"]
    g.vs["type"] = ["function", "function", "function"]
    g.vs["file"] = ["pkg/mod.go", "pkg/mod.go", "pkg/mod.go"]
    g.vs["start_line"] = [9, 19, 29]  # 0-based; GraphNav shifts +1
    g.vs["end_line"] = [14, 24, 34]
    g.add_edges([(0, 1)])  # foo -> bar
    return g


def _nav() -> GraphNav:
    return GraphNav(_toy_graph())


def test_graphnav_resolves_real_symbol():
    nav = _nav()
    assert nav.resolves("pkg/mod.go", "foo")
    assert not nav.resolves("pkg/mod.go", "doesNotExist")


def test_graphnav_neighbors_one_hop():
    nav = _nav()
    nb = nav.neighbors([("pkg/mod.go", "foo")], max_nodes=10)
    names = {c["name"] for c in nb}
    assert "pkg/mod.go:bar" in names  # foo's 1-hop neighbour
    # spans are 1-based (0-based 19/24 -> 20/25)
    bar = next(c for c in nb if c["name"] == "pkg/mod.go:bar")
    assert (bar["start_line"], bar["end_line"]) == (20, 25)


def test_verify_ok_when_symbol_resolves():
    nav = _nav()
    v = graph_verify("Symbols: pkg/mod.go:foo()", nav)
    assert v.ok and v.n_resolved == 1 and ("pkg/mod.go", "foo") in v.seeds


def test_verify_not_ok_when_unanchored():
    nav = _nav()
    v = graph_verify("Symbols: ghost/missing.go:phantom()", nav)
    assert not v.ok and v.n_resolved == 0


def test_verify_ok_with_locations_line():
    nav = _nav()
    # No resolvable Symbols:, but a structured Locations: span anchors it.
    v = graph_verify("Locations: pkg/mod.go:20-25", nav)
    assert v.ok


def test_expansion_seeds_and_render():
    seeds = expansion_seeds_from_candidates(
        [{"file": "pkg/mod.go", "name": "pkg/mod.go:foo"}]
    )
    assert ("pkg/mod.go", "foo") in seeds
    nav = _nav()
    text = render_expansion(nav.neighbors(seeds))
    assert "pkg/mod.go:20-25" in text and "neighbour" in text
