"""Unit tests for CodeGraph.query_range and the per-file range indexes.

Covers the surface introduced for the LSP-aligned graph_expand redesign:

- `defined` query uses overlap semantics (matches IDE outline behavior),
  including nested scopes
- `outgoing` returns edges with anchor inside the query range
- `incoming` returns edges into nodes whose def is inside the range,
  regardless of where the anchors come from
- multi-edges between the same `(src, tgt)` pair are preserved
- `build_range_indexes` is idempotent
- `save_graph` / `load_graph` round-trips the indexes
- old pickles raise on load (forces re-decode rather than silent drift)
"""

from __future__ import annotations

import pickle
import tempfile
from pathlib import Path

import pytest

from codeminer.graph.code_graph import _SCHEMA_VERSION, CodeGraph, RangeQueryResult
from codeminer.types import EDGE_TYPE_REFERENCE

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _build_simple_graph() -> CodeGraph:
    """Two functions in foo.py, with cross-references in both directions.

    foo.py:bar  defined lines 5–20, references baz at line 12
    foo.py:baz  defined lines 25–40, references bar at line 28
    """
    g = CodeGraph()
    g.add_file_node("foo.py")
    g.add_symbol_node("foo.py:bar", 10, 5, 20, "function")
    g.add_symbol_node("foo.py:baz", 30, 25, 40, "function")

    g.update_current_scope("foo.py:bar", 5, 20)
    g.add_symbol_reference("foo.py:baz", anchor_line=12)

    g.update_current_scope("foo.py:baz", 25, 40)
    g.add_symbol_reference("foo.py:bar", anchor_line=28)

    g.build_range_indexes()
    return g


def _build_nested_graph() -> CodeGraph:
    """Class with two nested methods, all overlapping the outer class scope.

    foo.py:Outer        class lines 1–100
    foo.py:Outer.meth1  method lines 10–30
    foo.py:Outer.meth2  method lines 50–80
    """
    g = CodeGraph()
    g.add_file_node("foo.py")
    g.add_symbol_node("foo.py:Outer", 1, 1, 100, "class")
    g.add_symbol_node("foo.py:Outer.meth1", 10, 10, 30, "method")
    g.add_symbol_node("foo.py:Outer.meth2", 50, 50, 80, "method")
    g.build_range_indexes()
    return g


def _build_multi_edge_graph() -> CodeGraph:
    """Same `bar` symbol called twice from different lines inside `caller`."""
    g = CodeGraph()
    g.add_file_node("foo.py")
    g.add_symbol_node("foo.py:bar", 5, 5, 6, "function")
    g.add_symbol_node("foo.py:caller", 10, 10, 100, "function")

    g.update_current_scope("foo.py:caller", 10, 100)
    g.add_symbol_reference("foo.py:bar", anchor_line=15)
    g.add_symbol_reference("foo.py:bar", anchor_line=42)
    g.add_symbol_reference("foo.py:bar", anchor_line=88)

    g.build_range_indexes()
    return g


# ---------------------------------------------------------------------------
# `defined` query
# ---------------------------------------------------------------------------


def test_defined_overlap_partial_left():
    g = _build_simple_graph()
    bar_vid = g.name_to_vertex["foo.py:bar"]

    # Range starts before bar's def (5) but ends inside it.
    r = g.query_range("foo.py", 1, 10)
    assert [n.vid for n in r.defined] == [bar_vid]


def test_defined_overlap_partial_right():
    g = _build_simple_graph()
    bar_vid = g.name_to_vertex["foo.py:bar"]

    # Range starts inside bar's def and ends after it.
    r = g.query_range("foo.py", 18, 22)
    assert [n.vid for n in r.defined] == [bar_vid]


def test_defined_overlap_fully_inside():
    g = _build_simple_graph()
    bar_vid = g.name_to_vertex["foo.py:bar"]

    # Range fully inside bar's def — should still match.
    r = g.query_range("foo.py", 8, 15)
    assert [n.vid for n in r.defined] == [bar_vid]


def test_defined_no_overlap():
    g = _build_simple_graph()
    r = g.query_range("foo.py", 100, 200)
    assert r.defined == []


def test_defined_includes_nested_scopes():
    """Outer class, inner method, and outer scope all match a range that
    covers them — IDE outline behavior."""
    g = _build_nested_graph()

    # Range 12–25 overlaps Outer (1–100) AND Outer.meth1 (10–30).
    r = g.query_range("foo.py", 12, 25)
    names = {n.name for n in r.defined}
    assert names == {"foo.py:Outer", "foo.py:Outer.meth1"}


def test_defined_unknown_file():
    g = _build_simple_graph()
    r = g.query_range("does_not_exist.py", 0, 9999)
    assert r.defined == []
    assert r.outgoing == []
    assert r.incoming == []


# ---------------------------------------------------------------------------
# `outgoing` query
# ---------------------------------------------------------------------------


def test_outgoing_anchor_inside_range():
    g = _build_simple_graph()

    # Reference bar -> baz is anchored at line 12.
    r = g.query_range("foo.py", 10, 15)
    assert len(r.outgoing) == 1
    edge_ref = r.outgoing[0]
    assert edge_ref.edge_kind == EDGE_TYPE_REFERENCE
    assert edge_ref.anchor_line == 12
    assert edge_ref.anchor_file == "foo.py"


def test_outgoing_excludes_anchors_outside_range():
    g = _build_simple_graph()

    # Range 0–10 misses both anchors (12 and 28).
    r = g.query_range("foo.py", 0, 10)
    assert r.outgoing == []


