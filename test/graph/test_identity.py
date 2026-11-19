# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for reading a graph vertex name without its position marker."""

from __future__ import annotations

import pytest

from codenib.graph.identity import short_name, strip_position_markers, symbol_id


@pytest.mark.parametrize(
    ("name", "stripped"),
    [
        ("a.py:C.total(_codenib_l12_c8)", "a.py:C.total()"),
        # Some builders already append the call parentheses.
        ("a.py:C.total(_codenib_l12_c8)()", "a.py:C.total()"),
        ("a.py:Form@codenib-l30-c4", "a.py:Form"),
        ("a.py:Form@codenib-l30-c4.clean(_codenib_l31_c8)", "a.py:Form.clean()"),
        ("a.py:plain()", "a.py:plain()"),
        ("a.py", "a.py"),
    ],
)
def test_markers_are_removed_and_a_function_keeps_its_parentheses(name, stripped):
    assert strip_position_markers(name) == stripped


def test_symbol_id_and_short_name_ignore_markers():
    assert symbol_id("a.py:C.total(_codenib_l12_c8)") == "a.py:C.total"
    assert symbol_id("a.py:Form@codenib-l30-c4.clean()") == "a.py:Form.clean"
    assert symbol_id("a.py") == "a.py"
    assert short_name("a.py:C.total(_codenib_l12_c8)()") == "total"
    assert short_name("t.py:test_x().Form@codenib-l12-c4") == "Form"
