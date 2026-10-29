# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reject #871's impossible hierarchy at shared graph artifact boundaries."""

import pickle

import pytest

from codenib.graph.code_graph import CodeGraph
from codenib.types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE, NODE_TYPE_FUNCTION


def _graph():
    graph = CodeGraph()
    graph.add_file_node("sample.py")
    graph.add_symbol_node("outer", 0, 0, 10, NODE_TYPE_FUNCTION)
    graph.add_symbol_node("inner", 2, 2, 4, NODE_TYPE_FUNCTION)
    graph._add_edge("outer", "inner", EDGE_TYPE_CONTAIN)
    return graph


@pytest.mark.parametrize("cycle", (("outer", "outer"), ("inner", "outer")))
def test_save_rejects_cyclic_containment_without_replacing_an_artifact(tmp_path, cycle):
    graph = _graph()
    graph._add_edge(*cycle, EDGE_TYPE_CONTAIN)
    destination = tmp_path / "graph.pkl"
    destination.write_bytes(b"previous artifact")

    with pytest.raises(ValueError, match="containment cycle"):
        graph.save_graph(destination)

    assert destination.read_bytes() == b"previous artifact"


@pytest.mark.parametrize("cycle", (("outer", "outer"), ("inner", "outer")))
def test_load_rejects_a_pre_integrity_graph_with_cyclic_containment(tmp_path, cycle):
    destination = tmp_path / "graph.pkl"
    _graph().save_graph(destination)
    # Model a previously accepted artifact, preserving its schema and layout.
    payload = pickle.loads(destination.read_bytes())
    payload["graph"].add_edge(*cycle, type=EDGE_TYPE_CONTAIN)
    destination.write_bytes(pickle.dumps(payload))

    with pytest.raises(ValueError, match="containment cycle"):
        CodeGraph.load_graph(destination)


def test_recursive_calls_remain_valid_across_graph_roundtrip(tmp_path):
    graph = _graph()
    graph._add_edge("outer", "outer", EDGE_TYPE_REFERENCE)
    graph._add_edge("inner", "outer", EDGE_TYPE_REFERENCE)
    destination = tmp_path / "graph.pkl"

    graph.save_graph(destination)
    restored = CodeGraph.load_graph(destination)

    assert restored.graph.ecount() == graph.graph.ecount()
    assert len(restored.graph.es.select(type_eq=EDGE_TYPE_REFERENCE)) == 2


def test_python_provider_collision_cannot_be_published(tmp_path):
    from pathlib import Path

    from codenib.scip_interface.scip_decode_python import SCIPPythonGraphDecoder

    fixture = (
        Path(__file__).parents[1] / "fixtures" / "scip_python_nested_collision.fixture"
    )
    graph = SCIPPythonGraphDecoder(fixture).decode()
    destination = tmp_path / "graph.pkl"

    with pytest.raises(ValueError, match="containment cycle"):
        graph.save_graph(destination)

    assert not destination.exists()
