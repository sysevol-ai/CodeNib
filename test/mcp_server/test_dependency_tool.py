# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the dependency_subgraph MCP tool impl (#133, #19)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import igraph as ig
import pytest
from mcp import Client

from codenib.compiler.manifest import RepoManifest
from codenib.graph.code_graph import CodeGraph
from codenib.mcp import server
from codenib.mcp.context import ServerContext
from codenib.mcp.tools.dependency import dependency_subgraph_impl
from codenib.source_fingerprint import capture_repository_source
from codenib.types import EDGE_TYPE_REFERENCE, NODE_TYPE_FIELD, NODE_TYPE_FUNCTION


def _graph() -> CodeGraph:
    g = CodeGraph()
    gg = ig.Graph(directed=True)
    gg.add_vertices(3)
    gg.vs["name"] = ["hashA", "hashB", "hashC"]
    gg.vs["unified_name"] = ["a.c:caller()", "a.c:mid()", "b.c:leaf()"]
    gg.vs["type"] = ["function"] * 3
    gg.vs["file"] = ["a.c", "a.c", "b.c"]
    gg.vs["start_line"] = [0, 10, 5]
    gg.vs["end_line"] = [9, 20, 8]
    gg.add_edges([(0, 1), (1, 2)])  # caller -> mid -> leaf
    gg.es["type"] = ["reference", "reference"]
    g.graph = gg
    g.name_to_vertex = {"hashA": 0, "hashB": 1, "hashC": 2}
    g._unified_to_names = {}
    return g


def _dense_graph(size: int = 12) -> CodeGraph:
    g = CodeGraph()
    graph = ig.Graph(directed=True)
    graph.add_vertices(size)
    names = [f"hash{index}" for index in range(size)]
    graph.vs["name"] = names
    graph.vs["unified_name"] = [f"dense.py:node_{index}()" for index in range(size)]
    graph.vs["type"] = ["function"] * size
    graph.vs["file"] = ["dense.py"] * size
    graph.vs["start_line"] = list(range(size))
    graph.vs["end_line"] = list(range(size))
    graph.add_edges(
        (source, target)
        for source in range(size)
        for target in range(size)
        if source != target
    )
    graph.es["type"] = ["reference"] * graph.ecount()
    g.graph = graph
    g.name_to_vertex = {name: index for index, name in enumerate(names)}
    g._unified_to_names = {}
    return g


def test_impact_direction_returns_transitive_callers():
    ctx = SimpleNamespace(symbol_graph=_graph())
    out = dependency_subgraph_impl(ctx, "leaf", direction="impact", depth=3)
    assert out["root"] == "b.c:leaf()"
    assert {n["name"] for n in out["nodes"]} == {"a.c:mid()", "a.c:caller()"}
    assert {n["name"]: n["line"] for n in out["nodes"]} == {
        "a.c:caller()": 1,
        "a.c:mid()": 11,
    }
    assert all(set(e) == {"source", "target", "type"} for e in out["edges"])


def test_both_direction_neighborhood():
    ctx = SimpleNamespace(symbol_graph=_graph())
    out = dependency_subgraph_impl(ctx, "mid", direction="both", depth=1)
    assert {n["name"] for n in out["nodes"]} == {"a.c:caller()", "b.c:leaf()"}


def test_both_direction_honors_node_limit_without_dangling_edges():
    ctx = SimpleNamespace(symbol_graph=_graph())

    out = dependency_subgraph_impl(
        ctx,
        "mid",
        direction="both",
        depth=3,
        max_nodes=2,
    )

    names = {out["root"], *(node["name"] for node in out["nodes"])}
    assert len(out["nodes"]) == 1
    assert out["truncated"] is True
    assert all(
        edge["source"] in names and edge["target"] in names for edge in out["edges"]
    )


def test_dependency_tool_caps_dense_edge_output():
    out = dependency_subgraph_impl(
        SimpleNamespace(symbol_graph=_dense_graph()),
        "node_0",
        direction="both",
        depth=2,
        max_nodes=10,
        max_edges=40,
    )

    assert len(out["nodes"]) <= 9
    assert len(out["edges"]) == 40
    assert out["truncated"] is True


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"symbol": ""}, "symbol must not be empty"),
        ({"symbol": "mid", "direction": "sideways"}, "direction must be"),
        ({"symbol": "mid", "depth": 9}, "depth must be between"),
        ({"symbol": "mid", "max_nodes": 101}, "max_nodes must be between"),
        ({"symbol": "mid", "max_edges": True}, "max_edges must be between"),
        ({"symbol": "mid", "max_edges": 0}, "max_edges must be between"),
        ({"symbol": "mid", "max_edges": 2001}, "max_edges must be between"),
    ],
)
def test_dependency_tool_validates_bounded_request(kwargs, message):
    with pytest.raises(ValueError, match=message):
        dependency_subgraph_impl(SimpleNamespace(symbol_graph=_graph()), **kwargs)


