# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Multi-edge correctness: edit one call site, verify only one edge is removed.

#125 review correctness risk: dropping `are_adjacent` dedup means a
`(src, tgt)` pair now carries multiple edges (one per anchor). Patcher
deletion logic must remove edges by `(src, tgt, type, anchor_line)`, not
by source/target alone. Severance reporting must surface one record per
anchor so the remap path can re-create one edge per call site.
"""

import pytest

from codeminer.graph.code_graph import CodeGraph
from codeminer.graph.incremental.change_mgr import ChangedLineHunk, map_old_line_to_new
from codeminer.graph.incremental.patcher_base import PatcherBase
from codeminer.graph.incremental.subgraph_mgr import SubgraphMgr
from codeminer.types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE


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


def test_rebase_file_scope_reference_anchors_only_updates_file_node():
    """Module-level anchors follow hunks without double-shifting symbols."""
    g = _setup_outgoing_with_anchors([11, 15])
    g._add_edge(
        "a.py",
        "b.py:Tgt",
        EDGE_TYPE_REFERENCE,
        anchor_file="a.py",
        anchor_line=11,
    )
    g._add_edge(
        "a.py",
        "b.py:Tgt",
        EDGE_TYPE_REFERENCE,
        anchor_file="a.py",
        anchor_line=15,
    )
    mgr = _NoopMgr(project_root="/tmp/x", code_graph=g)
    hunks = [ChangedLineHunk(10, 3, 10, 6)]

    moved, removed = mgr.rebase_file_scope_reference_anchors(
        "a.py", lambda line: map_old_line_to_new(line, hunks)
    )

    assert (moved, removed) == (1, 1)
    assert _outgoing_ref_anchor_lines(g, "a.py") == [18]
    assert _outgoing_ref_anchor_lines(g, "a.py:Foo") == [11, 15]


def test_classify_symbol_affected_when_edit_removes_its_old_tail():
    """An old-side-only overlap must refresh a symbol's shortened range."""
    patcher = _StubPatcher(
        project_root="/tmp/classifyproj",
        code_graph=CodeGraph(project_root="/tmp/classifyproj"),
    )
    old_symbols = {
        "a.py:Foo": {
            "vertex_name": "a.py:Foo:10",
            "start_line": 10,
            "end_line": 30,
        }
    }
    new_symbols = {
        "a.py:Foo": {
            "kind": 12,
            "start_line": 10,
            "end_line": 20,
        }
    }

    classified = patcher._classify_symbols(
        old_symbols,
        new_symbols,
        [(21, 30, 21, 24)],
    )

    assert classified["affected"] == ["a.py:Foo"]
    assert classified["unchanged"] == []


def test_synthesize_lsp_omission_when_declaration_is_unchanged():
    old_symbols = {
        "a.py:Foo": {
            "vertex_name": "a.py:Foo",
            "start_line": 10,
            "end_line": 30,
            "selection_line": 10,
            "selection_character": 4,
            "node_type": "function",
            "parent_uname": "a.py:Container",
        }
    }
    new_symbols = {}
    hunks = [ChangedLineHunk(30, 1, 30, 2)]

    count = _StubPatcher._synthesize_missing_symbol_locations(
        old_symbols,
        new_symbols,
        hunks,
    )

    assert count == 1
    assert new_symbols["a.py:Foo"]["start_line"] == 10
    assert new_symbols["a.py:Foo"]["end_line"] == 31
    assert new_symbols["a.py:Foo"]["node_type"] == "function"
    assert new_symbols["a.py:Foo"]["parent_uname"] == "a.py:Container"
    assert new_symbols["a.py:Foo"]["sel_range"]["start"]["character"] == 4
    patcher = _StubPatcher(
        project_root="/tmp/classifyproj",
        code_graph=CodeGraph(project_root="/tmp/classifyproj"),
    )
    classified = patcher._classify_symbols(
        old_symbols,
        new_symbols,
        [hunks[0].as_inclusive_range()],
    )
    assert classified["affected"] == ["a.py:Foo"]


def test_does_not_synthesize_symbol_when_declaration_is_edited():
    old_symbols = {
        "a.py:Foo": {
            "vertex_name": "a.py:Foo",
            "start_line": 10,
            "end_line": 30,
            "selection_line": 10,
        }
    }
    new_symbols = {}

    count = _StubPatcher._synthesize_missing_symbol_locations(
        old_symbols,
        new_symbols,
        [ChangedLineHunk(10, 21, 10, 0)],
    )

    assert count == 0
    assert new_symbols == {}


def test_classify_ignores_lsp_only_symbol_outside_changed_hunk():
    patcher = _StubPatcher(
        project_root="/tmp/classifyproj",
        code_graph=CodeGraph(project_root="/tmp/classifyproj"),
    )
    new_symbols = {
        "a.py:local": {
            "kind": 13,
            "start_line": 40,
            "end_line": 40,
        },
        "a.py:added()": {
            "kind": 12,
            "start_line": 10,
            "end_line": 12,
        },
    }

    classified = patcher._classify_symbols({}, new_symbols, [(10, 10, 10, 12)])

    assert classified["added"] == ["a.py:added()"]
    assert classified["backend_only"] == ["a.py:local"]


