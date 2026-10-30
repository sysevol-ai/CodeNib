# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Keep source chunk aliases attached to the correct physical definition."""

from types import SimpleNamespace

import pytest

from codenib.code_chunker import CodeChunk
from codenib.graph.code_graph import CodeGraph
from codenib.index import BM25CodeIndexer
from codenib.mcp.tools.search import search_context_impl
from codenib.ops.expand import (
    ExpandContext,
    expand_graph_neighbors,
    expand_graph_region,
    nodeinfo_to_queried,
)
from codenib.types import EDGE_TYPE_REFERENCE, NODE_TYPE_FUNCTION, QueriedNode

ALIAS = "a.py:item()"


@pytest.fixture
def source_collision():
    graph = CodeGraph()
    chunks = []
    for start, target in ((0, "left"), (4, "right")):
        physical = f"a.py:item(_codenib_l{start}_c4)"
        graph._add_vertex(
            physical,
            {
                "type": NODE_TYPE_FUNCTION,
                "file": "a.py",
                "start_line": start,
                "end_line": start + 2,
                "unified_name": ALIAS,
                "has_definition": True,
            },
        )
        callee = f"a.py:{target}()"
        graph._add_vertex(
            callee,
            {
                "type": NODE_TYPE_FUNCTION,
                "file": "a.py",
                "start_line": start + 8,
                "end_line": start + 9,
                "unified_name": callee,
                "has_definition": True,
            },
        )
        graph._add_edge(physical, callee, EDGE_TYPE_REFERENCE)
        chunks.append(
            CodeChunk(
                content=f"def item():\n    return {target}()\n",
                node_id=ALIAS,
                name="item",
                chunk_type=NODE_TYPE_FUNCTION,
                file="a.py",
                start_line=start,
                end_line=start + 2,
            )
        )
    # Legacy reference-only entries can carry synthetic ranges; they are not
    # evidence that a source chunk belongs to another physical definition.
    graph._add_vertex(
        "external-item",
        {
            "type": NODE_TYPE_FUNCTION,
            "file": "a.py",
            "start_line": 0,
            "end_line": 6,
            "unified_name": ALIAS,
            "has_definition": False,
        },
    )
    graph.build_range_indexes()
    index = BM25CodeIndexer(chunks=chunks, max_k=2)
    seeds = nodeinfo_to_queried(index.search("item", top_k=2))
    return graph, {seed.start_line: seed for seed in seeds}


@pytest.mark.parametrize("method", ["neighbors", "region"])
@pytest.mark.parametrize("start,target", [(0, "left"), (4, "right")])
def test_bm25_source_span_selects_the_correct_physical_definition(
    source_collision, method, start, target
):
    graph, seeds = source_collision
    context = ExpandContext(code_graph=graph)
    seed = seeds[start]
    if method == "neighbors":
        results = expand_graph_neighbors(context, [seed], direction="successors")
    else:
        results = expand_graph_region(context, [seed], direction="successors", hops=1)
    callees = {
        node.node_name
        for node in results
        if node.node_name in {"a.py:left()", "a.py:right()"}
    }
    assert callees == {f"a.py:{target}()"}


def test_structural_search_expands_both_source_definitions(source_collision):
    graph, seeds = source_collision
    index = BM25CodeIndexer(
        chunks=[
            CodeChunk(
                content=seed.content,
                node_id=seed.node_id,
                name="item",
                chunk_type=NODE_TYPE_FUNCTION,
                file=seed.file,
                start_line=seed.start_line,
                end_line=seed.end_line,
            )
            for seed in seeds.values()
        ],
        max_k=2,
    )
    ctx = SimpleNamespace(
        bm25=index,
        vector=None,
        grep_jev=None,
        symbol_graph=graph,
        artifact={},
        manifest=SimpleNamespace(repo_path=None, commit=None, source_fingerprint=None),
    )
    result = search_context_impl(ctx, "references of item", top_k=10)

    assert result["plan"]["name"] == "structural_graph"
    names = {node["node_name"] for node in result["results"]}
    assert {"a.py:left()", "a.py:right()"} <= names


@pytest.mark.parametrize(
    "file,start,end",
    [
        ("a.py", None, None),
        ("a.py", 0, 6),
        ("other.py", 0, 2),
        ("a.py", 4, 1),
    ],
)
def test_non_unique_or_invalid_source_match_does_not_guess(
    source_collision, file, start, end
):
    graph, _ = source_collision
    seed = QueriedNode(
        node_id=ALIAS,
        node_name=ALIAS,
        type=NODE_TYPE_FUNCTION,
        file=file,
        start_line=start,
        end_line=end,
    )
    assert expand_graph_neighbors(ExpandContext(code_graph=graph), [seed]) == []