def test_both_direction_respects_edge_budget():
    ctx = SimpleNamespace(symbol_graph=_graph())
    out = dependency_subgraph_impl(ctx, "mid", depth=2, max_edges=1)

    assert [node["name"] for node in out["nodes"]] == ["b.c:leaf()"]
    assert len(out["edges"]) == 1
    assert out["truncated"] is True


def test_missing_graph_returns_error():
    out = dependency_subgraph_impl(SimpleNamespace(symbol_graph=None), "x")
    assert "error" in out


@pytest.mark.parametrize(
    ("alias", "canonical"),
    (
        ("callers", "impact"),
        ("dependency", "dependencies"),
        ("callees", "dependencies"),
    ),
)
def test_legacy_implementation_alias_preserves_reference_result(alias, canonical):
    ctx = SimpleNamespace(symbol_graph=_graph())
    assert dependency_subgraph_impl(ctx, "mid", direction=alias) == (
        dependency_subgraph_impl(ctx, "mid", direction=canonical)
    )


@pytest.mark.parametrize("mode", ("auto", "legacy"))
@pytest.mark.parametrize(
    ("direction", "reference_direction"),
    (
        ("impact", "incoming"),
        ("dependencies", "outgoing"),
        ("both", "both"),
    ),
)
def test_client_receives_mixed_references_and_their_semantics(
    tmp_path, monkeypatch, mode, direction, reference_direction
):
    """Fields, annotations and passed function values must not become call claims."""
    repo = tmp_path / "repo"
    repo.mkdir()
    source = (
        "Alias = int\n"
        "field = 1\n\n"
        "def invoked():\n    return 1\n\n"
        "def callback():\n    return 2\n\n"
        "def selected(value: Alias):\n"
        "    return invoked(), callback, field, value\n"
    )
    (repo / "sample.py").write_text(source)
    graph = CodeGraph()
    graph.graph.add_vertices(5)
    names = ["selected", "invoked", "callback", "field", "Alias"]
    labels = [f"sample.py:{name}()" for name in names[:3]]
    labels += [f"sample.py:{name}" for name in names[3:]]
    graph.graph.vs["name"] = names
    graph.graph.vs["unified_name"] = labels
    graph.graph.vs["type"] = [NODE_TYPE_FUNCTION] * 3 + [NODE_TYPE_FIELD] * 2
    graph.graph.vs["file"] = ["sample.py"] * 5
    graph.graph.vs["start_line"] = [9, 3, 6, 1, 0]
    graph.graph.vs["end_line"] = [10, 4, 7, 1, 0]
    graph.graph.add_edges([(0, target) for target in range(1, 5)])
    graph.graph.es["type"] = [EDGE_TYPE_REFERENCE] * 4
    graph.name_to_vertex = {name: index for index, name in enumerate(names)}
    graph._unified_to_names = {}
    ctx = ServerContext(
        manifest=RepoManifest(repo_path=str(repo), languages=["python"]),
        symbol_graph=graph,
    )
    ctx._install_repository_source(capture_repository_source(repo))
    monkeypatch.setattr(server, "_ctx", ctx)
    monkeypatch.setattr(server.mcp, "_tool_surface", "full")

    def payload(result):
        assert result.is_error is False
        return json.loads(result.content[0].text)

    async def query():
        async with Client(server.mcp, mode=mode, cache=None) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            description = tools["dependency_subgraph"].description.lower()
            assert "static" in description and "function values" in description
            assert "dynamic" in description and "source call sites" in description
            incoming = reference_direction == "incoming"
            result = payload(
                await client.call_tool(
                    "dependency_subgraph",
                    {
                        "symbol": "Alias" if incoming else "selected",
                        "direction": direction,
                        "depth": 1,
                    },
                )
            )
            semantics = result["semantics"]
            assert semantics["edge_type"] == EDGE_TYPE_REFERENCE
            assert semantics["reference_direction"] == reference_direction
            assert "type use" in semantics["interpretation"]
            assert "function values" in semantics["interpretation"]
            assert "dynamic" in semantics["coverage"]
            assert "complete call coverage" in semantics["coverage"]
            assert result["direction"] == (
                "callers" if incoming else "both" if direction == "both" else "callees"
            )
            expected_targets = {labels[4]} if incoming else set(labels[1:])
            assert {(e["source"], e["target"], e["type"]) for e in result["edges"]} == {
                (labels[0], target, EDGE_TYPE_REFERENCE) for target in expected_targets
            }
            if not incoming:
                assert {node["kind"] for node in result["nodes"]} == {
                    NODE_TYPE_FIELD,
                    NODE_TYPE_FUNCTION,
                }
                assert {node["name"]: node["line"] for node in result["nodes"]} == {
                    labels[1]: 4,
                    labels[2]: 7,
                    labels[3]: 2,
                    labels[4]: 1,
                }
            window = payload(
                await client.call_tool(
                    "read_source",
                    {"file_path": "sample.py", "start_line": 10, "end_line": 11},
                )
            )
            assert window["content"] == "".join(source.splitlines(keepends=True)[9:11])

    try:
        asyncio.run(query())
    finally:
        ctx.close()
