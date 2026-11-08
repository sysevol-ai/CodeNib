# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The values SCIP graph decoders read, from the decoded text or the index.

The graph decoders read ``index.decoded``, the protobuf TextFormat text of
``index.scip``, with regular expressions. What they read depends on that
encoding: the ``range:`` pattern also matches ``enclosing_range:`` values,
TextFormat omits a zero ``symbol_roles``, a negative number matches nothing,
and paths keep their escaped form. A view holds exactly the values the
decoders read from one block, so the same decoder code runs on either
encoding and builds the same graph. Reading the parsed index skips the text
scan, which took most of the decode time.

The parsed-index readers take a fast path only for blocks whose text cannot
read differently: plain strings, without documentation or diagnostics. Any
other block is rendered as text and read by the text reader. Whether the text
splits into the index's own documents, occurrences and symbol blocks, which
the decoders assume, is checked once per index by ``text_blocks_match``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple, Optional

from google.protobuf.text_encoding import CEscape

from .scip_indexer_base import extract_symbol, index_text

if TYPE_CHECKING:
    from .scip_pb2 import Document, Index, Occurrence, SymbolInformation

# Every "range:" value, as the decoders' pattern finds it: range, then
# enclosing_range, then any "range: <digits>" inside a string.
_ANY_RANGE = re.compile(r"range:\s*(\d+)")
# Line-start "range:" values only: the occurrence's own range.
_LINE_RANGE = re.compile(r"(?:^|\n)\s*range:\s*(\d+)")
_ROLES = re.compile(r"symbol_roles:\s*(\d+)")
_ENCLOSING = re.compile(r"enclosing_range:\s*(\d+)")
_RELATIVE_PATH = re.compile(r'relative_path:\s*"([^"]+)"')
_KIND = re.compile(r"(?:^|\n)\s*kind:\s*([A-Za-z][A-Za-z0-9_]*)")
_DOCUMENTATION = re.compile(r'(?:^|\n)\s*documentation:\s*"((?:\\.|[^"\\])*)"')
# Characters that TextFormat escapes in a UTF-8 string.
_TEXT_ESCAPED = re.compile(r"[\"'\\\x00-\x1f\x7f]")
# Raw characters of a documentation string kept escaped in a symbol view; the
# one consumer matches an anchored pattern of at most 25 escaped characters.
DOCUMENTATION_HEAD = 64

_STDLIB = "python-stdlib"
_BLOCK_KEYWORDS = ("documents", "occurrences", "symbols")


class OccurrenceView(NamedTuple):
    """What the decoders read from one ``occurrences { ... }`` block."""

    # Every value the "range:" pattern matches, as strings: the range, then
    # the enclosing range, without negative values.
    ranges: list[str]
    # The occurrence's own non-negative range values.
    line_ranges: list[int]
    # ``extract_symbol`` of the block: unescaped, None when absent.
    symbol: Optional[str]
    # The first "symbol_roles:" value, None when the text has none.
    symbol_roles: Optional[int]
    enclosing_ranges: list[str]
    # Whether "python-stdlib" occurs anywhere in the block.
    python_stdlib: bool


class SymbolView(NamedTuple):
    """What the TypeScript kind reader takes from one ``symbols`` block."""

    symbol: Optional[str]
    # The first line-start "kind:" enum name, None when there is none.
    kind: Optional[str]
    # The first line-start documentation string, escaped as in the text. From
    # the parsed index only its first ``DOCUMENTATION_HEAD`` raw characters.
    documentation: Optional[str]


def occurrence_view_from_text(text: str) -> OccurrenceView:
    roles = _ROLES.search(text)
    return OccurrenceView(
        ranges=_ANY_RANGE.findall(text),
        line_ranges=[int(value) for value in _LINE_RANGE.findall(text)],
        symbol=extract_symbol(text),
        symbol_roles=int(roles.group(1)) if roles else None,
        enclosing_ranges=_ENCLOSING.findall(text),
        python_stdlib=_STDLIB in text,
    )


