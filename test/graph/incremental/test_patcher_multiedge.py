"""Multi-edge correctness: edit one call site, verify only one edge is removed.

#125 review correctness risk: dropping `are_adjacent` dedup means a
`(src, tgt)` pair now carries multiple edges (one per anchor). Patcher
deletion logic must remove edges by `(src, tgt, type, anchor_line)`, not
by source/target alone. Severance reporting must surface one record per
anchor so the remap path can re-create one edge per call site.
"""

from codeminer.graph.code_graph import CodeGraph
from codeminer.graph.incremental.patcher_base import PatcherBase
from codeminer.graph.incremental.subgraph_mgr import SubgraphMgr
from codeminer.types import EDGE_TYPE_REFERENCE


class _StubPatcher(PatcherBase):
    """Minimal PatcherBase subclass for unit tests on its protected methods."""

    def get_lsp_command(self):
        return ["echo"]

    def _build_unified_name(self, file_path, name, parent_unified_part, kind):
        return f"{file_path}:{name}"

    def _get_crossfile_token_types(self):
        return set()

    def flatten_symbols(self, file_path, lsp_symbols):
        return self._flatten_symbols_default(file_path, lsp_symbols)


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


# ---------------------------------------------------------------------------
# P1: delete_outgoing_in_anchor_ranges
# ---------------------------------------------------------------------------
def _setup_outgoing_with_anchors(anchor_lines, anchor_files=None):
    """Build a graph with one source `Foo` (in a.py) carrying multiple
    outgoing REFERENCE edges to `Tgt` (in b.py), each at a distinct anchor."""
    g = CodeGraph(project_root="/tmp/x")
    g.add_file_node("a.py")
    g.add_file_node("b.py")
    g._add_vertex(
        "a.py:Foo",
        {
            "type": "function",
            "file": "a.py",
            "start_line": 10,
            "end_line": 50,
            "unified_name": "a.py:Foo",
        },
    )
    g._add_vertex(
        "b.py:Tgt",
        {
            "type": "function",
            "file": "b.py",
            "start_line": 1,
            "end_line": 5,
            "unified_name": "b.py:Tgt",
        },
    )
    if anchor_files is None:
        anchor_files = ["a.py"] * len(anchor_lines)
    for af, al in zip(anchor_files, anchor_lines, strict=False):
        g._add_edge(
            "a.py:Foo",
            "b.py:Tgt",
            EDGE_TYPE_REFERENCE,
            anchor_file=af,
            anchor_line=al,
        )
    g.build_range_indexes()
    return g


def _outgoing_ref_anchor_lines(g, src_name):
    src = g.name_to_vertex[src_name]
    out_eids = g.graph.incident(src, mode="out")
    return sorted(
        g.graph.es[e].attributes().get("anchor_line")
        for e in out_eids
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    )


def test_delete_outgoing_in_anchor_ranges_single_range():
    """Delete only edges whose anchor_line falls within the given range."""
    g = _setup_outgoing_with_anchors([5, 15, 25, 50])
    mgr = _NoopMgr(project_root="/tmp/x", code_graph=g)
    removed = mgr.delete_outgoing_in_anchor_ranges(
        vertex_name="a.py:Foo",
        anchor_file="a.py",
        ranges=[(10, 30)],
    )
    assert removed == 2, f"expected 2 deletions (lines 15 and 25), got {removed}"
    survivors = _outgoing_ref_anchor_lines(g, "a.py:Foo")
    assert survivors == [5, 50], f"unexpected survivors: {survivors}"


def test_delete_outgoing_in_anchor_ranges_respects_anchor_file():
    """Edges in the same line range but a different anchor_file must stay.
    All three distinct anchors fall in [10, 20] numerically, but only the
    a.py ones should be deleted; b.py:15 must survive."""
    g = _setup_outgoing_with_anchors(
        [12, 15, 18],
        anchor_files=["a.py", "b.py", "a.py"],
    )
    mgr = _NoopMgr(project_root="/tmp/x", code_graph=g)
    removed = mgr.delete_outgoing_in_anchor_ranges(
        vertex_name="a.py:Foo",
        anchor_file="a.py",
        ranges=[(10, 20)],
    )
    assert removed == 2, f"expected 2 a.py edges deleted, got {removed}"
    src = g.name_to_vertex["a.py:Foo"]
    out_eids = g.graph.incident(src, mode="out")
    surviving = [
        g.graph.es[e].attributes()
        for e in out_eids
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    ]
    assert len(surviving) == 1
    assert surviving[0].get("anchor_file") == "b.py"
    assert surviving[0].get("anchor_line") == 15


