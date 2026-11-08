# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Regression for overlapping chunk ranges in materialized clangd graphs."""

import os
from pathlib import Path

import pytest

from codenib.ls_index.clangd_decode import (
    KIND_FUNCTION,
    KIND_INSTANCE_METHOD,
    ClangdGraphDecoder,
)
from codenib.types import NODE_TYPE_METHOD


@pytest.mark.parametrize("reverse_chunks", [False, True])
def test_nested_overloads_keep_distinct_source_ranges(
    tmp_path, monkeypatch, reverse_chunks
):
    source = tmp_path / "nested.cpp"
    source.write_text(
        """void outer() {
  class Inner {
   public:
    int target() {
      return 1;
    }
    int target(int value) {
      return value;
    }
  };
}
""",
        encoding="utf-8",
    )
    decoder = ClangdGraphDecoder(str(tmp_path / "index"), str(tmp_path))
    chunks = decoder._get_file_chunks("nested.cpp")
    if reverse_chunks:
        decoder._chunks_cache["nested.cpp"] = list(reversed(chunks))

    # Supply compiler definitions at the decoded-record boundary. The real
    # chunker emits both the enclosing function and nested method ranges,
    # reproducing the first-containing-chunk bug seen in macro-heavy fmt.
    def collect():
        decoder._symbols = {
            ident: {
                "name": name,
                "kind": kind,
                "definition": {
                    "file": source.as_uri(),
                    "start": (line, 4),
                    "end": (line, 10),
                },
            }
            for ident, name, kind, line in (
                ("outer", "outer", KIND_FUNCTION, 0),
                ("first", "Inner::target", KIND_INSTANCE_METHOD, 3),
                ("second", "Inner::target", KIND_INSTANCE_METHOD, 6),
            )
        }

    monkeypatch.setattr(decoder, "_collect_all_idx", collect)
    graph = decoder.materialize_code_graph()
    expected = {"outer": (0, 10), "first": (3, 5), "second": (6, 8)}
    for name, span in expected.items():
        vertex = graph.graph.vs[graph.name_to_vertex[name]]
        assert (vertex["start_line"], vertex["end_line"]) == span

    # A query inside the second overload must not report the first as enclosing.
    query = graph.query_range("nested.cpp", 7, 7)
    assert {node.name for node in query.defined} == {"outer", "second"}


def test_missing_chunk_keeps_compiler_definition_point(tmp_path):
    decoder = ClangdGraphDecoder(str(tmp_path / "index"), str(tmp_path))
    assert decoder._find_range("missing.cpp", 7) == (7, 7)


def test_conversion_function_is_a_method_with_an_enclosing_body(tmp_path, monkeypatch):
    source = tmp_path / "value.hpp"
    source.write_text(
        "class Value {\n  operator bool() const {\n    return true;\n  }\n};\n"
    )
    decoder = ClangdGraphDecoder(str(tmp_path / "index"), str(tmp_path))

    def collect():
        decoder._symbols = {
            "conversion": {
                "name": "operator bool",
                # clang/Index/IndexSymbol.h: SymbolKind::ConversionFunction.
                "kind": 24,
                "definition": {
                    "file": source.as_uri(),
                    "start": (1, 2),
                    "end": (1, 10),
                },
            }
        }

    monkeypatch.setattr(decoder, "_collect_all_idx", collect)
    graph = decoder.materialize_code_graph()
    node = graph.graph.vs[graph.name_to_vertex["conversion"]]
    assert node["type"] == NODE_TYPE_METHOD
    assert (node["start_line"], node["end_line"]) == (1, 3)
    assert {node.name for node in graph.query_range("value.hpp", 2, 2).defined} == {
        "conversion"
    }


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/main.cpp",
        "src/path with spaces/file.cpp",
        "src/name%20literal.cpp",
        "src/name%2Fliteral.cpp",
        "src/name%25literal.cpp",
        "src/hash#literal.cpp",
        "src/café.cpp",
        "src/c++/file.cpp",
    ],
)
def test_clangd_file_uri_to_relative_round_trip(
    tmp_path: Path, relative_path: str
) -> None:
    source = tmp_path / relative_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("int x = 1;\n", encoding="utf-8")

    decoder = ClangdGraphDecoder(str(tmp_path / "index"), str(tmp_path))
    uri = source.as_uri()
    assert decoder._file_uri_to_relative(uri) == relative_path


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-root conversion")
def test_clangd_file_uri_to_relative_windows_drive():
    root = "C:/workspace/repo"
    uri = "file:///C:/workspace/repo/src/main.cpp"
    decoder = ClangdGraphDecoder(f"{root}/index", root)
    assert decoder._file_uri_to_relative(uri) == "src/main.cpp"


def test_clangd_file_uri_to_relative_non_file_and_external():
    root = "/workspace/repo"
    decoder = ClangdGraphDecoder(f"{root}/index", root)
    assert (
        decoder._file_uri_to_relative("/external/path/file.cpp")
        == "/external/path/file.cpp"
    )
