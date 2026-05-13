# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the typed NodeRef / EdgeRef record shape returned by query_range."""

from codeminer.graph.code_graph import CodeGraph, EdgeRef, NodeRef, RangeQueryResult
from codeminer.types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE


def test_noderef_has_required_fields():
    ref = NodeRef(
        vid=1,
        name="scip-python python pkg 1.0 mod/foo.py:Foo#bar()",
        unified_name="mod/foo.py:Foo.bar",
        file="mod/foo.py",
        start_line=10,
        end_line=20,
        kind="method",
    )
    assert ref.vid == 1
    assert ref.unified_name == "mod/foo.py:Foo.bar"
    assert ref.kind == "method"


def test_edgeref_has_required_fields():
    ref = EdgeRef(
        eid=42,
        source_vid=1,
        target_vid=2,
        edge_kind="reference",
        anchor_file="mod/foo.py",
        anchor_line=15,
    )
    assert ref.eid == 42
    assert ref.edge_kind == "reference"
    assert ref.anchor_line == 15


def test_unified_to_names_built_after_index():
    g = CodeGraph(project_root="/tmp/x")
    g.add_file_node("a.py")
    g._add_vertex(
        "name1",
        {
            "type": "function",
            "file": "a.py",
            "start_line": 1,
            "end_line": 5,
            "unified_name": "a.py:foo",
        },
    )
    g._add_vertex(
        "name2",
        {
            "type": "function",
            "file": "b.py",
            "start_line": 1,
            "end_line": 5,
            "unified_name": "b.py:foo",
        },
    )
    g.build_range_indexes()
    # Distinct identities, distinct unified_names — each maps to its identity
    assert g._unified_to_names.get("a.py:foo") == ["name1"]
    assert g._unified_to_names.get("b.py:foo") == ["name2"]


def test_unified_to_names_collision():
    """When two identities share the same unified_name, both surface as candidates."""
    g = CodeGraph(project_root="/tmp/x")
    g.add_file_node("a.py")
    # Two distinct identity names but same unified_name "foo" (cross-file collision).
    g._add_vertex(
        "ident_a",
        {
            "type": "function",
            "file": "a.py",
            "start_line": 1,
            "end_line": 5,
            "unified_name": "foo",
        },
    )
    g._add_vertex(
        "ident_b",
        {
            "type": "function",
            "file": "b.py",
            "start_line": 1,
            "end_line": 5,
            "unified_name": "foo",
        },
    )
    g.build_range_indexes()
    candidates = g._unified_to_names.get("foo", [])
    assert sorted(candidates) == ["ident_a", "ident_b"]


def test_unified_to_names_round_trip(tmp_path):
    """Pickle round-trip preserves _unified_to_names (schema v3 amended)."""
    g = CodeGraph(project_root=str(tmp_path))
    g.add_file_node("a.py")
    g._add_vertex(
        "identity",
        {
            "type": "function",
            "file": "a.py",
            "start_line": 1,
            "end_line": 5,
            "unified_name": "a.py:foo",
        },
    )
    g.build_range_indexes()

    p = tmp_path / "g.pkl"
    g.save_graph(str(p))
    loaded = CodeGraph.load_graph(str(p))
    assert loaded._unified_to_names.get("a.py:foo") == ["identity"]


def test_unified_to_names_legacy_v3_pickle_tolerated(tmp_path):
    """A v3 pickle saved BEFORE this aux index existed must still load —
    the absent key is tolerated by load_graph's `data.get(...)` default."""
    import pickle

    import igraph as ig

    from codeminer.graph.code_graph import _SCHEMA_VERSION

    legacy = {
        "schema_version": _SCHEMA_VERSION,
        "project_root": str(tmp_path),
        "graph": ig.Graph(directed=True),
        "symbol_ranges": {},
        "name_to_vertex": {},
        "file_nodes": {},
        "file_edge_anchors": {},
        # Note: no "unified_to_names" key — simulating a pre-amend v3 pickle.
    }
    p = tmp_path / "legacy.pkl"
    with open(p, "wb") as f:
        pickle.dump(legacy, f)

    loaded = CodeGraph.load_graph(str(p))
    assert loaded._unified_to_names == {}


def _make_simple_graph():
    g = CodeGraph(project_root="/tmp/x")
    g.add_file_node("a.py")
    g._add_vertex(
        "Foo",
        {
            "type": "class",
            "file": "a.py",
            "start_line": 1,
            "end_line": 30,
            "unified_name": "a.py:Foo",
        },
    )
    g._add_vertex(
        "Foo.bar",
        {
            "type": "method",
            "file": "a.py",
            "start_line": 5,
            "end_line": 15,
            "unified_name": "a.py:Foo.bar",
        },
    )
    g._add_vertex(
        "Helper",
        {
            "type": "function",
            "file": "b.py",
            "start_line": 1,
            "end_line": 10,
            "unified_name": "b.py:Helper",
        },
    )
    # Foo CONTAINS Foo.bar (no anchor — invariant ii).
    g._add_edge("Foo", "Foo.bar", EDGE_TYPE_CONTAIN)
    # Foo.bar -> Helper, anchor at line 8 inside Foo.bar.
    g._add_edge(
        "Foo.bar", "Helper", EDGE_TYPE_REFERENCE, anchor_file="a.py", anchor_line=8
    )
    g.build_range_indexes()
    return g


def test_query_range_returns_noderefs():
    g = _make_simple_graph()
    res = g.query_range("a.py", 6, 9)
    assert all(isinstance(r, NodeRef) for r in res.defined)
    names = {r.unified_name for r in res.defined}
    assert names == {"a.py:Foo", "a.py:Foo.bar"}