def test_delete_outgoing_in_anchor_ranges_only_reference():
    """CONTAIN edges (no anchor) must NOT be touched."""
    g = _setup_outgoing_with_anchors([15])
    # Add a CONTAIN edge from Foo to a child symbol — no anchor, must survive.
    g._add_vertex(
        "a.py:Foo.helper",
        {
            "type": "function",
            "file": "a.py",
            "start_line": 20,
            "end_line": 25,
            "unified_name": "a.py:Foo.helper",
        },
    )
    from codeminer.types import EDGE_TYPE_CONTAIN
    g._add_edge("a.py:Foo", "a.py:Foo.helper", EDGE_TYPE_CONTAIN)

    mgr = _NoopMgr(project_root="/tmp/x", code_graph=g)
    removed = mgr.delete_outgoing_in_anchor_ranges(
        vertex_name="a.py:Foo",
        anchor_file="a.py",
        ranges=[(10, 30)],
    )
    assert removed == 1, f"expected 1 ref edge deleted, got {removed}"

    src = g.name_to_vertex["a.py:Foo"]
    out_eids = g.graph.incident(src, mode="out")
    contain_count = sum(
        1 for e in out_eids if g.graph.es[e]["type"] == EDGE_TYPE_CONTAIN
    )
    assert contain_count == 1, "CONTAIN edge must be preserved"


def test_delete_outgoing_in_anchor_ranges_multiple_ranges():
    """Multiple ranges: edge in any range gets deleted; edges outside all stay."""
    g = _setup_outgoing_with_anchors([3, 12, 18, 22, 35, 50])
    mgr = _NoopMgr(project_root="/tmp/x", code_graph=g)
    removed = mgr.delete_outgoing_in_anchor_ranges(
        vertex_name="a.py:Foo",
        anchor_file="a.py",
        ranges=[(10, 15), (20, 25)],
    )
    assert removed == 2, f"expected 2 (lines 12 and 22), got {removed}"
    survivors = _outgoing_ref_anchor_lines(g, "a.py:Foo")
    assert survivors == [3, 18, 35, 50], f"unexpected survivors: {survivors}"


# ---------------------------------------------------------------------------
# P2: shift_outgoing_anchor_lines
# ---------------------------------------------------------------------------
def test_shift_outgoing_anchor_lines_basic():
    """Uniform positive shift over the symbol's old body range."""
    g = _setup_outgoing_with_anchors([12, 18, 25])
    mgr = _NoopMgr(project_root="/tmp/x", code_graph=g)
    moved = mgr.shift_outgoing_anchor_lines(
        vertex_name="a.py:Foo",
        anchor_file="a.py",
        old_start=10,
        old_end=30,
        shift=5,
    )
    assert moved == 3, f"expected 3 anchor shifts, got {moved}"
    assert _outgoing_ref_anchor_lines(g, "a.py:Foo") == [17, 23, 30]


def test_shift_outgoing_anchor_lines_negative():
    """Negative shift moves anchors back. Anchors outside the old range stay."""
    g = _setup_outgoing_with_anchors([5, 22, 28, 40])
    mgr = _NoopMgr(project_root="/tmp/x", code_graph=g)
    moved = mgr.shift_outgoing_anchor_lines(
        vertex_name="a.py:Foo",
        anchor_file="a.py",
        old_start=20,
        old_end=30,
        shift=-3,
    )
    assert moved == 2, f"expected 2 (lines 22 and 28), got {moved}"
    assert _outgoing_ref_anchor_lines(g, "a.py:Foo") == [5, 19, 25, 40]


def test_shift_outgoing_anchor_lines_respects_anchor_file():
    """Anchors with same line range but different anchor_file must not shift."""
    g = _setup_outgoing_with_anchors(
        [12, 15, 18],
        anchor_files=["a.py", "b.py", "a.py"],
    )
    mgr = _NoopMgr(project_root="/tmp/x", code_graph=g)
    moved = mgr.shift_outgoing_anchor_lines(
        vertex_name="a.py:Foo",
        anchor_file="a.py",
        old_start=10,
        old_end=20,
        shift=100,
    )
    assert moved == 2, f"expected 2 (a.py edges only), got {moved}"
    src = g.name_to_vertex["a.py:Foo"]
    pairs = sorted(
        (
            g.graph.es[e].attributes().get("anchor_file"),
            g.graph.es[e].attributes().get("anchor_line"),
        )
        for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    )
    assert pairs == [("a.py", 112), ("a.py", 118), ("b.py", 15)]


