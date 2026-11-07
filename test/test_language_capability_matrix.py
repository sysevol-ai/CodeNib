# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the generated language capability matrix documentation."""

from __future__ import annotations

import re
from pathlib import Path

from scripts.language_capability_matrix import (
    render_block,
    render_landing_strip,
    replace_block,
    replace_landing_strip,
)


def test_language_capability_doc_is_current():
    path = Path("docs/language_capabilities.md")
    text = path.read_text(encoding="utf-8")

    assert replace_block(text, render_block()) == text


def test_homepage_language_strip_is_current():
    path = Path("landing/index.html")
    text = path.read_text(encoding="utf-8")

    assert replace_landing_strip(text, render_landing_strip()) == text


def test_every_language_has_an_icon():
    strip = render_landing_strip()

    for icon in re.findall(r'<use href="#lang-([\w-]+)"/>', strip):
        assert f'<symbol id="lang-{icon}"' in strip