def test_align_common_symbol_location_uses_git_mapping_for_lsp_drift():
    old_symbols = {
        "a.py:create()": {
            "vertex_name": "a.py:create()",
            "start_line": 41,
            "end_line": 44,
            "selection_line": 41,
        }
    }
    new_symbols = {
        "a.py:create()": {
            "kind": 12,
            "start_line": 47,
            "end_line": 50,
            "sel_range": {
                "start": {"line": 47, "character": 4},
                "end": {"line": 47, "character": 10},
            },
        }
    }

    aligned = _StubPatcher._align_common_symbol_locations(
        old_symbols,
        new_symbols,
        [ChangedLineHunk(old_start=33, old_count=0, new_start=33, new_count=1)],
    )

    assert aligned == 1
    assert new_symbols["a.py:create()"]["start_line"] == 42
    assert new_symbols["a.py:create()"]["end_line"] == 45
    assert new_symbols["a.py:create()"]["sel_range"]["start"]["line"] == 42


# ---------------------------------------------------------------------------
# P3: reconnect_outgoing / reconnect_incoming must anchor each new edge
# ---------------------------------------------------------------------------
class _FakeLSPDefRefs:
    """Stub lsp_client with canned definition() / references() responses."""

    def __init__(self, project_root, definitions=None, references=None):
        self.project_root = project_root
        self._defs = definitions or {}  # text → list[loc]
        self._refs = references or {}  # (file, line) → list[loc]
        self.reference_batches = []
        self.reference_error = None

    def definition(self, abs_file, line, character):
        # Look up by token text — but we don't have text here, so the
        # caller-side override of _get_semantic_tokens is responsible for
        # routing. Here we just return whatever is keyed by line.
        return self._defs.get(line, [])

    def references(self, abs_file, line, character, include_declaration=False):
        rel = abs_file.split(self.project_root + "/")[-1]
        return self._refs.get((rel, line, character), [])

    def references_batch(self, queries, include_declaration=False, max_in_flight=None):
        self.reference_batches.append(list(queries))
        return [
            (
                self.references(
                    abs_file,
                    line,
                    character,
                    include_declaration=include_declaration,
                ),
                self.reference_error,
            )
            for abs_file, line, character in queries
        ]


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
    mgr.build_indexes()
    mgr.canned_tokens = [
        {"line": 12, "character": 8, "text": "Bar"},
        {"line": 18, "character": 8, "text": "Bar"},
    ]

    # Both source positions resolve to the same target.
    fake_lsp = _FakeLSPDefRefs(project_root)
    fake_lsp._defs = {
        12: [
            {
                "targetUri": f"file://{project_root}/tgt.py",
                "targetSelectionRange": {"start": {"line": 1, "character": 0}},
            }
        ],
        18: [
            {
                "targetUri": f"file://{project_root}/tgt.py",
                "targetSelectionRange": {"start": {"line": 1, "character": 0}},
            }
        ],
    }
    fake_lsp.definition = lambda abs_file, line, character: [
        {
            "targetUri": f"file://{project_root}/tgt.py",
            "targetSelectionRange": {"start": {"line": 1, "character": 0}},
        }
    ]
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
        e
        for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    ]
    assert len(ref_eids) == 2, f"graph has {len(ref_eids)} ref edges"
    pairs = sorted(
        (
            g.graph.es[e].attributes().get("anchor_file"),
            g.graph.es[e].attributes().get("anchor_line"),
        )
        for e in ref_eids
    )
    assert pairs == [
        ("caller.py", 12),
        ("caller.py", 18),
    ], f"anchor metadata not threaded through; got {pairs}"


def test_reconnect_outgoing_resolves_same_text_per_source_position():
    project_root = "/tmp/recproj"
    g = _build_caller_target_graph(project_root)
    g.add_file_node("other.py")
    g._add_vertex(
        "other.py:Bar",
        {
            "type": "function",
            "file": "other.py",
            "start_line": 2,
            "end_line": 6,
            "unified_name": "other.py:Bar",
        },
    )
    g.symbol_ranges["other.py:Bar"] = (2, 6)
    mgr = _ReconnectMgr(project_root=project_root, code_graph=g)
    mgr.build_indexes()
    mgr.canned_tokens = [
        {"line": 12, "character": 8, "text": "Bar"},
        {"line": 18, "character": 8, "text": "Bar"},
    ]
    fake_lsp = _FakeLSPDefRefs(project_root)
    fake_lsp._defs = {
        12: [
            {
                "targetUri": f"file://{project_root}/tgt.py",
                "targetSelectionRange": {"start": {"line": 1, "character": 0}},
            }
        ],
        18: [
            {
                "targetUri": f"file://{project_root}/other.py",
                "targetSelectionRange": {"start": {"line": 2, "character": 0}},
            }
        ],
    }
    mgr.lsp_client = fake_lsp

    stats = {"incoming_added": 0, "outgoing_added": 0, "unmatched": 0}
    mgr.reconnect_outgoing(
        "caller.py", ["caller.py:Foo"], stats, line_ranges=[(10, 30)]
    )

    src = g.name_to_vertex["caller.py:Foo"]
    targets = {
        g.graph.vs[g.graph.es[e].target]["name"]
        for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    }
    assert targets == {"tgt.py:Bar", "other.py:Bar"}