def test_shift_outgoing_anchor_lines_only_reference():
    """CONTAIN edges (no anchor) stay untouched."""
    g = _setup_outgoing_with_anchors([15])
    g._add_vertex(
        "a.py:Foo.helper",
        {
            "type": "function",
            "file": "a.py",
            "start_line": 20,
            "end_line": 25,
            "unified_name": "a.py:Foo.helper",
        },
    )
    from codeminer.types import EDGE_TYPE_CONTAIN
    g._add_edge("a.py:Foo", "a.py:Foo.helper", EDGE_TYPE_CONTAIN)

    mgr = _NoopMgr(project_root="/tmp/x", code_graph=g)
    moved = mgr.shift_outgoing_anchor_lines(
        vertex_name="a.py:Foo",
        anchor_file="a.py",
        old_start=10,
        old_end=30,
        shift=5,
    )
    assert moved == 1
    src = g.name_to_vertex["a.py:Foo"]
    contain_count = sum(
        1
        for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_CONTAIN
    )
    assert contain_count == 1, "CONTAIN edge must stay"


def test_shift_outgoing_anchor_lines_zero_shift_noop():
    """shift=0 is a no-op: nothing moves, nothing reported."""
    g = _setup_outgoing_with_anchors([12, 18])
    mgr = _NoopMgr(project_root="/tmp/x", code_graph=g)
    moved = mgr.shift_outgoing_anchor_lines(
        vertex_name="a.py:Foo",
        anchor_file="a.py",
        old_start=10,
        old_end=30,
        shift=0,
    )
    assert moved == 0
    assert _outgoing_ref_anchor_lines(g, "a.py:Foo") == [12, 18]


# ---------------------------------------------------------------------------
# P3: reconnect_outgoing / reconnect_incoming must anchor each new edge
# ---------------------------------------------------------------------------
class _FakeLSPDefRefs:
    """Stub lsp_client with canned definition() / references() responses."""

    def __init__(self, project_root, definitions=None, references=None):
        self.project_root = project_root
        self._defs = definitions or {}      # text → list[loc]
        self._refs = references or {}       # (file, line) → list[loc]

    def definition(self, abs_file, line, character):
        # Look up by token text — but we don't have text here, so the
        # caller-side override of _get_semantic_tokens is responsible for
        # routing. Here we just return whatever is keyed by line.
        return self._defs.get(line, [])

    def references(self, abs_file, line, character, include_declaration=False):
        rel = abs_file.split(self.project_root + "/")[-1]
        return self._refs.get((rel, line, character), [])


class _ReconnectMgr(_NoopMgr):
    """Override LSP semantic-token fetch to feed canned tokens."""

    canned_tokens = []
    def_by_text = {}

    def _get_semantic_tokens(self, abs_file, file_path, line_ranges=None):
        return self.canned_tokens

    def _set_definitions(self, defs_by_text):
        self.def_by_text = defs_by_text


def _build_caller_target_graph(project_root="/tmp/recproj"):
    g = CodeGraph(project_root=project_root)
    g.add_file_node("caller.py")
    g.add_file_node("tgt.py")
    g._add_vertex(
        "caller.py:Foo",
        {
            "type": "function",
            "file": "caller.py",
            "start_line": 10,
            "end_line": 30,
            "unified_name": "caller.py:Foo",
        },
    )
    g._add_vertex(
        "tgt.py:Bar",
        {
            "type": "function",
            "file": "tgt.py",
            "start_line": 1,
            "end_line": 5,
            "unified_name": "tgt.py:Bar",
        },
    )
    g.build_range_indexes()
    return g


