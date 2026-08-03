# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit coverage for shared SCIP decoder normalization helpers."""

from codenib.scip_interface.scip_decode_utils import (
    format_unified_name,
    normalize_scip_descriptor,
    scip_symbol_descriptor,
)


def test_scip_symbol_descriptor_filters_unknown_tools():
    symbol = "semanticdb maven app app 1.0 app/Foo#run()."

    assert scip_symbol_descriptor(symbol, {"semanticdb"}) == "app/Foo#run()."
    assert scip_symbol_descriptor(symbol, {"scip-ruby"}) == ""
    assert scip_symbol_descriptor("local 0", {"semanticdb"}) == ""
    assert scip_symbol_descriptor(None, {"semanticdb"}) == ""


def test_normalize_scip_descriptor_strips_common_suffixes():
    assert normalize_scip_descriptor("app/Foo#run().") == "app/Foo#run"
    assert normalize_scip_descriptor("app/Foo#") == "app/Foo"
    assert (
        normalize_scip_descriptor(
            "app/Foo#run().",
            strip_empty_call=False,
            strip_trailing_hash=False,
        )
        == "app/Foo#run()"
    )


def test_format_unified_name_uses_display_when_possible():
    assert format_unified_name("src/Foo.java", "Foo.run()", "app/Foo#run") == (
        "src/Foo.java:Foo.run()"
    )
    assert format_unified_name("", "Foo", "app/Foo") == "Foo"
    assert format_unified_name("", "", "app/Foo") == "app/Foo"