def test_reconnect_outgoing_same_anchor_collapses():
    """If two tokens have IDENTICAL (line, character), _add_edge collapses
    them — same call site, can't be two distinct edges. One edge survives,
    anchored at that single line."""
    project_root = "/tmp/recproj"
    g = _build_caller_target_graph(project_root)
    mgr = _ReconnectMgr(project_root=project_root, code_graph=g)
    mgr.build_indexes()
    mgr.canned_tokens = [
        {"line": 12, "character": 8, "text": "Bar"},
        {"line": 12, "character": 8, "text": "Bar"},
    ]
    fake_lsp = _FakeLSPDefRefs(project_root)
    fake_lsp.definition = lambda abs_file, line, character: [
        {
            "targetUri": f"file://{project_root}/tgt.py",
            "targetSelectionRange": {"start": {"line": 1, "character": 0}},
        }
    ]
    mgr.lsp_client = fake_lsp
    stats = {"incoming_added": 0, "outgoing_added": 0, "unmatched": 0}
    mgr.reconnect_outgoing(
        "caller.py", ["caller.py:Foo"], stats, line_ranges=[(10, 30)]
    )
    src = g.name_to_vertex["caller.py:Foo"]
    ref_eids = [
        e
        for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    ]
    # Same anchor → _add_edge dedup collapses to 1 edge.
    assert len(ref_eids) == 1, f"expected 1 edge (same anchor), got {len(ref_eids)}"


def test_reconnect_outgoing_skips_declaration_location():
    project_root = "/tmp/recproj"
    graph = _build_caller_target_graph(project_root)
    manager = _ReconnectMgr(project_root=project_root, code_graph=graph)
    manager.build_indexes()
    manager.canned_tokens = [{"line": 1, "character": 0, "length": 3, "text": "Bar"}]
    fake_lsp = _FakeLSPDefRefs(project_root)
    fake_lsp.definition = lambda *args, **kwargs: [
        {
            "targetUri": f"file://{project_root}/tgt.py",
            "targetSelectionRange": {"start": {"line": 1, "character": 0}},
        }
    ]
    manager.lsp_client = fake_lsp
    stats = {"incoming_added": 0, "outgoing_added": 0, "unmatched": 0}

    manager.reconnect_outgoing(
        "tgt.py",
        ["tgt.py:Bar"],
        stats,
        line_ranges=[(1, 1)],
    )

    assert stats["outgoing_added"] == 0


def test_reconnect_outgoing_validates_class_and_constructor_references():
    project_root = "/tmp/recproj"
    graph = CodeGraph(project_root=project_root)
    graph.add_file_node("caller.py")
    graph.add_file_node("model.py")
    graph._add_vertex(
        "caller.py:build():10",
        {
            "type": "function",
            "file": "caller.py",
            "start_line": 10,
            "end_line": 20,
            "selection_line": 10,
            "selection_character": 4,
            "unified_name": "caller.py:build()",
        },
    )
    graph._add_vertex(
        "model.py:Model:1",
        {
            "type": "class",
            "file": "model.py",
            "start_line": 1,
            "end_line": 8,
            "selection_line": 1,
            "selection_character": 6,
            "unified_name": "model.py:Model",
        },
    )
    graph._add_vertex(
        "model.py:Model.__init__():2",
        {
            "type": "method",
            "file": "model.py",
            "start_line": 2,
            "end_line": 4,
            "selection_line": 2,
            "selection_character": 8,
            "unified_name": "model.py:Model.__init__()",
        },
    )
    graph._add_edge(
        "model.py:Model:1",
        "model.py:Model.__init__():2",
        EDGE_TYPE_CONTAIN,
    )
    graph.build_range_indexes()

    manager = _ReconnectMgr(project_root=project_root, code_graph=graph)
    manager.REGISTRY_LANGUAGE = "python"
    manager.mode = "symbol"
    manager.build_indexes()
    manager.canned_tokens = [
        {
            "line": 12,
            "character": 8,
            "length": 5,
            "text": "Model",
            "token_type": "class",
        }
    ]
    location = {
        "uri": f"file://{project_root}/caller.py",
        "range": {
            "start": {"line": 12, "character": 8},
            "end": {"line": 12, "character": 13},
        },
    }
    fake_lsp = _FakeLSPDefRefs(
        project_root,
        definitions={
            12: [
                {
                    "targetUri": f"file://{project_root}/model.py",
                    "targetSelectionRange": {"start": {"line": 1, "character": 6}},
                }
            ]
        },
        references={
            ("model.py", 1, 6): [location],
            ("model.py", 2, 8): [location],
        },
    )
    manager.lsp_client = fake_lsp
    stats = {"incoming_added": 0, "outgoing_added": 0, "unmatched": 0}

    manager.reconnect_outgoing(
        "caller.py",
        ["caller.py:build():10"],
        stats,
        line_ranges=[(10, 20)],
    )

    caller_id = graph.name_to_vertex["caller.py:build():10"]
    targets = {
        graph.graph.vs[graph.graph.es[edge_id].target]["name"]
        for edge_id in graph.graph.incident(caller_id, mode="out")
        if graph.graph.es[edge_id]["type"] == EDGE_TYPE_REFERENCE
    }
    assert targets == {
        "model.py:Model:1",
        "model.py:Model.__init__():2",
    }
    assert len(fake_lsp.reference_batches) == 1
    assert len(fake_lsp.reference_batches[0]) == 2


