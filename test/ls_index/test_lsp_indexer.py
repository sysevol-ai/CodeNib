# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the generic LSP graph indexer."""

from __future__ import annotations

import json

from codeminer.ls_index.lsp_graph_decode import GenericLSPGraphDecoder
from codeminer.ls_router import LSIndexer
from codeminer.types import EDGE_TYPE_CONTAIN, NODE_TYPE_CLASS, NODE_TYPE_METHOD


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
