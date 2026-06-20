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


def test_lsindexer_routes_java_to_generic_lsp_backend(tmp_path):
    indexer = LSIndexer(tmp_path, language="java")

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