def test_reverse_reference_batch_preserves_strict_failure():
    project_root = "/tmp/recproj"
    graph = _build_caller_target_graph(project_root)
    graph.graph.vs[graph.name_to_vertex["tgt.py:Bar"]]["selection_line"] = 1
    graph.graph.vs[graph.name_to_vertex["tgt.py:Bar"]]["selection_character"] = 0
    manager = _ReconnectMgr(project_root=project_root, code_graph=graph)
    manager.mode = "symbol"
    manager.REGISTRY_LANGUAGE = "python"
    manager.strict_lsp_requests = True
    manager.build_indexes()
    manager.canned_tokens = [{"line": 12, "character": 8, "length": 3, "text": "Bar"}]
    fake_lsp = _FakeLSPDefRefs(project_root)
    fake_lsp.definition = lambda *_args, **_kwargs: [
        {
            "targetUri": f"file://{project_root}/tgt.py",
            "targetSelectionRange": {"start": {"line": 1, "character": 0}},
        }
    ]
    fake_lsp.reference_error = {"code": -1, "message": "timeout"}
    manager.lsp_client = fake_lsp

    with pytest.raises(RuntimeError, match="reverse-reference oracle request failed"):
        manager.reconnect_outgoing(
            "caller.py",
            ["caller.py:Foo"],
            {"incoming_added": 0, "outgoing_added": 0, "unmatched": 0},
            line_ranges=[(10, 30)],
        )


def test_reconcile_touched_incoming_updates_unchanged_call_sites():
    project_root = "/tmp/recproj"
    graph = CodeGraph(project_root=project_root)
    for path in ("old.py", "new.py", "target.py"):
        graph.add_file_node(path)
    for path in ("old.py", "new.py"):
        graph._add_vertex(
            f"{path}:call():1",
            {
                "type": "function",
                "file": path,
                "start_line": 1,
                "end_line": 3,
                "unified_name": f"{path}:call()",
            },
        )
    target_name = "target.py:run():5"
    graph._add_vertex(
        target_name,
        {
            "type": "function",
            "file": "target.py",
            "start_line": 5,
            "end_line": 8,
            "selection_line": 5,
            "selection_character": 4,
            "unified_name": "target.py:run()",
        },
    )
    graph._add_edge(
        "old.py:call():1",
        target_name,
        EDGE_TYPE_REFERENCE,
        anchor_file="old.py",
        anchor_line=2,
    )
    graph.build_range_indexes()

    manager = _ReconnectMgr(project_root=project_root, code_graph=graph)
    manager.build_indexes()
    manager._touched_reference_targets.add(target_name)
    manager._reverse_reference_cache[target_name] = [
        {
            "uri": f"file://{project_root}/new.py",
            "range": {
                "start": {"line": 2, "character": 3},
                "end": {"line": 2, "character": 6},
            },
        }
    ]

    stats = manager.reconcile_touched_incoming()

    target_id = graph.name_to_vertex[target_name]
    incoming = [
        graph.graph.es[edge_id]
        for edge_id in graph.graph.incident(target_id, mode="in")
        if graph.graph.es[edge_id]["type"] == EDGE_TYPE_REFERENCE
    ]
    assert stats == {
        "targets_reconciled": 1,
        "incoming_removed": 1,
        "incoming_added": 1,
        "unmatched": 0,
    }
    assert len(incoming) == 1
    assert graph.graph.vs[incoming[0].source]["name"] == "new.py:call():1"
    assert incoming[0]["anchor_file"] == "new.py"
    assert incoming[0]["anchor_line"] == 2


def test_reconcile_touched_incoming_fetches_uncached_invalidated_target():
    project_root = "/tmp/recproj"
    graph = CodeGraph(project_root=project_root)
    for path in ("changed.py", "target.py"):
        graph.add_file_node(path)
    graph._add_vertex(
        "changed.py:call():1",
        {
            "type": "function",
            "file": "changed.py",
            "start_line": 1,
            "end_line": 3,
            "unified_name": "changed.py:call()",
        },
    )
    target_name = "target.py:run():5"
    graph._add_vertex(
        target_name,
        {
            "type": "function",
            "file": "target.py",
            "start_line": 5,
            "end_line": 8,
            "selection_line": 5,
            "selection_character": 4,
            "unified_name": "target.py:run()",
        },
    )
    graph.build_range_indexes()

    manager = _ReconnectMgr(project_root=project_root, code_graph=graph)
    manager.build_indexes()
    manager._touched_reference_targets.add(target_name)
    manager.lsp_client = _FakeLSPDefRefs(
        project_root,
        references={
            ("target.py", 5, 4): [
                {
                    "uri": f"file://{project_root}/changed.py",
                    "range": {
                        "start": {"line": 2, "character": 3},
                        "end": {"line": 2, "character": 6},
                    },
                }
            ]
        },
    )

    stats = manager.reconcile_touched_incoming(changed_files={"changed.py"})

    assert stats["targets_reconciled"] == 1
    assert stats["incoming_added"] == 1
    assert target_name in manager._reverse_reference_cache


