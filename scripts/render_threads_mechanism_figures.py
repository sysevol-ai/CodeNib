#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
"""Render the four mechanism figures of the thread context article.

Writes a wide and a narrow SVG for each to ``landing/assets/blogs/``:

- ``thread-budget``: the token budget takes from linked threads first.
- ``thread-logs``: a failed log is folded by the lines that repeat.
- ``thread-anchors``: a mention in the thread becomes a function in the code.
- ``thread-events``: one stored log, read from many positions.

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
    SUBTLE,
    TEAL,
    TEAL_DARK,
    TEAL_LINE,
    TEAL_SOFT,
    Figure,
)

MARGIN = 24
PANEL = 592
GAP = 32
PAD = 24
INNER = PANEL - 2 * PAD
WIDE = 2 * MARGIN + 2 * PANEL + GAP
NARROW = 2 * MARGIN + PANEL
TOP = 88  # from the top of a panel to its content

STYLES = {
    "kicker": {"size": 14, "weight": 650, "spacing": 0.08, "fill": ACCENT},
    "head": {"size": 28, "weight": 650, "spacing": -0.01},
    "phase": {"size": 13.5, "weight": 650, "spacing": 0.08, "fill": INK_2},
    "phase-r": {"size": 13.5, "weight": 650, "spacing": 0.08, "fill": TEAL_DARK},
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
    "code-new": {"size": 14, "weight": 500, "family": "mono", "fill": ACCENT_DARK},
}


def dashed(fig: Figure, x, y, w, h, stroke=LINE_STRONG):
    fig.path(
        f"M {x:.1f} {y:.1f} h {w:.1f} v {h:.1f} h {-w:.1f} z",
        stroke=stroke,
        sw=1.5,
        dash="4 4",
    )


# --------------------------------------------------------------------------- #
# 1. The budget

THREADS = [
    ("The thread the agent works on", [0.55, 0.9, 0.35, 0.7, 0.5], 0),
    ("A linked issue", [0.8, 0.45, 0.6, 0.3], 2),
    ("A linked pull request", [0.5, 0.75, 0.4], 1),
]
BAR = 380


def _age_mark(fig: Figure, cx: float, cy: float, newest: bool) -> None:
    """A filled dot for the newest post, a ring for the oldest."""
    fig.circle(cx, cy, 5, INK_2)
    if not newest:
        fig.circle(cx, cy, 3, "#ffffff")


def _posts(fig: Figure, x: float, y: float, packed: bool) -> None:
    # Legend, on the line of the panel label.
    right = x + INNER
    fig.text(right, y - 52, "newest", "dim", "end")
    right -= fig.measure("newest", "dim") + 12
    _age_mark(fig, right, y - 57, True)
    right -= 22
    fig.text(right, y - 52, "oldest", "dim", "end")
    right -= fig.measure("oldest", "dim") + 12
    _age_mark(fig, right, y - 57, False)
    for index, (name, posts, cut) in enumerate(THREADS):
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
            width = BAR * size
            if packed and i < cut:
                dashed(fig, x + 22, y, width, 14)
                fig.text(x + width + 34, y + 12, "left out, fetch by id", "dim")
            else:
                fill = TEAL if packed and index == 0 else LINE_STRONG
                fig.rect(x + 22, y, width, 14, rx=3, fill=fill)
            y += 22
        y += 16


BUDGET = {
    "slug": "thread-budget",
    "kicker": "DECISION 1",
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
            "WHAT THE STORE HOLDS",
            "phase",
            "One bar per post, as long as the post.",
            lambda fig, x, y: _posts(fig, x, y, False),
            378,
        ),
        (
            "WHAT THE CONTEXT KEEPS",
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
    top = y + 62
    fig.rect(x, top, INNER, 136, rx=8, fill=SUBTLE, stroke=LINE)
    for i, (line, style) in enumerate(FOLDED_LOG):
        fig.text(x + 16, top + 28 + i * 23, line, style)
    fig.text(x, top + 162, "The two lines before an error stay, as context.", "dim")


LOGS = {
    "slug": "thread-logs",
    "kicker": "DECISION 2",
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
            "THE LOG, AS THE JOB WROTE IT",
            "phase",
            "One problem in 300 files prints 300 lines.",
            _raw_log,
            238,
        ),
        (
            "WHAT THE CONTEXT KEEPS",
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
    fig.rect(x, y, INNER, 204, rx=8, fill=SUBTLE, stroke=LINE)
    fig.text(x + 16, y + 27, "pkg/io.py at commit 3f2a91c", "num")
    fig.rect(x + 16, y + 40, INNER - 32, 150, rx=6, fill="#ffffff", stroke=LINE)
    fig.text(x + 30, y + 63, "class Reader", "code")
    fig.text(x + INNER - 30, y + 63, "lines 120-260", "code-dim", "end")
    fig.rect(x + 30, y + 74, INNER - 60, 104, rx=6, fill=TEAL_SOFT, stroke=TEAL_LINE)
    fig.text(x + 44, y + 97, "def read(self)", "code-kept")
    fig.text(x + INNER - 44, y + 97, "lines 190-229", "code-kept", "end")
    fig.rect(x + 44, y + 111, 4, 20, rx=2, fill=TEAL)
    fig.text(x + 58, y + 126, "line 212, where the stack frame points", "desc")
    fig.rect(x + 44, y + 141, 4, 20, rx=2, fill=ACCENT)
    fig.text(x + 58, y + 156, "lines 205-214, changed by open pull request #790", "new")
    fig.text(
        x,
        y + 232,
        "- pkg/io.py:190-229 `Reader.read()` (frame; #812/comment/1042)",
        "code-kept",
    )


ANCHORS = {
    "slug": "thread-anchors",
    "kicker": "DECISION 3",
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
            "HOW A THREAD POINTS AT CODE",
            "phase",
            "Strongest evidence first. That is the order of the list.",
            _mentions,
            250,
        ),
        (
            "RESOLVED AT ONE COMMIT OF YOUR CLONE",
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
        ("## Also in progress on this code", "code-new"),
        (
            "- #790 (open PR by alice, updated 2026-11-02) Cache reads — changes "
            "`Reader.read()`",
            "code-new",
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
    ("#812/review/2207", ""),
    ("#812/comment/1041", "edited: same address, new record"),
    ("#812/check/3310", ""),
]
# (reader, state of each log position, what the reader gets, style)
READERS = [
    ("First context", "kkkk..", "ends with a cursor, #812/review/2207", "desc"),
    ("Later, with --since <cursor>", "oooonn", "only the edit and the check", "new"),
    ("Ticket A, at position 6", "oooooo", "nothing unread", "desc"),
    ("Ticket B, at position 2", "oonnnn", "4 unread, same stored thread", "new"),
]
CELL = {"k": TEAL, "o": LINE_STRONG, "n": ACCENT}


def _log(fig: Figure, x: float, y: float) -> None:
    for i, (address, remark) in enumerate(EVENTS):
        top = y + i * 34
        fig.rect(x, top, INNER, 28, rx=5, fill=SUBTLE, stroke=LINE)
        fig.text(x + 12, top + 19, str(i), "num")
        fig.text(x + 40, top + 19, address, "code-new" if remark else "code")
        if remark:
            fig.text(x + INNER - 12, top + 19, remark, "dim", "end")


def _readers(fig: Figure, x: float, y: float) -> None:
    for i, (reader, cells, gets, style) in enumerate(READERS):
        top = y + i * 52
        fig.text(x, top + 16, reader, "label")
        for j, state in enumerate(cells):
            cx = x + j * 30
            if state == ".":
                dashed(fig, cx, top + 26, 24, 14)
            else:
                fig.rect(cx, top + 26, 24, 14, rx=3, fill=CELL[state])
        fig.text(x + 196, top + 38, gets, style)
    fig.text(
        x,
        y + 228,
        "Each square is one event of the log. Blue is what the reader receives.",
        "dim",
    )


EVENT_LOG = {
    "slug": "thread-events",
    "kicker": "DECISION 4",
    "title": "One list of events, read from many positions",
    "desc": (
        "Left: a thread stored as a list of six events in order, each with "
        "a fixed address; an edited comment is appended as a new record under "
        "its old address. Right: readers keep a position in that log. A first "
        "context covers the four events that exist and ends with a cursor. A "
        "later read with --since returns only the edit and the check that "
        "came after. Two tickets on the same thread keep their own positions: "
        "one has nothing unread, the other four events. Measured: over the "
        "life of an issue, a first context and updates took a median of 2,897 "
        "tokens against 21,103 for rereading, on 151 issues; with 24 tickets "
        "following 71 threads of astral-sh/ruff, fetching everything took 278 "
        "requests and a poll with nothing new took 13."
    ),
    "panels": [
        (
            "WHAT IS STORED: THE EVENTS, IN ORDER",
            "phase",
            "Records are only added. An edit is one more record.",
            _log,
            208,
        ),
        (
            "WHAT EACH READER GETS",
            "phase-r",
            "A reader keeps a position in the log. Nothing is copied.",
            _readers,
            236,
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


def _panel(fig: Figure, x: float, y: float, height: float, panel) -> None:
    label, style, note, draw, _ = panel
    fig.rect(x, y, PANEL, height, rx=10, fill="#ffffff", stroke=LINE)
    fig.text(x + PAD, y + 36, label, style)
    fig.text(x + PAD, y + 62, note, "note")
    draw(fig, x + PAD, y + TOP)


def _band(fig: Figure, x: float, y: float, width: float, lines) -> float:
    """A full-width box of wrapped lines under the panels; returns its height."""
    rows = []
    for text, style in lines:
        wrapped = fig.wrap(text, style, width - 2 * PAD)
        rows += [(line, style, i == 0) for i, line in enumerate(wrapped)]
    height = 14
    placed = []
    for line, style, first in rows:
        height += 30 if first else 22
        placed.append((line, style, height))
    height += 20
    fig.rect(x, y, width, height, rx=10, fill=SUBTLE, stroke=LINE)
    for line, style, offset in placed:
        fig.text(x + PAD, y + offset, line, style)
    return height


def render(spec: dict, narrow: bool) -> str:
    width = NARROW if narrow else WIDE
    fig = Figure(width, 2000, STYLES)
    fig.text(MARGIN, 46, f"CODENIB THREADS · {spec['kicker']}", "kicker")
    y = 84
    for line in fig.wrap(spec["title"], "head", width - 2 * MARGIN):
        fig.text(MARGIN, y, line, "head")
        y += 34
    y -= 6
    height = TOP + max(panel[4] for panel in spec["panels"]) + 18
    for i, panel in enumerate(spec["panels"]):
        if narrow:
            _panel(fig, MARGIN, y, TOP + panel[4] + 18, panel)
            y += TOP + panel[4] + 18 + 20
        else:
            _panel(fig, MARGIN + i * (PANEL + GAP), y, height, panel)
    if not narrow:
        y += height + 20
    y += _band(fig, MARGIN, y, width - 2 * MARGIN, spec["band"])
    fig.height = int(y + MARGIN)
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
