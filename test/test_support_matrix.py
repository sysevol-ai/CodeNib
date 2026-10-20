# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the generated benchmark and context-method support tables."""

from __future__ import annotations

import copy

from scripts.support_matrix import (
    _DOCS_ROOT,
    _relative,
    load_data,
    pages_with_blocks,
    render_page,
    validate,
)


def test_support_matrix_data_matches_code_and_docs():
    assert validate(load_data()) == []


def test_support_matrix_pages_are_current():
    data = load_data()
    pages = pages_with_blocks()
    assert pages, "no page embeds a generated support-matrix block"
    for path in pages:
        page = path.relative_to(_DOCS_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        assert render_page(text, page, data) == text, page


def test_unclaimed_benchmark_module_is_reported():
    data = copy.deepcopy(load_data())
    data["shared_modules"] = []

    errors = validate(data)

    assert any(
        "codenib/eval/benchmarks/__init__.py is not claimed" in e for e in errors
    )


def test_unknown_cell_level_is_reported():
    data = copy.deepcopy(load_data())
    data["methods"][0]["cells"]["codenib_base"] = {"level": "Great"}

    errors = validate(data)

    assert any("unknown level Great" in error for error in errors)


def test_links_are_rewritten_per_page():
    link = "evaluation/grep_jev.md#what-was-measured"

    assert (
        _relative(link, "concepts/methods.md")
        == "../evaluation/grep_jev.md#what-was-measured"
    )
    assert _relative(link, "evaluation/index.md") == "grep_jev.md#what-was-measured"
    assert _relative("evaluation/index.md#l3", "evaluation/index.md") == "#l3"