def test_reconcile_touched_incoming_preserves_untouched_provider_facts():
    project_root = "/tmp/recproj"
    graph = CodeGraph(project_root=project_root)
    for path in ("changed.py", "stable.py", "target.py"):
        graph.add_file_node(path)
    for path in ("changed.py", "stable.py"):
        graph._add_vertex(
            f"{path}:call():1",
            {
                "type": "function",
                "file": path,
                "start_line": 1,
                "end_line": 3,
                "unified_name": f"{path}:call()",
            },
        )
    target_name = "target.py:run():5"
    graph._add_vertex(
        target_name,
        {
            "type": "function",
            "file": "target.py",
            "start_line": 5,
            "end_line": 8,
            "selection_line": 5,
            "selection_character": 4,
            "unified_name": "target.py:run()",
        },
    )
    for source_file in ("changed.py", "stable.py"):
        graph._add_edge(
            f"{source_file}:call():1",
            target_name,
            EDGE_TYPE_REFERENCE,
            anchor_file=source_file,
            anchor_line=2,
        )
    graph.build_range_indexes()

    manager = _ReconnectMgr(project_root=project_root, code_graph=graph)
    manager.build_indexes()
    manager._touched_reference_targets.add(target_name)
    manager._reverse_reference_cache[target_name] = []

    stats = manager.reconcile_touched_incoming(changed_files={"changed.py"})

    target_id = graph.name_to_vertex[target_name]
    incoming = [
        graph.graph.es[edge_id]
        for edge_id in graph.graph.incident(target_id, mode="in")
        if graph.graph.es[edge_id]["type"] == EDGE_TYPE_REFERENCE
    ]
    assert stats["incoming_removed"] == 1
    assert stats["incoming_added"] == 0
    assert len(incoming) == 1
    assert incoming[0]["anchor_file"] == "stable.py"


def test_reconnect_incoming_field_anchors_caller_site():
    """A new field must recover uses that lie outside the changed range."""
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
            "type": "field",
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
        e
        for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    ]
    assert len(ref_eids) == 1
    attrs = g.graph.es[ref_eids[0]].attributes()
    assert (
        attrs.get("anchor_file") == "caller.py"
    ), f"anchor_file should be the caller site file; got {attrs}"
    assert (
        attrs.get("anchor_line") == 7
    ), f"anchor_line should be the call line; got {attrs}"


def test_reconnect_incoming_lsp_route_keeps_provider_method_references():
    project_root = "/tmp/recproj"
    graph = _build_caller_target_graph(project_root)
    graph.graph.vs[graph.name_to_vertex["tgt.py:Bar"]]["type"] = "method"
    fake_lsp = _FakeLSPDefRefs(project_root)
    fake_lsp.references = lambda *args, **kwargs: [
        {
            "uri": f"file://{project_root}/caller.py",
            "range": {"start": {"line": 12, "character": 4}},
        }
    ]
    manager = _NoopMgr(
        project_root=project_root,
        code_graph=graph,
        lsp_client=fake_lsp,
    )
    manager.respect_backend_exclusions = False
    manager.symbol_selection_ranges["tgt.py:Bar"] = (1, 0, 1, 3)
    manager.build_indexes()
    manager._reference_resolves_to = lambda *_args: False
    stats = {"incoming_added": 0, "outgoing_added": 0, "unmatched": 0}

    manager.reconnect_incoming("tgt.py", ["tgt.py:Bar"], stats)

    assert stats["incoming_added"] == 1


def test_reconnect_incoming_skips_provider_declaration_location():
    project_root = "/tmp/recproj"
    graph = _build_caller_target_graph(project_root)
    fake_lsp = _FakeLSPDefRefs(project_root)
    fake_lsp.references = lambda *args, **kwargs: [
        {
            "uri": f"file://{project_root}/tgt.py",
            "range": {"start": {"line": 1, "character": 0}},
        }
    ]
    manager = _NoopMgr(
        project_root=project_root,
        code_graph=graph,
        lsp_client=fake_lsp,
    )
    manager.symbol_selection_ranges["tgt.py:Bar"] = (1, 0, 1, 3)
    manager.build_indexes()
    stats = {"incoming_added": 0, "outgoing_added": 0, "unmatched": 0}

    manager.reconnect_incoming("tgt.py", ["tgt.py:Bar"], stats)

    assert stats["incoming_added"] == 0


def test_reference_validation_rejects_other_interface_method():
    project_root = "/tmp/recproj"
    graph = _build_caller_target_graph(project_root)
    graph.graph.vs[graph.name_to_vertex["tgt.py:Bar"]]["type"] = "method"
    graph.add_file_node("other.py")
    graph._add_vertex(
        "other.py:Bar",
        {
            "type": "method",
            "file": "other.py",
            "start_line": 3,
            "end_line": 3,
            "unified_name": "other.py:Bar",
        },
    )
    graph.symbol_ranges["other.py:Bar"] = (3, 3)
    manager = _NoopMgr(project_root=project_root, code_graph=graph)
    manager.build_indexes()
    manager.lsp_client = _FakeLSPDefRefs(
        project_root,
        definitions={
            7: [
                {
                    "targetUri": f"file://{project_root}/other.py",
                    "targetSelectionRange": {"start": {"line": 3, "character": 0}},
                }
            ]
        },
    )
    location = {
        "uri": f"file://{project_root}/caller.py",
        "range": {"start": {"line": 7, "character": 4}},
    }

    assert not manager._reference_resolves_to(location, "tgt.py:Bar")
    assert manager._reference_resolves_to(location, "other.py:Bar")


