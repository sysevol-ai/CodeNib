# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
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

from pathlib import Path

from codeminer.graph.code_graph import CodeGraph
from codeminer.graph.incremental.change_mgr import ChangedLineHunk
from codeminer.graph.incremental.patcher_go import PatcherGo
from codeminer.graph.incremental.patcher_python import PatcherPython
from codeminer.graph.incremental.patcher_rust import PatcherRust
from codeminer.graph.incremental.patcher_ts import PatcherTS
from codeminer.ls_index.lsp_graph_decode import GenericLSPGraphDecoder
from codeminer.types import (
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_FIELD,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
)

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


class _BatchReferencesMixin:
    """Expose the production client's ordered batch contract in test doubles."""

    def references_batch(self, queries, **kwargs):
        return [(self.references(*query, **kwargs), None) for query in queries]


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

    def test_old_symbol_selection_line_falls_back_from_legacy_none(self):
        graph = CodeGraph(project_root="/tmp")
        graph._add_vertex("legacy.ts", {"type": NODE_TYPE_FILE})
        vertex_name = "legacy.ts:run():3"
        graph._add_vertex(
            vertex_name,
            {
                "type": NODE_TYPE_FUNCTION,
                "file": "legacy.ts",
                "start_line": 3,
                "end_line": 7,
                "selection_line": None,
                "unified_name": "legacy.ts:run()",
            },
        )
        patcher = PatcherTS("/tmp", graph)
        patcher.build_indexes()

        old = patcher.get_old_symbols("legacy.ts")["legacy.ts:run()"]

        assert old["selection_line"] == 3
        assert old["end_line"] == 7

    def test_reference_scope_prefers_new_nested_symbol_over_outer_hint(self):
        graph = CodeGraph(project_root="/tmp")
        patcher = PatcherTS("/tmp", graph)
        patcher.rebuild_file_subgraph(
            "nested.ts",
            [
                _sym(
                    "outer",
                    12,
                    0,
                    10,
                    children=[_sym("inner", 12, 4, 6)],
                )
            ],
        )
        graph.build_range_indexes()

        scope = patcher._reference_scope_for_token(
            "nested.ts",
            {"line": 5},
            "nested.ts:outer():0",
        )

        assert scope == "nested.ts:outer().inner():4"

    def test_workspace_warmup_visits_all_language_source_files(self, tmp_path):
        for name in ("component.ts", "helper.js", "README.md"):
            (tmp_path / name).write_text("export const value = 1;\n")
        dependency = tmp_path / "node_modules" / "pkg" / "index.ts"
        dependency.parent.mkdir(parents=True)
        dependency.write_text("export const dependency = 1;\n")
        visited = []

        class FakeClient:
            def document_symbol(self, file_path):
                visited.append(Path(file_path).name)
                return []

            def wait_until_idle(self, **kwargs):
                assert kwargs == {}
                visited.append("idle")
                return True

        patcher = PatcherTS(str(tmp_path), CodeGraph(), lsp_client=FakeClient())

        assert patcher.warmup_workspace() == 2
        assert patcher.workspace_initial_idle is True
        assert patcher.workspace_warmup_idle is True
        assert visited == ["idle", "component.ts", "helper.js", "idle"]

    def test_reverse_reference_reconciliation_finds_overloaded_target(self, tmp_path):
        source = tmp_path / "src.js"
        source.write_text("node.type\n")
        target = tmp_path / "types.d.ts"
        target.write_text("interface Node { type: string }\n")
        graph = CodeGraph(project_root=str(tmp_path))
        graph._add_vertex("types.d.ts", {"type": NODE_TYPE_FILE})
        vertex_name = "types.d.ts:Node.type:0"
        graph._add_vertex(
            vertex_name,
            {
                "type": NODE_TYPE_FIELD,
                "file": "types.d.ts",
                "start_line": 0,
                "end_line": 0,
                "selection_line": 0,
                "selection_character": 17,
                "unified_name": "types.d.ts:Node.type",
            },
        )

        class FakeClient(_BatchReferencesMixin):
            def references(self, *args, **kwargs):
                return [
                    {
                        "uri": source.as_uri(),
                        "range": {
                            "start": {"line": 0, "character": 5},
                            "end": {"line": 0, "character": 9},
                        },
                    }
                ]

        patcher = PatcherTS(str(tmp_path), graph, lsp_client=FakeClient())
        patcher.build_indexes()

        targets = patcher._reverse_reference_targets(
            "src.js",
            {"line": 0, "character": 5, "length": 4, "text": "type"},
        )

        assert targets == [vertex_name]

    def test_reverse_reference_reconciliation_validates_direct_target(self, tmp_path):
        source = tmp_path / "src.js"
        source.write_text("render(node)\n")
        target = tmp_path / "types.d.ts"
        target.write_text("declare function render(node: unknown): void\n")
        graph = CodeGraph(project_root=str(tmp_path))
        vertex_name = "types.d.ts:render():0"
        graph._add_vertex(
            vertex_name,
            {
                "type": NODE_TYPE_FUNCTION,
                "file": "types.d.ts",
                "start_line": 0,
                "end_line": 0,
                "selection_line": 0,
                "selection_character": 17,
                "unified_name": "types.d.ts:render()",
            },
        )

        class FakeClient(_BatchReferencesMixin):
            def references(self, *args, **kwargs):
                return []

        patcher = PatcherTS(str(tmp_path), graph, lsp_client=FakeClient())
        patcher.build_indexes()

        targets = patcher._reverse_reference_targets(
            "src.js",
            {"line": 0, "character": 0, "length": 6, "text": "render"},
            direct_targets=[vertex_name],
        )

        assert targets == []

    def test_strict_reverse_references_have_no_fanout_cutoff(self, tmp_path):
        source = tmp_path / "source.js"
        source.write_text("render(node)\n")
        graph = CodeGraph(project_root=str(tmp_path))
        target_names = []
        for index in range(65):
            relative = f"types{index}.d.ts"
            (tmp_path / relative).write_text(
                "declare function render(node: unknown): void\n"
            )
            vertex_name = f"{relative}:render():0"
            graph._add_vertex(
                vertex_name,
                {
                    "type": NODE_TYPE_FUNCTION,
                    "file": relative,
                    "start_line": 0,
                    "end_line": 0,
                    "selection_line": 0,
                    "selection_character": 17,
                    "unified_name": f"{relative}:render()",
                },
            )
            target_names.append(vertex_name)

        class FakeClient(_BatchReferencesMixin):
            def __init__(self):
                self.calls = 0

            def references(self, file_path, *args, **kwargs):
                self.calls += 1
                if Path(file_path).name != "types64.d.ts":
                    return []
                return [
                    {
                        "uri": source.as_uri(),
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 6},
                        },
                    }
                ]

        client = FakeClient()
        patcher = PatcherTS(
            str(tmp_path),
            graph,
            lsp_client=client,
            strict_lsp_requests=True,
        )
        patcher.build_indexes()

        targets = patcher._reverse_reference_targets(
            "source.js",
            {"line": 0, "character": 0, "length": 6, "text": "render"},
        )

        assert targets == [target_names[-1]]
        assert client.calls == 65

    def test_typescript_property_participates_in_incremental_classification(self):
        patcher = PatcherTS("/tmp", CodeGraph())

        flattened = patcher.flatten_symbols(
            "index.d.ts",
            [_sym("Options", 11, 0, 4, children=[_sym("onProgress", 7, 1, 1)])],
        )

        assert "index.d.ts:Options.onProgress" in flattened

    def test_typescript_duplicate_identity_requires_file_rebuild(self):
        graph = CodeGraph(project_root="/tmp")
        graph.add_file_node("callbacks.ts")
        for line in (2, 5):
            graph._add_vertex(
                f"callbacks.ts:callback():{line}",
                {
                    "type": NODE_TYPE_FUNCTION,
                    "file": "callbacks.ts",
                    "start_line": line,
                    "end_line": line,
                    "unified_name": "callbacks.ts:callback()",
                },
            )
        patcher = PatcherTS("/tmp", graph)
        patcher.build_indexes()

        reason = patcher._file_level_rebuild_reason(
            "callbacks.ts",
            {},
            {},
            [ChangedLineHunk(0, 0, 0, 1)],
        )

        assert reason == "duplicate hierarchical symbol identities cross an edited hunk"

        unchanged_reason = patcher._file_level_rebuild_reason(
            "callbacks.ts",
            {},
            {},
            [ChangedLineHunk(10, 1, 10, 1)],
        )
        assert unchanged_reason is None

    def test_rust_hierarchy_drift_requires_file_rebuild(self):
        patcher = PatcherRust("/tmp", CodeGraph())
        old_symbols = {
            "driver.rs:Driver.new()": {
                "start_line": 44,
                "end_line": 84,
                "selection_line": 45,
                "selection_character": 18,
            }
        }
        new_symbols = {
            "driver.rs:new()": {
                "start_line": 44,
                "end_line": 84,
                "sel_range": {
                    "start": {"line": 45, "character": 18},
                    "end": {"line": 45, "character": 21},
                },
            }
        }

        reason = patcher._file_level_rebuild_reason(
            "driver.rs",
            old_symbols,
            new_symbols,
            [ChangedLineHunk(100, 1, 100, 2)],
        )

        assert reason == "symbol hierarchy changed at an unchanged declaration"

    def test_same_location_with_different_leaf_does_not_force_rebuild(self):
        patcher = PatcherRust("/tmp", CodeGraph())
        old_symbols = {
            "driver.rs:Driver.new()": {
                "start_line": 44,
                "end_line": 84,
                "selection_line": 45,
                "selection_character": 18,
            }
        }
        new_symbols = {
            "driver.rs:build()": {
                "start_line": 44,
                "end_line": 84,
                "sel_range": {
                    "start": {"line": 45, "character": 18},
                    "end": {"line": 45, "character": 23},
                },
            }
        }

        reason = patcher._file_level_rebuild_reason(
            "driver.rs",
            old_symbols,
            new_symbols,
            [ChangedLineHunk(100, 1, 100, 2)],
        )

        assert reason is None

    def test_python_decorator_reference_uses_parent_scope(self, tmp_path):
        source = tmp_path / "example.py"
        source.write_text(
            "class Service:\n"
            "    @deprecated('old')\n"
            "    def run(self):\n"
            "        return helper()\n"
        )
        graph = CodeGraph(project_root=str(tmp_path))
        patcher = PatcherPython(str(tmp_path), graph)
        patcher.rebuild_file_subgraph(
            "example.py",
            [
                _sym(
                    "Service",
                    5,
                    0,
                    3,
                    children=[_sym("run", 6, 1, 3)],
                )
            ],
        )
        method = "example.py:Service.run():1"

        decorator_scope = patcher._reference_scope_for_token(
            "example.py", {"line": 1}, method
        )
        body_scope = patcher._reference_scope_for_token(
            "example.py", {"line": 3}, method
        )

        assert decorator_scope == "example.py:Service:0"
        assert body_scope == method

    def test_python_lsp_route_keeps_decorator_in_document_symbol_scope(self, tmp_path):
        source = tmp_path / "example.py"
        source.write_text(
            "class Service:\n"
            "    @deprecated('old')\n"
            "    def run(self):\n"
            "        return helper()\n"
        )
        graph = CodeGraph(project_root=str(tmp_path))
        patcher = PatcherPython(str(tmp_path), graph, respect_backend_exclusions=False)
        patcher.rebuild_file_subgraph(
            "example.py",
            [
                _sym(
                    "Service",
                    5,
                    0,
                    3,
                    children=[_sym("run", 6, 1, 3)],
                )
            ],
        )
        method = "example.py:Service.run():1"

        assert (
            patcher._reference_scope_for_token("example.py", {"line": 1}, method)
            == method
        )

    def test_lsp_file_replacement_reuses_full_index_lowering(self, tmp_path):
        file_path = "example.py"
        base_symbols = [
            _sym(
                "Service",
                5,
                0,
                8,
                children=[
                    _sym(
                        "run",
                        6,
                        1,
                        8,
                        children=[_sym("helper", 12, 4, 6)],
                    )
                ],
            )
        ]
        target_symbols = [
            _sym(
                "Service",
                5,
                0,
                9,
                children=[
                    _sym(
                        "run",
                        6,
                        1,
                        9,
                        children=[_sym("helper", 12, 5, 7)],
                    )
                ],
            )
        ]
        decoder = GenericLSPGraphDecoder(
            str(tmp_path / "unused.json"), project_root=str(tmp_path)
        )
        graph = decoder.decode_documents({file_path: base_symbols})

        class FakeClient:
            def document_symbol(self, _file_path):
                return target_symbols

        patcher = PatcherPython(
            str(tmp_path),
            graph,
            lsp_client=FakeClient(),
            mode="naive",
            respect_backend_exclusions=False,
        )
        context = patcher._rebuild_prepare_vertices(
            file_path,
            remap_severed=False,
            replace_file=True,
        )
        expected = decoder.decode_documents({file_path: target_symbols})

        actual_vertices = {
            (vertex["name"], tuple(sorted(vertex.attributes().items())))
            for vertex in graph.graph.vs
        }
        expected_vertices = {
            (vertex["name"], tuple(sorted(vertex.attributes().items())))
            for vertex in expected.graph.vs
        }
        actual_edges = {
            (
                graph.graph.vs[edge.source]["name"],
                graph.graph.vs[edge.target]["name"],
                edge["type"],
            )
            for edge in graph.graph.es
        }
        expected_edges = {
            (
                expected.graph.vs[edge.source]["name"],
                expected.graph.vs[edge.target]["name"],
                edge["type"],
            )
            for edge in expected.graph.es
        }

        assert actual_vertices == expected_vertices
        assert actual_edges == expected_edges
        assert context["severed_in"] == []
        assert context["severed_out"] == []
        assert context["replace_file"] is True
        assert context["rebuild_file_scope"] is True
        assert "example.py:Service.run().helper():5" in context["new_vertices"]
        assert "example.py:Service.helper():5" not in graph.name_to_vertex

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
        vertex = g.graph.vs[g.name_to_vertex[created[0]]]
        assert vertex["selection_character"] == 4

    def test_go_lsp_route_preserves_symbol_parent_and_range(self):
        graph = CodeGraph(project_root="/tmp")
        patcher = PatcherGo(
            "/tmp",
            graph,
            respect_backend_exclusions=False,
        )
        symbols = [
            _sym(
                "Router",
                23,
                4,
                20,
                children=[_sym("Handle", 6, 8, 12)],
            )
        ]

        flattened = patcher.flatten_symbols("router.go", symbols)

        assert flattened["router.go:Router"]["end_line"] == 20
        assert patcher._added_parent_unified_name("router.go", "Router") == (
            "router.go:Router"
        )

        patcher.rebuild_file_subgraph("router.go", symbols)
        patcher.build_indexes()
        assert (
            patcher._reference_scope_for_token(
                "router.go",
                {"line": 8},
                "router.go:Router.Handle():8",
            )
            == "router.go:Router.Handle():8"
        )

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
        assert g.graph.vs[g.name_to_vertex[vname]]["selection_line"] == 0


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
        from codeminer.graph.incremental.lsp_client import LSPClient

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

    def test_lexical_supplement_covers_jsx_properties_with_utf16_positions(
        self, tmp_path
    ):
        src = tmp_path / "component.tsx"
        line = "\t😀 render(<progress value={0} max='100' />)"
        src.write_text(line + "\n", encoding="utf-8")
        patcher = PatcherTS(str(tmp_path), CodeGraph())

        tokens = patcher._get_lexical_reference_tokens(str(src), [(0, 0)])
        by_text = {token["text"]: token for token in tokens}

        assert {"render", "progress", "value", "max"} <= set(by_text)
        progress_offset = line.index("progress")
        assert by_text["progress"]["character"] == (
            len(line[:progress_offset].encode("utf-16-le")) // 2
        )

    def test_strict_file_scope_supplements_navigable_doc_tokens(self, tmp_path):
        source = tmp_path / "source.js"
        target = tmp_path / "target.d.ts"
        source.write_text("/** @param {Thing} value */\n", encoding="utf-8")
        target.write_text("class Thing {}\n", encoding="utf-8")

        graph = CodeGraph(project_root=str(tmp_path))
        graph.add_file_node("source.js")
        graph.add_file_node("target.d.ts")
        target_name = "target.d.ts:Thing:0"
        graph._add_vertex(
            target_name,
            {
                "type": "class",
                "file": "target.d.ts",
                "start_line": 0,
                "end_line": 0,
                "selection_line": 0,
                "selection_character": 6,
                "unified_name": "target.d.ts:Thing",
            },
        )
        graph.build_range_indexes()
        thing_character = source.read_text().index("Thing")

        class FakeClient(_BatchReferencesMixin):
            last_error = None

            def definition(self, file_path, line, character):
                if Path(file_path) == source and character == thing_character:
                    return [
                        {
                            "uri": target.as_uri(),
                            "range": {"start": {"line": 0, "character": 6}},
                        }
                    ]
                return []

            def references(self, file_path, line, character, **_kwargs):
                if Path(file_path) != target:
                    return []
                return [
                    {
                        "uri": source.as_uri(),
                        "range": {
                            "start": {"line": 0, "character": thing_character},
                            "end": {
                                "line": 0,
                                "character": thing_character + len("Thing"),
                            },
                        },
                    }
                ]

        patcher = PatcherTS(
            str(tmp_path),
            graph,
            lsp_client=FakeClient(),
            strict_lsp_requests=True,
        )
        patcher._get_semantic_tokens = lambda *_args, **_kwargs: []
        patcher.build_indexes()
        stats = {"incoming_added": 0, "outgoing_added": 0, "unmatched": 0}

        patcher.reconnect_outgoing(
            "source.js",
            [],
            stats,
            line_ranges=None,
            scope_filter={"source.js"},
        )

        edges = [edge for edge in graph.graph.es if edge["type"] == EDGE_TYPE_REFERENCE]
        assert len(edges) == 1
        assert graph.graph.vs[edges[0].source]["name"] == "source.js"
        assert graph.graph.vs[edges[0].target]["name"] == target_name

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

    def test_reference_types_python_include_variable(self):
        p = PatcherPython("/tmp", CodeGraph())
        types = p._get_crossfile_token_types()
        assert "variable" in types


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

    def test_strict_match_does_not_promote_local_to_scope(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        p.rebuild_file_subgraph(
            "src/lib.rs",
            [_sym("X", 13, 2, 2), _sym("run", 12, 5, 20)],
        )

        result = p.match_location_to_vertex(
            "src/lib.rs",
            12,
            symbol_name="X",
            allow_scope_fallback=False,
            allow_name_fallback=False,
        )

        assert result is None

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

    def test_unique_name_fallback_resolves_duplicate_lsp_declaration(self):
        g = CodeGraph(project_root="/tmp")
        p = PatcherRust("/tmp", g)
        p.rebuild_file_subgraph("src/lib.rs", [_sym("assert_greater", 12, 105, 105)])

        result = p.match_location_to_vertex(
            "src/lib.rs",
            341,
            symbol_name="assert_greater",
        )

        assert result == "src/lib.rs:assert_greater():105"


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
    def test_python_lifts_method_local_function_to_class_scope(self):
        patcher = PatcherPython("/tmp", CodeGraph())
        symbols = [
            _sym(
                "GroupBy",
                5,
                0,
                40,
                children=[
                    _sym(
                        "_restore_dim_order",
                        6,
                        10,
                        25,
                        children=[_sym("lookup_order", 12, 12, 19)],
                    )
                ],
            )
        ]

        result = patcher.flatten_symbols("groupby.py", symbols)

        assert "groupby.py:GroupBy.lookup_order()" in result
        assert "groupby.py:GroupBy._restore_dim_order().lookup_order()" not in result
        assert result["groupby.py:GroupBy.lookup_order()"]["kind"] == 6
        assert result["groupby.py:GroupBy.lookup_order()"]["parent_uname"] == "GroupBy"

    def test_python_duplicate_local_function_keeps_first_declaration(self):
        patcher = PatcherPython("/tmp", CodeGraph())
        symbols = [
            _sym(
                "GroupBy",
                5,
                0,
                50,
                children=[
                    _sym(
                        "_reduce_method",
                        6,
                        10,
                        40,
                        children=[
                            _sym("wrapped_func", 12, 14, 20),
                            _sym("wrapped_func", 12, 25, 34),
                        ],
                    )
                ],
            )
        ]

        result = patcher.flatten_symbols("groupby.py", symbols)

        wrapped = result["groupby.py:GroupBy.wrapped_func()"]
        assert wrapped["start_line"] == 14
        assert wrapped["end_line"] == 20

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
