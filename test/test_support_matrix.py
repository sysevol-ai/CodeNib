# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the generated benchmark and context-method support tables."""

from __future__ import annotations

import copy
import shutil

import pytest

from scripts.support_matrix import (
    _DOCS_ROOT,
    _PROJECT_ROOT,
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


@pytest.mark.parametrize(
    "page,kind,mutation",
    [
        ("concepts/methods.md", "methods", "remove"),
        ("evaluation/index.md", "grid:L1", "remove"),
        ("evaluation/index.md", "grid:L2+L3", "remove"),
        ("evaluation/index.md", "grid:L2+L3", "mismatch"),
        ("evaluation/index.md", "benchmarks", "duplicate"),
    ],
)
def test_missing_or_malformed_generated_tables_fail_validation(
    tmp_path, page, kind, mutation
):
    # Preserve the real validation inputs while isolating document mutations.
    for child in _PROJECT_ROOT.iterdir():
        if child.name == "docs":
            shutil.copytree(child, tmp_path / "docs")
        elif child.name != ".git":
            (tmp_path / child.name).symlink_to(
                child, target_is_directory=child.is_dir()
            )
    data = load_data()
    assert validate(data, tmp_path) == []
    path = tmp_path / "docs" / page
    text = path.read_text()
    begin = f"<!-- BEGIN CODENIB_SUPPORT_MATRIX:{kind} -->"
    end = f"<!-- END CODENIB_SUPPORT_MATRIX:{kind} -->"
    start = text.index(begin)
    stop = text.index(end, start) + len(end)
    if mutation == "remove":
        text = text[:start] + text[stop:]
    elif mutation == "mismatch":
        text = text.replace(end, "<!-- END CODENIB_SUPPORT_MATRIX:wrong -->", 1)
    else:
        text += "\n" + text[start:stop]
    path.write_text(text)
    errors = validate(data, tmp_path)
    assert any(f"required generated block {kind}" in error for error in errors)
    if mutation == "mismatch":
        assert any("malformed generated" in error for error in errors)