# ---------------------------------------------------------------------------
# P5: file-wide prepass rebases anchors before shifted/affected vertex updates
# ---------------------------------------------------------------------------
def test_shifted_rebases_outgoing_anchor_lines():
    """When a symbol shifts (body unchanged but start_line moves), the
    patcher's shifted branch must shift every outgoing reference anchor by
    `new_start - old_start` so range queries still match."""
    project_root = "/tmp/shftproj"
    g = _setup_outgoing_with_anchors([12, 18, 25])  # Foo body 10-30
    patcher = _StubPatcher(
        project_root=project_root,
        code_graph=g,
        strict_lsp_requests=True,
    )
    patcher.symbol_selection_ranges["a.py:Foo"] = (10, 4, 10, 7)

    file_stats = {
        "vertices_deleted": 0,
        "vertices_created": 0,
        "vertices_shifted": 0,
        "refs_incoming": 0,
        "refs_outgoing": 0,
        "refs_remapped": 0,
        "refs_unmatched": 0,
    }
    patcher.rebase_reference_anchors_for_file("a.py", lambda line: line + 5)
    patcher._process_shifted(
        uname="a.py:Foo",
        old={"vertex_name": "a.py:Foo", "start_line": 10, "end_line": 30},
        new={
            "start_line": 15,
            "end_line": 35,
            "sel_range": {
                "start": {"line": 15, "character": 4},
                "end": {"line": 15, "character": 7},
            },
        },
        file_path="a.py",
        file_stats=file_stats,
    )

    # Vertex renamed.
    new_vname = "a.py:Foo:15"
    assert new_vname in g.name_to_vertex, "vertex should be renamed"
    assert "a.py:Foo" not in g.name_to_vertex, "old vname should be gone"
    assert g.graph.vs[g.name_to_vertex[new_vname]]["selection_line"] == 15

    # Outgoing anchors shifted by +5.
    src = g.name_to_vertex[new_vname]
    anchor_lines = sorted(
        g.graph.es[e].attributes().get("anchor_line")
        for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    )
    assert anchor_lines == [
        17,
        23,
        30,
    ], f"anchors should shift +5 to [17,23,30]; got {anchor_lines}"
    assert file_stats["vertices_shifted"] == 1
    assert patcher._touched_reference_targets == {new_vname}