def test_reconnect_outgoing_anchors_each_call_site():
    """After fix: 2 call sites to same target produce 2 anchored edges,
    each carrying its own (anchor_file, anchor_line)."""
    project_root = "/tmp/recproj"
    g = _build_caller_target_graph(project_root)
    mgr = _ReconnectMgr(project_root=project_root, code_graph=g)
    mgr.canned_tokens = [
        {"line": 12, "character": 8, "text": "Bar"},
        {"line": 18, "character": 8, "text": "Bar"},
    ]

    # Single LSP definition response — both tokens resolve to same target.
    fake_lsp = _FakeLSPDefRefs(project_root)
    fake_lsp._defs = {
        12: [{"targetUri": f"file://{project_root}/tgt.py",
              "targetSelectionRange": {"start": {"line": 1, "character": 0}}}],
        18: [{"targetUri": f"file://{project_root}/tgt.py",
              "targetSelectionRange": {"start": {"line": 1, "character": 0}}}],
    }
    # _get_semantic_tokens dedups by text BEFORE calling lsp.definition;
    # but the dedup uses text as the key. Stub: same text "Bar" → only one
    # definition lookup. So return the line-1 target either way.
    fake_lsp.definition = lambda abs_file, line, character: [{
        "targetUri": f"file://{project_root}/tgt.py",
        "targetSelectionRange": {"start": {"line": 1, "character": 0}},
    }]
    mgr.lsp_client = fake_lsp

    stats = {"incoming_added": 0, "outgoing_added": 0, "unmatched": 0}
    mgr.reconnect_outgoing(
        "caller.py", ["caller.py:Foo"], stats, line_ranges=[(10, 30)]
    )

    assert stats["outgoing_added"] == 2, (
        f"expected 2 anchored edges (one per call site), "
        f"got {stats['outgoing_added']}"
    )
    src = g.name_to_vertex["caller.py:Foo"]
    ref_eids = [
        e for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    ]
    assert len(ref_eids) == 2, f"graph has {len(ref_eids)} ref edges"
    pairs = sorted(
        (g.graph.es[e].attributes().get("anchor_file"),
         g.graph.es[e].attributes().get("anchor_line"))
        for e in ref_eids
    )
    assert pairs == [("caller.py", 12), ("caller.py", 18)], (
        f"anchor metadata not threaded through; got {pairs}"
    )


def test_reconnect_outgoing_same_anchor_collapses():
    """If two tokens have IDENTICAL (line, character), _add_edge collapses
    them — same call site, can't be two distinct edges. One edge survives,
    anchored at that single line."""
    project_root = "/tmp/recproj"
    g = _build_caller_target_graph(project_root)
    mgr = _ReconnectMgr(project_root=project_root, code_graph=g)
    mgr.canned_tokens = [
        {"line": 12, "character": 8, "text": "Bar"},
        {"line": 12, "character": 8, "text": "Bar"},
    ]
    fake_lsp = _FakeLSPDefRefs(project_root)
    fake_lsp.definition = lambda abs_file, line, character: [{
        "targetUri": f"file://{project_root}/tgt.py",
        "targetSelectionRange": {"start": {"line": 1, "character": 0}},
    }]
    mgr.lsp_client = fake_lsp
    stats = {"incoming_added": 0, "outgoing_added": 0, "unmatched": 0}
    mgr.reconnect_outgoing(
        "caller.py", ["caller.py:Foo"], stats, line_ranges=[(10, 30)]
    )
    src = g.name_to_vertex["caller.py:Foo"]
    ref_eids = [
        e for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    ]
    # Same anchor → _add_edge dedup collapses to 1 edge.
    assert len(ref_eids) == 1, f"expected 1 edge (same anchor), got {len(ref_eids)}"


