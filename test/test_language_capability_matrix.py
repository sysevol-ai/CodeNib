# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the generated language capability matrix documentation."""

from __future__ import annotations

from pathlib import Path

from scripts.language_capability_matrix import (
    render_block,
    render_landing_grid,
    replace_block,
    replace_landing_grid,
)


def test_language_capability_doc_is_current():
    path = Path("docs/language_capabilities.md")
    text = path.read_text(encoding="utf-8")

    assert replace_block(text, render_block()) == text


def test_homepage_language_grid_is_current():
    path = Path("landing/index.html")
    text = path.read_text(encoding="utf-8")

    assert replace_landing_grid(text, render_landing_grid()) == text
