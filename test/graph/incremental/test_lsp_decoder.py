# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for LSP message decoding across all 4 message types.

Tests the full decode pipeline for each LSP message format:
  - documentSymbol → rebuild_file_subgraph → vertices + containment edges
  - semanticTokens → decode_semantic_tokens → filtered token list
  - definition → location resolution → target vertex matching
  - references → location resolution → incoming edge building

Uses mock LSP responses (no actual LSP server needed).
"""

from codenib.graph.code_graph import CodeGraph
from codenib.graph.incremental.patcher_go import PatcherGo
from codenib.graph.incremental.patcher_python import PatcherPython
from codenib.graph.incremental.patcher_rust import PatcherRust
from codenib.graph.incremental.patcher_ts import PatcherTS

# ═══════════════════════════════════════════════════════════════
# Helpers: mock LSP responses
# ═══════════════════════════════════════════════════════════════


def _sym(name, kind, start, end, children=None):
    """Build a documentSymbol response dict."""
    sym = {
        "name": name,
        "kind": kind,
        "range": {
            "start": {"line": start, "character": 0},
            "end": {"line": end, "character": 0},
        },
        "selectionRange": {
            "start": {"line": start, "character": 4},
            "end": {"line": start, "character": 4 + len(name)},
        },
    }
    if children:
        sym["children"] = children
    return sym


def _semantic_tokens_data(tokens_spec):
    """Build semanticTokens response data from a list of token specs.

    Each spec is (delta_line, delta_char, length, type_idx, mod_bits).
    """
    data = []
    for spec in tokens_spec:
        data.extend(spec)
    return {"data": data}


# ═══════════════════════════════════════════════════════════════
# 1. documentSymbol decoding
# ═══════════════════════════════════════════════════════════════


class TestDocumentSymbol:
    """Test documentSymbol → rebuild_file_subgraph → graph vertices."""

    def test_rust_flat_functions(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        symbols = [
            _sym("add", 12, 0, 5),  # Function
            _sym("multiply", 12, 7, 12),  # Function
        ]
        created = p.rebuild_file_subgraph("src/math.rs", symbols)

        assert len(created) == 2
        assert "src/math.rs:add():0" in created
        assert "src/math.rs:multiply():7" in created
        assert "src/math.rs" in g.name_to_vertex

    def test_rust_struct_with_methods(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        symbols = [
            _sym(
                "Router",
                23,
                5,
                50,
                children=[
                    _sym("handle", 6, 10, 30),
                    _sym("new", 6, 35, 45),
                ],
            ),
            _sym("run", 12, 55, 60),
        ]
        created = p.rebuild_file_subgraph("src/lib.rs", symbols)

        assert len(created) == 4
        assert "src/lib.rs:Router:5" in created
        assert "src/lib.rs:Router.handle():10" in created
        assert "src/lib.rs:Router.new():35" in created
        assert "src/lib.rs:run():55" in created

        # Containment edges
        assert g.graph.are_adjacent(
            g.name_to_vertex["src/lib.rs"],
            g.name_to_vertex["src/lib.rs:Router:5"],
        )
        assert g.graph.are_adjacent(
            g.name_to_vertex["src/lib.rs:Router:5"],
            g.name_to_vertex["src/lib.rs:Router.handle():10"],
        )

    def test_rust_impl_block_naming(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        symbols = [
            _sym(
                "impl Violation for EqWithoutHash",
                19,
                50,
                80,
                children=[
                    _sym("message", 6, 55, 70),
                ],
            ),
        ]
        created = p.rebuild_file_subgraph("src/rules.rs", symbols)

        assert "src/rules.rs:EqWithoutHash<Violation>.message():55" in created

    def test_rust_mod_transparent(self):
        """mod tests is transparent — children don't get 'tests.' prefix."""
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        symbols = [
            _sym(
                "tests",
                2,
                40,
                80,
                children=[  # Module
                    _sym("test_add", 12, 42, 50),
                    _sym("test_mul", 12, 52, 60),
                ],
            ),
        ]
        created = p.rebuild_file_subgraph("src/math.rs", symbols)

        assert "src/math.rs:test_add():42" in created
        assert "src/math.rs:test_mul():52" in created

    def test_go_pointer_receiver(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherGo("/tmp", g)
        symbols = [
            _sym("(*Handler).ServeHTTP", 6, 10, 50),
        ]
        created = p.rebuild_file_subgraph("server.go", symbols)

        assert "server.go:Handler.ServeHTTP():10" in created

    def test_ts_constructor(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherTS("/tmp", g)
        symbols = [
            _sym(
                "Axios",
                5,
                0,
                100,
                children=[
                    _sym("<constructor>", 9, 10, 30),
                    _sym("request", 6, 35, 80),
                ],
            ),
        ]
        created = p.rebuild_file_subgraph("index.ts", symbols)

        assert "index.ts:Axios:0" in created
        assert "index.ts:Axios.constructor():10" in created
        assert "index.ts:Axios.request():35" in created
        attrs = g.get_node_info_by_name("index.ts:Axios:0")
        assert attrs["type"] == "class"
        assert attrs["symbol_kind"] == "class"
        assert attrs["has_definition"] is True
        assert attrs["selection_line"] == 0

    def test_python_class_method(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherPython("/tmp", g)
        symbols = [
            _sym(
                "KMeans",
                5,
                10,
                200,
                children=[
                    _sym("fit", 6, 20, 80),
                    _sym("predict", 6, 90, 150),
                ],
            ),
        ]
        created = p.rebuild_file_subgraph("cluster.py", symbols)

        assert "cluster.py:KMeans:10" in created
        assert "cluster.py:KMeans.fit():20" in created
        assert "cluster.py:KMeans.predict():90" in created

    def test_selection_range_stored(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        symbols = [_sym("main", 12, 0, 10)]
        p.rebuild_file_subgraph("src/main.rs", symbols)

        vname = "src/main.rs:main():0"
        assert vname in p.symbol_selection_ranges
        assert p.symbol_selection_ranges[vname][0] == 0  # start line
        assert p.symbol_selection_ranges[vname][1] == 4  # start char


# ═══════════════════════════════════════════════════════════════
# 2. semanticTokens decoding
# ═══════════════════════════════════════════════════════════════


class _MockClient:
    """Mock LSP client with semantic tokens legend."""

    semantic_tokens_legend = {
        "tokenTypes": [
            "namespace",
            "type",
            "class",
            "function",
            "method",
            "property",
            "variable",
            "parameter",
            "keyword",
        ],
        "tokenModifiers": [
            "declaration",
            "definition",
            "readonly",
        ],
    }

    def _abs_path(self, p):
        return p

    def decode_semantic_tokens(self, response, file_path):
        from codenib.graph.incremental.lsp_client import LSPClient

        return LSPClient.decode_semantic_tokens(self, response, file_path)


class TestSemanticTokensDecode:
    """Test semanticTokens response → decoded token list."""

    def test_basic_decode(self, tmp_path):
        src = tmp_path / "test.rs"
        src.write_text("fn add(a: i32) {\n    a + 1\n}\n")

        client = _MockClient()
        client._abs_path = lambda p: str(src)

        response = _semantic_tokens_data(
            [
                (0, 0, 2, 8, 0),  # L0:0 "fn" keyword
                (0, 3, 3, 3, 2),  # L0:3 "add" function, definition
                (0, 4, 1, 7, 1),  # L0:7 "a" parameter, declaration
            ]
        )

        tokens = client.decode_semantic_tokens(response, str(src))

        assert len(tokens) == 3
        assert tokens[0]["token_type"] == "keyword"
        assert tokens[0]["text"] == "fn"
        assert tokens[1]["token_type"] == "function"
        assert tokens[1]["text"] == "add"
        assert "definition" in tokens[1]["modifiers"]
        assert tokens[2]["token_type"] == "parameter"
        assert "declaration" in tokens[2]["modifiers"]

    def test_multiline_tokens(self, tmp_path):
        src = tmp_path / "test.rs"
        src.write_text("use std::io;\nfn main() {}\n")

        client = _MockClient()
        client._abs_path = lambda p: str(src)

        response = _semantic_tokens_data(
            [
                (0, 4, 3, 0, 0),  # L0:4 "std" namespace
                (1, 3, 4, 3, 2),  # L1:3 "main" function, definition
            ]
        )

        tokens = client.decode_semantic_tokens(response, str(src))

        assert len(tokens) == 2
        assert tokens[0]["line"] == 0
        assert tokens[0]["text"] == "std"
        assert tokens[1]["line"] == 1
        assert tokens[1]["text"] == "main"

    def test_crossfile_types_rust(self):
        p = PatcherRust("/tmp", CodeGraph())
        types = p._get_crossfile_token_types()
        assert "function" in types
        assert "method" in types
        assert "property" in types
        assert "keyword" not in types
        assert "parameter" not in types

    def test_crossfile_types_ts_includes_variable(self):
        p = PatcherTS("/tmp", CodeGraph())
        types = p._get_crossfile_token_types()
        assert "variable" in types

    def test_crossfile_types_python_excludes_variable(self):
        p = PatcherPython("/tmp", CodeGraph())
        types = p._get_crossfile_token_types()
        assert "variable" not in types


# ═══════════════════════════════════════════════════════════════
# 3. definition response → vertex matching
# ═══════════════════════════════════════════════════════════════


class TestDefinitionDecode:
    def test_match_exact_line(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        p.rebuild_file_subgraph(
            "src/lib.rs",
            [
                _sym(
                    "Router",
                    23,
                    5,
                    50,
                    children=[
                        _sym("handle", 6, 10, 30),
                    ],
                ),
            ],
        )

        result = p.match_location_to_vertex("src/lib.rs", 10)
        assert result == "src/lib.rs:Router.handle():10"

    def test_match_within_scope(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        p.rebuild_file_subgraph(
            "src/lib.rs",
            [
                _sym(
                    "Router",
                    23,
                    5,
                    50,
                    children=[
                        _sym("handle", 6, 10, 30),
                    ],
                ),
            ],
        )

        result = p.match_location_to_vertex("src/lib.rs", 20)
        assert result == "src/lib.rs:Router.handle():10"

    def test_match_class_scope(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        p.rebuild_file_subgraph(
            "src/lib.rs",
            [
                _sym(
                    "Router",
                    23,
                    5,
                    50,
                    children=[
                        _sym("handle", 6, 10, 30),
                    ],
                ),
            ],
        )

        result = p.match_location_to_scope("src/lib.rs", 40)
        assert result == "src/lib.rs:Router:5"

    def test_fallback_to_file(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        p.rebuild_file_subgraph(
            "src/lib.rs",
            [
                _sym("run", 12, 5, 10),
            ],
        )

        result = p.match_location_to_scope("src/lib.rs", 20)
        assert result == "src/lib.rs"

    def test_cross_file(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        p.rebuild_file_subgraph("src/lib.rs", [_sym("Router", 23, 5, 50)])
        p.rebuild_file_subgraph("src/main.rs", [_sym("main", 12, 0, 10)])

        result = p.match_location_to_vertex("src/lib.rs", 5)
        assert result == "src/lib.rs:Router:5"

    def test_unknown_file(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        assert p.match_location_to_vertex("unknown.rs", 10) is None


# ═══════════════════════════════════════════════════════════════
# 4. references response → scope matching
# ═══════════════════════════════════════════════════════════════


class TestReferencesDecode:
    def test_scope_inside_function(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        p.rebuild_file_subgraph(
            "src/main.rs",
            [
                _sym("main", 12, 0, 20),
                _sym("helper", 12, 25, 40),
            ],
        )

        assert p.match_location_to_scope("src/main.rs", 5) == "src/main.rs:main():0"
        assert p.match_location_to_scope("src/main.rs", 30) == "src/main.rs:helper():25"

    def test_scope_at_file_level(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        p.rebuild_file_subgraph(
            "src/main.rs",
            [
                _sym("main", 12, 5, 20),
            ],
        )

        assert p.match_location_to_scope("src/main.rs", 2) == "src/main.rs"

    def test_innermost_scope_wins(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        p.rebuild_file_subgraph(
            "src/lib.rs",
            [
                _sym(
                    "Router",
                    23,
                    0,
                    100,
                    children=[
                        _sym(
                            "handle",
                            6,
                            10,
                            50,
                            children=[
                                _sym("inner", 12, 20, 30),
                            ],
                        ),
                    ],
                ),
            ],
        )

        scope = p.match_location_to_scope("src/lib.rs", 25)
        assert scope == "src/lib.rs:Router.handle().inner():20"


# ═══════════════════════════════════════════════════════════════
# 5. flatten_symbols (documentSymbol → classify-ready dict)
# ═══════════════════════════════════════════════════════════════


class TestFlattenSymbols:
    def test_filters_local_variables(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        symbols = [
            _sym(
                "add",
                12,
                0,
                10,
                children=[
                    _sym("result", 13, 2, 2),  # Variable inside function
                ],
            ),
            _sym("MAX_SIZE", 14, 12, 12),  # Constant at top level
        ]
        result = p.flatten_symbols("src/lib.rs", symbols)

        assert "src/lib.rs:add()" in result
        assert "src/lib.rs:result" not in result
        assert "src/lib.rs:MAX_SIZE" in result

    def test_struct_over_impl(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        symbols = [
            _sym("Config", 23, 5, 50),
            _sym("impl Config", 19, 55, 80),
        ]
        result = p.flatten_symbols("src/config.rs", symbols)

        assert "src/config.rs:Config" in result
        assert result["src/config.rs:Config"]["start_line"] == 5
        assert result["src/config.rs:Config"]["end_line"] == 50

    def test_go_pointer_receiver(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherGo("/tmp", g)
        symbols = [
            _sym("Handler", 23, 0, 5),
            _sym("(*Handler).ServeHTTP", 6, 10, 50),
        ]
        result = p.flatten_symbols("server.go", symbols)

        assert "server.go:Handler" in result
        assert "server.go:Handler.ServeHTTP()" in result
