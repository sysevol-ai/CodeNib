#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
"""Render the figures of the always-on agent article.

Writes a wide and a narrow SVG for each to ``landing/assets/blogs/``:

- ``agent-wake``: one pass of ``codenib threads agent``: code reads the news,
  and only news that asks for a response wakes the model, which gets its
  note, what it said and the news, and can only read.
- ``agent-results``: cost, drafts and reviewer points raised first, against
  an agent that rereads everything and keeps no memory.
- ``agent-samples``: reviewer points raised by one to four independent
  first-look reviews, and by three merged into one draft.

The events and notes in ``agent-wake`` are a made-up example of the rules in
``codenib/threads/agent.py``. Every measured number is read from
``docs/assets/threads_agent_replay_summary.json``.

Uses the drawing helpers of ``render_contextbench_blog_figures.py`` and needs
the same fontTools with WOFF2 support::

    python scripts/render_agent_replay_figures.py
"""

from __future__ import annotations

import argparse
import json
import logging
import re
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
    Figure,
)

SUMMARY = ROOT / "docs" / "assets" / "threads_agent_replay_summary.json"

MARGIN = 32
INNER = 560
PHONE_INNER = 416
PHONE_MARGIN = 24
GAP = 80
WIDE = 2 * MARGIN + 2 * INNER + GAP
NARROW = 2 * PHONE_MARGIN + PHONE_INNER

STYLES = {
    "phase": {"size": 16.5, "weight": 600},
    "note": {"size": 15.5, "fill": INK_2},
    "row": {"size": 15},
    "row-dim": {"size": 15, "fill": INK_3},
    "wake": {"size": 14, "weight": 600, "fill": TEAL_DARK},
    "read": {"size": 14, "fill": INK_3},
    "label": {"size": 14, "weight": 600, "fill": INK_2},
    "body": {"size": 15},
    "code": {"size": 14, "weight": 500, "family": "mono"},
    "kept": {"size": 15, "weight": 600, "fill": TEAL_DARK},
    "other": {"size": 15, "weight": 600, "fill": ACCENT_DARK},
    "value": {"size": 15, "weight": 600, "family": "mono"},
    "ratio": {"size": 15.5, "weight": 600},
    "axis": {"size": 13.5, "fill": INK_3},
    "band": {"size": 15.5, "fill": INK_2},
    "band-strong": {"size": 15.5, "weight": 600},
}


def minus(text: str) -> str:
    """Typeset negative numbers with a minus sign, not a hyphen."""
    return re.sub(r"(?<!\w)-(?=\d)", "−", text)


def rule(fig: Figure, x: float, y: float, width: float) -> None:
    fig.path(f"M {x:.1f} {y:.1f} H {x + width:.1f}", stroke=LINE, sw=1.5)


def wrapped(fig: Figure, x, y, text, style, width, leading=22) -> float:
    """Write wrapped text; returns the height it took."""
    lines = fig.wrap(text, style, width)
    for i, line in enumerate(lines):
        fig.text(x, y + i * leading, line, style)
    return leading * len(lines)


def heading(fig: Figure, x, y, title, note, width) -> float:
    fig.text(x, y + 30, title, "phase")
    return 54 + wrapped(fig, x, y + 54, note, "note", width)


def band(fig: Figure, y: float, width: float, margin: float, parts) -> float:
    rule(fig, 0, y, width)
    height = 14
    for text, style in parts:
        height += 22
        height += wrapped(fig, margin, y + height, text, style, width - 2 * margin) - 22
    return height + 20


# --------------------------------------------------------------------------- #
# 1. One pass

# What changed, the same on a phone when it fits, and whether it wakes the agent.
NEWS = [
    ("label “performance” added", None, False),
    ("check “lint” passed", None, False),
    ("codecov[bot] posted coverage", None, False),
    ("review requested from a maintainer", None, False),
    (
        "maintainer, on src/parse.rs:88: “handle the empty case”",
        "maintainer review on parse.rs:88",
        True,
    ),
    ("the author pushed commit 3f2a91c", None, True),
]

HANDED = [
    (
        "Its note on this thread",
        "Asked: accept empty input. Said: the guard at parse.rs:88 is missing. "
        "Left: a test.",
    ),
    ("What it said here before", "A reply draft, not yet sent by a maintainer."),
    ("The news", "Only the review comment and the commit."),
    ("The diff", "Saved to a file by the pass, which it may read."),
]


