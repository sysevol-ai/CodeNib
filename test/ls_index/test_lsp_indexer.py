# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the generic LSP graph indexer."""

from __future__ import annotations

import json
from pathlib import Path

from codeminer.ls_index.lsp_graph_decode import (
    GenericLSPGraphDecoder,
    iter_lsp_symbol_definitions,
)
from codeminer.ls_index.lsp_indexer import GenericLSPIndexer
from codeminer.ls_router import LSIndexer
from codeminer.types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_CLASS,
    NODE_TYPE_METHOD,
)


def _sym(name, kind, start, end, children=None):
    symbol = {
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
        symbol["children"] = children
    return symbol


def test_generic_lsp_decoder_builds_java_symbol_graph(tmp_path):
    index = tmp_path / "index.lsp.json"
    index.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "language": "java",
                "files": [
                    {
                        "path": "src/main/java/app/Router.java",
                        "symbols": [
                            _sym(
                                "Router",
                                5,
                                2,
                                40,
                                children=[
                                    _sym("Router", 9, 4, 8),
                                    _sym("handle", 6, 10, 30),
                                ],
                            )
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    graph = GenericLSPGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    file_name = "src/main/java/app/Router.java"
    class_name = f"{file_name}:Router:2"
    ctor_name = f"{file_name}:Router.constructor():4"
    method_name = f"{file_name}:Router.handle():10"

    assert graph.graph.vs[graph.name_to_vertex[class_name]]["type"] == NODE_TYPE_CLASS
    assert graph.graph.vs[graph.name_to_vertex[ctor_name]]["type"] == NODE_TYPE_METHOD
    assert graph.graph.vs[graph.name_to_vertex[method_name]]["type"] == NODE_TYPE_METHOD

    class_query = graph.query_range(file_name, 10, 10, kinds={EDGE_TYPE_CONTAIN})
    assert [node.name for node in class_query.defined] == [class_name, method_name]


def test_generic_lsp_decoder_keeps_ruby_module_parent_names(tmp_path):
    index = tmp_path / "index.lsp.json"
    index.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "language": "ruby",
                "files": [
                    {
                        "path": "lib/invoice.rb",
                        "symbols": [
                            _sym(
                                "Smoke",
                                2,
                                0,
                                10,
                                children=[
                                    _sym(
                                        "Invoice",
                                        5,
                                        1,
                                        5,
                                        children=[_sym("total", 6, 2, 4)],
                                    ),
                                    _sym("self.normalize", 12, 7, 9),
                                ],
                            )
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    graph = GenericLSPGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    assert "lib/invoice.rb:Smoke.Invoice:1" in graph.name_to_vertex
    assert "lib/invoice.rb:Smoke.Invoice.total():2" in graph.name_to_vertex
    assert "lib/invoice.rb:Smoke.normalize():7" in graph.name_to_vertex

    definitions = list(
        iter_lsp_symbol_definitions(
            "lib/invoice.rb",
            [
                _sym(
                    "Smoke",
                    2,
                    0,
                    10,
                    children=[_sym("self.normalize", 12, 7, 9)],
                )
            ],
            language="ruby",
        )
    )
    assert definitions[-1]["unified_name"] == "lib/invoice.rb:Smoke.normalize()"


def test_generic_lsp_decoder_normalizes_ruby_constructor_and_instance_fields(
    tmp_path,
):
    symbols = [
        _sym(
            "Rake",
            2,
            0,
            20,
            children=[
                _sym(
                    "Application",
                    5,
                    1,
                    19,
                    children=[
                        _sym("name", 8, 2, 2),
                        _sym(
                            "initialize",
                            9,
                            3,
                            8,
                            children=[_sym("@name", 8, 4, 4)],
                        ),
                        _sym(
                            "collect_command_line_tasks",
                            6,
                            10,
                            18,
                            children=[_sym("@top_level_tasks", 8, 12, 12)],
                        ),
                    ],
                )
            ],
        )
    ]
    index = tmp_path / "index.lsp.json"
    index.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "language": "ruby",
                "files": [{"path": "lib/rake/application.rb", "symbols": symbols}],
            }
        ),
        encoding="utf-8",
    )

    graph = GenericLSPGraphDecoder(str(index), project_root=str(tmp_path)).decode()
    definitions = list(
        iter_lsp_symbol_definitions(
            "lib/rake/application.rb",
            symbols,
            language="ruby",
        )
    )

    assert (
        "lib/rake/application.rb:Rake.Application.initialize():3"
        in graph.name_to_vertex
    )
    assert "lib/rake/application.rb:Rake.Application.name():2" in graph.name_to_vertex
    assert "lib/rake/application.rb:Rake.Application.@name:4" in graph.name_to_vertex
    assert (
        "lib/rake/application.rb:Rake.Application.@top_level_tasks:12"
        in graph.name_to_vertex
    )
    assert {
        item["unified_name"]
        for item in definitions
        if item["unified_name"].endswith((".@name", ".@top_level_tasks"))
    } == {
        "lib/rake/application.rb:Rake.Application.@name",
        "lib/rake/application.rb:Rake.Application.@top_level_tasks",
    }


def test_generic_lsp_decoder_normalizes_ruby_singleton_scope_and_colons(tmp_path):
    symbols = [
        _sym(
            "Rake::NameSpace",
            5,
            0,
            5,
            children=[
                _sym(
                    "<< self",
                    5,
                    1,
                    4,
                    children=[_sym("record_task_metadata", 6, 2, 3)],
                )
            ],
        )
    ]
    index = tmp_path / "index.lsp.json"
    index.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "language": "ruby",
                "files": [{"path": "lib/rake/name_space.rb", "symbols": symbols}],
            }
        ),
        encoding="utf-8",
    )

    graph = GenericLSPGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    assert "lib/rake/name_space.rb:Rake.NameSpace:0" in graph.name_to_vertex
    assert (
        "lib/rake/name_space.rb:Rake.NameSpace.record_task_metadata():2"
        in graph.name_to_vertex
    )
    assert all("<< self" not in name for name in graph.name_to_vertex)


def test_lsindexer_can_force_java_generic_lsp_backend(tmp_path):
    indexer = LSIndexer(tmp_path, language="java", graph_route="lsp")

    assert indexer.language == "java"
    assert indexer._delegate.__class__.__name__ == "GenericLSPIndexer"
    assert indexer._delegate.index_file.name == "index.lsp.json"


def test_generic_lsp_indexer_uses_resolved_lsp_command(tmp_path, monkeypatch):
    source = tmp_path / "Example.java"
    source.write_text("class Example {}\n", encoding="utf-8")
    calls = []

    class FakeLSPClient:
        @staticmethod
        def get_lsp_command(language):
            assert language == "java"
            return ["/tmp/resolved-jdtls", "--stdio"]

        def __init__(self, command, project_root, language):
            calls.append((command, project_root, language))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def document_symbol(self, file_path):
            return []

    monkeypatch.setattr("codeminer.ls_index.lsp_indexer.LSPClient", FakeLSPClient)

    indexer = GenericLSPIndexer(tmp_path, language="java")

    assert indexer.generate_index()
    assert calls == [(["/tmp/resolved-jdtls", "--stdio"], str(tmp_path), "java")]


def test_generic_lsp_indexer_normalizes_language_alias(tmp_path):
    indexer = GenericLSPIndexer(tmp_path, language="rb")

    assert indexer.language == "ruby"


def test_generic_lsp_decoder_adds_reference_edges(tmp_path):
    foo = tmp_path / "src/main/java/app/Foo.java"
    bar = tmp_path / "src/main/java/app/Bar.java"
    foo.parent.mkdir(parents=True)
    foo.write_text("class Foo { void run() {} }\n", encoding="utf-8")
    bar.write_text("class Bar { void call() { new Foo().run(); } }\n", encoding="utf-8")

    foo_symbols = [_sym("Foo", 5, 0, 10, children=[_sym("run", 6, 2, 4)])]
    bar_symbols = [_sym("Bar", 5, 0, 10, children=[_sym("call", 6, 2, 8)])]
    target = list(
        iter_lsp_symbol_definitions("src/main/java/app/Foo.java", foo_symbols)
    )[1]

    index = tmp_path / "index.lsp.json"
    index.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "language": "java",
                "files": [
                    {
                        "path": "src/main/java/app/Foo.java",
                        "symbols": foo_symbols,
                        "references": [
                            {
                                "target_unified_name": target["unified_name"],
                                "target_start_line": target["start_line"],
                                "target_file": "src/main/java/app/Foo.java",
                                "locations": [
                                    {
                                        "uri": bar.as_uri(),
                                        "range": {
                                            "start": {"line": 3, "character": 35},
                                            "end": {"line": 3, "character": 38},
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "path": "src/main/java/app/Bar.java",
                        "symbols": bar_symbols,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    graph = GenericLSPGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    source = "src/main/java/app/Bar.java:Bar.call():2"
    target_name = "src/main/java/app/Foo.java:Foo.run():2"
    edge = graph.graph.es.find(
        _source=graph.name_to_vertex[source],
        _target=graph.name_to_vertex[target_name],
    )

    assert edge["type"] == EDGE_TYPE_REFERENCE
    assert edge["anchor_file"] == "src/main/java/app/Bar.java"
    assert edge["anchor_line"] == 3


def test_generic_lsp_indexer_collects_references_from_selection_range(tmp_path):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def references(self, file_path, line, character, include_declaration=False):
            self.calls.append((file_path, line, character, include_declaration))
            return [
                {
                    "uri": (tmp_path / "src/Foo.java").as_uri(),
                    "range": {
                        "start": {"line": 7, "character": 12},
                        "end": {"line": 7, "character": 15},
                    },
                }
            ]

    symbols = [_sym("Foo", 5, 0, 10, children=[_sym("run", 6, 3, 5)])]
    indexer = GenericLSPIndexer(tmp_path, language="java")
    fake = FakeClient()

    refs = indexer._collect_references(fake, Path("src/Foo.java"), symbols)

    assert fake.calls == [
        (str(tmp_path / "src/Foo.java"), 0, 4, False),
        (str(tmp_path / "src/Foo.java"), 3, 4, False),
    ]
    assert refs[1]["target_unified_name"] == "src/Foo.java:Foo.run()"
    assert refs[1]["target_start_line"] == 3
    assert refs[1]["locations"][0]["range"]["start"]["line"] == 7