def occurrence_view(occurrence: "Occurrence") -> OccurrenceView:
    """The view of an occurrence that its decoded text would give."""

    symbol = occurrence.symbol
    if (
        occurrence.override_documentation
        or occurrence.diagnostics
        or _TEXT_ESCAPED.search(symbol)
        or "range:" in symbol
        or "symbol_roles:" in symbol
    ):
        return occurrence_view_from_text(index_text(occurrence))
    line_ranges = [value for value in occurrence.range if value >= 0]
    enclosing = [str(value) for value in occurrence.enclosing_range if value >= 0]
    roles = occurrence.symbol_roles
    return OccurrenceView(
        ranges=[str(value) for value in line_ranges] + enclosing,
        line_ranges=line_ranges,
        symbol=symbol or None,
        symbol_roles=roles if roles > 0 else None,
        enclosing_ranges=enclosing,
        python_stdlib=_STDLIB in symbol,
    )


def document_path_from_text(text: str) -> Optional[str]:
    match = _RELATIVE_PATH.search(text)
    return match.group(1) if match else None


def document_path(document: "Document") -> Optional[str]:
    """``relative_path`` as the decoders read it: escaped, up to a quote."""

    path = document.relative_path
    if not path:
        return None
    if _TEXT_ESCAPED.search(path):
        return CEscape(path, as_utf8=True).split('"', 1)[0]
    return path


def symbol_view_from_text(text: str) -> SymbolView:
    kind = _KIND.search(text)
    documentation = _DOCUMENTATION.search(text)
    return SymbolView(
        symbol=extract_symbol(text),
        kind=kind.group(1) if kind else None,
        documentation=documentation.group(1) if documentation else None,
    )


def symbol_view(info: "SymbolInformation") -> SymbolView:
    """The view of a symbol that its decoded text would give."""

    from .scip_pb2 import SymbolInformation

    symbol = info.symbol
    nested = info.signature_documentation
    kind_name = None
    if info.kind:
        try:
            kind_name = SymbolInformation.Kind.Name(info.kind)
        except ValueError:
            kind_name = None  # TextFormat prints the number, which no name matches
    # A signature document's own symbols would be read as this one's kind.
    if not symbol or _TEXT_ESCAPED.search(symbol) or nested.symbols:
        return symbol_view_from_text(index_text(info))
    documentation = None
    if info.documentation:
        documentation = CEscape(
            info.documentation[0][:DOCUMENTATION_HEAD], as_utf8=True
        )
    return SymbolView(symbol=symbol, kind=kind_name, documentation=documentation)


def text_block_counts(text: str) -> dict[str, int]:
    """How often each block keyword occurs anywhere in the decoded text."""

    return {keyword: text.count(keyword) for keyword in _BLOCK_KEYWORDS}


def text_blocks_match(
    index: "Index", text: str, counts: Optional[dict[str, int]] = None
) -> bool:
    """Whether the decoded text splits into exactly the index's own blocks.

    The text decoders split on the words ``documents``, ``occurrences`` and
    ``symbols`` followed by ``{``, wherever they occur, and read a document's
    first ``relative_path``. That gives the index's own blocks unless a string
    holds one of those words before a brace, a document has no path, or a
    symbol's signature document has occurrences or symbols of its own. Then
    the caller decodes the text instead.
    """

    expected = {
        "documents": len(index.documents),
        "occurrences": 0,
        # "external_symbols" contains the word too.
        "symbols": len(index.external_symbols),
    }
    for document in index.documents:
        if not document.relative_path:
            return False
        expected["occurrences"] += len(document.occurrences)
        expected["symbols"] += len(document.symbols)
        for info in document.symbols:
            nested = info.signature_documentation
            if nested.occurrences or nested.symbols:
                return False
    for info in index.external_symbols:
        nested = info.signature_documentation
        if nested.occurrences or nested.symbols:
            return False
    counts = counts if counts is not None else text_block_counts(text)
    if counts == expected:
        return True
    # A string mentions a keyword; it only matters before a brace.
    return all(
        len(re.findall(keyword + r"\s*\{", text)) == expected[keyword]
        for keyword in _BLOCK_KEYWORDS
    )
