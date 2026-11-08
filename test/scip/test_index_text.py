# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""``index.decoded`` text written by the fast encoder matches TextFormat."""

from __future__ import annotations

import pytest
from google.protobuf.internal import api_implementation
from google.protobuf.text_format import MessageToString

from codenib.scip_interface import scip_indexer_base
from codenib.scip_interface.scip_indexer_base import index_text
from codenib.scip_interface.scip_pb2 import Index


def _edge_case_index() -> Index:
    """Every field shape and string escape that the text format distinguishes."""

    index = Index()
    tool = index.metadata.tool_info
    tool.name = "tést \U0001f600 中"
    tool.arguments.extend(
        [
            "",
            'a"b',
            "a'b",
            "a\\b",
            "\x00\x01\x1f\x7f",
            "\n\r\t\v\f\b\a",
            "\u0080ÿ ﻿",
        ]
    )
    index.metadata.project_root = "file:///x"
    index.metadata.text_document_encoding = 1
    document = index.documents.add()
    document.relative_path = 'dir/ä "q".py'
    document.language = "python"
    document.text = "def f():\n  return '}{'  # documents {\n"
    document.position_encoding = 2
    occurrence = document.occurrences.add()
    occurrence.range.extend([0, -1, 2147483647, -2147483648])
    occurrence.symbol = "local 1"
    occurrence.symbol_roles = -5
    occurrence.override_documentation.extend(["doc é", ""])
    occurrence.syntax_kind = 7
    occurrence.enclosing_range.extend([1, 2, 3, 4])
    diagnostic = occurrence.diagnostics.add()
    diagnostic.severity = 2
    diagnostic.code = "E1"
    diagnostic.message = "msg\n"
    diagnostic.source = "src"
    diagnostic.tags.extend([1, 2])
    unknown_enum = document.occurrences.add()
    unknown_enum.symbol = "x"
    unknown_enum.syntax_kind = 999
    symbol = document.symbols.add()
    symbol.symbol = "scip-python python p 1 m/f()."
    symbol.documentation.append("```python\ndef f()\n```")
    symbol.kind = 17
    symbol.display_name = "f"
    symbol.enclosing_symbol = "e"
    relationship = symbol.relationships.add()
    relationship.symbol = "y"
    relationship.is_implementation = True
    symbol.signature_documentation.language = "python"
    symbol.signature_documentation.text = "def f()"
    external = index.external_symbols.add()
    external.symbol = "ext"
    external.kind = 5000
    # A field this schema does not know: TextFormat omits it.
    with_unknown = Index()
    with_unknown.ParseFromString(index.SerializeToString() + bytes([0x98, 0x06, 5]))
    return with_unknown


@pytest.mark.parametrize("message", [Index(), _edge_case_index()])
def test_index_text_matches_text_format(message):
    assert index_text(message) == MessageToString(message, as_utf8=True)


def test_index_text_uses_text_format_off_upb(monkeypatch):
    message = _edge_case_index()
    calls = []

    def text_format(index, **kwargs):
        calls.append(kwargs)
        return MessageToString(index, **kwargs)

    monkeypatch.setattr(api_implementation, "Type", lambda: "python")
    monkeypatch.setattr(
        "google.protobuf.text_format.MessageToString", text_format, raising=True
    )
    assert scip_indexer_base.index_text(message) == MessageToString(
        message, as_utf8=True
    )
    assert calls == [{"as_utf8": True}]