def _news(fig: Figure, x: float, y: float, width: float, share: float) -> float:
    narrow = width < INNER
    for i, (text, short, wakes) in enumerate(NEWS):
        text = short if narrow and short else text
        top = y + i * 40
        if wakes:
            fig.circle(x + 6, top + 15, 5.5, TEAL)
        else:
            fig.path(
                f"M {x + 1:.1f} {top + 15:.1f} a 5 5 0 1 0 10 0 a 5 5 0 1 0 -10 0",
                stroke=LINE_STRONG,
                sw=1.5,
            )
        tag, style = ("wakes it", "wake") if wakes else ("read, no run", "read")
        tag_width = fig.measure(tag, style)
        line = text
        room = width - 22 - tag_width - 16
        while fig.measure(line, "row") > room and len(line) > 8:
            line = line[:-2].rstrip() + "…"
            line = line.replace("……", "…")
        fig.text(x + 22, top + 20, line, "row" if wakes else "row-dim")
        fig.text(x + width, top + 20, tag, style, "end")
        if i < len(NEWS) - 1:
            rule(fig, x + 22, top + 34, width - 22)
    y += len(NEWS) * 40 + 16
    used = wrapped(
        fig, x, y + 4,
        f"In the replays, {share:.0%} of passes with a change asked for no "
        "response and woke no model.",
        "note", width,
    )  # fmt: skip
    return len(NEWS) * 40 + 20 + used


def _handed(fig: Figure, x: float, y: float, width: float) -> float:
    top = y
    for label, text in HANDED:
        fig.text(x, top + 16, label, "label")
        top += 24
        top += wrapped(fig, x, top + 14, text, "body", width) + 10
    top += 4
    fig.path(
        f"M {x + 8:.1f} {top:.1f} V {top + 26:.1f}",
        stroke=INK_3, sw=1.5, marker="arrow-gray",
    )  # fmt: skip
    top += 40
    fig.text(x, top + 14, "Claude Code, once, read-only", "kept")
    top += 22
    top += wrapped(
        fig, x, top + 14,
        "Read, Grep and Glob in the clone. No shell, no writes, no network, "
        "no GitHub token.",
        "body", width,
    )  # fmt: skip
    top += 14
    fig.path(
        f"M {x + 8:.1f} {top:.1f} V {top + 26:.1f}",
        stroke=INK_3, sw=1.5, marker="arrow-gray",
    )  # fmt: skip
    top += 40
    chips = [("reply → outbox", TEAL), ("question → outbox", LINE_STRONG),
             ("nothing", LINE_STRONG)]  # fmt: skip
    cx = x
    for text, stroke in chips:
        w = fig.measure(text, "row") + 24
        if cx + w > x + width:
            cx = x
            top += 40
        fig.rect(cx, top, w, 30, rx=15, stroke=stroke, sw=1.5)
        fig.text(cx + w / 2, top + 20, text, "row", "middle")
        cx += w + 10
    top += 30
    top += 12 + wrapped(
        fig, x, top + 22,
        "One waiting draft per thread. Nothing is posted until a maintainer "
        "sends it.",
        "note", width,
    )  # fmt: skip
    return top - y


def render_wake(summary: dict, narrow: bool) -> str:
    inner = PHONE_INNER if narrow else INNER
    margin = PHONE_MARGIN if narrow else MARGIN
    width = NARROW if narrow else WIDE
    fig = Figure(width, 3000, STYLES)
    share = summary["passes_read_without_waking_the_agent"]["share_read_without_a_run"]
    columns = [
        ("1. Code reads the news",
         "Every 15 minutes a pass fetches what changed. Only news that asks "
         "for a response wakes the model.",
         lambda f, x, y: _news(f, x, y, inner, share)),
        ("2. The model gets its memory and the news",
         "The pass claims the news, runs the agent, files its decision, and "
         "only then marks the news read.",
         lambda f, x, y: _handed(f, x, y, inner)),
    ]  # fmt: skip
    y = 4
    heights = []
    for i, (title, note, draw) in enumerate(columns):
        x = margin if narrow else margin + i * (inner + GAP)
        if narrow and i:
            rule(fig, 0, y, width)
        top = heading(fig, x, y, title, note, inner) + 12
        h = top + draw(fig, x, y + top)
        heights.append(h)
        if narrow:
            y += h + 20
    if not narrow:
        height = max(heights) + 20
        middle = margin + inner + GAP / 2
        fig.path(f"M {middle:.1f} {y:.1f} V {y + height:.1f}", stroke=LINE, sw=1.5)
        y += height
    c = summary["codenib_vs_stateless_five_repositories"]
    y += band(
        fig, y, width, margin,
        [
            ("Measured on 76 merged pull requests from five repositories, against "
             "an agent with the same wakes that rereads everything and keeps no "
             "memory:", "band"),
            (f"{c['cost_usd']['ratio']:.2f}× the cost, {c['drafts']['ratio']:.2f}× the "
             f"drafts, {c['raised_first']['ratio']:.2f}× the reviewer points raised "
             "first.", "band-strong"),
        ],
    )  # fmt: skip
    fig.height = int(y + 10)
    return fig.svg(
        "One pass of an always-on review agent",
        "Left: six things that changed on a pull request since the last pass. A "
        "label, a passing check, a bot comment and a review request are read "
        "without waking the model; a maintainer's review comment and a new "
        f"commit wake it. In the replays, {share:.0%} of passes with a change "
        "woke no model. Right: the agent is handed its note on the thread, what "
        "it said there before, only the news, and the diff saved to a file. "
        "Claude Code runs once with read-only tools in the clone and no GitHub "
        "token, and ends with a reply for the outbox, a question, or nothing; "
        "nothing is posted until a maintainer sends it. Measured on 76 merged "
        "pull requests from five repositories against an agent that rereads "
        f"everything and keeps no memory: {c['cost_usd']['ratio']:.2f} times the "
        f"cost, {c['drafts']['ratio']:.2f} times the drafts and "
        f"{c['raised_first']['ratio']:.2f} times the reviewer points raised first.",
    )


