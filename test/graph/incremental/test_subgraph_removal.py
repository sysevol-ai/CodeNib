# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for SubgraphMgr subgraph deletion and index building."""

from codeminer.graph.code_graph import CodeGraph
from codeminer.graph.incremental.patcher_rust import PatcherRust
from codeminer.types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_CLASS,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    ROOT_NODE,
)


def _build_two_file_graph():
    """Build a small graph with 2 files, cross-file references.

    Structure:
        . (root)
        ├── src/main.rs  (file)
        │   ├── main()     (function, lines 0-10)
        │   └── Config     (class, lines 12-20)
        └── src/lib.rs   (file)
            ├── run()      (function, lines 0-5)
            └── Router     (class, lines 7-30)
                └── handle()  (method, lines 8-25)

    Cross-file references:
        main() → Router       (main.rs references lib.rs)
        main() → run()        (main.rs references lib.rs)
        handle() → Config     (lib.rs references main.rs)
    """
    g = CodeGraph(project_root="/tmp/test_project")
    g.add_root_node(ROOT_NODE)

    g.add_file_node("src/main.rs")
    g._add_edge(ROOT_NODE, "src/main.rs", EDGE_TYPE_CONTAIN)

    g.add_symbol_node(
        "main_fn",
        line=0,
        scope_start_line=0,
        scope_end_line=10,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    g.graph.vs[g.name_to_vertex["main_fn"]]["file"] = "src/main.rs"
    g._add_edge("src/main.rs", "main_fn", EDGE_TYPE_CONTAIN)

    g.add_symbol_node(
        "Config",
        line=12,
        scope_start_line=12,
        scope_end_line=20,
        symbol_type=NODE_TYPE_CLASS,
    )
    g.graph.vs[g.name_to_vertex["Config"]]["file"] = "src/main.rs"
    g._add_edge("src/main.rs", "Config", EDGE_TYPE_CONTAIN)

    g.add_file_node("src/lib.rs")
    g._add_edge(ROOT_NODE, "src/lib.rs", EDGE_TYPE_CONTAIN)

    g.add_symbol_node(
        "run_fn",
        line=0,
        scope_start_line=0,
        scope_end_line=5,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    g.graph.vs[g.name_to_vertex["run_fn"]]["file"] = "src/lib.rs"
    g._add_edge("src/lib.rs", "run_fn", EDGE_TYPE_CONTAIN)

    g.add_symbol_node(
        "Router",
        line=7,
        scope_start_line=7,
        scope_end_line=30,
        symbol_type=NODE_TYPE_CLASS,
    )
    g.graph.vs[g.name_to_vertex["Router"]]["file"] = "src/lib.rs"
    g._add_edge("src/lib.rs", "Router", EDGE_TYPE_CONTAIN)

    g.add_symbol_node(
        "handle_fn",
        line=8,
        scope_start_line=8,
        scope_end_line=25,
        symbol_type=NODE_TYPE_METHOD,
    )
    g.graph.vs[g.name_to_vertex["handle_fn"]]["file"] = "src/lib.rs"
    g._add_edge("Router", "handle_fn", EDGE_TYPE_CONTAIN)

    g._add_edge("main_fn", "Router", EDGE_TYPE_REFERENCE)
    g._add_edge("main_fn", "run_fn", EDGE_TYPE_REFERENCE)
    g._add_edge("handle_fn", "Config", EDGE_TYPE_REFERENCE)

    return g


def _make_mgr(g):
    """Create a PatcherRust (which is a SubgraphMgr) for testing."""
    mgr = PatcherRust("/tmp/test_project", g)
    return mgr


class TestBuildIndexes:
    def test_build_file_to_vertices_index(self):
        g = _build_two_file_graph()
        mgr = _make_mgr(g)
        mgr.build_indexes()

        assert "src/main.rs" in g.file_to_vertices
        assert "src/lib.rs" in g.file_to_vertices

        main_vids = g.file_to_vertices["src/main.rs"]
        main_names = {g.graph.vs[vid]["name"] for vid in main_vids}
        assert "src/main.rs" in main_names
        assert "main_fn" in main_names
        assert "Config" in main_names

        lib_vids = g.file_to_vertices["src/lib.rs"]
        lib_names = {g.graph.vs[vid]["name"] for vid in lib_vids}
        assert "src/lib.rs" in lib_names
        assert "run_fn" in lib_names
        assert "Router" in lib_names
        assert "handle_fn" in lib_names

    def test_build_unified_name_index(self):
        g = _build_two_file_graph()
        g.graph.vs[g.name_to_vertex["main_fn"]]["unified_name"] = "src/main.rs:main()"
        g.graph.vs[g.name_to_vertex["Config"]]["unified_name"] = "src/main.rs:Config"
        g.graph.vs[g.name_to_vertex["Router"]]["unified_name"] = "src/lib.rs:Router"

        mgr = _make_mgr(g)
        mgr.build_indexes()

        assert "src/main.rs:main()" in g.unified_name_to_vertex
        assert g.unified_name_to_vertex["src/main.rs:main()"] == [
            g.name_to_vertex["main_fn"]
        ]
        assert "src/lib.rs:Router" in g.unified_name_to_vertex

    def test_build_unified_name_index_one_to_many(self):
        g = CodeGraph()
        g._add_vertex(
            "scip_key_1", {"type": NODE_TYPE_FUNCTION, "unified_name": "f.rs:new()"}
        )
        g._add_vertex(
            "scip_key_2", {"type": NODE_TYPE_FUNCTION, "unified_name": "f.rs:new()"}
        )
        mgr = _make_mgr(g)
        mgr.build_indexes()

        assert len(g.unified_name_to_vertex["f.rs:new()"]) == 2


class TestDeleteFileSubgraph:
    def test_delete_removes_correct_vertices(self):
        g = _build_two_file_graph()
        mgr = _make_mgr(g)
        mgr.build_indexes()

        initial_vcount = g.graph.vcount()
        result = mgr.delete_file_subgraph("src/main.rs")

        assert set(result["deleted_vertex_names"]) == {
            "src/main.rs",
            "main_fn",
            "Config",
        }
        assert g.graph.vcount() == initial_vcount - 3

    def test_delete_cleans_name_to_vertex(self):
        g = _build_two_file_graph()
        mgr = _make_mgr(g)
        mgr.build_indexes()
        mgr.delete_file_subgraph("src/main.rs")

        assert "src/main.rs" not in g.name_to_vertex
        assert "main_fn" not in g.name_to_vertex
        assert "Config" not in g.name_to_vertex

        for name, vid in g.name_to_vertex.items():
            assert g.graph.vs[vid]["name"] == name

    def test_delete_cleans_symbol_ranges(self):
        g = _build_two_file_graph()
        mgr = _make_mgr(g)
        mgr.build_indexes()

        assert "main_fn" in g.symbol_ranges
        assert "Config" in g.symbol_ranges

        mgr.delete_file_subgraph("src/main.rs")

        assert "main_fn" not in g.symbol_ranges
        assert "Config" not in g.symbol_ranges
        assert "run_fn" in g.symbol_ranges
        assert "Router" in g.symbol_ranges

    def test_delete_reports_severed_outgoing_refs(self):
        g = _build_two_file_graph()
        mgr = _make_mgr(g)
        mgr.build_indexes()
        result = mgr.delete_file_subgraph("src/main.rs")

        outgoing_names = {(e[0], e[1]) for e in result["severed_outgoing_refs"]}
        assert ("main_fn", "Router") in outgoing_names
        assert ("main_fn", "run_fn") in outgoing_names

    def test_delete_reports_severed_incoming_refs(self):
        g = _build_two_file_graph()
        mgr = _make_mgr(g)
        mgr.build_indexes()
        result = mgr.delete_file_subgraph("src/main.rs")

        incoming_names = {(e[0], e[1]) for e in result["severed_incoming_refs"]}
        assert ("handle_fn", "Config") in incoming_names

    def test_delete_preserves_remaining_graph(self):
        g = _build_two_file_graph()
        mgr = _make_mgr(g)
        mgr.build_indexes()
        mgr.delete_file_subgraph("src/main.rs")

        assert "src/lib.rs" in g.name_to_vertex
        assert "run_fn" in g.name_to_vertex
        assert "Router" in g.name_to_vertex
        assert "handle_fn" in g.name_to_vertex
        assert ROOT_NODE in g.name_to_vertex

        lib_vid = g.name_to_vertex["src/lib.rs"]
        router_vid = g.name_to_vertex["Router"]
        handle_vid = g.name_to_vertex["handle_fn"]
        assert g.graph.are_adjacent(lib_vid, router_vid)
        assert g.graph.are_adjacent(router_vid, handle_vid)

    def test_delete_cleans_file_to_vertices_index(self):
        g = _build_two_file_graph()
        mgr = _make_mgr(g)
        mgr.build_indexes()
        mgr.delete_file_subgraph("src/main.rs")

        assert "src/main.rs" not in g.file_to_vertices
        assert "src/lib.rs" in g.file_to_vertices

    def test_delete_cleans_unified_name_index(self):
        g = _build_two_file_graph()
        g.graph.vs[g.name_to_vertex["main_fn"]]["unified_name"] = "src/main.rs:main()"
        g.graph.vs[g.name_to_vertex["Router"]]["unified_name"] = "src/lib.rs:Router"
        mgr = _make_mgr(g)
        mgr.build_indexes()

        mgr.delete_file_subgraph("src/main.rs")

        assert "src/main.rs:main()" not in g.unified_name_to_vertex
        assert "src/lib.rs:Router" in g.unified_name_to_vertex

    def test_delete_nonexistent_file(self):
        g = _build_two_file_graph()
        mgr = _make_mgr(g)
        result = mgr.delete_file_subgraph("nonexistent.rs")

        assert result["deleted_vertex_names"] == []
        assert result["severed_incoming_refs"] == []
        assert result["severed_outgoing_refs"] == []
        assert g.graph.vcount() == 8

    def test_delete_without_index(self):
        g = _build_two_file_graph()
        mgr = _make_mgr(g)
        result = mgr.delete_file_subgraph("src/main.rs")

        assert set(result["deleted_vertex_names"]) == {
            "src/main.rs",
            "main_fn",
            "Config",
        }

    def test_name_to_vertex_consistency_after_delete(self):
        g = _build_two_file_graph()
        mgr = _make_mgr(g)
        mgr.build_indexes()
        mgr.delete_file_subgraph("src/main.rs")

        for name, vid in g.name_to_vertex.items():
            assert vid < g.graph.vcount(), f"vid {vid} out of range for {name}"
            assert g.graph.vs[vid]["name"] == name

    def test_delete_both_files(self):
        g = _build_two_file_graph()
        mgr = _make_mgr(g)
        mgr.build_indexes()
        mgr.delete_file_subgraph("src/main.rs")
        mgr.delete_file_subgraph("src/lib.rs")

        assert g.graph.vcount() == 1
        assert ROOT_NODE in g.name_to_vertex
