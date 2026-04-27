"""Multi-edge correctness: edit one call site, verify only one edge is removed.

#125 review correctness risk: dropping `are_adjacent` dedup means a
`(src, tgt)` pair now carries multiple edges (one per anchor). Patcher
deletion logic must remove edges by `(src, tgt, type, anchor_line)`, not
by source/target alone. Severance reporting must surface one record per
anchor so the remap path can re-create one edge per call site.
"""

from codeminer.graph.code_graph import CodeGraph
from codeminer.graph.incremental.subgraph_mgr import SubgraphMgr
from codeminer.types import EDGE_TYPE_REFERENCE


class _NoopMgr(SubgraphMgr):
    """Minimal concrete SubgraphMgr to test the deletion / severance API."""

    def _build_unified_name(self, file_path, name, parent_unified_part, kind):
        return f"{file_path}:{name}"

    def _get_crossfile_token_types(self):
        return set()


def _setup_two_calls_one_target():
    g = CodeGraph(project_root="/tmp/x")
    g.add_file_node("caller.py")
    g._add_vertex(
        "caller_fn",
        {
            "type": "function",
            "file": "caller.py",
            "start_line": 1,
            "end_line": 20,
            "unified_name": "caller.py:caller_fn",
        },
    )
    g._add_vertex(
        "Target",
        {
            "type": "function",
            "file": "target.py",
            "start_line": 1,
            "end_line": 5,
            "unified_name": "target.py:Target",
        },
    )
    # Two call sites to the same target, anchored at lines 5 and 10.
    g._add_edge(
        "caller_fn",
        "Target",
        EDGE_TYPE_REFERENCE,
        anchor_file="caller.py",
        anchor_line=5,
    )
    g._add_edge(
        "caller_fn",
        "Target",
        EDGE_TYPE_REFERENCE,
        anchor_file="caller.py",
        anchor_line=10,
    )
    g.build_range_indexes()
    return g


def test_two_anchors_produce_two_edges():
    """Sanity: multi-edge schema preserves both call sites as distinct edges."""
    g = _setup_two_calls_one_target()
    src = g.name_to_vertex["caller_fn"]
    out_eids = g.graph.incident(src, mode="out")
    ref_eids = [e for e in out_eids if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE]
    assert (
        len(ref_eids) == 2
    ), f"expected 2 multi-edges (one per anchor), got {len(ref_eids)}"


def test_delete_one_anchor_keeps_other():
    """Simulate: anchor at line 5 is removed, anchor at line 10 survives.

    The patcher path under test is `subgraph_mgr.delete_edges_by_anchor`.
    Verify the line-10 edge remains intact afterwards.
    """
    g = _setup_two_calls_one_target()
    mgr = _NoopMgr(project_root="/tmp/x", code_graph=g)
    removed = mgr.delete_edges_by_anchor(
        source_name="caller_fn",
        target_name="Target",
        edge_type=EDGE_TYPE_REFERENCE,
        anchor_file="caller.py",
        anchor_line=5,
    )
    assert removed == 1

    src = g.name_to_vertex["caller_fn"]
    out_eids = g.graph.incident(src, mode="out")
    ref_eids = [e for e in out_eids if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE]
    assert len(ref_eids) == 1, f"expected 1 surviving edge, got {len(ref_eids)}"
    surviving = g.graph.es[ref_eids[0]].attributes()
    assert (
        surviving.get("anchor_line") == 10
    ), f"wrong edge survived; anchor_line={surviving.get('anchor_line')}"


def test_severed_edges_carry_anchor_per_site():
    """When a vertex is deleted, severed_outgoing must carry one entry per
    anchor (no (src, tgt) dedup), so remap can re-create one edge per site."""
    g = _setup_two_calls_one_target()
    mgr = _NoopMgr(project_root="/tmp/x", code_graph=g)
    result = mgr.delete_vertices_by_name(["caller_fn"])
    severed = result["severed_outgoing_refs"]
    target_entries = [s for s in severed if s[1] == "Target"]
    assert len(target_entries) == 2, (
        f"expected 2 severed entries (per-anchor), got {len(target_entries)}: "
        f"{target_entries}"
    )
    # Each entry must be a 6-tuple with (anchor_file, anchor_line) at the tail.
    for entry in target_entries:
        assert (
            len(entry) == 6
        ), f"severed entry expected 6-tuple, got {len(entry)}-tuple: {entry}"
        anchor_file = entry[4]
        anchor_line = entry[5]
        assert anchor_file == "caller.py"
        assert anchor_line in (
            5,
            10,
        ), f"unexpected anchor_line {anchor_line}; expected 5 or 10"
    # Both anchor lines surface (distinct call sites).
    anchor_lines = sorted(e[5] for e in target_entries)
    assert anchor_lines == [
        5,
        10,
    ], f"expected per-anchor entries for lines [5, 10]; got {anchor_lines}"


def test_delete_vertices_by_file_skips_internal_edges():
    """Severed edges should NOT include edges where both endpoints are
    inside the file being deleted — those are cascaded by vertex deletion."""
    g = CodeGraph(project_root="/tmp/x")
    g.add_file_node("a.py")
    # Two symbols in the same file with an internal reference edge.
    g._add_vertex(
        "a.py:Foo",
        {
            "type": "function",
            "file": "a.py",
            "start_line": 1,
            "end_line": 5,
            "unified_name": "a.py:Foo",
        },
    )
    g._add_vertex(
        "a.py:Bar",
        {
            "type": "function",
            "file": "a.py",
            "start_line": 10,
            "end_line": 15,
            "unified_name": "a.py:Bar",
        },
    )
    # Foo -> Bar internal reference (anchor in a.py)
    g._add_edge(
        "a.py:Foo",
        "a.py:Bar",
        EDGE_TYPE_REFERENCE,
        anchor_file="a.py",
        anchor_line=3,
    )
    # External vertex referencing into a.py:Bar — this severance IS expected.
    g.add_file_node("b.py")
    g._add_vertex(
        "b.py:caller",
        {
            "type": "function",
            "file": "b.py",
            "start_line": 1,
            "end_line": 10,
            "unified_name": "b.py:caller",
        },
    )
    g._add_edge(
        "b.py:caller",
        "a.py:Bar",
        EDGE_TYPE_REFERENCE,
        anchor_file="b.py",
        anchor_line=2,
    )
    g.build_range_indexes()

    mgr = _NoopMgr(project_root="/tmp/x", code_graph=g)
    result = mgr.delete_file_subgraph("a.py")

    # Internal Foo->Bar edge is cascaded; should NOT appear in either list.
    severed_out = result["severed_outgoing_refs"]
    internal = [s for s in severed_out if s[0] == "a.py:Foo" and s[1] == "a.py:Bar"]
    assert (
        len(internal) == 0
    ), f"internal a.py:Foo->a.py:Bar leaked into severed_outgoing: {internal}"

    # External b.py:caller -> a.py:Bar IS an external severance, must surface.
    severed_in = result["severed_incoming_refs"]
    external = [s for s in severed_in if s[0] == "b.py:caller" and s[1] == "a.py:Bar"]
    assert len(external) == 1, (
        f"expected exactly 1 external severance entry, got {len(external)}: "
        f"{external}"
    )
