# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the dark recoloring of codenib.ai blog figures."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.blog_figure_theme import (
    CARD,
    INK,
    INK_2,
    LABEL_BOX,
    dark_figure,
    is_dark_figure,
)

PAGES = [Path("landing/index.html"), *Path("landing/blogs").rglob("index.html")]
FIGURES = sorted(
    {
        Path("landing/assets/blogs") / name
        for page in PAGES
        for name in re.findall(
            r"/assets/blogs/([\w-]+\.svg)", page.read_text(encoding="utf-8")
        )
    }
)


def test_text_and_shapes_map_separately():
    svg = dark_figure(
        '<svg><rect fill="#ffffff"/><rect fill="#0f172a"/>'
        '<text fill="#0f172a">ink</text><text fill="#ffffff">on box</text></svg>'
    )

    assert f'<rect fill="{CARD}"/>' in svg
    assert f'<rect fill="{LABEL_BOX}"/>' in svg
    assert f'<text fill="{INK}">ink</text>' in svg
    assert '<text fill="#ffffff">on box</text>' in svg


def test_stylesheet_classes_are_text():
    svg = dark_figure("<svg><defs><style>.a{fill:#0f172a}</style></defs></svg>")

    assert f".a{{fill:{INK}}}" in svg


def test_stylesheet_classes_on_shapes_are_shapes():
    svg = dark_figure(
        "<svg><style>.card{fill:#ffffff}.label{fill:#0f172a}</style>"
        '<rect class="card"/><text class="label">x</text></svg>'
    )

    assert f".card{{fill:{CARD}}}" in svg
    assert f".label{{fill:{INK}}}" in svg


def test_dark_label_boxes_and_arrowheads_differ():
    svg = dark_figure(
        '<svg><defs><marker id="a"><path fill="#0f172a"/></marker></defs>'
        '<path fill="#0f172a"/></svg>'
    )

    assert f'<marker id="a"><path fill="{INK}"/></marker>' in svg
    assert f'</defs><path fill="{LABEL_BOX}"/>' in svg


def test_dark_text_stays_readable():
    svg = dark_figure('<svg><text fill="#111827">x</text></svg>')

    assert 'fill="#111827"' not in svg


def test_group_paint_follows_its_children():
    svg = dark_figure('<svg><g fill="#f8fafc"/><g fill="#334155"/></svg>')

    assert '<g fill="#334155"/>' not in svg
    assert f'<g fill="{INK_2}"/>' in svg


def test_recoloring_twice_changes_nothing():
    once = dark_figure('<?xml version="1.0"?><svg><rect fill="#ffffff"/></svg>')

    assert dark_figure(once) == once


@pytest.mark.parametrize("path", FIGURES, ids=lambda path: path.name)
def test_published_figures_are_dark(path: Path):
    text = path.read_text(encoding="utf-8")

    assert is_dark_figure(text)
    shapes = [
        attrs
        for name, attrs in re.findall(r"<([a-zA-Z]+)\b([^<>]*)>", text)
        if name not in {"text", "tspan"}
    ]
    assert not [attrs for attrs in shapes if re.search(r'fill="#f{3}(f{3})?"', attrs)]