def test_reconnect_incoming_anchors_caller_site():
    """reconnect_incoming must anchor the new edge on the caller site."""
    project_root = "/tmp/recproj"
    g = CodeGraph(project_root=project_root)
    g.add_file_node("caller.py")
    g.add_file_node("tgt.py")
    g._add_vertex(
        "caller.py:Caller",
        {
            "type": "function",
            "file": "caller.py",
            "start_line": 5,
            "end_line": 15,
            "unified_name": "caller.py:Caller",
        },
    )
    g._add_vertex(
        "tgt.py:Bar",
        {
            "type": "function",
            "file": "tgt.py",
            "start_line": 1,
            "end_line": 5,
            "unified_name": "tgt.py:Bar",
        },
    )
    # match_location_to_scope reads symbol_ranges (populated by decoder /
    # patcher in normal flow); do it explicitly here.
    g.symbol_ranges["caller.py:Caller"] = (5, 15)
    g.symbol_ranges["tgt.py:Bar"] = (1, 5)
    g.build_range_indexes()

    fake_lsp = _FakeLSPDefRefs(project_root)
    # references() must return list of LSP locations for any (file,line,char).
    fake_lsp.references = lambda abs_file, line, character, include_declaration=False: [
        {
            "uri": f"file://{project_root}/caller.py",
            "range": {"start": {"line": 7, "character": 4}},
        }
    ]
    mgr = _NoopMgr(project_root=project_root, code_graph=g, lsp_client=fake_lsp)
    # selection ranges populated for the target so reconnect_incoming finds it
    mgr.symbol_selection_ranges["tgt.py:Bar"] = (1, 4, 1, 7)
    # match_location_to_scope reads file_to_vertices, populated by build_indexes()
    mgr.build_indexes()

    stats = {"incoming_added": 0, "outgoing_added": 0, "unmatched": 0}
    mgr.reconnect_incoming("tgt.py", ["tgt.py:Bar"], stats)

    assert stats["incoming_added"] == 1, f"expected 1 incoming edge, got {stats}"
    src = g.name_to_vertex["caller.py:Caller"]
    ref_eids = [
        e for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    ]
    assert len(ref_eids) == 1
    attrs = g.graph.es[ref_eids[0]].attributes()
    assert attrs.get("anchor_file") == "caller.py", (
        f"anchor_file should be the caller site file; got {attrs}"
    )
    assert attrs.get("anchor_line") == 7, (
        f"anchor_line should be the call line; got {attrs}"
    )


# ---------------------------------------------------------------------------
# P5: shifted branch must rebase outgoing anchors via shift_outgoing_anchor_lines
# ---------------------------------------------------------------------------
def test_shifted_rebases_outgoing_anchor_lines():
    """When a symbol shifts (body unchanged but start_line moves), the
    patcher's shifted branch must shift every outgoing reference anchor by
    `new_start - old_start` so range queries still match."""
    project_root = "/tmp/shftproj"
    g = _setup_outgoing_with_anchors([12, 18, 25])  # Foo body 10-30
    patcher = _StubPatcher(project_root=project_root, code_graph=g)
    patcher.symbol_selection_ranges["a.py:Foo"] = (10, 4, 10, 7)

    file_stats = {
        "vertices_deleted": 0, "vertices_created": 0, "vertices_shifted": 0,
        "refs_incoming": 0, "refs_outgoing": 0, "refs_remapped": 0,
        "refs_unmatched": 0,
    }
    patcher._process_shifted(
        uname="a.py:Foo",
        old={"vertex_name": "a.py:Foo", "start_line": 10, "end_line": 30},
        new={"start_line": 15, "end_line": 35,
             "sel_range": {"start": {"line": 15, "character": 4},
                           "end": {"line": 15, "character": 7}}},
        file_path="a.py",
        file_stats=file_stats,
    )

    # Vertex renamed.
    new_vname = "a.py:Foo:15"
    assert new_vname in g.name_to_vertex, "vertex should be renamed"
    assert "a.py:Foo" not in g.name_to_vertex, "old vname should be gone"

    # Outgoing anchors shifted by +5.
    src = g.name_to_vertex[new_vname]
    anchor_lines = sorted(
        g.graph.es[e].attributes().get("anchor_line")
        for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    )
    assert anchor_lines == [17, 23, 30], (
        f"anchors should shift +5 to [17,23,30]; got {anchor_lines}"
    )
    assert file_stats["vertices_shifted"] == 1


