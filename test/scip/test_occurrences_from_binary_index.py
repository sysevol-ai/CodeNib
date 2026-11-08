# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Occurrences read from the binary SCIP index instead of its decoded text."""

from __future__ import annotations

from codenib.compiler.artifact_fingerprints import regular_file_fingerprint
from codenib.profiler import Profiler
from codenib.scip_interface.lsp_occurrence_index import SCIPOccurrenceIndex
from codenib.scip_interface.scip_indexer_python import SCIPPythonIndexer
from codenib.scip_interface.scip_pb2 import Index


def _indexer(tmp_path) -> SCIPPythonIndexer:
    index = Index()
    index.metadata.text_document_encoding = 1  # UTF8
    for path, symbols in (
        ("pkg/a.py", ["scip-python python pkg 0 `pkg.a`/f().", "local 1"]),
        ("pkg/b.py", ['scip-python python pkg 0 `pkg.b`/"quoted\\name"().']),
    ):
        document = index.documents.add()
        document.relative_path = path
        for line, symbol in enumerate(symbols):
            occurrence = document.occurrences.add()
            occurrence.range.extend([line, 0, line, 3] if line else [line, 4, 5])
            occurrence.symbol = symbol
            occurrence.symbol_roles = 1 if line == 0 else 0
        empty = document.occurrences.add()  # no symbol: skipped by both readers
        empty.range.extend([9, 0, 1])
    (tmp_path / "index.scip").write_bytes(index.SerializeToString())
    indexer = SCIPPythonIndexer.__new__(SCIPPythonIndexer)
    indexer.index_file = tmp_path / "index.scip"
    indexer.decoded_file = tmp_path / "index.decoded"
    indexer.profiler = Profiler("test")
    indexer._decoded_from = None
    indexer._target_dir = None
    indexer.exclude_patterns = []
    return indexer


def test_binary_occurrences_match_the_decoded_text(tmp_path):
    indexer = _indexer(tmp_path)
    assert indexer.decode_index()
    occurrences, fingerprint = indexer._occurrences_from_binary_index()
    from_text = SCIPOccurrenceIndex.from_decoded_file(indexer.decoded_file)
    assert occurrences.occurrences == from_text.occurrences
    assert occurrences._by_file == from_text._by_file
    assert occurrences._by_symbol == from_text._by_symbol
    assert occurrences.position_encoding == from_text.position_encoding
    assert fingerprint == regular_file_fingerprint(indexer.decoded_file)


def test_a_changed_binary_index_falls_back_to_the_text(tmp_path):
    indexer = _indexer(tmp_path)
    assert indexer.decode_index()
    (tmp_path / "index.scip").write_bytes(b"\x0a\x00")  # another index since
    assert indexer._occurrences_from_binary_index() == (None, None)


def test_without_a_decode_in_this_run_the_text_is_read(tmp_path):
    assert _indexer(tmp_path)._occurrences_from_binary_index() == (None, None)
