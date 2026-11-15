#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
"""Render the thread context blog figure as self-contained SVG files.

Writes ``thread-context.svg`` and ``thread-context-narrow.svg`` to
``landing/assets/blogs/``: an agent reading a GitHub thread piece by piece,
beside the one context ``codenib threads`` prepares, and the measured input
tokens. The bars on the left are a sketch of the mechanism, not measurements;
the figures in the bottom band are copied from ``docs/threads.md``.

Uses the drawing helpers of ``render_contextbench_blog_figures.py`` and needs
the same fontTools with WOFF2 support. The wide SVG is also the article's
sharing image and the README figure::

    python scripts/render_threads_blog_figure.py
    node scripts/render_svg_png.mjs \\
        landing/assets/blogs/thread-context.svg \\
        landing/assets/blogs/thread-context.png
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from blog_figure_theme import dark_figure
from render_contextbench_blog_figures import (
    ACCENT,
    ACCENT_DARK,
    INK_2,
    LINE,
    LINE_STRONG,
    OUT,
    ROOT,
    TEAL_DARK,
    Figure,
    scaled,
)

TITLE = "One read of a GitHub thread instead of a tool call per piece"
DESC = (
    "Left: an agent reading a thread itself makes one tool call for the issue, "
    "its comments, the linked pull request, the reviews, the checks and a "
    "failed log, and each call carries the conversation so far. Right: codenib "
    "threads prepares one document with status, coverage, the ask, code "
    "locations, failing CI, other open pull requests on the same code, linked "
    "threads and discussion, ending in a cursor, so a later read returns only "
    "what changed. On 76 pull request takeovers, input tokens against reading "
    "with gh fell 21% with Codex (gpt-6.1-sol) and 45% with Claude Code "
    "(Claude Opus 5.5). Over the life of an "
    "issue, a first context and updates took a median of 2,897 tokens against "
    "21,103 for rereading the thread, on 151 issues."
)

# The pieces an agent fetches, in the order docs/threads.md lists them.
PIECES = [
    "The issue",
    "Its comments",
    "The linked pull request",
    "The reviews",
    "The checks",
    "A failed log",
]

# What the prepared context holds: (part, what it says).
PARTS = [
    ("Status", "open or merged, checks of the latest commit"),
    ("Coverage", "complete, or how much was cut"),
    ("The ask", "title and body"),
    ("Code locations", "files, symbols and their source"),
    ("Failing CI", "the lines around each error"),
    ("Also in progress", "open pull requests on the same code"),
    ("Linked threads", "state, changes and reviews"),
    ("Discussion", "review comments with their lines"),
    ("Event ids", "every part cites where it came from"),
    ("Cursor", "the newest event it saw"),
]

STATS = [
    ("−21%", "Codex · gpt-6.1-sol"),
    ("−45%", "Claude Code · Claude Opus 5.5"),
]
STATS_SCOPE = (
    "input tokens on 76 pull request takeovers, against the agent reading with gh"
)
RETURNS = ("2,897 against 21,103", "tokens over the life of an issue")
RETURNS_SCOPE = "updates against rereading; 151 issues, a median of 6 returns"
FOOT = (
    "Answer quality holds. Files found, prepared context against gh: Codex 0.75 "
    "against 0.74, Claude Code 0.80 against 0.83."
)

STYLES = {
    "head": {"size": 34, "weight": 650, "spacing": -0.01},
    "phase": {"size": 21, "weight": 600},
    "phase-r": {"size": 21, "weight": 600, "fill": TEAL_DARK},
    "note": {"size": 19, "fill": INK_2},
    "piece": {"size": 20, "weight": 560},
    "num": {"size": 16, "family": "mono", "fill": INK_2},
    "file": {"size": 17, "weight": 500, "family": "mono", "fill": TEAL_DARK},
    "part": {"size": 19, "weight": 620},
    "desc": {"size": 17.5, "fill": INK_2},
    "mono": {"size": 17.5, "weight": 500, "family": "mono", "fill": TEAL_DARK},
    "stat": {"size": 44, "weight": 620, "spacing": -0.03, "fill": ACCENT_DARK},
    "stat-r": {"size": 44, "weight": 620, "spacing": -0.03, "fill": TEAL_DARK},
    "stat-label": {"size": 20, "weight": 560},
    "scope": {"size": 17, "fill": INK_2},
    "foot": {"size": 17, "fill": INK_2},
}


def _calls(fig: Figure, x: float, y: float, bar_x: float, step: float, new: float):
    """Six tool calls; each bar is what the call sends, growing with the last."""
    for i, piece in enumerate(PIECES):
        top = y + i * 50
        fig.text(x, top + 23, f"{i + 1}", "num")
        fig.text(x + 30, top + 24, piece, "piece")
        carried = 34 + i * step
        fig.rect(bar_x, top + 6, carried, 22, rx=3, fill=LINE_STRONG)
        fig.rect(bar_x + carried + 3, top + 6, new, 22, rx=3, fill=ACCENT)
    legend = y + len(PIECES) * 50 + 20
    fig.rect(x, legend - 11, 12, 12, rx=2, fill=LINE_STRONG)
    fig.text(x + 20, legend, "the conversation so far, sent again", "desc")
    width = fig.measure("the conversation so far, sent again", "desc")
    fig.rect(x + 44 + width, legend - 11, 12, 12, rx=2, fill=ACCENT)
    fig.text(x + 64 + width, legend, "the new piece", "desc")


def rule(fig: Figure, x: float, y: float, width: float) -> None:
    """A horizontal hairline."""
    fig.path(f"M {x:.1f} {y:.1f} H {x + width:.1f}", stroke=LINE, sw=1.5)


def wide() -> str:
    # One surface, like the mechanism figures: two columns divided by a
    # hairline and the measured figures under a rule.
    fig = Figure(1600, 840, STYLES)
    fig.text(64, 92, TITLE, "head")
    rule(fig, 0, 130, 1600)

    # Left: the agent reads the thread itself.
    fig.text(64, 178, "The agent reads the thread itself", "phase")
    fig.text(
        64, 206, "One tool call per piece. Each call carries what came before.", "note"
    )
    _calls(fig, 64, 236, 328, 56, 46)
    fig.path("M 776 130 V 620", stroke=LINE, sw=1.5)

    # Right: one prepared context.
    fig.text(824, 178, "codenib threads prepares it once", "phase-r")
    fig.text(
        824, 206, "One document in a token budget, built by code. No model.", "note"
    )
    fig.text(824, 254, "THREAD_CONTEXT.md", "file")
    for i, (part, desc) in enumerate(PARTS):
        x = 824 if i < 5 else 1206
        y = 300 + (i % 5) * 52
        fig.text(x, y, part, "part")
        fig.text(x, y + 23, desc, "desc")
    fig.text(824, 582, "When the agent comes back:", "note")
    offset = fig.measure("When the agent comes back: ", "note")
    fig.text(824 + offset, 582, "--since <cursor>", "mono")
    offset += fig.measure("--since <cursor> ", "mono") + 6
    fig.text(824 + offset, 582, "returns only what changed.", "note")
    rule(fig, 0, 620, 1600)

    # Measured, from docs/threads.md.
    for i, (value, label) in enumerate(STATS):
        x = 64 + i * 290
        fig.text(x, 696, value, "stat")
        fig.text(x, 730, label, "stat-label")
    fig.text(64, 758, STATS_SCOPE, "scope")
    fig.text(824, 696, RETURNS[0], "stat-r")
    fig.text(824, 730, RETURNS[1], "stat-label")
    fig.text(824, 758, RETURNS_SCOPE, "scope")
    fig.text(64, 806, FOOT, "foot")
    return fig.svg(TITLE, DESC)


def narrow() -> str:
    styles = scaled(STYLES, 1.0)
    styles["head"] = {**STYLES["head"], "size": 27}
    styles["stat"] = {**STYLES["stat"], "size": 38}
    styles["stat-r"] = {**STYLES["stat-r"], "size": 38}
    fig = Figure(640, 1400, styles)
    y = 58
    for line in fig.wrap(TITLE, "head", 592):
        fig.text(24, y, line, "head")
        y += 34

    top = y - 10
    rule(fig, 0, top, 640)
    fig.text(24, top + 38, "The agent reads the thread itself", "phase")
    fig.text(24, top + 66, "One tool call per piece.", "note")
    fig.text(24, top + 90, "Each call carries what came before.", "note")
    _calls(fig, 24, top + 106, 292, 40, 34)

    top += 452
    rule(fig, 0, top, 640)
    fig.text(24, top + 38, "codenib threads prepares it once", "phase-r")
    fig.text(24, top + 66, "One document in a token budget,", "note")
    fig.text(24, top + 90, "built by code. No model.", "note")
    fig.text(24, top + 128, "THREAD_CONTEXT.md", "file")
    for i, (part, desc) in enumerate(PARTS):
        row = top + 162 + i * 32
        fig.text(24, row, part, "part")
        fig.text(196, row, desc, "desc")
    fig.text(24, top + 498, "When the agent comes back:", "note")
    fig.text(24, top + 522, "--since <cursor>", "mono")
    offset = fig.measure("--since <cursor> ", "mono") + 6
    fig.text(24 + offset, top + 522, "returns only what changed.", "note")

    top += 548
    rule(fig, 0, top, 640)
    y = top + 58
    for value, label in STATS:
        fig.text(24, y, value, "stat")
        fig.text(160, y - 6, label, "stat-label")
        y += 54
    y -= 20
    for line in fig.wrap(STATS_SCOPE, "scope", 592):
        fig.text(24, y, line, "scope")
        y += 22
    y += 40
    fig.text(24, y, RETURNS[0], "stat-r")
    fig.text(24, y + 30, RETURNS[1], "stat-label")
    y += 56
    for line in fig.wrap(RETURNS_SCOPE, "scope", 592):
        fig.text(24, y, line, "scope")
        y += 22
    y += 18
    for line in fig.wrap(FOOT, "foot", 592):
        fig.text(24, y, line, "foot")
        y += 22
    fig.height = int(y + 14)
    return fig.svg(TITLE, DESC)


FIGURES = {"thread-context.svg": wide, "thread-context-narrow.svg": narrow}


def main() -> None:
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, render in FIGURES.items():
        path = args.out / name
        path.write_text(dark_figure(render()), encoding="utf-8")
        print(
            f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}"
            f"  {path.stat().st_size // 1024} KiB"
        )


if __name__ == "__main__":
    main()