def test_affected_preserved_clears_in_range_outgoing():
    """Case 3 (preserved): hunk modifies a single line inside body but
    line count is preserved. Edges anchored in the changed range must be
    deleted; edges outside are kept (will be picked up by Round 2 LSP)."""
    project_root = "/tmp/affproj"
    g = _setup_outgoing_with_anchors([12, 18, 25])  # Foo body 10-30
    patcher = _StubPatcher(project_root=project_root, code_graph=g)
    patcher.symbol_selection_ranges["a.py:Foo"] = (10, 4, 10, 7)

    file_stats = {
        "vertices_deleted": 0, "vertices_created": 0, "vertices_shifted": 0,
        "vertices_affected_preserved": 0, "vertices_affected_rebuilt": 0,
        "refs_incoming": 0, "refs_outgoing": 0, "refs_remapped": 0,
        "refs_unmatched": 0,
    }
    # Hunk: line 18 (old) → line 18 (new). 1 line in / 1 line out → preserved.
    hunks = [(18, 18, 18, 18)]
    _new_vname, new_ranges = patcher._process_affected(
        uname="a.py:Foo",
        old={"vertex_name": "a.py:Foo", "start_line": 10, "end_line": 30},
        new={"start_line": 10, "end_line": 30,
             "sel_range": {"start": {"line": 10, "character": 4},
                           "end": {"line": 10, "character": 7}}},
        hunks=hunks,
        file_path="a.py",
        file_stats=file_stats,
    )

    new_vname = "a.py:Foo:10"
    src = g.name_to_vertex[new_vname]
    surviving = sorted(
        g.graph.es[e].attributes().get("anchor_line")
        for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    )
    assert surviving == [12, 25], (
        f"only line-18 anchor should be cleared; got {surviving}"
    )
    # Round 2 needs to know which lines to query in the NEW coordinate
    # system; for this hunk that's line 18.
    assert (18, 18) in new_ranges, f"new_changed_ranges missing 18; got {new_ranges}"
    assert file_stats["vertices_affected_preserved"] == 1


def test_affected_length_changed_clears_all_outgoing():
    """Case 4 (length_changed): hunk inserts lines into the body. Net line
    count not preserved, fast-path anchor shift is unsafe → delete every
    outgoing reference edge from this vertex; Round 2 rebuilds them."""
    project_root = "/tmp/affproj"
    g = _setup_outgoing_with_anchors([12, 18, 25])  # Foo body 10-30
    patcher = _StubPatcher(project_root=project_root, code_graph=g)
    patcher.symbol_selection_ranges["a.py:Foo"] = (10, 4, 10, 7)

    file_stats = {
        "vertices_deleted": 0, "vertices_created": 0, "vertices_shifted": 0,
        "vertices_affected_preserved": 0, "vertices_affected_rebuilt": 0,
        "refs_incoming": 0, "refs_outgoing": 0, "refs_remapped": 0,
        "refs_unmatched": 0,
    }
    # Hunk: 1 line at old 18 becomes 4 lines at new 18-21. Length changed.
    hunks = [(18, 18, 18, 21)]
    _new_vname, new_ranges = patcher._process_affected(
        uname="a.py:Foo",
        old={"vertex_name": "a.py:Foo", "start_line": 10, "end_line": 30},
        new={"start_line": 10, "end_line": 33,
             "sel_range": {"start": {"line": 10, "character": 4},
                           "end": {"line": 10, "character": 7}}},
        hunks=hunks,
        file_path="a.py",
        file_stats=file_stats,
    )

    new_vname = "a.py:Foo:10"
    src = g.name_to_vertex[new_vname]
    surviving = [
        e for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    ]
    assert len(surviving) == 0, (
        "case 4 must clear ALL outgoing ref edges; got "
        f"{[g.graph.es[e].attributes() for e in surviving]}"
    )
    # Round 2 reconnects over the entire new body.
    assert new_ranges == [(10, 33)], (
        f"slow path should report full new body; got {new_ranges}"
    )
    assert file_stats["vertices_affected_rebuilt"] == 1


def test_affected_preserved_with_start_line_shift_uniform_rebase():
    """Case 3 + symbol shifted by file-level insertion: anchors outside the
    changed range shift by `delta = new.start - old.start`; in-range
    anchors are deleted."""
    project_root = "/tmp/affproj"
    g = _setup_outgoing_with_anchors([12, 18, 25])  # Foo body 10-30
    patcher = _StubPatcher(project_root=project_root, code_graph=g)
    patcher.symbol_selection_ranges["a.py:Foo"] = (10, 4, 10, 7)

    file_stats = {
        "vertices_deleted": 0, "vertices_created": 0, "vertices_shifted": 0,
        "vertices_affected_preserved": 0, "vertices_affected_rebuilt": 0,
        "refs_incoming": 0, "refs_outgoing": 0, "refs_remapped": 0,
        "refs_unmatched": 0,
    }
    # 5 lines inserted at top of file → symbol shifts +5; old hunk modifies
    # what was old line 18 (now new line 23). Length preserved.
    hunks = [(18, 18, 23, 23)]
    _new_vname, new_ranges = patcher._process_affected(
        uname="a.py:Foo",
        old={"vertex_name": "a.py:Foo", "start_line": 10, "end_line": 30},
        new={"start_line": 15, "end_line": 35,
             "sel_range": {"start": {"line": 15, "character": 4},
                           "end": {"line": 15, "character": 7}}},
        hunks=hunks,
        file_path="a.py",
        file_stats=file_stats,
    )

    new_vname = "a.py:Foo:15"
    src = g.name_to_vertex[new_vname]
    surviving = sorted(
        g.graph.es[e].attributes().get("anchor_line")
        for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    )
    # 12 → 17, 25 → 30; anchor at 18 is in changed region → deleted.
    assert surviving == [17, 30], (
        f"expected [17, 30] (uniform +5 shift, line-18 deleted); got {surviving}"
    )
    assert (23, 23) in new_ranges
    assert file_stats["vertices_affected_preserved"] == 1


