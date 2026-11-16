# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
"""The README's drawn figures follow the rules in AGENTS.md."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
RAW = "https://raw.githubusercontent.com/sysevol-ai/CodeNib/main/"
MIN_TEXT = 13
WIDTH_ATTR = 'width="830"'

PICTURES = re.findall(r"<picture>(.*?)</picture>", README, re.S)
DRAWN = sorted((ROOT / "assets" / "readme").glob("*.svg"))


def test_each_drawn_figure_ships_in_both_themes_and_is_used() -> None:
    assert DRAWN, "expected drawn figures in assets/readme"
    names = {path.name for path in DRAWN}
    for name in names:
        stem, _, theme = name.removesuffix(".svg").rpartition("-")
        assert theme in {"light", "dark"}, name
        assert {f"{stem}-light.svg", f"{stem}-dark.svg"} <= names, name
        assert f"{RAW}assets/readme/{name}" in README, f"{name} is not in the README"


def test_each_picture_follows_the_reader_theme() -> None:
    assert PICTURES
    for picture in PICTURES:
        dark = re.search(
            r'<source media="\(prefers-color-scheme: dark\)" srcset="([^"]+)"',
            picture,
        )
        light = re.search(r'<img src="([^"]+)"', picture)
        assert dark and light, picture
        assert dark.group(1).endswith("-dark.svg")
        assert light.group(1).endswith("-light.svg")
        assert WIDTH_ATTR in picture
        alt = re.search(r'alt="([^"]*)"', picture)
        assert alt and len(alt.group(1)) > 60, "alt text states the point"


@pytest.mark.parametrize("path", DRAWN, ids=lambda path: path.name)
def test_drawn_figure_is_readable_at_the_column_width(path: Path) -> None:
    svg = path.read_text(encoding="utf-8")
    root = re.search(r"<svg\b[^>]*>", svg).group(0)
    assert WIDTH_ATTR in root
    sizes = [float(size) for size in re.findall(r"font-size:([\d.]+)px", svg)]
    assert sizes and min(sizes) >= MIN_TEXT
    # No surface of its own: the figure sits on the reader's page.
    assert not re.search(r'<rect width="\d+" height="\d+" fill=', svg)
    assert "@font-face" in svg


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_caller_path_shows_the_recorded_session(theme: str) -> None:
    record = json.loads(
        (ROOT / "landing/assets/demos/codegraph-claude.json").read_text("utf-8")
    )
    svg = (ROOT / f"assets/readme/caller-path-{theme}.svg").read_text("utf-8")
    drawn = set(re.findall(r">([\w.]+\.py:\d+)<", svg))
    result = record["dependency_result"]
    deepest = max(node["depth"] for node in result["nodes"])
    path = {
        f"{Path(node['file']).name}:{node['line']}"
        for node in result["nodes"]
        if node["depth"] < deepest
    }
    root = next(a for a in record["source_anchors"] if a["symbol"] == result["root"])
    path.add(f"{Path(root['file']).name}:{root['start_line']}")
    assert drawn == path
    for node in result["nodes"]:
        if node["depth"] == deepest:
            assert f"{node['name'].rpartition(':')[2]}<" in svg


def test_readme_images_are_the_two_wiki_screenshots_and_drawn_figures() -> None:
    used = set(re.findall(rf"{re.escape(RAW)}assets/readme/([\w.-]+)", README))
    shipped = {path.name for path in (ROOT / "assets" / "readme").iterdir()}
    assert used == shipped
