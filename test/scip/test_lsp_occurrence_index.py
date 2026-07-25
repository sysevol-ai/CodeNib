# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from codenib.scip_interface.lsp_occurrence_index import SCIPOccurrenceIndex

_DECODED = r"""
metadata {
  text_document_encoding: UTF8
}
documents {
  relative_path: "pkg/reader.go"
  occurrences {
    range: 3
    range: 6
    range: 7
    symbol: "local 0"
    symbol_roles: 1
  }
  occurrences {
    range: 4
    range: 1
    range: 2
    symbol: "local 0"
    symbol_roles: 8
  }
  occurrences {
    range: 5
    range: 1
    range: 6
    range: 2
    symbol: "local 0"
    symbol_roles: 8
  }
  occurrences {
    range: 8
    range: 2
    range: 8
    symbol: "scip-go gomod example.com/repo v1 pkg/Foo#"
    symbol_roles: 1
  }
}
documents {
  relative_path: "pkg/other.go"
  occurrences {
    range: 1
    range: 0
    range: 1
    symbol: "local 0"
    symbol_roles: 1
  }
  occurrences {
    range: 2
    range: 0
    range: 3
    symbol: "scip-go gomod example.com/repo v1 pkg/Foo#"
    symbol_roles: 8
  }
}
"""


def test_exact_occurrence_index_resolves_local_definition_and_references():
    index = SCIPOccurrenceIndex.from_decoded_text(_DECODED)

    definitions = index.definitions(file_path="pkg/reader.go", line=4, character=1)
    references = index.references(file_path="pkg/reader.go", line=3, character=6)

    assert index.position_encoding == "UTF8"
    assert [(row.file_path, row.start_line) for row in definitions] == [
        ("pkg/reader.go", 3)
    ]
    assert [(row.file_path, row.start_line) for row in references] == [
        ("pkg/reader.go", 3),
        ("pkg/reader.go", 4),
        ("pkg/reader.go", 5),
    ]


def test_local_symbol_ids_are_scoped_to_one_document():
    index = SCIPOccurrenceIndex.from_decoded_text(_DECODED)

    references = index.references(file_path="pkg/other.go", line=1, character=0)

    assert [(row.file_path, row.start_line) for row in references] == [
        ("pkg/other.go", 1)
    ]


def test_global_symbols_cross_documents_and_exclude_declarations():
    index = SCIPOccurrenceIndex.from_decoded_text(_DECODED)

    references = index.references(
        file_path="pkg/other.go",
        line=2,
        character=1,
        include_declaration=False,
    )
    definitions = index.definitions(file_path="pkg/other.go", line=2, character=1)

    assert [(row.file_path, row.start_line) for row in references] == [
        ("pkg/other.go", 2)
    ]
    assert [(row.file_path, row.start_line) for row in definitions] == [
        ("pkg/reader.go", 8)
    ]


def test_occurrence_index_round_trips(tmp_path):
    path = tmp_path / "lsp_index.pkl"
    SCIPOccurrenceIndex.from_decoded_text(_DECODED).save(path)

    loaded = SCIPOccurrenceIndex.load(path)

    assert loaded.occurrence_at("pkg/reader.go", 3, 6).is_definition
    assert len(loaded.occurrences) == 6