def test_outgoing_default_is_reference_only():
    """CONTAIN edges have no meaningful anchor; outgoing default filters them out."""
    g = _make_simple_graph()
    res = g.query_range("a.py", 6, 9)
    edge_kinds = {e.edge_kind for e in res.outgoing}
    assert edge_kinds <= {
        EDGE_TYPE_REFERENCE
    }, f"outgoing leaked non-reference edges: {edge_kinds}"


def test_outgoing_kinds_opt_in():
    g = _make_simple_graph()
    res = g.query_range("a.py", 0, 30, kinds={EDGE_TYPE_REFERENCE, EDGE_TYPE_CONTAIN})
    # opt-in returns at least the reference edge.
    assert len(res.outgoing) >= 1


def test_incoming_default_is_reference_only():
    """Like outgoing, incoming defaults to REFERENCE-only — CONTAIN inbound
    (e.g. 'Foo CONTAINS Foo.bar') is filtered out of the typical query."""
    g = _make_simple_graph()
    # Range covering Foo.bar; the inbound CONTAIN edge from Foo is incoming
    # to Foo.bar but should not surface by default.
    res = g.query_range("a.py", 6, 9)
    edge_kinds = {e.edge_kind for e in res.incoming}
    assert edge_kinds <= {
        EDGE_TYPE_REFERENCE
    }, f"incoming leaked non-reference edges: {edge_kinds}"


def test_incoming_kinds_opt_in():
    g = _make_simple_graph()
    res = g.query_range("a.py", 6, 9, kinds={EDGE_TYPE_REFERENCE, EDGE_TYPE_CONTAIN})
    # opt-in: the CONTAIN inbound from Foo to Foo.bar surfaces.
    contain_inbound = [e for e in res.incoming if e.edge_kind == EDGE_TYPE_CONTAIN]
    assert len(contain_inbound) >= 1


def test_incoming_returns_edgerefs_with_target_vid():
    g = _make_simple_graph()
    res = g.query_range("a.py", 0, 30)
    for edge in res.incoming:
        assert isinstance(edge, EdgeRef)
        assert edge.target_vid is not None


def test_depth_only_one_supported():
    import pytest

    g = _make_simple_graph()
    res = g.query_range("a.py", 0, 30, depth=1)
    assert isinstance(res, RangeQueryResult)
    with pytest.raises(NotImplementedError):
        g.query_range("a.py", 0, 30, depth=2)


def test_query_range_by_symbol_uses_identity():
    """query_range_by_symbol resolves the identity `name` (not unified_name)
    and returns the symbol in `defined`."""
    g = _make_simple_graph()
    res = g.query_range_by_symbol("Foo.bar")
    unified_names = {n.unified_name for n in res.defined}
    assert "a.py:Foo.bar" in unified_names


def test_query_range_by_symbol_unknown_returns_empty():
    g = _make_simple_graph()
    res = g.query_range_by_symbol("DoesNotExist")
    assert res.defined == [] and res.outgoing == [] and res.incoming == []


def test_query_range_by_symbol_no_range_returns_empty():
    """A vertex without start_line/end_line (e.g. a SCIP reference-only
    vertex) yields an empty result, not a crash."""
    g = CodeGraph(project_root="/tmp/x")
    # Add a vertex with no file/start_line/end_line attributes.
    g._add_vertex("ref_only", {"type": "function"})
    g.build_range_indexes()
    res = g.query_range_by_symbol("ref_only")
    assert res.defined == [] and res.outgoing == [] and res.incoming == []


def test_query_range_excludes_anchor_at_end_line_plus_one():
    """Regression: bisect upper bound must not leak in `(end_line+1, eid=0)`.

    Previously the upper bound used `bisect_right(arr, (end_line+1, 0))`,
    which considers `(end_line+1, 0)` to compare equal-or-less and thus
    *includes* it. Trigger condition: anchored edge at `end_line+1` whose
    eid is 0 — i.e. it is the very first edge ever added. With `bisect_left`
    the entry is correctly excluded.
    """
    g = CodeGraph(project_root="/tmp/x")
    g.add_file_node("a.py")
    # Define two scopes in a.py: caller spans lines 0-4, neighbor at line 5.
    g._add_vertex(
        "caller",
        {
            "type": "function",
            "file": "a.py",
            "start_line": 0,
            "end_line": 4,
            "unified_name": "a.py:caller",
        },
    )
    g._add_vertex(
        "neighbor",
        {
            "type": "function",
            "file": "a.py",
            "start_line": 5,
            "end_line": 9,
            "unified_name": "a.py:neighbor",
        },
    )
    g._add_vertex(
        "Target",
        {
            "type": "function",
            "file": "b.py",
            "start_line": 0,
            "end_line": 1,
            "unified_name": "b.py:Target",
        },
    )

    # Add the offending anchor FIRST so it lands at eid 0 — this is the
    # narrow trigger the buggy bound used to leak.
    g._add_edge(
        "neighbor",
        "Target",
        EDGE_TYPE_REFERENCE,
        anchor_file="a.py",
        anchor_line=5,  # exactly query end_line + 1
    )
    # An in-range anchor for sanity (lands inside [0,4]).
    g._add_edge(
        "caller",
        "Target",
        EDGE_TYPE_REFERENCE,
        anchor_file="a.py",
        anchor_line=2,
    )
    g.build_range_indexes()

    res = g.query_range("a.py", 0, 4)
    anchor_lines = sorted(e.anchor_line for e in res.outgoing)
    assert anchor_lines == [2], (
        "Outgoing slice leaked an anchor at end_line+1: " f"{anchor_lines}"
    )