# ---------------------------------------------------------------------------
# P7: severed_incoming uname fallback when src vname has been renamed
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# P8: severed remap rebase anchor_line via per-file shift table
# ---------------------------------------------------------------------------
def test_remap_severed_anchor_line_rebased_by_shift_table():
    """When the severed entry's src file has been shifted (e.g. from an
    earlier sibling-file patch in the same batch), anchor_line must be
    rebased by the shift table before the rebuilt edge is written."""
    project_root = "/tmp/r2proj"
    g = CodeGraph(project_root=project_root)
    g.add_file_node("caller.py")
    g.add_file_node("tgt.py")
    g._add_vertex(
        "caller.py:Caller",
        {
            "type": "function",
            "file": "caller.py",
            "start_line": 5,
            "end_line": 15,
            "unified_name": "caller.py:Caller",
        },
    )
    g._add_vertex(
        "tgt.py:Bar:1",
        {
            "type": "function",
            "file": "tgt.py",
            "start_line": 1,
            "end_line": 5,
            "unified_name": "tgt.py:Bar",
        },
    )
    g.symbol_ranges["caller.py:Caller"] = (5, 15)
    g.symbol_ranges["tgt.py:Bar:1"] = (1, 5)
    g.build_range_indexes()

    patcher = _StubPatcher(project_root=project_root, code_graph=g)

    # Severed entry was recorded with anchor_line=7 (pre-shift). The src
    # file has since been shifted by +5 in [5, 15] → new anchor should be 12.
    severed_incoming = [
        (
            "caller.py:Caller", "tgt.py:Bar",
            "caller.py:Caller", "tgt.py:Bar",
            "caller.py", 7,
        )
    ]
    anchor_shifts = {
        "caller.py": [(5, 15, 5)],   # (old_start, old_end, delta)
    }

    remapped = patcher._remap_severed_edges(
        new_vertices=["tgt.py:Bar:1"],
        severed_incoming=severed_incoming,
        severed_outgoing=[],
        anchor_shifts=anchor_shifts,
    )
    assert remapped == 1
    src = g.name_to_vertex["caller.py:Caller"]
    ref_eids = [
        e for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    ]
    assert len(ref_eids) == 1
    attrs = g.graph.es[ref_eids[0]].attributes()
    assert attrs.get("anchor_line") == 12, (
        f"anchor_line should rebase 7 + 5 = 12; got {attrs}"
    )


def test_remap_severed_anchor_line_no_shift_table_keeps_anchor():
    """No shift table → anchors remain at their recorded line (back-compat)."""
    project_root = "/tmp/r2proj"
    g = CodeGraph(project_root=project_root)
    g.add_file_node("caller.py")
    g.add_file_node("tgt.py")
    g._add_vertex(
        "caller.py:Caller",
        {
            "type": "function",
            "file": "caller.py",
            "start_line": 5,
            "end_line": 15,
            "unified_name": "caller.py:Caller",
        },
    )
    g._add_vertex(
        "tgt.py:Bar:1",
        {
            "type": "function",
            "file": "tgt.py",
            "start_line": 1,
            "end_line": 5,
            "unified_name": "tgt.py:Bar",
        },
    )
    g.symbol_ranges["caller.py:Caller"] = (5, 15)
    g.symbol_ranges["tgt.py:Bar:1"] = (1, 5)
    g.build_range_indexes()

    patcher = _StubPatcher(project_root=project_root, code_graph=g)
    remapped = patcher._remap_severed_edges(
        new_vertices=["tgt.py:Bar:1"],
        severed_incoming=[(
            "caller.py:Caller", "tgt.py:Bar",
            "caller.py:Caller", "tgt.py:Bar",
            "caller.py", 7,
        )],
        severed_outgoing=[],
    )
    assert remapped == 1
    src = g.name_to_vertex["caller.py:Caller"]
    ref_eids = [
        e for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    ]
    attrs = g.graph.es[ref_eids[0]].attributes()
    assert attrs.get("anchor_line") == 7, "no shift table → anchor unchanged"


