# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from io import BytesIO

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


def test_occurrence_index_loads_from_caller_owned_stream(tmp_path):
    path = tmp_path / "lsp_index.pkl"
    SCIPOccurrenceIndex.from_decoded_text(_DECODED).save(path)
    stream = BytesIO(path.read_bytes())

    loaded = SCIPOccurrenceIndex.load_stream(
        stream,
        input_label="authenticated lsp_index.pkl",
    )

    assert loaded.occurrence_at("pkg/reader.go", 3, 6).is_definition
    assert len(loaded.occurrences) == 6
    assert not stream.closed


def test_enclosing_range_does_not_replace_the_selection_range():
    decoded = _DECODED.replace(
        'symbol: "local 0"\n    symbol_roles: 1',
        'symbol: "local 0"\n    symbol_roles: 1\n'
        "    enclosing_range: 2\n    enclosing_range: 0\n"
        "    enclosing_range: 7\n    enclosing_range: 0",
        1,
    )
    index = SCIPOccurrenceIndex.from_decoded_text(decoded)

    definitions = index.definitions(file_path="pkg/reader.go", line=4, character=1)

    assert [
        (row.start_line, row.start_character, row.end_character) for row in definitions
    ] == [(3, 6, 7)]


def test_one_selection_can_resolve_multiple_conditional_definitions():
    decoded = _DECODED.replace(
        "    range: 5\n    range: 1\n    range: 6\n    range: 2",
        "    range: 4\n    range: 1\n    range: 2",
    ).replace(
        "  occurrences {\n    range: 8",
        "  occurrences {\n    range: 10\n    range: 6\n    range: 7\n"
        '    symbol: "local 1"\n    symbol_roles: 1\n  }\n'
        "  occurrences {\n    range: 4\n    range: 1\n    range: 2\n"
        '    symbol: "local 1"\n    symbol_roles: 8\n  }\n'
        "  occurrences {\n    range: 8",
    )
    index = SCIPOccurrenceIndex.from_decoded_text(decoded)

    definitions = index.definitions(file_path="pkg/reader.go", line=4, character=1)
    references = index.references(file_path="pkg/reader.go", line=4, character=1)

    assert {row.start_line for row in definitions} == {3, 10}
    assert {row.start_line for row in references} == {3, 4, 10}
    assert len(references) == 3
