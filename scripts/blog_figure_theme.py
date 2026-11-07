#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Recolor codenib.ai blog figures for the site's dark pages.

The figures were drawn on white. An SVG loaded through ``<img>`` cannot read the
page's colors, so each file carries its own. This maps them onto the site's
neutral greys and keeps data colors recognisable. Text and shapes map separately
because the same slate is ink on a card but the fill of a dark label box.
"""

from __future__ import annotations

import argparse
import colorsys
import re
from pathlib import Path

DARK_MARKER = 'data-theme="dark"'

CARD = "#1a1b1e"
RAISED = "#212327"
LINE = "#303338"
LINE_STRONG = "#44484f"
LABEL_BOX = "#0c0d0f"
INK = "#e8eaed"
INK_2 = "#bdc1c6"
INK_3 = "#9aa0a6"
BLUE_TEXT = "#8ab4f8"
TEAL_TEXT = "#5fd4c4"

TEXT_COLORS = {
    "#0f172a": INK,
    "#334155": INK_2,
    "#475569": INK_2,
    "#64748b": INK_3,
    "#94a3b8": INK_3,
    "#cbd5e1": INK_2,
    "#2563eb": BLUE_TEXT,
    "#1d4ed8": BLUE_TEXT,
    "#1e3a8a": "#aecbfa",
    "#0d9488": TEAL_TEXT,
    "#0f766e": TEAL_TEXT,
    "#0ea5e9": "#7cc9f2",
    "#166534": "#7fdca0",
}

SHAPE_COLORS = {
    "#ffffff": CARD,
    "#fff": CARD,
    "#f8fafc": RAISED,
    "#e2e8f0": LINE,
    "#e0e6ee": LINE,
    "#dbe5f1": LINE,
    "#d4ddec": LINE,
    "#cbd5e1": LINE_STRONG,
    "#bfcbd9": LINE_STRONG,
    "#94a3b8": "#80868b",
    "#64748b": "#8d939b",
    "#2563eb": "#5f93ee",
    "#0ea5e9": "#2aa8e0",
    "#0d9488": "#1d9c8f",
    "#0f766e": "#1d9c8f",
    "#16a34a": "#2fae5e",
    "#93c5fd": "#5f93ee",
    "#bfdbfe": "#38557f",
    "#93b4e8": "#4a6a9c",
    "#dbeafe": "#263a5c",
    "#bae6fd": "#2c5a70",
    "#99f6e4": "#1f5e56",
    "#a7f3d0": "#1f5e56",
    "#99c9c0": "#3b6f68",
    "#eff6ff": "#1c2636",
    "#f0fdfa": "#16272a",
    "#ecfdf5": "#16272a",
    "#172554": "#1d1f23",
    "#1e3a8a": "#222428",
}

# Data colors that already read well on a dark card.
KEEP = {"#eb6834", "#c66b2a", "#4fd1e1", "#42d7e8", "#7186f3", "#818cf8", "#5eead4"}

_PAINT_ATTR = re.compile(r'\b(fill|stroke|stop-color)="(#[0-9a-fA-F]{3,6})"')
_CSS_PAINT = re.compile(r"\b(fill|stroke|stop-color)(\s*:\s*)(#[0-9a-fA-F]{3,6})")
_STYLE_BLOCK = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.S)
_TAG = re.compile(r"<([a-zA-Z][\w-]*)\b([^<>]*)>")
_MARKER_BLOCK = re.compile(r"<marker\b.*?</marker>", re.S)
_CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_CLASS_ATTR = re.compile(r'\bclass="([^"]+)"')
_DARK_ROOT = re.compile(r'<svg\b[^>]*\bdata-theme="dark"')
_SHAPE_TAGS = {"rect", "path", "circle", "ellipse", "line", "polygon", "polyline"}


def _muted(color: str, readable: bool = False) -> str:
    """Take the navy out of an unlisted color, so every figure shares one grey.

    With ``readable``, a dark color is also lifted so text stays legible.
    """

    value = color.lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    red, green, blue = (int(value[index : index + 2], 16) / 255 for index in (0, 2, 4))
    hue, light, saturation = colorsys.rgb_to_hls(red, green, blue)
    if readable and light < 0.55:
        light = 0.92 - light * 0.4
    channels = colorsys.hls_to_rgb(hue, light, saturation * 0.12)
    return "#" + "".join(f"{round(channel * 255):02x}" for channel in channels)


def map_text(color: str) -> str:
    """Return a text color's dark-page equivalent."""

    key = color.lower()
    if key in TEXT_COLORS:
        return TEXT_COLORS[key]
    if key in KEEP or key in {"#ffffff", "#fff"}:
        return key
    return _muted(key, readable=True)