# --------------------------------------------------------------------------- #
# 2. Results


def render_results(summary: dict, narrow: bool) -> str:
    margin = PHONE_MARGIN if narrow else MARGIN
    width = NARROW if narrow else WIDE
    c = summary["codenib_vs_stateless_five_repositories"]
    panels = [
        ("Cost of all runs", "cost_usd", lambda v: f"${v:,.2f}"),
        ("Drafts filed for a maintainer", "drafts", lambda v: f"{v:,.0f}"),
        ("Reviewer points raised first", "raised_first",
         lambda v: f"{v:,.1f}".rstrip("0").rstrip(".")),
    ]  # fmt: skip
    fig = Figure(width, 2000, STYLES)
    gap = 40
    panel_w = width - 2 * margin if narrow else (width - 2 * margin - 2 * gap) / 3
    y0 = 8
    bottom = y0
    for i, (title, key, fmt) in enumerate(panels):
        x = margin if narrow else margin + i * (panel_w + gap)
        y = y0 if not narrow else bottom
        if narrow and i:
            rule(fig, 0, y, width)
            y += 8
        fig.text(x, y + 26, title, "phase")
        values = [("CodeNib agent", c[key]["codenib"], TEAL, "kept"),
                  ("Stateless agent", c[key]["stateless"], ACCENT, "other")]  # fmt: skip
        top = max(v for _, v, _, _ in values)
        bar_w = panel_w - 80
        for j, (name, value, color, style) in enumerate(values):
            by = y + 52 + j * 58
            fig.text(x, by + 14, name, style)
            length = max(2.0, bar_w * value / top)
            fig.rect(x, by + 24, length, 16, rx=3, fill=color)
            fig.text(x + length + 8, by + 37, fmt(value), "value")
        lo, hi = c[key]["ratio_ci95"]
        fig.text(
            x, y + 52 + 2 * 58 + 22,
            f"{c[key]['ratio']:.2f}×  (95% interval {lo:.2f}–{hi:.2f})", "ratio",
        )  # fmt: skip
        bottom = y + 52 + 2 * 58 + 40
    y = bottom + 10
    y += band(
        fig, y, width, margin,
        [("76 merged pull requests from astral-sh/ruff, pandas, prometheus, vite "
          "and tokio, replayed with Claude Sonnet 5.5. Both agents wake for the "
          "same news and use the same model, tools and instructions; the "
          "stateless one rereads the whole context and keeps no memory.", "band")],
    )  # fmt: skip
    fig.height = int(y + 10)
    return fig.svg(
        "Cost, drafts and reviewer points, with and without memory",
        f"Over 76 replayed pull requests, the CodeNib agent cost "
        f"${c['cost_usd']['codenib']:.2f} against ${c['cost_usd']['stateless']:.2f} "
        f"({c['cost_usd']['ratio']:.2f} times), filed {c['drafts']['codenib']:.0f} "
        f"drafts against {c['drafts']['stateless']:.0f} "
        f"({c['drafts']['ratio']:.2f} times), and raised "
        f"{c['raised_first']['codenib']:.1f} reviewer points before the reviewer "
        f"did, against {c['raised_first']['stateless']:.0f} "
        f"({c['raised_first']['ratio']:.2f} times, 95% interval "
        f"{c['raised_first']['ratio_ci95'][0]:.2f} to "
        f"{c['raised_first']['ratio_ci95'][1]:.2f}).",
    )


# --------------------------------------------------------------------------- #
# 3. Samples


