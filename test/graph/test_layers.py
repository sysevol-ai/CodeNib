# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util

import pytest

from codenib.graph.code_graph import CodeGraph
from codenib.graph.layers import (
    GraphLayerSpec,
    _classify_edge_layers_python,
    build_graph_layers,
    classify_edge_layers,
)
from codenib.types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_IMPORT,
    EDGE_TYPE_REFERENCE,
    EDGE_TYPE_TYPE_USE,
    GRAPH_LAYER_ALL,
    GRAPH_LAYER_CONTAINMENT,
    GRAPH_LAYER_DEPENDENCY,
    GRAPH_LAYER_IMPORT,
    GRAPH_LAYER_REFERENCE,
    GRAPH_LAYER_TYPE_USE,
)


def _graph() -> CodeGraph:
    graph = CodeGraph()
    graph.add_file_node("a.py")
    graph.add_symbol_node("a.py:Caller", 1, 0, 20, "class")
    graph.add_symbol_node("a.py:Caller.run()", 4, 4, 10, "method")
    graph.add_symbol_node("a.py:Target", 30, 30, 40, "function")
    graph._add_edge("a.py", "a.py:Caller", EDGE_TYPE_CONTAIN)
    graph._add_edge("a.py:Caller", "a.py:Caller.run()", EDGE_TYPE_CONTAIN)
    graph._add_edge(
        "a.py:Caller.run()",
        "a.py:Target",
        EDGE_TYPE_REFERENCE,
        anchor_file="a.py",
        anchor_line=6,
    )
    graph._add_edge(
        "a.py:Caller.run()",
        "a.py:Target",
        EDGE_TYPE_REFERENCE,
        anchor_file="a.py",
        anchor_line=8,
    )
    graph._add_edge("a.py:Caller.run()", "pkg.module", EDGE_TYPE_IMPORT)
    graph._add_edge("a.py:Caller.run()", "pkg.Type", EDGE_TYPE_TYPE_USE)
    graph.build_range_indexes()
    return graph


def test_build_graph_layers_indexes_overlapping_default_layers():
    index = build_graph_layers(_graph(), use_core=False)

    assert index.edge_ids(GRAPH_LAYER_ALL) == [0, 1, 2, 3, 4, 5]
    assert index.edge_ids(GRAPH_LAYER_CONTAINMENT) == [0, 1]
    assert index.edge_ids(GRAPH_LAYER_REFERENCE) == [2, 3]
    assert index.edge_ids(GRAPH_LAYER_IMPORT) == [4]
    assert index.edge_ids(GRAPH_LAYER_TYPE_USE) == [5]
    assert index.edge_ids(GRAPH_LAYER_DEPENDENCY) == [2, 3, 4, 5]


def test_layer_view_exposes_neighbors_and_subgraph():
    graph = _graph()
    view = graph.layer(GRAPH_LAYER_REFERENCE, use_core=False)

    refs = view.neighbor_edges("a.py:Caller.run()", direction="forward")
    assert [ref.anchor_line for ref in refs] == [6, 8]
    assert {ref.target_name for ref in refs} == {"a.py:Target"}

    sub = view.to_igraph()
    assert sub.ecount() == 2
    assert sub.vcount() == 2


def test_layer_summary_counts_endpoint_nodes():
    summary = _graph().layers(use_core=False).summary()

    assert summary[GRAPH_LAYER_CONTAINMENT].edge_count == 2
    assert summary[GRAPH_LAYER_CONTAINMENT].node_count == 3
    assert summary[GRAPH_LAYER_DEPENDENCY].edge_count == 4


def test_query_range_accepts_layer_name():
    graph = _graph()

    dependency = graph.query_range("a.py", 6, 6, layer=GRAPH_LAYER_DEPENDENCY)
    assert [edge.anchor_line for edge in dependency.outgoing] == [6]

    containment = graph.query_range("a.py", 4, 10, layer=GRAPH_LAYER_CONTAINMENT)
    assert [edge.edge_kind for edge in containment.incoming] == [
        EDGE_TYPE_CONTAIN,
        EDGE_TYPE_CONTAIN,
    ]


def test_query_range_rejects_layer_and_kinds_together():
    with pytest.raises(ValueError, match="either kinds or layer"):
        _graph().query_range(
            "a.py",
            0,
            1,
            kinds={EDGE_TYPE_REFERENCE},
            layer=GRAPH_LAYER_REFERENCE,
        )


def test_query_range_rejects_unknown_layer():
    with pytest.raises(ValueError, match="Unknown graph layer"):
        _graph().query_range("a.py", 0, 1, layer="not-a-layer")


def test_python_classifier_matches_expected_default_layers():
    result = classify_edge_layers(
        [
            EDGE_TYPE_CONTAIN,
            EDGE_TYPE_REFERENCE,
            EDGE_TYPE_IMPORT,
            EDGE_TYPE_TYPE_USE,
            "custom",
        ],
        use_core=False,
    )

    assert result[GRAPH_LAYER_ALL] == [0, 1, 2, 3, 4]
    assert result[GRAPH_LAYER_CONTAINMENT] == [0]
    assert result[GRAPH_LAYER_REFERENCE] == [1]
    assert result[GRAPH_LAYER_IMPORT] == [2]
    assert result[GRAPH_LAYER_TYPE_USE] == [3]
    assert result[GRAPH_LAYER_DEPENDENCY] == [1, 2, 3]


def test_raw_python_classifier_accepts_custom_specs():
    result = _classify_edge_layers_python(
        [EDGE_TYPE_REFERENCE, EDGE_TYPE_IMPORT],
        {"calls": GraphLayerSpec("calls", frozenset({EDGE_TYPE_REFERENCE}))},
    )

    assert result == {"calls": [0]}


def test_cpp_classifier_matches_python_when_extension_is_built():
    if importlib.util.find_spec("codenib_core") is None:
        pytest.skip("codenib_core extension is not built")

    edge_types = [
        EDGE_TYPE_CONTAIN,
        EDGE_TYPE_REFERENCE,
        EDGE_TYPE_IMPORT,
        EDGE_TYPE_TYPE_USE,
        "custom",
    ]
    assert classify_edge_layers(edge_types, use_core=True) == classify_edge_layers(
        edge_types, use_core=False
    )