def test_affected_preserved_clears_in_range_outgoing():
    """Case 3 (preserved): hunk modifies a single line inside body but
    line count is preserved. Edges anchored in the changed range must be
    deleted; edges outside are kept (will be picked up by Round 2 LSP)."""
    project_root = "/tmp/affproj"
    g = _setup_outgoing_with_anchors([12, 18, 25])  # Foo body 10-30
    patcher = _StubPatcher(
        project_root=project_root,
        code_graph=g,
        strict_lsp_requests=True,
    )
    patcher.symbol_selection_ranges["a.py:Foo"] = (10, 4, 10, 7)

    file_stats = {
        "vertices_deleted": 0,
        "vertices_created": 0,
        "vertices_shifted": 0,
        "vertices_affected_preserved": 0,
        "vertices_affected_rebuilt": 0,
        "refs_incoming": 0,
        "refs_outgoing": 0,
        "refs_remapped": 0,
        "refs_unmatched": 0,
    }
    # Hunk: line 18 (old) → line 18 (new). 1 line in / 1 line out → preserved.
    hunks = [ChangedLineHunk(18, 1, 18, 1)]
    patcher.rebase_reference_anchors_for_file(
        "a.py", lambda line: map_old_line_to_new(line, hunks)
    )
    _new_vname, new_ranges = patcher._process_affected(
        uname="a.py:Foo",
        old={"vertex_name": "a.py:Foo", "start_line": 10, "end_line": 30},
        new={
            "start_line": 10,
            "end_line": 30,
            "sel_range": {
                "start": {"line": 10, "character": 4},
                "end": {"line": 10, "character": 7},
            },
        },
        detailed_hunks=hunks,
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
    assert surviving == [
        12,
        25,
    ], f"only line-18 anchor should be cleared; got {surviving}"
    # Round 2 needs to know which lines to query in the NEW coordinate
    # system; for this hunk that's line 18.
    assert (18, 18) in new_ranges, f"new_changed_ranges missing 18; got {new_ranges}"
    assert file_stats["vertices_affected_preserved"] == 1
    assert g.graph.vs[g.name_to_vertex[new_vname]]["selection_line"] == 10
    assert patcher._touched_reference_targets == {new_vname}


def test_affected_length_changed_rebases_unchanged_outgoing():
    """A length-changing hunk drops edited anchors and maps retained ones."""
    project_root = "/tmp/affproj"
    g = _setup_outgoing_with_anchors([12, 18, 25])  # Foo body 10-30
    patcher = _StubPatcher(project_root=project_root, code_graph=g)
    patcher.symbol_selection_ranges["a.py:Foo"] = (10, 4, 10, 7)

    file_stats = {
        "vertices_deleted": 0,
        "vertices_created": 0,
        "vertices_shifted": 0,
        "vertices_affected_preserved": 0,
        "vertices_affected_rebuilt": 0,
        "refs_incoming": 0,
        "refs_outgoing": 0,
        "refs_remapped": 0,
        "refs_unmatched": 0,
    }
    # Hunk: 1 line at old 18 becomes 4 lines at new 18-21. Length changed.
    hunks = [ChangedLineHunk(18, 1, 18, 4)]
    patcher.rebase_reference_anchors_for_file(
        "a.py", lambda line: map_old_line_to_new(line, hunks)
    )
    _new_vname, new_ranges = patcher._process_affected(
        uname="a.py:Foo",
        old={"vertex_name": "a.py:Foo", "start_line": 10, "end_line": 30},
        new={
            "start_line": 10,
            "end_line": 33,
            "sel_range": {
                "start": {"line": 10, "character": 4},
                "end": {"line": 10, "character": 7},
            },
        },
        detailed_hunks=hunks,
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
    assert surviving == [12, 28]
    assert new_ranges == [(18, 21)]
    assert file_stats["vertices_affected_preserved"] == 1
    assert file_stats["vertices_affected_rebuilt"] == 0


def test_affected_preserved_with_start_line_shift_uniform_rebase():
    """Case 3 + symbol shifted by file-level insertion: anchors outside the
    changed range shift by `delta = new.start - old.start`; in-range
    anchors are deleted."""
    project_root = "/tmp/affproj"
    g = _setup_outgoing_with_anchors([12, 18, 25])  # Foo body 10-30
    patcher = _StubPatcher(project_root=project_root, code_graph=g)
    patcher.symbol_selection_ranges["a.py:Foo"] = (10, 4, 10, 7)

    file_stats = {
        "vertices_deleted": 0,
        "vertices_created": 0,
        "vertices_shifted": 0,
        "vertices_affected_preserved": 0,
        "vertices_affected_rebuilt": 0,
        "refs_incoming": 0,
        "refs_outgoing": 0,
        "refs_remapped": 0,
        "refs_unmatched": 0,
    }
    # 5 lines inserted at top of file → symbol shifts +5; old hunk modifies
    # what was old line 18 (now new line 23). Length preserved.
    hunks = [
        ChangedLineHunk(0, 0, 0, 5),
        ChangedLineHunk(18, 1, 23, 1),
    ]
    patcher.rebase_reference_anchors_for_file(
        "a.py", lambda line: map_old_line_to_new(line, hunks)
    )
    _new_vname, new_ranges = patcher._process_affected(
        uname="a.py:Foo",
        old={"vertex_name": "a.py:Foo", "start_line": 10, "end_line": 30},
        new={
            "start_line": 15,
            "end_line": 35,
            "sel_range": {
                "start": {"line": 15, "character": 4},
                "end": {"line": 15, "character": 7},
            },
        },
        detailed_hunks=hunks,
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
    assert surviving == [
        17,
        30,
    ], f"expected [17, 30] (uniform +5 shift, line-18 deleted); got {surviving}"
    assert (23, 23) in new_ranges
    assert file_stats["vertices_affected_preserved"] == 1


def test_incremental_connect_skips_affected_symbol_without_new_ranges(monkeypatch):
    g = _setup_outgoing_with_anchors([12])
    patcher = _StubPatcher(project_root="/tmp/affproj", code_graph=g)

    def fail_reconnect(*args, **kwargs):
        raise AssertionError("empty changed ranges must not trigger a full-file query")

    monkeypatch.setattr(patcher, "reconnect_outgoing", fail_reconnect)
    stats = patcher._incremental_connect_edges(
        "a.py",
        {
            "affected_vnames": ["a.py:Foo"],
            "affected_changed_ranges": [[]],
            "added_vnames": [],
            "file_scope_changed_ranges": [],
        },
    )

    assert stats == {
        "refs_incoming": 0,
        "refs_outgoing": 0,
        "refs_unmatched": 0,
    }


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
            "caller.py:Caller",
            "tgt.py:Bar",
            "caller.py:Caller",
            "tgt.py:Bar",
            "caller.py",
            7,
        )
    ]
    anchor_shifts = {
        "caller.py": [(5, 15, 5)],  # (old_start, old_end, delta)
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
        e
        for e in g.graph.incident(src, mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    ]
    assert len(ref_eids) == 1
    attrs = g.graph.es[ref_eids[0]].attributes()
    assert (
        attrs.get("anchor_line") == 12
    ), f"anchor_line should rebase 7 + 5 = 12; got {attrs}"


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
        severed_incoming=[
            (
                "caller.py:Caller",
                "tgt.py:Bar",
                "caller.py:Caller",
                "tgt.py:Bar",
                "caller.py",
                7,
            )
        ],
        severed_outgoing=[],
    )
    assert remapped == 1
    src = g.name_to_vertex["caller.py:Caller"]
    ref_eids = [
        e
        for e in g.graph.incident(src, mode="out")
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
            "caller.py:Caller",  # src_name (STALE — already renamed)
            "tgt.py:Bar",  # tgt_name
            "caller.py:Caller",  # src_uname (stable)
            "tgt.py:Bar",  # tgt_uname
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
        e
        for e in g.graph.incident(src, mode="out")
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
        "vertices_deleted": 0,
        "vertices_created": 0,
        "vertices_shifted": 0,
        "refs_incoming": 0,
        "refs_outgoing": 0,
        "refs_remapped": 0,
        "refs_unmatched": 0,
    }
    patcher._process_shifted(
        uname="a.py:Foo",
        old={"vertex_name": "a.py:Foo", "start_line": 10, "end_line": 30},
        new={
            "start_line": 10,
            "end_line": 32,
            "sel_range": {
                "start": {"line": 10, "character": 4},
                "end": {"line": 10, "character": 7},
            },
        },
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


def test_edge_batch_flush_invalidates_edge_index():
    """Regression: EdgeBatch.flush bypasses CodeGraph._add_edge; if it leaves
    a stale _edge_index in place, a follow-up _add_edge for one of the
    flushed keys will think the edge doesn't exist and create a duplicate.
    """
    from codeminer.graph.incremental.subgraph_mgr import EdgeBatch

    g = CodeGraph(project_root="/tmp/x")
    g.add_file_node("a.py")
    g._add_vertex(
        "src",
        {
            "type": "function",
            "file": "a.py",
            "start_line": 0,
            "end_line": 9,
            "unified_name": "a.py:src",
        },
    )
    g._add_vertex(
        "tgt",
        {
            "type": "function",
            "file": "a.py",
            "start_line": 10,
            "end_line": 19,
            "unified_name": "a.py:tgt",
        },
    )

    # Force the lazy _edge_index to materialise via a normal _add_edge.
    g._add_edge("src", "tgt", EDGE_TYPE_REFERENCE, anchor_file="a.py", anchor_line=1)
    assert g._edge_index is not None  # built lazily on the call above

    # Stage and flush a *different* anchor via EdgeBatch.
    batch = EdgeBatch(g)
    staged = batch.stage(
        "src", "tgt", EDGE_TYPE_REFERENCE, anchor_file="a.py", anchor_line=2
    )
    assert staged is True
    batch.flush()

    ref_eids_before = [
        e
        for e in g.graph.incident(g.name_to_vertex["src"], mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    ]
    assert len(ref_eids_before) == 2

    # Re-add the flushed key via _add_edge — must dedup, not duplicate.
    g._add_edge("src", "tgt", EDGE_TYPE_REFERENCE, anchor_file="a.py", anchor_line=2)
    ref_eids_after = [
        e
        for e in g.graph.incident(g.name_to_vertex["src"], mode="out")
        if g.graph.es[e]["type"] == EDGE_TYPE_REFERENCE
    ]
    assert len(ref_eids_after) == 2, (
        "EdgeBatch.flush left _edge_index stale: a follow-up _add_edge "
        "for the same anchor created a duplicate"
    )


def test_merge_stats_keeps_affected_vertex_counts():
    total = {}

    PatcherBase._merge_stats(
        total,
        {
            "vertices_affected_preserved": 3,
            "vertices_affected_rebuilt": 1,
        },
    )

    assert total == {
        "vertices_affected_preserved": 3,
        "vertices_affected_rebuilt": 1,
    }


def test_parent_remains_affected_when_child_body_changes():
    patcher = _StubPatcher("/tmp", CodeGraph(project_root="/tmp"))
    old = {
        "a.py:Service": {"start_line": 1, "end_line": 20},
        "a.py:Service.run()": {"start_line": 5, "end_line": 10},
    }
    new = {
        "a.py:Service": {"start_line": 1, "end_line": 21},
        "a.py:Service.run()": {"start_line": 5, "end_line": 11},
    }

    classified = patcher._classify_symbols(old, new, [(7, 7, 7, 8)])

    assert classified["affected"] == ["a.py:Service", "a.py:Service.run()"]
    assert classified["shifted"] == []


def test_file_anchor_rebase_does_not_trust_source_vertex_file():
    graph = CodeGraph(project_root="/tmp")
    graph.add_file_node("changed.go")
    graph.add_file_node("other.go")
    graph._add_vertex(
        "other.go:init",
        {
            "type": "function",
            "file": "other.go",
            "start_line": 1,
            "end_line": 3,
            "unified_name": "init()",
        },
    )
    graph._add_vertex(
        "target.go:Target",
        {
            "type": "class",
            "file": "target.go",
            "start_line": 1,
            "end_line": 2,
            "unified_name": "Target",
        },
    )
    graph._add_edge(
        "other.go:init",
        "target.go:Target",
        EDGE_TYPE_REFERENCE,
        anchor_file="changed.go",
        anchor_line=10,
    )
    manager = _NoopMgr("/tmp", graph)

    moved, removed = manager.rebase_reference_anchors_for_file(
        "changed.go", lambda line: line + 1
    )

    assert (moved, removed) == (1, 0)
    anchors = [
        edge["anchor_line"]
        for edge in graph.graph.es
        if edge["type"] == EDGE_TYPE_REFERENCE
        and edge.attributes().get("anchor_file") == "changed.go"
    ]
    assert anchors == [11]


def test_file_anchor_invalidation_tracks_targets_for_reconciliation():
    graph = _setup_two_calls_one_target()
    manager = _NoopMgr(project_root="/tmp/x", code_graph=graph)

    moved, removed = manager.rebase_reference_anchors_for_file(
        "caller.py",
        lambda _line: None,
        reconcile_removed_targets=True,
    )

    assert (moved, removed) == (0, 2)
    assert manager._touched_reference_targets == {"Target"}