def render_samples(summary: dict, narrow: bool) -> str:
    s = summary["first_look_sampling"]
    curve = [s["raised_by_any_of_k_reviews"][str(k)] for k in range(1, 5)]
    merged = s["raised_by_the_union_of_reviews_1_to_3"], s["raised_by_their_merge"]
    points = s["reviewer_points"]
    margin = PHONE_MARGIN if narrow else MARGIN
    width = NARROW if narrow else WIDE
    chart_w = PHONE_INNER if narrow else INNER
    fig = Figure(width, 2000, STYLES)
    x0, y0 = margin + 40, 40
    plot_w, plot_h = chart_w - 60, 300
    top_value = 20
    sx = lambda k: x0 + (k - 1) / 3 * plot_w  # noqa: E731
    sy = lambda v: y0 + plot_h - v / top_value * plot_h  # noqa: E731
    for v in (0, 5, 10, 15, 20):
        rule(fig, x0, sy(v), plot_w)
        fig.text(x0 - 10, sy(v) + 5, str(v), "axis", "end")
    for k in range(1, 5):
        fig.text(sx(k), y0 + plot_h + 24, str(k), "axis", "middle")
    fig.text(
        x0 + plot_w / 2, y0 + plot_h + 48,
        "independent reviews at the first look", "axis", "middle",
    )  # fmt: skip
    fig.text(x0 - 34, y0 - 16, f"reviewer points raised first, of {points}", "axis")
    d = " ".join(f"{'M' if k == 1 else 'L'} {sx(k):.1f} {sy(v):.1f}"
                 for k, v in zip(range(1, 5), curve, strict=True))  # fmt: skip
    fig.path(d, stroke=ACCENT, sw=2.5)
    for k, v in zip(range(1, 5), curve, strict=True):
        fig.circle(sx(k), sy(v), 5.5, ACCENT)
        label = f"{v:.1f}".rstrip("0").rstrip(".")
        if k == 1:  # below the line, clear of the axis labels
            fig.text(sx(k) + 12, sy(v) + 22, label, "value")
        else:
            fig.text(sx(k), sy(v) - 12, label, "value", "middle")
    fig.circle(sx(3), sy(merged[1]), 7, TEAL)
    # Under the point, above the next grid line.
    fig.text(sx(3), sy(merged[1]) + 28, f"{merged[1]}: merged into one draft",
             "kept", "middle")  # fmt: skip
    notes_x = margin if narrow else margin + chart_w + GAP
    notes_y = y0 + plot_h + 80 if narrow else 30
    notes_w = PHONE_INNER if narrow else INNER
    lo, hi = s["merge_minus_one_review_ci95"]
    notes = [
        ("Each review is a sample.",
         f"One review raised {curve[0]:.1f} of {points} points on average. Any of "
         f"four raised {curve[3]:.0f}: the reviews differ, and each finds a few "
         "points the others miss.", "phase"),
        ("Merged, it is still one message.",
         f"Three reviews merged into one draft raised {merged[1]}, of the "
         f"{merged[0]} the three found between them. An agent that reviews again "
         "on every wake gains coverage the same way, but posts each time.", "phase"),
        ("Not yet significant.",
         minus(f"{merged[1] - curve[0]:+.2f} points over one review, 95% interval "
               f"{lo:+.1f} to {hi:+.1f}, on {s['pull_requests']} pull requests. "
               "The first look then costs about four times as much."), "phase"),
    ]  # fmt: skip
    y = notes_y
    for title, text, style in notes:
        fig.text(notes_x, y + 22, title, style)
        y += 34 + wrapped(fig, notes_x, y + 46, text, "note", notes_w) + 10
    fig.height = int(max(y, y0 + plot_h + 60) + 20)
    return fig.svg(
        "Reviewer points raised by more independent first-look reviews",
        f"On {s['pull_requests']} pull requests with {points} reviewer points, one "
        f"first-look review raised {curve[0]:.2f} points before the reviewer did, "
        f"on average. Any of two raised {curve[1]:.1f}, any of three {curve[2]:.1f} "
        f"and any of four {curve[3]:.1f}. Three reviews merged into one draft "
        f"raised {merged[1]} of the {merged[0]} they found together: "
        + minus(
            f"{merged[1] - curve[0]:+.2f} over one review, 95% interval "
            f"{lo:+.1f} to {hi:+.1f}."
        ),
    )


SPECS = [("agent-wake", render_wake), ("agent-results", render_results),
         ("agent-samples", render_samples)]  # fmt: skip


def main() -> None:
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    summary = json.loads(SUMMARY.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    for slug, render in SPECS:
        for narrow in (False, True):
            path = args.out / f"{slug}{'-narrow' if narrow else ''}.svg"
            svg = render(summary, narrow)
            path.write_text(dark_figure(svg), encoding="utf-8")
            size = svg.split('viewBox="0 0 ', 1)[1].split('"', 1)[0]
            print(
                f"{path.relative_to(ROOT)}  {size}  {path.stat().st_size // 1024} KiB"
            )


if __name__ == "__main__":
    main()
