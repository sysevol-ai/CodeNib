#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
"""Render the four mechanism figures of the thread context article.

Writes a wide and a narrow SVG for each to ``landing/assets/blogs/``:

- ``thread-budget``: the token budget takes from linked threads first.
- ``thread-logs``: a failed log is folded by the lines that repeat.
- ``thread-anchors``: a mention in the thread becomes a function in the code.
- ``thread-events``: one stored log, read from many positions.

The figures share one vocabulary with the article's walkthrough: teal is
what the document keeps, blue is what an agent is sent, a dashed outline is
what was left out, and an agent is a round badge with its letter.

The threads, logs and code in the figures are made-up examples that follow
the rules in ``codenib/threads/``; the figures in the bottom band of
``thread-events`` are measured and copied from ``docs/threads.md``.

Uses the drawing helpers of ``render_contextbench_blog_figures.py`` and needs
the same fontTools with WOFF2 support::

    python scripts/render_threads_mechanism_figures.py
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
    INK_3,
    LINE,
    LINE_STRONG,
    OUT,
    ROOT,
    TEAL,
    TEAL_DARK,
    TEAL_LINE,
    TEAL_SOFT,
    Figure,
)

# One surface: two columns divided by a hairline, notes under a rule. Nothing
# is boxed inside a box.
MARGIN = 32
INNER = 560  # width of a column; render() narrows it for the phone drawing
PHONE_INNER = 416  # drawn near the size a phone shows it, so its type stays legible
PHONE_MARGIN = 24
GAP = 80  # between the columns; the divider runs down its middle
WIDE = 2 * MARGIN + 2 * INNER + GAP
NARROW = 2 * PHONE_MARGIN + PHONE_INNER
TOP = 78  # from the top of a column to its content

STYLES = {
    "phase": {"size": 16.5, "weight": 600},
    "phase-r": {"size": 16.5, "weight": 600, "fill": TEAL_DARK},
    "note": {"size": 15.5, "fill": INK_2},
    "label": {"size": 16.5, "weight": 600},
    "desc": {"size": 15, "fill": INK_2},
    "dim": {"size": 14, "fill": INK_3},
    "kept": {"size": 15, "weight": 600, "fill": TEAL_DARK},
    "new": {"size": 15, "weight": 600, "fill": ACCENT_DARK},
    "num": {"size": 14, "family": "mono", "fill": INK_2},
    "code": {"size": 14, "weight": 500, "family": "mono"},
    "code-dim": {"size": 14, "family": "mono", "fill": INK_3},
    "code-kept": {"size": 14, "weight": 500, "family": "mono", "fill": TEAL_DARK},
    "other": {"size": 15, "weight": 600},
    "badge": {"size": 13, "weight": 700},
    "code-new": {"size": 14, "weight": 500, "family": "mono", "fill": ACCENT_DARK},
}


def dashed(fig: Figure, x, y, w, h, stroke=LINE_STRONG):
    fig.path(
        f"M {x:.1f} {y:.1f} h {w:.1f} v {h:.1f} h {-w:.1f} z",
        stroke=stroke,
        sw=1.5,
        dash="4 4",
    )


def rule(fig: Figure, x: float, y: float, width: float) -> None:
    """A horizontal hairline."""
    fig.path(f"M {x:.1f} {y:.1f} H {x + width:.1f}", stroke=LINE, sw=1.5)


def agent(fig: Figure, cx: float, cy: float, letter: str) -> None:
    """An agent: a round badge with its letter, the same in every figure."""
    fig.circle(cx, cy, 11, LINE_STRONG)
    fig.text(cx, cy + 4.5, letter, "badge", "middle")


# --------------------------------------------------------------------------- #
# 1. The budget

THREADS = [
    ("The thread the agent works on", [0.55, 0.9, 0.35, 0.7, 0.5], 0),
    ("A linked issue", [0.8, 0.45, 0.6, 0.3], 2),
    ("A linked pull request", [0.5, 0.75, 0.4], 1),
]


def phone() -> bool:
    return INNER == PHONE_INNER


def _age_mark(fig: Figure, cx: float, cy: float, newest: bool) -> None:
    """A filled dot for the newest post, a ring for the oldest."""
    fig.circle(cx, cy, 5, INK_2)
    if not newest:
        fig.circle(cx, cy, 3, "#ffffff")


def _posts(fig: Figure, x: float, y: float, packed: bool) -> None:
    # Legend, on the line of the panel label.
    right = x + INNER
    fig.text(right, y - 48, "newest", "dim", "end")
    right -= fig.measure("newest", "dim") + 12
    _age_mark(fig, right, y - 53, True)
    right -= 22
    fig.text(right, y - 48, "oldest", "dim", "end")
    right -= fig.measure("oldest", "dim") + 12
    _age_mark(fig, right, y - 53, False)
    for name, posts, cut in THREADS:
        fig.text(x, y + 16, name, "label")
        if packed:
            status = (
                "every post whole" if not cut else f"newest {len(posts) - cut} kept"
            )
            fig.text(x + INNER, y + 16, status, "kept" if not cut else "desc", "end")
        y += 30
        last = y + (len(posts) - 1) * 22
        fig.path(
            f"M {x + 5:.1f} {y + 7:.1f} V {last + 7:.1f}", stroke=LINE_STRONG, sw=1.5
        )
        _age_mark(fig, x + 5, y + 7, False)
        _age_mark(fig, x + 5, last + 7, True)
        for i, size in enumerate(posts):
            width = (INNER - 180) * size
            if packed and i < cut:
                dashed(fig, x + 22, y, width, 14)
                note = "left out" if phone() else "left out, fetch by id"
                fig.text(x + width + 34, y + 12, note, "dim")
            else:
                fill = TEAL if packed else LINE_STRONG
                fig.rect(x + 22, y, width, 14, rx=3, fill=fill)
            y += 22
        y += 16


BUDGET = {
    "slug": "thread-budget",
    "title": "The budget takes from linked threads first",
    "desc": (
        "Left: three threads in the store, the one the agent works on and two "
        "linked to it, each a list of posts of different lengths. Right: the "
        "prepared context keeps every post of the agent's own thread whole, "
        "and leaves out the oldest posts of the linked threads. Its second "
        "line says which case applies. Own posts are shortened only "
        "past three times the budget."
    ),
    "panels": [
        (
            "What the store holds",
            "phase",
            "One bar per post, as long as the post.",
            lambda fig, x, y: _posts(fig, x, y, False),
            378,
        ),
        (
            "What the context keeps",
            "phase-r",
            "A budget of 3,000 tokens by default.",
            lambda fig, x, y: _posts(fig, x, y, True),
            378,
        ),
    ],
    "band": [
        (
            "The second line of the document says which of three cases applies: "
            "complete, this thread complete and linked threads partial, or partial.",
            "desc",
        ),
        (
            "Posts of the agent's own thread are shortened only past three times "
            "the budget, 9,000 tokens at the default.",
            "desc",
        ),
    ],
}


# --------------------------------------------------------------------------- #
# 2. Failed logs

RAW_LOG = [
    ("Run actions/checkout", "code-dim"),
    ("Set up Python 3.12", "code-dim"),
    ("Install dependencies", "code-dim"),
    ("Run ruff check .", "code-dim"),
    ("error: src/app/a.py:12: unused import os", "code"),
    ("error: src/app/b.py:40: unused import re", "code"),
    ("error: src/app/c.py:7: unused import sys", "code"),
    ("error: src/lib/d.py:88: unused import json", "code"),
    ("error: src/lib/e.py:3: unused import typing", "code"),
    ("... 295 more lines like these", "code-dim"),
]
FOLDED_LOG = [
    ("Install dependencies", "code-dim"),
    ("Run ruff check .", "code-dim"),
    ("error: src/app/a.py:12: unused import os", "code"),
    ("error: src/app/b.py:40: unused import re", "code"),
    ("[... 298 more lines like the two above ...]", "code-kept"),
]


def _raw_log(fig: Figure, x: float, y: float) -> None:
    for i, (line, style) in enumerate(RAW_LOG):
        fig.text(x, y + 16 + i * 23, line, style)


def _folded_log(fig: Figure, x: float, y: float) -> None:
    fig.text(x, y + 16, "Lines are compared with paths and numbers blanked:", "desc")
    fig.text(x, y + 42, "error: <path>:<n>: unused import", "code-kept")
    rule(fig, x, y + 62, INNER)
    for i, (line, style) in enumerate(FOLDED_LOG):
        fig.text(x, y + 90 + i * 23, line, style)
    fig.text(x, y + 222, "The two lines before an error stay, as context.", "dim")


LOGS = {
    "slug": "thread-logs",
    "title": "A failed log, folded by the lines that repeat",
    "desc": (
        "Left: a job log with four set-up lines and 300 error lines that differ "
        "only in the file, the line number and the name. Right: the context "
        "keeps the lines around an error, compares them with paths and numbers "
        "blanked, shows a repeated line twice and counts the rest: 298 more "
        "lines like the two above. Jobs whose failing lines mostly match are "
        "printed once with the names of the jobs that share the failure, and "
        "are never merged by name."
    ),
    "panels": [
        (
            "The log, as the job wrote it",
            "phase",
            "One problem in 300 files prints 300 lines.",
            _raw_log,
            238,
        ),
        (
            "What the context keeps",
            "phase-r",
            "Lines around an error. A repeat is shown twice, then counted.",
            _folded_log,
            238,
        ),
    ],
    "band": [
        (
            "Jobs are compared the same way. When most failing lines of two jobs "
            "match, the failure is printed once:",
            "desc",
        ),
        (
            "[#812/check/3310] lint (linux) failed (same failure in: lint (macos), "
            "lint (windows))",
            "code-kept",
        ),
        (
            "Jobs are never merged by name: a matrix runs several jobs under one name.",
            "desc",
        ),
    ],
}


# --------------------------------------------------------------------------- #
# 3. Code locations

MENTIONS = [
    ("Stack frame", 'File "pkg/io.py", line 212, in read'),
    ("Review comment on a line", "pkg/io.py, line 198"),
    ("File path", "pkg/io.py"),
    ("Name in backticks", "`Reader.read()`"),
    ("File a diff touches", "pkg/cache.py"),
]


def _mentions(fig: Figure, x: float, y: float) -> None:
    for i, (kind, example) in enumerate(MENTIONS):
        top = y + i * 50
        fig.text(x, top + 17, str(i + 1), "num")
        fig.text(x + 26, top + 17, kind, "label")
        fig.text(x + 26, top + 39, example, "code-dim")


def _resolved(fig: Figure, x: float, y: float) -> None:
    """A file as nested ranges: indentation guides, the resolved range tinted."""
    fig.text(x, y + 16, "pkg/io.py at commit 3f2a91c", "num")
    fig.text(x, y + 46, "class Reader", "code")
    fig.text(x + INNER, y + 46, "lines 120-260", "code-dim", "end")
    fig.path(f"M {x + 6:.1f} {y + 58:.1f} V {y + 174:.1f}", stroke=LINE_STRONG, sw=1.5)
    fig.rect(x + 22, y + 60, INNER - 22, 112, rx=6, fill=TEAL_SOFT)
    fig.text(x + 36, y + 85, "def read(self)", "code-kept")
    fig.text(x + INNER - 14, y + 85, "lines 190-229", "code-kept", "end")
    fig.path(f"M {x + 42:.1f} {y + 98:.1f} V {y + 158:.1f}", stroke=TEAL_LINE, sw=1.5)
    fig.text(x + 58, y + 118, "line 212, where the stack frame points", "desc")
    changed = "open PR #790" if phone() else "open pull request #790"
    fig.text(x + 58, y + 148, f"lines 205-214, changed by {changed}", "other")
    rule(fig, x, y + 196, INNER)
    if phone():
        fig.text(x, y + 224, "- pkg/io.py:190-229 `Reader.read()`", "code-kept")
        fig.text(x, y + 246, "  (frame; #812/comment/1041)", "code-kept")
    else:
        fig.text(
            x,
            y + 228,
            "- pkg/io.py:190-229 `Reader.read()` (frame; #812/comment/1041)",
            "code-kept",
        )


ANCHORS = {
    "slug": "thread-anchors",
    "title": "From a mention in the thread to a function in the code",
    "desc": (
        "Left: the five ways a thread points at code, strongest evidence "
        "first: a stack frame, a review comment on a line, a file path, a name "
        "in backticks, and a file a diff touches. Right: the mention is "
        "resolved at one commit of the clone. Line 212 of pkg/io.py lies in "
        "the method read, lines 190 to 229, inside the class Reader, and the "
        "context lists that range with the evidence and the event it came "
        "from. An open pull request that changes lines 205 to 214 falls in "
        "the same range, so the context lists it as also in progress."
    ),
    "panels": [
        (
            "How a thread points at code",
            "phase",
            "Strongest evidence first. That is the order of the list.",
            _mentions,
            250,
        ),
        (
            "Resolved at one commit of your clone",
            "phase-r",
            "A line maps to the innermost function or type around it.",
            _resolved,
            250,
        ),
    ],
    "band": [
        (
            "With line ranges, an open pull request that changes the same function "
            "can be found by overlap:",
            "desc",
        ),
        ("## Also in progress on this code", "code-kept"),
        (
            "- #790 (open PR by alice, updated 2026-11-02) Cache reads — changes "
            "`Reader.read()`",
            "code-kept",
        ),
        (
            "A bare name is looked up first in files the thread names. One defined "
            "in more than three places is dropped.",
            "desc",
        ),
    ],
}


# --------------------------------------------------------------------------- #
# 4. The event log

EVENTS = [
    ("#812/opened/812", ""),
    ("#812/comment/1041", ""),
    ("#812/comment/1042", ""),
    ("#812/review_comment/2207", ""),
    ("#812/comment/1041", "edited: same address, new record"),
    ("#812/check/3310", ""),
]
# (reader, state of each log position, what the reader gets, style)
READERS = [
    ("A", "first read", "nnnn..", "sent 4 records, ends with a cursor", "new"),
    (
        "A",
        "back, with --since <cursor>",
        "oooonn",
        "sent 2: the edit and the check",
        "new",
    ),
    ("B", "opens a ticket", "nnnnnn", "sent all 6, from the same stored thread", "new"),
    ("B", "polls again", "oooooo", "nothing new, nothing sent", "desc"),
]
CELL = {"o": LINE_STRONG, "n": ACCENT}


def _log(fig: Figure, x: float, y: float) -> None:
    for i, (address, remark) in enumerate(EVENTS):
        top = y + i * 34
        if i:
            rule(fig, x, top - 3, INNER)
        fig.text(x + 12, top + 19, str(i), "num", "end")
        fig.text(x + 30, top + 19, address, "code")
        if remark:
            fig.text(x + INNER, top + 19, remark, "dim", "end")


def _readers(fig: Figure, x: float, y: float) -> None:
    pitch = 70 if phone() else 52
    for i, (who, does, cells, gets, style) in enumerate(READERS):
        top = y + i * pitch
        agent(fig, x + 11, top + 11, who)
        fig.text(x + 30, top + 16, f"Agent {who}, {does}", "label")
        for j, state in enumerate(cells):
            cx = x + 30 + j * 30
            if state == ".":
                dashed(fig, cx, top + 26, 24, 14)
            else:
                fig.rect(cx, top + 26, 24, 14, rx=3, fill=CELL[state])
        if phone():
            fig.text(x + 30, top + 60, gets, style)
        else:
            fig.text(x + 226, top + 38, gets, style)
    legend = "A square is one record. Blue is sent to the agent, grey it has read."
    for i, line in enumerate(fig.wrap(legend, "dim", INNER)):
        fig.text(x, y + 4 * pitch + 20 + i * 20, line, "dim")


EVENT_LOG = {
    "slug": "thread-events",
    "title": "One list of events, read from many positions",
    "desc": (
        "Left: a thread stored as a list of six events in order, each with "
        "a fixed address; an edited comment is appended as a new record under "
        "its old address. Right: agents keep a position in that list. Agent A's "
        "first read is sent the four records that exist and ends with a "
        "cursor. When it comes back with --since it is sent only the edit "
        "and the check that came after. Agent B opens a ticket on the same "
        "stored thread and is sent all six; when it polls again nothing is "
        "new and nothing is sent. Measured: over the life of an issue, a "
        "first context and updates took a median of 2,897 tokens against "
        "21,103 for rereading, on 151 issues; with 24 tickets following 71 "
        "threads of astral-sh/ruff, fetching everything took 278 requests "
        "and a poll with nothing new took 13."
    ),
    "panels": [
        (
            "What is stored: the events, in order",
            "phase",
            "Records are only added. An edit is one more record.",
            _log,
            208,
        ),
        (
            "What each agent is sent",
            "phase-r",
            "An agent keeps a position in the list. Nothing is copied.",
            _readers,
            (236, 326),
        ),
    ],
    "band": [
        (
            "Measured. 151 issues, a median of 6 returns each: 2,897 tokens for a "
            "first context and updates, against 21,103 for reading the thread again"
            " each time.",
            "desc",
        ),
        (
            "24 tickets following 71 threads of astral-sh/ruff: 278 requests to "
            "fetch everything, 13 for a poll with nothing new.",
            "desc",
        ),
    ],
}

SPECS = [BUDGET, LOGS, ANCHORS, EVENT_LOG]


# --------------------------------------------------------------------------- #
# Layout shared by the four figures


def _column(fig: Figure, x: float, y: float, panel) -> float:
    """Draw a column; returns the extra height a wrapped note took."""
    label, style, note, draw, _ = panel
    fig.text(x, y + 30, label, style)
    lines = fig.wrap(note, "note", INNER)
    for i, line in enumerate(lines):
        fig.text(x, y + 54 + i * 22, line, "note")
    extra = 22 * (len(lines) - 1)
    draw(fig, x, y + TOP + extra)
    return extra


def _notes(fig: Figure, x: float, y: float, width: float, lines) -> float:
    """Wrapped lines under the columns; returns the height they take."""
    height = 8
    for text, style in lines:
        for i, line in enumerate(fig.wrap(text, style, width)):
            height += 22 if i else 30
            fig.text(x, y + height, line, style)
    return height


def render(spec: dict, narrow: bool) -> str:
    global INNER
    INNER = PHONE_INNER if narrow else 560
    margin = PHONE_MARGIN if narrow else MARGIN
    width = NARROW if narrow else WIDE
    fig = Figure(width, 2000, STYLES)
    # A column's height is one number, or (wide, phone) when the phone drawing
    # stacks what the wide one sets side by side.
    heights = [
        panel[4][narrow] if isinstance(panel[4], tuple) else panel[4]
        for panel in spec["panels"]
    ]
    # No title in the drawing: the section heading above the figure names it.
    y = 4
    for i, panel in enumerate(spec["panels"]):
        if narrow:
            if i:
                rule(fig, 0, y, width)
            y += _column(fig, margin, y, panel)
            y += TOP + heights[i] + 22
        else:
            _column(fig, margin + i * (INNER + GAP), y, panel)
    if not narrow:
        height = TOP + max(heights) + 22
        middle = margin + INNER + GAP / 2
        fig.path(f"M {middle:.1f} {y:.1f} V {y + height:.1f}", stroke=LINE, sw=1.5)
        y += height
    rule(fig, 0, y, width)
    y += _notes(fig, margin, y, width - 2 * margin, spec["band"])
    fig.height = int(y + 30)
    return fig.svg(spec["title"], spec["desc"])


def main() -> None:
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for spec in SPECS:
        for narrow in (False, True):
            name = f"{spec['slug']}{'-narrow' if narrow else ''}.svg"
            path = args.out / name
            svg = render(spec, narrow)
            path.write_text(dark_figure(svg), encoding="utf-8")
            shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            size = svg.split('viewBox="0 0 ', 1)[1].split('"', 1)[0]
            print(f"{shown}  {size}  {path.stat().st_size // 1024} KiB")


if __name__ == "__main__":
    main()
