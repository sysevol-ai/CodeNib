# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from codenib.graph.code_graph import CodeGraph
from codenib.types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE


def test_typed_adjacency_filters_edges_and_deduplicates_multiedges():
    graph = CodeGraph()
    for name in ("seed", "reference", "contained"):
        graph._add_vertex(name, {"type": "function"})
    graph._add_edge(
        "seed",
        "reference",
        EDGE_TYPE_REFERENCE,
        anchor_file="src/a.py",
        anchor_line=1,
    )
    graph._add_edge(
        "seed",
        "reference",
        EDGE_TYPE_REFERENCE,
        anchor_file="src/a.py",
        anchor_line=2,
    )
    graph._add_edge("seed", "contained", EDGE_TYPE_CONTAIN)

    reference_id = graph.name_to_vertex["reference"]
    contained_id = graph.name_to_vertex["contained"]

    assert graph.get_successors("seed", {EDGE_TYPE_REFERENCE}) == [reference_id]
    assert graph.get_successors("seed", {EDGE_TYPE_CONTAIN}) == [contained_id]
    assert graph.get_predecessors("reference", {EDGE_TYPE_REFERENCE}) == [
        graph.name_to_vertex["seed"]
    ]