def test_remap_severed_incoming_uname_fallback():
    """If the caller's vname was renamed in this batch, severed_incoming
    must still remap via src_uname instead of silently dropping the entry."""
    project_root = "/tmp/r1proj"
    g = CodeGraph(project_root=project_root)
    g.add_file_node("caller.py")
    g.add_file_node("tgt.py")
    # Caller already RENAMED to its new vname (simulating a sibling file
    # patch that ran before this one in the same batch).
    g._add_vertex(
        "caller.py:Caller:5",
        {
            "type": "function",
            "file": "caller.py",
            "start_line": 5,
            "end_line": 15,
            "unified_name": "caller.py:Caller",
        },
    )
    # Target rebuilt under a new vname, with same unified_name.
    g._add_vertex(
        "tgt.py:Bar:1",
        {
            "type": "function",
            "file": "tgt.py",
            "start_line": 1,
            "end_line": 5,
            "unified_name": "tgt.py:Bar",
        },
    )
    g.symbol_ranges["caller.py:Caller:5"] = (5, 15)
    g.symbol_ranges["tgt.py:Bar:1"] = (1, 5)
    g.build_range_indexes()

    patcher = _StubPatcher(project_root=project_root, code_graph=g)
    # Severed incoming entry recorded earlier when the OLD caller.py:Caller
    # vname was still in the graph.
    severed_incoming = [
        (
            "caller.py:Caller",     # src_name (STALE — already renamed)
            "tgt.py:Bar",           # tgt_name
            "caller.py:Caller",     # src_uname (stable)
            "tgt.py:Bar",           # tgt_uname
            "caller.py",
            7,
        )
    ]

    remapped = patcher._remap_severed_edges(
        new_vertices=["tgt.py:Bar:1"],
        severed_incoming=severed_incoming,
        severed_outgoing=[],
    )

    # Without the uname fallback this returns 0 and the edge is lost.
    assert remapped == 1, (
        "uname fallback should resurrect the edge when src_name is stale; "
        f"remapped={remapped}"
    )
    src = g.name_to_vertex["caller.py:Caller:5"]
    ref_eids = [
        e for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    ]
    assert len(ref_eids) == 1
    attrs = g.graph.es[ref_eids[0]].attributes()
    assert attrs.get("anchor_file") == "caller.py"
    assert attrs.get("anchor_line") == 7


def test_shifted_zero_shift_is_attribute_only_update():
    """Defensive: if start_line did not actually change, no shift call;
    only attrs (e.g. end_line) get refreshed."""
    project_root = "/tmp/shftproj"
    g = _setup_outgoing_with_anchors([12, 18])  # Foo body 10-30
    patcher = _StubPatcher(project_root=project_root, code_graph=g)
    patcher.symbol_selection_ranges["a.py:Foo"] = (10, 4, 10, 7)

    file_stats = {
        "vertices_deleted": 0, "vertices_created": 0, "vertices_shifted": 0,
        "refs_incoming": 0, "refs_outgoing": 0, "refs_remapped": 0,
        "refs_unmatched": 0,
    }
    patcher._process_shifted(
        uname="a.py:Foo",
        old={"vertex_name": "a.py:Foo", "start_line": 10, "end_line": 30},
        new={"start_line": 10, "end_line": 32,
             "sel_range": {"start": {"line": 10, "character": 4},
                           "end": {"line": 10, "character": 7}}},
        file_path="a.py",
        file_stats=file_stats,
    )

    # Anchors unchanged.
    src = g.name_to_vertex["a.py:Foo:10"]  # renamed because start in vname
    anchor_lines = sorted(
        g.graph.es[e].attributes().get("anchor_line")
        for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    )
    assert anchor_lines == [12, 18]
