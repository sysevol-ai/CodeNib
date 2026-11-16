#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
"""Render the README's figures as self-contained SVG files.

Each figure makes one point and is written twice to ``assets/readme/``, as
``NAME-light.svg`` and ``NAME-dark.svg``, for the ``<picture>`` element that
follows the reader's GitHub theme. The rules these figures follow are in
``AGENTS.md`` under "Figures and Images".

A figure is drawn at the width GitHub shows it, so a font size here is the
size the reader sees. It has no background of its own, and its colours are
GitHub's text colours for each theme, so it sits on the page.

Uses the ``Figure`` helper of ``render_contextbench_blog_figures.py`` and
needs the same fontTools with WOFF2 support::

    python scripts/render_readme_figures.py
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path

from render_contextbench_blog_figures import ROOT, Figure

OUT = ROOT / "assets" / "readme"

# The width of the README column on github.com.
WIDTH = 830

# GitHub's own text and border colours, so a figure reads as part of the page.
THEMES = {
    "light": {
        "ink": "#1f2328",
        "muted": "#59636e",
        "base": "#afb8c1",
        "rule": "#d1d9e0",
        "accent": "#0969da",
    },
    "dark": {
        "ink": "#f0f6fc",
        "muted": "#9198a1",
        "base": "#656c76",
        "rule": "#3d444d",
        "accent": "#4493f8",
    },
}

# Input tokens of the prepared context against the agent reading with gh, on
# the 76 pull request takeovers in docs/threads.md: (agent, model, change).
TAKEOVERS = [
    ("Codex", "gpt-6.1-sol", -21),
    ("Claude Code", "Claude Opus 5.5", -45),
]

_BACKGROUND = re.compile(r'<rect width="\d+" height="\d+" fill="#[0-9a-fA-F]+"/>\n?')


def thread_tokens(c: dict[str, str]) -> str:
    """The same 76 takeovers: input tokens reading with gh, and prepared."""
    styles = {
        "agent": {"size": 17, "weight": 620, "fill": c["ink"]},
        "model": {"size": 15, "fill": c["muted"]},
        "row": {"size": 15, "fill": c["muted"]},
        "row-on": {"size": 15, "weight": 560, "fill": c["ink"]},
        "change": {"size": 24, "weight": 650, "spacing": -0.02, "fill": c["accent"]},
    }
    bar_x, full, bar_h, group = 168, 500, 28, 142
    fig = Figure(WIDTH, group * len(TAKEOVERS) - 22, styles)
    for i, (agent, model, change) in enumerate(TAKEOVERS):
        top = i * group
        if i:
            fig.path(f"M 0 {top - 22} H {WIDTH}", stroke=c["rule"], sw=1)
        fig.text(0, top + 16, agent, "agent")
        fig.text(fig.measure(agent, "agent") + 10, top + 16, model, "model")
        y = top + 38
        fig.text(0, y + 19, "reads with gh", "row")
        fig.rect(bar_x, y, full, bar_h, rx=4, fill=c["base"])
        y += bar_h + 10
        width = full * (100 + change) / 100
        fig.text(0, y + 19, "codenib threads", "row-on")
        fig.rect(bar_x, y, width, bar_h, rx=4, fill=c["accent"])
        fig.text(bar_x + width + 14, y + 23, f"\u2212{-change}%", "change")
    return fig.svg(
        "Input tokens on 76 pull request takeovers",
        "Two pairs of bars. With Codex on gpt-6.1-sol, the context prepared by "
        "codenib threads used 21% fewer input tokens than the agent reading "
        "the thread with gh. With Claude Code on Claude Opus 5.5 it used 45% "
        "fewer.",
    )


FIGURES = {"thread-tokens": thread_tokens}


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
        for theme, colors in THEMES.items():
            path = args.out / f"{name}-{theme}.svg"
            path.write_text(on_page(render(colors)), encoding="utf-8")
            shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            print(f"{shown}  {path.stat().st_size // 1024} KiB")


if __name__ == "__main__":
    main()
