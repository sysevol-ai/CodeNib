# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for LSPDecoderBase location matching logic (used by reference reconnection)."""

from codeminer.graph.code_graph import CodeGraph
from codeminer.types import (
    EDGE_TYPE_CONTAIN,
    NODE_TYPE_CLASS,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
)


def _make_decoder():
    """Create a concrete decoder with a mock graph (no actual LSP client needed)."""
    from codeminer.graph.incremental.patcher_rust import PatcherRust

    g = CodeGraph(project_root="/tmp/test")

    # Build a graph with known symbol_ranges
    g.add_file_node("src/lib.rs")
    g._add_vertex(
        "src/lib.rs:Router:5",
        {
            "type": NODE_TYPE_CLASS,
            "file": "src/lib.rs",
            "start_line": 5,
            "end_line": 50,
            "unified_name": "src/lib.rs:Router",
        },
    )
    g.symbol_ranges["src/lib.rs:Router:5"] = (5, 50)
    g._add_edge("src/lib.rs", "src/lib.rs:Router:5", EDGE_TYPE_CONTAIN)

    g._add_vertex(
        "src/lib.rs:Router.handle():10",
        {
            "type": NODE_TYPE_METHOD,
            "file": "src/lib.rs",
            "start_line": 10,
            "end_line": 30,
            "unified_name": "src/lib.rs:Router.handle()",
        },
    )
    g.symbol_ranges["src/lib.rs:Router.handle():10"] = (10, 30)
    g._add_edge(
        "src/lib.rs:Router:5", "src/lib.rs:Router.handle():10", EDGE_TYPE_CONTAIN
    )

    g._add_vertex(
        "src/lib.rs:Router.new():35",
        {
            "type": NODE_TYPE_METHOD,
            "file": "src/lib.rs",
            "start_line": 35,
            "end_line": 45,
            "unified_name": "src/lib.rs:Router.new()",
        },
    )
    g.symbol_ranges["src/lib.rs:Router.new():35"] = (35, 45)
    g._add_edge("src/lib.rs:Router:5", "src/lib.rs:Router.new():35", EDGE_TYPE_CONTAIN)

    g.add_file_node("src/main.rs")
    g._add_vertex(
        "src/main.rs:main():0",
        {
            "type": NODE_TYPE_FUNCTION,
            "file": "src/main.rs",
            "start_line": 0,
            "end_line": 20,
            "unified_name": "src/main.rs:main()",
        },
    )
    g.symbol_ranges["src/main.rs:main():0"] = (0, 20)
    g._add_edge("src/main.rs", "src/main.rs:main():0", EDGE_TYPE_CONTAIN)

    decoder = PatcherRust("/tmp/test", g)
    # match_location_to_* needs file_to_vertices, populated by build_indexes()
    decoder.build_indexes()
    return decoder, g


class TestMatchLocationToVertex:
    def test_exact_start_line_match(self):
        decoder, _ = _make_decoder()
        result = decoder.match_location_to_vertex("src/lib.rs", 10)
        assert result == "src/lib.rs:Router.handle():10"

    def test_exact_match_class(self):
        decoder, _ = _make_decoder()
        result = decoder.match_location_to_vertex("src/lib.rs", 5)
        assert result == "src/lib.rs:Router:5"

    def test_no_exact_match_falls_to_scope(self):
        decoder, _ = _make_decoder()
        # Line 20 is inside Router.handle() (10-30)
        result = decoder.match_location_to_vertex("src/lib.rs", 20)
        assert result == "src/lib.rs:Router.handle():10"

    def test_wrong_file_returns_none(self):
        decoder, _ = _make_decoder()
        result = decoder.match_location_to_vertex("nonexistent.rs", 10)
        assert result is None


class TestMatchLocationToScope:
    def test_innermost_scope(self):
        decoder, _ = _make_decoder()
        # Line 15 is inside Router.handle() (10-30) which is inside Router (5-50)
        # Should return the innermost: Router.handle()
        result = decoder.match_location_to_scope("src/lib.rs", 15)
        assert result == "src/lib.rs:Router.handle():10"

    def test_outer_scope(self):
        decoder, _ = _make_decoder()
        # Line 48 is inside Router (5-50) but outside handle/new
        result = decoder.match_location_to_scope("src/lib.rs", 48)
        assert result == "src/lib.rs:Router:5"

    def test_outside_all_scopes_fallback_to_file(self):
        decoder, _ = _make_decoder()
        # Line 55 is outside all symbol ranges
        result = decoder.match_location_to_scope("src/lib.rs", 55)
        assert result == "src/lib.rs"

    def test_different_file(self):
        decoder, _ = _make_decoder()
        result = decoder.match_location_to_scope("src/main.rs", 5)
        assert result == "src/main.rs:main():0"

    def test_file_not_in_graph(self):
        decoder, _ = _make_decoder()
        result = decoder.match_location_to_scope("unknown.rs", 5)
        assert result is None
