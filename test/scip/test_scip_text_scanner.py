# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Preserve shared TextFormat literal and block boundaries during scanning."""

import json

import pytest

from codenib.scip_interface.scip_indexer_base import extract_scip_blocks, extract_symbol


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('symbol: "plain"', "plain"),
        ('symbol: ""', ""),
        ('symbol:\t "包😀.run()."', "包😀.run()."),
        (r'symbol: "a\"{}\\suffix"', 'a"{}\\suffix'),
        (r'symbol: "a\nb\tc\rd"', "a\nb\tc\rd"),
        (r'''symbol: "it\'s"''', "it's"),
        # Keep the shared decoder's existing lenient escape semantics.
        (r'symbol: "\x41\q\0"', "x41q0"),
        ('other_symbol: "wrong" /symbol: "right"', "right"),
        ('symbol: false symbol: "right"', "right"),
        ('symbol:\n "wrong"', None),
        ('symbol: "unterminated\\', None),
        ("", None),
    ],
)
def test_symbol_literal_boundaries_and_escape_values(text, expected):
    assert extract_symbol(text) == expected


def test_block_depth_ignores_quoted_braces_and_escaped_quotes():
    symbol = 'scip-typescript npm app 1.0 `object"{}\\suffix`.'
    first = f" symbol: {json.dumps(symbol, ensure_ascii=False)} nested {{ value: 1 }} "
    second = ' symbol: "next" '
    text = f"occurrences {{{first}}}\noccurrences {{{second}}}"

    assert extract_scip_blocks(text, "occurrences") == [first, second]
    assert extract_symbol(first) == symbol


def test_block_keyword_boundary_and_whitespace_stay_exact():
    text = (
        'other_occurrences { symbol: "wrong" } '
        '/occurrences { symbol: "wrong" } '
        'éoccurrences { symbol: "wrong" } '
        'occurrences\n\t{ symbol: "right" }'
    )

    assert extract_scip_blocks(text, "occurrences") == [' symbol: "right" ']


@pytest.mark.parametrize(
    "tail",
    [
        'occurrences { symbol: "unterminated }',
        'occurrences { symbol: "trailing\\',
        'occurrences { nested { symbol: "missing outer brace" }',
    ],
)
def test_incomplete_block_does_not_discard_an_earlier_complete_block(tail):
    text = 'occurrences { symbol: "complete" } ' + tail

    assert extract_scip_blocks(text, "occurrences") == [' symbol: "complete" ']


def test_backslash_outside_a_string_does_not_escape_the_opening_quote():
    body = r' \ "quoted{}" nested { value: 1 } '

    assert extract_scip_blocks("occurrences {" + body + "}", "occurrences") == [body]


@pytest.mark.parametrize("value", ["a" * 100000, '{}\\"\n😀' * 1000])
def test_long_literal_preserves_escaped_values_and_complete_block_boundary(value):
    body = f" symbol: {json.dumps(value, ensure_ascii=False)} "

    assert extract_symbol(body) == value
    assert extract_scip_blocks("occurrences {" + body + "}", "occurrences") == [body]
    assert (
        extract_scip_blocks('occurrences { symbol: "' + value + "\\", "occurrences")
        == []
    )
