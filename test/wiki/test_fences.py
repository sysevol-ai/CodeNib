# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from codenib.wiki.fences import count_code_fences, fence_marker, strip_code_fences
from codenib.wiki.quality import prose_integrity_report


def test_strip_code_fences_matches_closers_of_the_same_length():
    markdown = (
        "Intro prose. [E1]\n\n"
        "````rust hl=2\n"
        "//! ```text\n"
        "//!  /--- line number column\n"
        "//! ```\n"
        "fn render() {}\n"
        "````\n\n"
        "Source excerpt. [E1]\n"
    )

    stripped = strip_code_fences(markdown)

    assert "line number" not in stripped
    assert "Intro prose. [E1]" in stripped
    assert "Source excerpt. [E1]" in stripped
    assert count_code_fences(markdown) == 1


def test_strip_code_fences_handles_tildes_and_unterminated_fences():
    assert strip_code_fences("a\n\n~~~\nx\n~~~\n\nb").split() == ["a", "b"]
    assert strip_code_fences("a\n\n```\nnever closed").split() == ["a"]


def test_fence_marker_outgrows_backticks_in_the_source():
    assert fence_marker(["plain"]) == "```"
    assert fence_marker(["//! ```text", "x"]) == "````"
    assert fence_marker(["a `````b`````"]) == "``````"


def test_prose_integrity_ignores_evidence_ids_quoted_inside_code():
    markdown = (
        "`vo_raise()` reports the datatype. [E1]\n\n"
        "```python\n"
        "    vo_raise(E06, (field.datatype, field.ID), config)\n"
        "```\n\n"
        "Source excerpt. [E1]\n"
    )

    assert prose_integrity_report(markdown)["narrated_evidence_ids"] == []
