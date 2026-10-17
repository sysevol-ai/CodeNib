# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Regression for overlapping chunk ranges in materialized clangd graphs."""

import pytest

from codenib.ls_index.clangd_decode import (
    KIND_FUNCTION,
    KIND_INSTANCE_METHOD,
    ClangdGraphDecoder,
)


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
