# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for graph-backed agent-runner answer verification."""

import igraph

from codeminer.agent.agent_types import AgentResult
from codeminer.eval.agent_runner.verify_expand import (
    GraphNav,
    expansion_seeds_from_candidates,
    graph_verify,
    render_expansion,
    run_verify_expand,
)


def _toy_graph() -> igraph.Graph:
    # Three symbols in one file: foo calls bar; baz is separate.
    graph = igraph.Graph(directed=True)
    graph.add_vertices(3)
    graph.vs["name"] = ["pkg/mod.go:foo", "pkg/mod.go:bar", "pkg/mod.go:baz"]
    graph.vs["unified_name"] = graph.vs["name"]
    graph.vs["type"] = ["function", "function", "function"]
    graph.vs["file"] = ["pkg/mod.go", "pkg/mod.go", "pkg/mod.go"]
    graph.vs["start_line"] = [9, 19, 29]
    graph.vs["end_line"] = [14, 24, 34]
    graph.add_edges([(0, 1)])
    return graph


def _nav() -> GraphNav:
    return GraphNav(_toy_graph())


def test_graphnav_resolves_real_symbol():
    nav = _nav()
    assert nav.resolves("pkg/mod.go", "foo")
    assert not nav.resolves("pkg/mod.go", "doesNotExist")


def test_graphnav_neighbors_one_hop():
    nav = _nav()
    neighbors = nav.neighbors([("pkg/mod.go", "foo")], max_nodes=10)
    names = {candidate["name"] for candidate in neighbors}
    assert "pkg/mod.go:bar" in names
    bar = next(
        candidate for candidate in neighbors if candidate["name"] == "pkg/mod.go:bar"
    )
    assert (bar["start_line"], bar["end_line"]) == (20, 25)


def test_verify_ok_when_symbol_resolves():
    nav = _nav()
    verdict = graph_verify("Symbols: pkg/mod.go:foo()", nav)
    assert verdict.ok
    assert verdict.n_resolved == 1
    assert ("pkg/mod.go", "foo") in verdict.seeds


def test_verify_not_ok_when_unanchored():
    nav = _nav()
    verdict = graph_verify("Symbols: ghost/missing.go:phantom()", nav)
    assert not verdict.ok
    assert verdict.n_resolved == 0


def test_verify_ok_with_locations_line():
    nav = _nav()
    verdict = graph_verify("Locations: pkg/mod.go:20-25", nav)
    assert verdict.ok


def test_expansion_seeds_and_render():
    seeds = expansion_seeds_from_candidates(
        [{"file": "pkg/mod.go", "name": "pkg/mod.go:foo"}]
    )
    assert ("pkg/mod.go", "foo") in seeds
    nav = _nav()
    text = render_expansion(nav.neighbors(seeds))
    assert "pkg/mod.go:20-25" in text
    assert "neighbour" in text


def test_run_verify_expand_skips_when_answer_resolves():
    calls = []
    result = AgentResult(answer="Symbols: pkg/mod.go:foo()")

    out = run_verify_expand(
        result,
        query="Where is foo?",
        nav=_nav(),
        preload_candidates=[],
        rerun=lambda q: calls.append(q),
    )

    assert out.result is result
    assert out.triggered is False
    assert out.resolved == 1
    assert calls == []


def test_run_verify_expand_reruns_with_candidate_seed_expansion():
    calls = []
    retry_result = AgentResult(answer="Locations: pkg/mod.go:20-25")

    def rerun(prompt: str):
        calls.append(prompt)
        return retry_result

    out = run_verify_expand(
        AgentResult(answer="Symbols: ghost/missing.go:phantom()"),
        query="Where should this change go?",
        nav=_nav(),
        preload_candidates=[{"file": "pkg/mod.go", "name": "pkg/mod.go:foo"}],
        rerun=rerun,
    )

    assert out.result is retry_result
    assert out.triggered is True
    assert out.resolved == 0
    assert len(calls) == 1
    assert calls[0].startswith("Where should this change go?")
    assert "pkg/mod.go:20-25" in calls[0]


def test_run_verify_expand_skips_when_disabled():
    calls = []
    result = AgentResult(answer="Symbols: ghost/missing.go:phantom()")

    out = run_verify_expand(
        result,
        query="Where should this change go?",
        nav=_nav(),
        preload_candidates=[{"file": "pkg/mod.go", "name": "pkg/mod.go:foo"}],
        rerun=lambda q: calls.append(q),
        enabled=False,
    )

    assert out.result is result
    assert out.triggered is False
    assert out.resolved is None
    assert calls == []
