# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the generic LSP graph indexer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    method = graph.graph.vs[graph.name_to_vertex[method_name]]
    assert method["selection_line"] == 10
    assert method["selection_character"] == 4

    class_query = graph.query_range(file_name, 10, 10, kinds={EDGE_TYPE_CONTAIN})
    assert [node.name for node in class_query.defined] == [class_name, method_name]


def test_generic_lsp_decoder_normalizes_go_receiver_method(tmp_path):
    index = tmp_path / "index.lsp.json"
    index.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "language": "go",
                "files": [
                    {
                        "path": "routergroup.go",
                        "symbols": [
                            _sym("(*RouterGroup).createStaticHandler", 6, 184, 205)
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    graph = GenericLSPGraphDecoder(str(index), project_root=str(tmp_path)).decode()

    assert (
        "routergroup.go:RouterGroup.createStaticHandler():184" in graph.name_to_vertex
    )


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
    class_name = "lib/rake/application.rb:Rake.Application:1"
    init_name = "lib/rake/application.rb:Rake.Application.initialize():3"
    collect_name = (
        "lib/rake/application.rb:Rake.Application.collect_command_line_tasks():10"
    )
    name_field = "lib/rake/application.rb:Rake.Application.@name:4"
    task_field = "lib/rake/application.rb:Rake.Application.@top_level_tasks:12"
    assert "lib/rake/application.rb:Rake.Application.name():2" in graph.name_to_vertex
    assert name_field in graph.name_to_vertex
    assert task_field in graph.name_to_vertex
    contain_edges = {
        (graph.graph.vs[edge.source]["name"], graph.graph.vs[edge.target]["name"])
        for edge in graph.graph.es
        if edge["type"] == EDGE_TYPE_CONTAIN
    }
    assert (class_name, name_field) in contain_edges
    assert (class_name, task_field) in contain_edges
    assert (init_name, name_field) not in contain_edges
    assert (collect_name, task_field) not in contain_edges
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

        def close_document(self, file_path):
            pass

    monkeypatch.setattr("codeminer.ls_index.lsp_indexer.LSPClient", FakeLSPClient)

    indexer = GenericLSPIndexer(tmp_path, language="java")

    assert indexer.generate_index()
    assert calls == [(["/tmp/resolved-jdtls", "--stdio"], str(tmp_path), "java")]


def test_generic_lsp_indexer_skips_generated_dependency_trees(tmp_path):
    source = tmp_path / "src" / "app.ts"
    dependency = tmp_path / "node_modules" / "pkg" / "index.ts"
    generated = tmp_path / "dist" / "bundle.ts"
    for path in (source, dependency, generated):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export const value = 1;\n", encoding="utf-8")

    indexer = GenericLSPIndexer(tmp_path, language="typescript")

    assert list(indexer._iter_source_files()) == [Path("src/app.ts")]


def test_generic_lsp_indexer_applies_uniform_strict_request_budget(
    tmp_path, monkeypatch
):
    (tmp_path / "module.py").write_text("def run():\n    pass\n", encoding="utf-8")
    clients = []

    class FakeLSPClient:
        @staticmethod
        def get_lsp_command(_language):
            return ["fake-lsp"]

        def __init__(self, *_args):
            clients.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def document_symbol(self, _file_path):
            return []

        def close_document(self, file_path):
            pass

    monkeypatch.setattr("codeminer.ls_index.lsp_indexer.LSPClient", FakeLSPClient)

    indexer = GenericLSPIndexer(tmp_path, language="python")
    assert indexer.generate_index(
        strict_references=True,
        request_timeout_s=73.0,
        reference_timeout_s=73.0,
        reference_retries=2,
    )

    client = clients[0]
    assert client.strict_request_failures is True
    assert client.operation_retries == 2
    assert client.reference_retries == 2
    assert client.reference_timeout_s == 73.0
    assert client.document_symbol_timeout_s == 73.0
    assert client.definition_timeout_s == 73.0
    assert client.semantic_tokens_timeout_s == 73.0


def test_generic_lsp_indexer_propagates_outer_timeout(tmp_path, monkeypatch):
    (tmp_path / "module.py").write_text("value = 1\n", encoding="utf-8")

    class TimedOutLSPClient:
        @staticmethod
        def get_lsp_command(_language):
            return ["fake-lsp"]

        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def document_symbol(self, _file_path):
            raise TimeoutError("strategy deadline")

        def close_document(self, _file_path):
            pass

    monkeypatch.setattr("codeminer.ls_index.lsp_indexer.LSPClient", TimedOutLSPClient)

    indexer = GenericLSPIndexer(tmp_path, language="python")
    with pytest.raises(TimeoutError, match="strategy deadline"):
        indexer.generate_index()


def test_generic_lsp_indexer_strict_reference_oracle_rejects_timeout(tmp_path):
    indexer = GenericLSPIndexer(tmp_path, language="python")
    symbols = [_sym("run", 12, 3, 5)]

    class TimedOutClient:
        last_error = {"code": -1, "message": "timeout"}

        def references_batch(self, queries, **kwargs):
            return [([], self.last_error) for _query in queries]

    with pytest.raises(RuntimeError, match="reference oracle request failed"):
        indexer._collect_references(
            TimedOutClient(), Path("module.py"), symbols, strict=True
        )


def test_generic_lsp_indexer_treats_unavailable_document_as_empty(tmp_path):
    indexer = GenericLSPIndexer(tmp_path, language="go")
    symbols = [_sym("run", 12, 3, 5)]

    class UnavailableClient:
        last_error = {
            "code": 0,
            "message": "no package metadata for file file:///repo/fuzz.go",
        }

        def references_batch(self, queries, **kwargs):
            return [([], self.last_error) for _query in queries]

    assert (
        indexer._collect_references(
            UnavailableClient(), Path("fuzz.go"), symbols, strict=True
        )
        == []
    )


def test_generic_lsp_indexer_collects_all_symbols_before_references(
    tmp_path, monkeypatch
):
    for name in ("A.java", "B.java"):
        (tmp_path / name).write_text(f"class {name[0]} {{}}\n", encoding="utf-8")
    events = []

    class FakeLSPClient:
        @staticmethod
        def get_lsp_command(language):
            return ["fake-lsp"]

        def __init__(self, command, project_root, language):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def document_symbol(self, file_path):
            events.append(("symbols", Path(file_path).name))
            return [_sym(Path(file_path).stem, 5, 0, 0)]

        def wait_until_idle(self):
            events.append(("idle", None))
            return True

        def wait_for_analysis(self, file_path):
            events.append(("analysis", Path(file_path).name))
            return True

        def references_batch(self, queries, **kwargs):
            for file_path, _line, _character in queries:
                events.append(("references", Path(file_path).name))
            return [([], None) for _query in queries]

        def close_document(self, file_path):
            events.append(("close", Path(file_path).name))

    monkeypatch.setattr("codeminer.ls_index.lsp_indexer.LSPClient", FakeLSPClient)

    indexer = GenericLSPIndexer(tmp_path, language="java")

    assert indexer.generate_index(include_references=True)
    assert events == [
        ("idle", None),
        ("symbols", "A.java"),
        ("close", "A.java"),
        ("symbols", "B.java"),
        ("close", "B.java"),
        ("idle", None),
        ("analysis", "A.java"),
        ("references", "A.java"),
        ("close", "A.java"),
        ("references", "B.java"),
        ("close", "B.java"),
    ]


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

        def references_batch(
            self, queries, include_declaration=False, max_in_flight=None
        ):
            outcomes = []
            for file_path, line, character in queries:
                self.calls.append(
                    (file_path, line, character, include_declaration, max_in_flight)
                )
                outcomes.append(
                    (
                        [
                            {
                                "uri": (tmp_path / "src/Foo.java").as_uri(),
                                "range": {
                                    "start": {"line": 7, "character": 12},
                                    "end": {"line": 7, "character": 15},
                                },
                            }
                        ],
                        None,
                    )
                )
            return outcomes

    symbols = [_sym("Foo", 5, 0, 10, children=[_sym("run", 6, 3, 5)])]
    indexer = GenericLSPIndexer(tmp_path, language="java")
    fake = FakeClient()

    refs = indexer._collect_references(fake, Path("src/Foo.java"), symbols)

    assert fake.calls == [
        (str(tmp_path / "src/Foo.java"), 0, 4, False, 10),
        (str(tmp_path / "src/Foo.java"), 3, 4, False, 10),
    ]
    assert refs[1]["target_unified_name"] == "src/Foo.java:Foo.run()"
    assert refs[1]["target_start_line"] == 3
    assert refs[1]["locations"][0]["range"]["start"]["line"] == 7