def map_shape(color: str, tag: str, prop: str) -> str:
    """Return a shape color's dark-page equivalent."""

    key = color.lower()
    if key == "#0f172a":
        return LABEL_BOX if tag in {"rect", "path"} and prop == "fill" else INK
    if key in SHAPE_COLORS:
        return SHAPE_COLORS[key]
    if key in KEEP:
        return key
    return TEXT_COLORS.get(key) or _muted(key)


def _recolor_tag(match: re.Match[str]) -> str:
    tag, attrs = match.group(1), match.group(2)
    is_text = tag in {"text", "tspan"}

    def paint(prop: str, color: str) -> str:
        # A group's paint reaches its children: pale fills are cards, the rest text.
        as_text = is_text or (tag == "g" and color.lower() not in SHAPE_COLORS)
        return map_text(color) if as_text else map_shape(color, tag, prop)

    attrs = _PAINT_ATTR.sub(
        lambda item: f'{item.group(1)}="{paint(item.group(1), item.group(2))}"', attrs
    )
    attrs = _CSS_PAINT.sub(
        lambda item: f"{item.group(1)}{item.group(2)}"
        f"{paint(item.group(1), item.group(3))}",
        attrs,
    )
    return f"<{tag}{attrs}>"


def _shape_classes(svg: str) -> set[str]:
    """Return classes used only on drawn shapes, never on text or groups."""

    shapes: set[str] = set()
    others: set[str] = set()
    for tag, attrs in _TAG.findall(svg):
        found = _CLASS_ATTR.search(attrs)
        if found:
            (shapes if tag in _SHAPE_TAGS else others).update(found.group(1).split())
    return shapes - others


def _recolor_rule(rule: re.Match[str], shape_classes: set[str]) -> str:
    selector, declarations = rule.group(1), rule.group(2)
    is_shape = any(
        name in shape_classes for name in re.findall(r"\.([\w-]+)", selector)
    ) or any(re.search(rf"\b{tag}\b", selector) for tag in _SHAPE_TAGS)

    def paint(item: re.Match[str]) -> str:
        prop, color = item.group(1), item.group(3)
        mapped = map_shape(color, "rect", prop) if is_shape else map_text(color)
        return f"{prop}{item.group(2)}{mapped}"

    return f"{selector}{{{_CSS_PAINT.sub(paint, declarations)}}}"


def _recolor_style(match: re.Match[str], shape_classes: set[str]) -> str:
    body = _CSS_RULE.sub(
        lambda rule: _recolor_rule(rule, shape_classes), match.group(2)
    )
    return f"{match.group(1)}{body}{match.group(3)}"


def _recolor_marker(match: re.Match[str]) -> str:
    # An arrowhead belongs to its line, so it stays visible on a dark card.
    return _PAINT_ATTR.sub(
        lambda item: f'{item.group(1)}="'
        f'{map_shape(item.group(2), "marker", item.group(1))}"',
        match.group(0),
    )


def is_dark_figure(svg: str) -> bool:
    """Return whether ``svg`` has already been recolored."""

    return _DARK_ROOT.search(svg) is not None


def dark_figure(svg: str) -> str:
    """Return ``svg`` recolored for a dark page. Marked files pass through."""

    if is_dark_figure(svg):
        return svg
    shape_classes = _shape_classes(svg)
    blocks: list[str] = []

    def hold(replacement: str) -> str:
        blocks.append(replacement)
        return f"\0{len(blocks) - 1}\0"

    held = _STYLE_BLOCK.sub(lambda item: hold(_recolor_style(item, shape_classes)), svg)
    held = _MARKER_BLOCK.sub(lambda item: hold(_recolor_marker(item)), held)
    recolored = _TAG.sub(_recolor_tag, held)
    restored = re.sub(r"\0(\d+)\0", lambda item: blocks[int(item.group(1))], recolored)
    return restored.replace("<svg", f"<svg {DARK_MARKER}", 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.paths:
        path.write_text(dark_figure(path.read_text(encoding="utf-8")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
