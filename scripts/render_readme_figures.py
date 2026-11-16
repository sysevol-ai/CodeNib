#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
"""Render the README's figures as self-contained SVG files.

Each figure makes one point and is written twice to ``assets/readme/``, as
``NAME-light.svg`` and ``NAME-dark.svg``, for the ``<picture>`` element that
follows the reader's GitHub theme. The rules these figures follow are in
``AGENTS.md`` under "Figures and Images".

A figure is drawn at the width GitHub shows it, so a font size here is the
size the reader sees. It has no background of its own and sits on the page.

Uses the drawing helpers of ``render_contextbench_blog_figures.py`` and needs
the same fontTools with WOFF2 support::

    python scripts/render_readme_figures.py
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path

from blog_figure_theme import dark_figure
from render_contextbench_blog_figures import (
    ACCENT,
    INK_2,
    LINE,
    LINE_STRONG,
    ROOT,
    TEAL_DARK,
    Figure,
)

OUT = ROOT / "assets" / "readme"

# The width of the README column on github.com.
WIDTH = 830

# The smallest text a figure may use, in pixels as shown.
MIN_TEXT = 13

STYLES = {
    "group": {"size": 15, "weight": 600},
    "group-r": {"size": 15, "weight": 600, "fill": TEAL_DARK},
    "piece": {"size": 15},
    "file": {"size": 14, "weight": 500, "family": "mono"},
    "num": {"size": 13, "family": "mono", "fill": INK_2},
    "legend": {"size": 13.5, "fill": INK_2},
}

# The pieces an agent fetches, in the order docs/threads.md lists them.
PIECES = [
    "The issue",
    "Its comments",
    "The linked pull request",
    "The reviews",
    "The checks",
    "A failed log",
]

_BACKGROUND = re.compile(r'<rect width="\d+" height="\d+" fill="#[0-9a-fA-F]+"/>\n?')


def thread_calls() -> str:
    """Six calls that each resend the conversation, against one read."""
    fig = Figure(WIDTH, 338, STYLES)
    x, bar_x, pitch, bar_h = 2, 236, 30, 14
    new, gap = 50, 3

    fig.text(x, 16, "The agent reads the thread itself", "group")
    top = 34
    for i, piece in enumerate(PIECES):
        y = top + i * pitch
        fig.text(x, y + 16, f"{i + 1}", "num")
        fig.text(x + 24, y + 16, piece, "piece")
        carried = 30 + i * 88
        fig.rect(bar_x, y + 4, carried, bar_h, rx=2, fill=LINE_STRONG)
        fig.rect(bar_x + carried + gap, y + 4, new, bar_h, rx=2, fill=ACCENT)

    rule = top + len(PIECES) * pitch + 12
    fig.path(f"M 0 {rule} H {WIDTH}", stroke=LINE, sw=1)

    fig.text(x, rule + 30, "codenib threads prepares it once", "group-r")
    y = rule + 44
    fig.text(x, y + 16, "1", "num")
    fig.text(x + 24, y + 16, "THREAD_CONTEXT.md", "file")
    for i in range(len(PIECES)):
        fig.rect(bar_x + i * (new + gap), y + 4, new, bar_h, rx=2, fill=ACCENT)

    legend = y + 56
    fig.rect(x, legend - 10, 10, 10, rx=2, fill=LINE_STRONG)
    fig.text(x + 18, legend, "the conversation so far, sent again", "legend")
    offset = fig.measure("the conversation so far, sent again", "legend") + 44
    fig.rect(x + offset, legend - 10, 10, 10, rx=2, fill=ACCENT)
    fig.text(x + offset + 18, legend, "a piece of the thread", "legend")
    return fig.svg(
        "One read of a GitHub thread instead of a tool call per piece",
        "Top: an agent reading a thread itself makes six calls, for the issue, "
        "its comments, the linked pull request, the reviews, the checks and a "
        "failed log. Each call sends the conversation so far again, so the "
        "sixth call sends the most. Bottom: codenib threads writes the same "
        "six pieces into one document, THREAD_CONTEXT.md, which the agent "
        "reads once. The bar lengths sketch the mechanism; they are not "
        "measurements.",
    )


FIGURES = {"thread-calls": thread_calls}


def on_page(svg: str) -> str:
    """Drop the figure's own background, so it sits on the reader's page."""
    return _BACKGROUND.sub("", svg, count=1)


def main() -> None:
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, render in FIGURES.items():
        light = render()
        for theme, svg in (("light", light), ("dark", dark_figure(light))):
            path = args.out / f"{name}-{theme}.svg"
            path.write_text(on_page(svg), encoding="utf-8")
            shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            print(f"{shown}  {path.stat().st_size // 1024} KiB")


if __name__ == "__main__":
    main()