def test_outgoing_boundary_inclusive():
    g = _build_simple_graph()

    # Boundary case: start_line == anchor.
    r = g.query_range("foo.py", 12, 13)
    assert len(r.outgoing) == 1

    # Boundary case: end_line == anchor.
    r = g.query_range("foo.py", 11, 12)
    assert len(r.outgoing) == 1


# ---------------------------------------------------------------------------
# `incoming` query
# ---------------------------------------------------------------------------


def test_incoming_returns_edges_into_defined_nodes():
    g = _build_simple_graph()

    # Range covers bar's def — incoming should include the baz->bar edge.
    r = g.query_range("foo.py", 5, 20)
    assert len(r.incoming) == 1
    edge_ref = r.incoming[0]
    bar_vid = g.name_to_vertex["foo.py:bar"]
    assert edge_ref.target_vid == bar_vid


def test_incoming_no_definitions_in_range():
    g = _build_simple_graph()

    # Range with no symbol defs — no incoming.
    r = g.query_range("foo.py", 100, 200)
    assert r.incoming == []


def test_recursion_self_edge_not_in_both_outgoing_and_incoming():
    """Invariant (iii): outgoing ∩ incoming = ∅ even for recursive calls.

    A recursive function `f` defined at lines 5–20 with a self-reference at
    line 12 produces a single REFERENCE edge whose source == target == f.
    Without filtering, that edge appears in `outgoing` (anchor in range)
    AND `incoming` (target def in range). The implementation drops self-
    edges from `incoming` so the recursion shows up exactly once.
    """
    g = CodeGraph()
    g.add_file_node("foo.py")
    g.add_symbol_node("foo.py:f", 10, 5, 20, "function")
    g.update_current_scope("foo.py:f", 5, 20)
    g.add_symbol_reference("foo.py:f", anchor_line=12)
    g.build_range_indexes()

    r = g.query_range("foo.py", 5, 20)
    assert len(r.outgoing) == 1
    assert r.outgoing[0].anchor_line == 12
    assert r.incoming == []


# ---------------------------------------------------------------------------
# Multi-edge handling (no `_add_edge` dedup)
# ---------------------------------------------------------------------------


def test_multi_edge_same_pair_preserved():
    g = _build_multi_edge_graph()

    # caller calls bar three times → three distinct edges
    caller_vid = g.name_to_vertex["foo.py:caller"]
    bar_vid = g.name_to_vertex["foo.py:bar"]
    edges = [
        g.graph.es[eid]
        for eid in g.graph.incident(caller_vid, mode="out")
        if g.graph.es[eid].target == bar_vid
    ]
    assert len(edges) == 3
    anchor_lines = sorted(e["anchor_line"] for e in edges)
    assert anchor_lines == [15, 42, 88]


def test_multi_edge_outgoing_per_call_site():
    g = _build_multi_edge_graph()

    # Range covers only the line-42 anchor.
    r = g.query_range("foo.py", 40, 50)
    assert len(r.outgoing) == 1
    assert r.outgoing[0].anchor_line == 42

    # Range covering all three.
    r = g.query_range("foo.py", 10, 100)
    assert len(r.outgoing) == 3


# ---------------------------------------------------------------------------
# build_range_indexes idempotency + persistence roundtrip
# ---------------------------------------------------------------------------


def test_build_range_indexes_is_idempotent():
    g = _build_simple_graph()
    snapshot_nodes = dict(g._file_nodes)
    snapshot_edges = dict(g._file_edge_anchors)

    g.build_range_indexes()
    g.build_range_indexes()

    assert g._file_nodes == snapshot_nodes
    assert g._file_edge_anchors == snapshot_edges


def test_save_load_graph_roundtrip():
    g = _build_simple_graph()

    with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
        path = f.name

    try:
        g.save_graph(path)
        g2 = CodeGraph.load_graph(path)

        # Indexes survive pickle.
        assert g2._file_nodes == g._file_nodes
        assert g2._file_edge_anchors == g._file_edge_anchors

        # Same query produces same result.
        r1 = g.query_range("foo.py", 8, 15)
        r2 = g2.query_range("foo.py", 8, 15)
        assert r1.defined == r2.defined
        assert r1.outgoing == r2.outgoing
        assert r1.incoming == r2.incoming
    finally:
        Path(path).unlink(missing_ok=True)


def test_load_legacy_pickle_raises():
    """Old pickles (schema_version != current) must raise on load to force a
    re-decode rather than silently returning a graph with empty / stale
    range indexes."""
    g = CodeGraph()
    g.add_file_node("foo.py")
    g.add_symbol_node("foo.py:bar", 10, 5, 20, "function")
    # Intentionally do not call build_range_indexes; mimic an old pickle by
    # hand-crafting the data dict without the new fields.

    legacy_data = {
        # No "schema_version" key — load_graph treats as None and raises
        "project_root": None,
        "graph": g.graph,
        "symbol_ranges": g.symbol_ranges,
        "name_to_vertex": g.name_to_vertex,
    }
    with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
        path = f.name
    try:
        with open(path, "wb") as fh:
            pickle.dump(legacy_data, fh)

        with pytest.raises(ValueError, match="schema_version"):
            CodeGraph.load_graph(path)
    finally:
        Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


def test_query_range_returns_range_query_result():
    g = _build_simple_graph()
    r = g.query_range("foo.py", 0, 0)
    assert isinstance(r, RangeQueryResult)
    assert isinstance(r.defined, list)
    assert isinstance(r.outgoing, list)
    assert isinstance(r.incoming, list)


def test_schema_version_constant_exposed():
    assert _SCHEMA_VERSION >= 3
