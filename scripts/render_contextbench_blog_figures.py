#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
"""Render the ContextBench blog figures as self-contained SVG files.

Writes four files to ``landing/assets/blogs/``:

- ``contextbench-search-handoff.svg`` / ``-narrow.svg``: how two search windows,
  the coordinator and the repair window exchange work, and where each score is
  measured. The wide version is also the article's sharing image.
- ``contextbench-task-sources.svg`` / ``-narrow.svg``: native Pass@1 and model
  usage per task source.

Every SVG embeds a Geist subset, so it renders in the site font when it is
loaded through ``<img>`` or opened on its own. Values are copied from the
frozen submission results (``RESULTS.md`` at the pinned submission commit).

Requires fontTools with WOFF2 support (``pip install "fonttools[woff]"``).
Then render the 1600x840 sharing image with Playwright::

    python scripts/render_contextbench_blog_figures.py
    node scripts/render_svg_png.mjs \\
        landing/assets/blogs/contextbench-search-handoff.svg \\
        landing/assets/blogs/contextbench-search-handoff.png
"""

from __future__ import annotations

import argparse
import base64
import io
import logging
import math
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parents[1]
FONTS = ROOT / "landing" / "assets" / "fonts"
OUT = ROOT / "landing" / "assets" / "blogs"

INK = "#0f172a"
INK_2 = "#475569"
INK_3 = "#94a3b8"
LINE = "#e2e8f0"
LINE_STRONG = "#cbd5e1"
SUBTLE = "#f8fafc"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
ACCENT_SOFT = "#eff6ff"
ACCENT_LINE = "#bfdbfe"
SKY = "#0ea5e9"
SKY_LINE = "#bae6fd"
TEAL = "#0d9488"
TEAL_DARK = "#0f766e"
TEAL_LINE = "#99f6e4"
TEAL_SOFT = "#f0fdfa"
ON_DARK = "#cbd5e1"

SANS = "Geist, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif"
MONO = "'Geist Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

# Native outcomes and recorded search-plus-repair model usage by task source.
TASK_SOURCES = [
    ("SWE-Bench-Verified", 152, 174, 0.3910),
    ("SWE-PolyBench", 92, 116, 0.6486),
    ("Multi-SWE-Bench", 110, 156, 0.7939),
    ("SWE-Bench-Pro", 34, 54, 1.3289),
]
ALL_RESOLVED, ALL_TASKS, ALL_USD = 388, 500, 0.6778


class FontFace:
    """Measures text with a variable font and records the glyphs it needs."""

    def __init__(self, family: str, path: Path):
        self.family = family
        self.path = path
        self.used: set[str] = set()
        self._instances: dict[int, TTFont] = {}

    def _instance(self, weight: int) -> TTFont:
        if weight not in self._instances:
            font = TTFont(self.path)
            self._instances[weight] = instancer.instantiateVariableFont(
                font, {"wght": weight}
            )
        return self._instances[weight]

    def width(self, text: str, size: float, weight: int, spacing: float) -> float:
        font = self._instance(weight)
        cmap = font.getBestCmap()
        hmtx = font["hmtx"]
        upm = font["head"].unitsPerEm
        units = sum(hmtx[cmap[ord(c)]][0] if ord(c) in cmap else upm // 2 for c in text)
        return units / upm * size + spacing * size * len(text)

    def css(self) -> str:
        if not self.used:
            return ""
        options = subset.Options()
        options.flavor = "woff2"
        options.layout_features = ["kern", "liga", "calt"]
        options.name_IDs = [0, 1, 2, 3, 4, 5, 6]
        font = TTFont(self.path)
        subsetter = subset.Subsetter(options)
        subsetter.populate(text="".join(sorted(self.used)))
        subsetter.subset(font)
        buffer = io.BytesIO()
        font.flavor = "woff2"
        font.save(buffer)
        data = base64.b64encode(buffer.getvalue()).decode("ascii")
        return (
            f"@font-face{{font-family:{self.family!r};font-weight:100 900;"
            f"src:url(data:font/woff2;base64,{data}) format('woff2');}}"
        )


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def element(name: str, content: str | None = None, **attrs) -> str:
    """Serialize one SVG element; ``stroke_width`` becomes ``stroke-width``."""
    parts = [name]
    for key, value in attrs.items():
        if value is None:
            continue
        if isinstance(value, float):
            value = f"{value:.1f}"
        parts.append('{}="{}"'.format(key.rstrip("_").replace("_", "-"), value))
    tag = " ".join(parts)
    return f"<{tag}/>" if content is None else f"<{tag}>{content}</{name}>"


class Figure:
    """Collects SVG markup; text styles are named, measured and emitted as CSS."""

    def __init__(self, width: int, height: int, styles: dict[str, dict]):
        self.width = width
        self.height = height
        self.styles = styles
        self.fonts = {
            "sans": FontFace("Geist", FONTS / "Geist-Variable.woff2"),
            "mono": FontFace("Geist Mono", FONTS / "GeistMono-Variable.woff2"),
        }
        self.parts: list[str] = []

    def _style(self, name: str) -> dict:
        style = {"family": "sans", "weight": 400, "spacing": 0.0, "fill": INK}
        style.update(self.styles[name])
        return style

    def measure(self, text: str, name: str) -> float:
        s = self._style(name)
        return self.fonts[s["family"]].width(text, s["size"], s["weight"], s["spacing"])

    def text(self, x, y, text, name, anchor="start", fill=None):
        s = self._style(name)
        self.fonts[s["family"]].used.update(text)
        self.parts.append(
            element(
                "text",
                esc(text),
                x=float(x),
                y=float(y),
                class_=name,
                text_anchor=None if anchor == "start" else anchor,
                fill=fill,
            )
        )

    def lines(self, x, y, lines, name, leading, anchor="start"):
        for i, line in enumerate(lines):
            self.text(x, y + i * leading, line, name, anchor)

    def wrap(self, text: str, name: str, width: float) -> list[str]:
        lines, current = [], ""
        for word in text.split():
            trial = f"{current} {word}".strip()
            if current and self.measure(trial, name) > width:
                lines.append(current)
                current = word
            else:
                current = trial
        return lines + [current] if current else lines

    def rect(self, x, y, w, h, rx=0, fill="none", stroke=None, sw=1.5):
        self.parts.append(
            element(
                "rect",
                x=float(x),
                y=float(y),
                width=float(w),
                height=float(h),
                rx=rx,
                fill=fill,
                stroke=stroke,
                stroke_width=sw if stroke else None,
            )
        )

    def circle(self, cx, cy, r, fill):
        self.parts.append(element("circle", cx=float(cx), cy=float(cy), r=r, fill=fill))

    def path(self, d, stroke=None, sw=2, marker=None, dash=None, fill="none"):
        self.parts.append(
            element(
                "path",
                d=d,
                fill=fill,
                stroke=stroke,
                stroke_width=sw if stroke else None,
                stroke_linecap="round" if stroke else None,
                stroke_linejoin="round" if stroke else None,
                stroke_dasharray=dash,
                marker_end=f"url(#{marker})" if marker else None,
            )
        )

    def svg(self, title: str, desc: str) -> str:
        css = [f.css() for f in self.fonts.values()]
        for name in self.styles:
            s = self._style(name)
            family = SANS if s["family"] == "sans" else MONO
            rule = (
                f"font-family:{family};font-size:{s['size']}px;"
                f"font-weight:{s['weight']};fill:{s['fill']};"
            )
            if s["spacing"]:
                rule += f"letter-spacing:{s['spacing']}em;"
            css.append(f".{name}{{{rule}}}")
        markers = "".join(
            element(
                "marker",
                element("path", d="M0,0 L10,5 L0,10 z", fill=color),
                id=f"arrow-{key}",
                viewBox="0 0 10 10",
                refX=8.5,
                refY=5,
                markerWidth=7,
                markerHeight=7,
                orient="auto-start-reverse",
            )
            for key, color in (
                ("gray", INK_3),
                ("blue", ACCENT),
                ("sky", SKY),
                ("teal", TEAL),
                ("ink", INK),
            )
        )
        body = "\n".join(self.parts)
        head = (
            element("title", esc(title), id="t")
            + "\n"
            + element("desc", esc(desc), id="d")
            + "\n"
            + element("defs", markers + element("style", "".join(css)))
            + "\n"
            + element("rect", width=self.width, height=self.height, fill="#ffffff")
        )
        return (
            element(
                "svg",
                f"\n{head}\n{body}\n",
                xmlns="http://www.w3.org/2000/svg",
                viewBox=f"0 0 {self.width} {self.height}",
                width=self.width,
                height=self.height,
                role="img",
                aria_labelledby="t d",
            )
            + "\n"
        )


# --------------------------------------------------------------------------- #
# Mechanism diagram


def scaled(base: dict[str, dict], s: float) -> dict[str, dict]:
    return {k: {**v, "size": round(v["size"] * s, 1)} for k, v in base.items()}


DIAGRAM_STYLES = {
    "kicker": {"size": 15, "weight": 650, "spacing": 0.08, "fill": ACCENT},
    "head": {"size": 34, "weight": 650, "spacing": -0.01},
    "status": {"size": 15, "weight": 600, "fill": ACCENT_DARK},
    "phase": {"size": 14, "weight": 650, "spacing": 0.08, "fill": ACCENT},
    "phase-r": {"size": 14, "weight": 650, "spacing": 0.08, "fill": TEAL_DARK},
    "note": {"size": 15, "fill": INK_2},
    "card": {"size": 19, "weight": 620},
    "tag": {"size": 15, "fill": INK_2},
    "tool": {"size": 15, "weight": 500, "family": "mono"},
    "desc": {"size": 16, "fill": INK_2},
    "label": {"size": 15, "fill": INK_2},
    "issue": {"size": 18, "weight": 620},
    "panel": {"size": 16, "weight": 600},
    "big": {"size": 26, "weight": 650, "spacing": -0.02, "fill": ACCENT_DARK},
    "big-r": {"size": 26, "weight": 650, "spacing": -0.02, "fill": TEAL_DARK},
    "nav": {"size": 15, "weight": 600, "fill": "#ffffff"},
    "nav-2": {"size": 14, "fill": ON_DARK},
    "coord": {"size": 19, "weight": 650, "fill": "#ffffff"},
    "coord-2": {"size": 15, "fill": ON_DARK},
    "foot": {"size": 18, "weight": 500},
    "foot-2": {"size": 15, "fill": INK_2},
}

# Illustrative observed ranges on four files: (start, end) as track fractions.
RANGES = {
    "first": [[(0.08, 0.30), (0.52, 0.60)], [(0.18, 0.44)], [], [(0.70, 0.79)]],
    "scout": [[], [(0.40, 0.58)], [(0.06, 0.20), (0.62, 0.84)], [(0.28, 0.50)]],
    "repair": [[(0.70, 0.86)], [], [(0.38, 0.47)], []],
}
STATE_LAYERS = {
    "first": [("first", ACCENT)],
    "union": [("scout", SKY), ("first", ACCENT)],
    "final": [("scout", SKY), ("first", ACCENT), ("repair", TEAL)],
}

CARDS = [
    {
        "title": "First search",
        "dot": ACCENT,
        "stroke": ACCENT_LINE,
        "tag": "Haiku 5.5 · ≤ 20 turns",
        "tools": ["glob", "grep", "read"],
        "loop": "next call chosen from what was just read",
        "desc": "Looks for relevant functions, callers, configuration, interfaces "
        "and tests",
    },
    {
        "title": "Complementary scout",
        "dot": SKY,
        "stroke": SKY_LINE,
        "tag": "Haiku 5.5",
        "tools": ["glob", "grep", "read"],
        "loop": "starts from the issue + navigation",
        "desc": "Looks for what the first search missed: entry points, callers, "
        "alternate paths, tests",
    },
    {
        "title": "Repair",
        "dot": TEAL,
        "stroke": TEAL_LINE,
        "tag": "Opus 5.5",
        "tools": ["read · grep", "edit", "self-test"],
        "loop": "fetches source through CodeNib tools",
        "desc": None,
    },
]

PANELS = [
    (
        "first",
        "Ranges observed by the first search",
        None,
        "merged, duplicates removed",
    ),
    (
        "union",
        "+ scout ranges, overlaps count once",
        "77.07%",
        "line recall, search only",
    ),
    ("final", "+ confirmed repair reads", "77.90%", "final line recall"),
]

HEADLINE = "Search reaches 77% line recall before repair begins."
FOOT = (
    "Navigation carries locations, not source. Each window reads the code it "
    "needs; the coordinator keeps the union of what was observed."
)
FOOT_2 = (
    "500 ContextBench tasks, scores macro-averaged per task · fixed role routing · "
    "glob, grep and reads only: no embeddings, Jev or graph expansion"
)
DIAGRAM_TITLE = "How CodeNib searches before repair on ContextBench"
DIAGRAM_DESC = (
    "An original issue goes to a first Claude Haiku 5.5 search window, which "
    "repeatedly chooses glob, grep and read calls from what it has just read, for "
    "up to 20 turns. The CodeNib coordinator merges the source ranges it observed "
    "and passes navigation of at most 2,048 tokens, locations only and no source, "
    "to a complementary Haiku scout window. The scout runs its own search loop to "
    "find missed entry points, callers, alternate paths and tests. The coordinator "
    "unions both windows' observed ranges, counting overlaps once: this search "
    "context reaches 77.07% line recall on 500 tasks. Bounded navigation then goes "
    "to a Claude Opus 5.5 repair window, which fetches source through CodeNib "
    "tools, edits and self-tests; 388 of 500 tasks are resolved, 77.60% native "
    "Pass@1. Adding confirmed repair reads gives 77.90% final line recall. Search "
    "costs $0.0114 of model usage per task; repair accounts for 98.32% of model "
    "usage. Submitted results; leaderboard review pending."
)


def status_pill(fig, x_right, y_mid, s=1.0, x_left=None):
    label = "Submitted · leaderboard review pending"
    w = fig.measure(label, "status") + 40 * s
    h = 34 * s
    x = x_left if x_left is not None else x_right - w
    fig.rect(x, y_mid - h / 2, w, h, rx=h / 2, fill=ACCENT_SOFT, stroke=ACCENT_LINE)
    fig.circle(x + 16 * s, y_mid, 4 * s, ACCENT)
    fig.text(x + 28 * s, y_mid + 5.2 * s, label, "status")
    return h


def tool_row(fig, x, y, w, tools, s, dot):
    """Draw tool pills centered in [x, x+w] at top y; return pill extents."""
    pad, gap, h = 13 * s, 28 * s, 36 * s
    widths = [fig.measure(t, "tool") + 2 * pad for t in tools]
    total = sum(widths) + gap * (len(tools) - 1)
    cx = x + (w - total) / 2
    extents = []
    for i, (tool, tw) in enumerate(zip(tools, widths, strict=True)):
        fig.rect(cx, y, tw, h, rx=9 * s, fill=SUBTLE, stroke=LINE_STRONG, sw=1.2)
        fig.text(cx + tw / 2, y + h / 2 + 5.2 * s, tool, "tool", anchor="middle")
        extents.append((cx, cx + tw))
        if i < len(tools) - 1:
            ax = cx + tw + 5 * s
            fig.path(
                f"M{ax:.1f},{y + h / 2:.1f} H{ax + gap - 11 * s:.1f}",
                INK_3,
                1.6,
                "arrow-gray",
            )
        cx += tw + gap
    # The return path: the next tool call depends on what was just read.
    left = (extents[0][0] + extents[0][1]) / 2
    right = (extents[-1][0] + extents[-1][1]) / 2
    bottom, drop = y + h, 22 * s
    fig.path(
        f"M{right:.1f},{bottom + 3 * s:.1f} C{right:.1f},{bottom + drop + 6 * s:.1f} "
        f"{right - 18 * s:.1f},{bottom + drop:.1f} {right - 34 * s:.1f},"
        f"{bottom + drop:.1f} H{left + 34 * s:.1f} C{left + 18 * s:.1f},"
        f"{bottom + drop:.1f} {left:.1f},{bottom + drop + 6 * s:.1f} {left:.1f},"
        f"{bottom + 5 * s:.1f}",
        dot,
        1.8,
        {ACCENT: "arrow-blue", SKY: "arrow-sky", TEAL: "arrow-teal"}[dot],
    )
    return bottom + drop


def window_card(fig, x, y, w, h, card, s, outcome=False):
    pad = 24 * s
    fig.rect(x, y, w, h, rx=16 * s, fill="#ffffff", stroke=card["stroke"], sw=1.6)
    fig.circle(x + pad + 5 * s, y + 34 * s, 6 * s, card["dot"])
    fig.text(x + pad + 19 * s, y + 40 * s, card["title"], "card")
    if card["tag"]:
        fig.text(x + w - pad, y + 40 * s, card["tag"], "tag", anchor="end")
    loop_bottom = tool_row(
        fig, x + pad, y + 66 * s, w - 2 * pad, card["tools"], s, card["dot"]
    )
    fig.text(x + w / 2, loop_bottom + 24 * s, card["loop"], "label", anchor="middle")
    if outcome:
        base = loop_bottom + 68 * s
        fig.path(f"M{x + pad:.1f},{base - 26 * s:.1f} H{x + w - pad:.1f}", LINE, 1.2)
        fig.text(x + pad, base, "Final patch, graded natively", "label")
        big_y = base + 36 * s
        fig.text(x + pad, big_y, "388 / 500", "big-r")
        bw = fig.measure("388 / 500", "big-r")
        fig.text(x + pad + bw + 10 * s, big_y, "resolved · 77.60% Pass@1", "desc")
    else:
        lines = fig.wrap(card["desc"], "desc", w - 2 * pad)
        fig.lines(x + pad, loop_bottom + 70 * s, lines, "desc", 24 * s)


def ledger_tracks(fig, x, y, w, state, s):
    gap, th = 16 * s, 8 * s
    for i in range(4):
        ty = y + i * gap
        fig.rect(x, ty, w, th, rx=th / 2, fill=LINE)
        for key, color in STATE_LAYERS[state]:
            for a, b in RANGES[key][i]:
                fig.rect(x + a * w, ty, (b - a) * w, th, rx=th / 2, fill=color)
    return y + 3 * gap + th


def ledger_panel_height(s):
    return (32 + 3 * 16 + 8 + 38) * s


def ledger_panel(fig, x, y, w, panel, s):
    state, title, big, label = panel
    fig.text(x, y + 16 * s, title, "panel")
    tracks_bottom = ledger_tracks(fig, x, y + 32 * s, w, state, s)
    base = tracks_bottom + 38 * s
    if big:
        style = "big" if state == "union" else "big-r"
        fig.text(x, base, big, style)
        fig.text(x + fig.measure(big, style) + 10 * s, base, label, "desc")
    else:
        fig.text(x, base, label, "desc")


def nav_pill(fig, cx, cy, s, second):
    first = "navigation · ≤ 2,048 tokens" if second is None else "navigation · bounded"
    sub = "locations only, no source"
    w = max(fig.measure(first, "nav"), fig.measure(sub, "nav-2")) + 36 * s
    h = 54 * s
    fig.rect(cx - w / 2, cy - h / 2, w, h, rx=12 * s, fill=INK)
    fig.text(cx, cy - 3 * s, first, "nav", anchor="middle")
    fig.text(cx, cy + 17 * s, sub, "nav-2", anchor="middle")


def down_arrow(fig, x, y0, y1, label, color, marker, s, side="right"):
    fig.path(f"M{x:.1f},{y0:.1f} V{y1:.1f}", color, 2, marker)
    if label:
        lx = x + 12 * s if side == "right" else x - 12 * s
        fig.text(
            lx,
            (y0 + y1) / 2 + 5 * s,
            label,
            "label",
            "start" if side == "right" else "end",
        )


def search_handoff_wide() -> str:
    fig = Figure(1600, 840, DIAGRAM_STYLES)
    left, right = 56, 1544
    fig.text(left, 60, "CODENIB ON CONTEXTBENCH · 500 TASKS", "kicker")
    fig.text(left, 106, HEADLINE, "head")
    status_pill(fig, right, 52)

    issue_w, gap = 176, 40
    card_w = (right - left - issue_w - gap - 2 * gap) / 3
    xs = [left + issue_w + gap + i * (card_w + gap) for i in range(3)]
    top, card_h = 186, 262

    # Phase brackets.
    for x0, x1, label, style, note in (
        (
            xs[0],
            xs[1] + card_w,
            "SEARCH ONLY",
            "phase",
            "two Haiku windows · no edits · $0.0114 model usage / task",
        ),
        (xs[2], xs[2] + card_w, "REPAIR", "phase-r", "98.32% of model usage"),
    ):
        fig.text(x0, 150, label, style)
        fig.text(x1, 150, note, "note", anchor="end")
        fig.path(f"M{x0:.1f},{170:.1f} V162 H{x1:.1f} V170", LINE_STRONG, 1.4)

    # Issue.
    iy = top + card_h / 2 - 72
    fig.rect(left, iy, issue_w, 144, rx=14, fill=SUBTLE, stroke=LINE_STRONG)
    fig.text(left + 20, iy + 40, "Original issue", "issue")
    fig.lines(
        left + 20, iy + 72, ["No reference fix,", "test patch or hints"], "desc", 24
    )
    fig.path(
        f"M{left + issue_w + 4:.1f},{top + card_h / 2:.1f} H{xs[0] - 6:.1f}",
        ACCENT,
        2,
        "arrow-blue",
    )

    for i, (x, card) in enumerate(zip(xs, CARDS, strict=True)):
        window_card(fig, x, top, card_w, card_h, card, 1.0, outcome=i == 2)

    # Coordinator lane.
    lane_y, lane_h = 572, 168
    fig.rect(left, lane_y, right - left, lane_h, rx=16, fill=SUBTLE, stroke=LINE)
    fig.path(
        f"M{left + 16},{lane_y} H{left + issue_w} V{lane_y + lane_h} "
        f"H{left + 16} A16,16 0 0 1 {left},{lane_y + lane_h - 16} V{lane_y + 16} "
        f"A16,16 0 0 1 {left + 16},{lane_y} z",
        fill=INK,
    )
    fig.lines(left + 20, lane_y + 42, ["CodeNib", "coordinator"], "coord", 24)
    fig.lines(
        left + 20,
        lane_y + 100,
        ["keeps observed", "source ranges for", "one source version"],
        "coord-2",
        21,
    )
    for x, panel in zip(xs, PANELS, strict=True):
        ledger_panel(fig, x + 20, lane_y + 20, card_w - 40, panel, 1.0)

    # Exchanges between the windows and the coordinator.
    card_bottom = top + card_h
    labels = ["observed ranges", "observed ranges", "confirmed reads"]
    colors = [(ACCENT, "arrow-blue"), (SKY, "arrow-sky"), (TEAL, "arrow-teal")]
    for x, label, (color, marker) in zip(xs, labels, colors, strict=True):
        down_arrow(fig, x + 150, card_bottom + 4, lane_y - 6, label, color, marker, 1.0)
    for i in range(2):
        cx = (xs[i] + card_w + xs[i + 1]) / 2
        cy = (card_bottom + lane_y) / 2
        dx = 72
        fig.path(
            f"M{cx - dx:.1f},{lane_y - 4:.1f} L{cx + dx:.1f},{card_bottom + 6:.1f}",
            INK,
            2,
            "arrow-ink",
        )
        nav_pill(fig, cx, cy, 1.0, None if i == 0 else True)

    fig.path(f"M{left},756 H{right}", LINE, 1.2)
    fig.text(left, 788, FOOT, "foot")
    fig.text(left, 816, FOOT_2, "foot-2")
    return fig.svg(DIAGRAM_TITLE, DIAGRAM_DESC)


def search_handoff_narrow() -> str:
    s = 1.3
    styles = scaled(DIAGRAM_STYLES, s)
    styles["head"] = {**DIAGRAM_STYLES["head"], "size": 40}
    W, left = 640, 28
    inner = W - 2 * left
    fig = Figure(W, 100, styles)
    y = 62
    fig.text(left, y, "CODENIB ON CONTEXTBENCH", "kicker")
    head_lines = fig.wrap(HEADLINE, "head", inner)
    fig.lines(left, y + 56, head_lines, "head", 48)
    y += 56 + 48 * (len(head_lines) - 1) + 44
    y += status_pill(fig, 0, y + 22, s, x_left=left) + 46

    def phase(label, style, note):
        nonlocal y
        fig.text(left, y, label, style)
        fig.text(left + inner, y, note, "note", anchor="end")
        y += 18

    # Issue.
    fig.rect(left, y, inner, 92, rx=14, fill=SUBTLE, stroke=LINE_STRONG)
    fig.text(left + 22, y + 40, "Original issue", "issue")
    fig.text(left + 22, y + 72, "No reference fix, test patch or hints", "desc")
    y += 92
    fig.path(f"M{W / 2:.1f},{y + 6:.1f} V{y + 42:.1f}", ACCENT, 2.4, "arrow-blue")
    y += 62
    phase("SEARCH ONLY", "phase", "$0.0114 model usage / task")

    card_h = 240 * s
    lane_pad = 22
    labels = ["observed ranges", "observed ranges", "confirmed reads"]
    colors = [(ACCENT, "arrow-blue"), (SKY, "arrow-sky"), (TEAL, "arrow-teal")]
    for i, (card, panel) in enumerate(zip(CARDS, PANELS, strict=True)):
        if i == 2:
            phase("REPAIR", "phase-r", "98.32% of model usage")
        window_card(fig, left, y, inner, card_h, card, s, outcome=i == 2)
        y += card_h
        color, marker = colors[i]
        down_arrow(fig, W / 2, y + 6, y + 58, labels[i], color, marker, s)
        y += 66
        panel_h = 64 + ledger_panel_height(1.15) + 26
        fig.rect(left, y, inner, panel_h, rx=16, fill=SUBTLE, stroke=LINE)
        chip = "CodeNib coordinator"
        cw = fig.measure(chip, "nav") + 28
        fig.rect(left + lane_pad, y + 18, cw, 32, rx=8, fill=INK)
        fig.text(left + lane_pad + 14, y + 40, chip, "nav")
        ledger_panel(fig, left + lane_pad, y + 64, inner - 2 * lane_pad, panel, 1.15)
        y += panel_h
        if i < 2:
            fig.path(f"M{W / 2:.1f},{y + 6:.1f} V{y + 30:.1f}", INK, 2)
            nav_pill(fig, W / 2, y + 66, s, None if i == 0 else True)
            fig.path(f"M{W / 2:.1f},{y + 102:.1f} V{y + 128:.1f}", INK, 2, "arrow-ink")
            y += 156
    y += 30
    fig.path(f"M{left},{y} H{left + inner}", LINE, 1.2)
    y += 44
    foot = fig.wrap(FOOT, "foot", inner)
    fig.lines(left, y, foot, "foot", 32)
    y += 32 * len(foot) + 8
    foot2 = fig.wrap(FOOT_2, "foot-2", inner)
    fig.lines(left, y, foot2, "foot-2", 28)
    y += 28 * (len(foot2) - 1) + 40
    fig.height = math.ceil(y)
    return fig.svg(DIAGRAM_TITLE, DIAGRAM_DESC)


# --------------------------------------------------------------------------- #
# Task-source chart

CHART_STYLES = {
    "kicker": {"size": 15, "weight": 650, "spacing": 0.08, "fill": ACCENT},
    "head": {"size": 30, "weight": 650, "spacing": -0.01},
    "legend": {"size": 16, "fill": INK_2},
    "col": {"size": 15, "weight": 600, "fill": INK_2},
    "row": {"size": 20, "weight": 620},
    "row-2": {"size": 16, "fill": INK_2},
    "pct": {"size": 18, "weight": 650, "fill": "#ffffff"},
    "usd": {"size": 17, "weight": 600, "fill": INK},
    "ref": {"size": 15, "weight": 600},
    "tick": {"size": 14, "fill": INK_3, "family": "mono"},
    "foot": {"size": 15, "fill": INK_2},
    "src": {"size": 14, "fill": INK_3},
}
CHART_HEAD = "388 of 500 resolved; unresolved attempts stay in"
CHART_FOOT = (
    "Pass@1 counts a final patch as resolved when it passes the pinned "
    "task-specific grading criteria. Groups differ in repositories, languages "
    "and grading, so they describe this run rather than a benchmark effect."
)
CHART_SRC = "Frozen submission results · model usage is recorded search + repair usage"
CHART_TITLE = "Native Pass@1 and model usage by task source"
CHART_DESC = (
    "Resolved tasks by source, sorted by Pass@1: SWE-Bench-Verified 152 of 174, "
    "87.36%, $0.3910 model usage per task; SWE-PolyBench 92 of 116, 79.31%, "
    "$0.6486; Multi-SWE-Bench 110 of 156, 70.51%, $0.7939; SWE-Bench-Pro 34 of "
    "54, 62.96%, $1.3289. All 500 tasks: 388 resolved, 77.60%, $0.6778 per task. "
    "All unresolved attempts remain included."
)
UNRESOLVED = "#e2e8f0"
USD_FILL = "#64748b"


def pct(n, d):
    return f"{100 * n / d:.2f}%"


def legend_swatches(fig, x, y, items):
    for kind, color, label in items:
        if kind == "box":
            fig.rect(x, y - 13, 16, 16, rx=4, fill=color)
            x += 26
        else:
            fig.path(f"M{x + 2},{y - 15} V{y + 4}", INK, 2, dash="4 4")
            x += 16
        fig.text(x, y, label, "legend")
        x += fig.measure(label, "legend") + 32


def task_sources_wide() -> str:
    fig = Figure(1600, 700, CHART_STYLES)
    left, right = 56, 1544
    fig.text(left, 60, "NATIVE PASS@1 BY TASK SOURCE", "kicker")
    fig.text(left, 102, CHART_HEAD, "head")
    legend_swatches(
        fig,
        left,
        150,
        [
            ("box", ACCENT, "Resolved"),
            ("box", UNRESOLVED, "Unresolved"),
            ("ref", INK, "All 500 tasks"),
        ],
    )

    bar_x, bar_w = 400, 760
    usd_x, usd_w, usd_max = 1250, 210, 1.5
    rows_top, step, bar_h = 254, 92, 40
    bottom = rows_top + step * (len(TASK_SOURCES) - 1) + bar_h + 18

    fig.text(bar_x, 198, "Resolved share of attempts", "col")
    fig.text(usd_x, 198, "Model usage per task", "col")
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        gx = bar_x + frac * bar_w
        fig.path(f"M{gx:.1f},{rows_top - 12} V{bottom}", LINE, 1)
        fig.text(gx, bottom + 26, f"{int(frac * 100)}%", "tick", anchor="middle")
    for usd in (0, 0.5, 1.0, 1.5):
        gx = usd_x + usd / usd_max * usd_w
        fig.path(f"M{gx:.1f},{rows_top - 12} V{bottom}", LINE, 1)
        fig.text(gx, bottom + 26, f"${usd:.2f}", "tick", anchor="middle")

    ref_x = bar_x + ALL_RESOLVED / ALL_TASKS * bar_w
    usd_ref = usd_x + ALL_USD / usd_max * usd_w
    for rx, label in (
        (ref_x, f"All 500 · {pct(ALL_RESOLVED, ALL_TASKS)}"),
        (usd_ref, f"All · ${ALL_USD:.2f}"),
    ):
        fig.path(f"M{rx:.1f},{rows_top - 18} V{bottom}", INK, 2, dash="5 5")
        fig.text(rx, rows_top - 26, label, "ref", anchor="middle")

    for i, (name, solved, total, usd) in enumerate(TASK_SOURCES):
        y = rows_top + i * step
        fig.text(left, y + 17, name, "row")
        fig.text(left, y + 43, f"{solved} of {total} resolved", "row-2")
        fig.rect(bar_x, y, bar_w, bar_h, rx=6, fill=UNRESOLVED)
        w = solved / total * bar_w
        fig.rect(bar_x, y, w, bar_h, rx=6, fill=ACCENT)
        fig.text(
            bar_x + w - 14, y + bar_h / 2 + 6.5, pct(solved, total), "pct", anchor="end"
        )
        uw = usd / usd_max * usd_w
        fig.rect(usd_x, y + 8, uw, bar_h - 16, rx=4, fill=USD_FILL)
        fig.text(right, y + bar_h / 2 + 6, f"${usd:.2f}", "usd", anchor="end")

    fy = bottom + 74
    fig.path(f"M{left},{fy - 30} H{right}", LINE, 1.2)
    fig.lines(left, fy, fig.wrap(CHART_FOOT, "foot", right - left), "foot", 24)
    fig.text(left, fy + 40, CHART_SRC, "src")
    fig.height = fy + 66
    return fig.svg(CHART_TITLE, CHART_DESC)


def task_sources_narrow() -> str:
    styles = scaled(CHART_STYLES, 1.3)
    styles["head"] = {**CHART_STYLES["head"], "size": 36}
    W, left = 640, 28
    inner = W - 2 * left
    fig = Figure(W, 100, styles)
    y = 62
    fig.text(left, y, "NATIVE PASS@1 BY TASK SOURCE", "kicker")
    head = fig.wrap(CHART_HEAD, "head", inner)
    fig.lines(left, y + 54, head, "head", 44)
    y += 54 + 44 * (len(head) - 1) + 52
    ref_x = left + ALL_RESOLVED / ALL_TASKS * inner
    rows_top = y + 34
    fig.text(
        ref_x, y, f"All 500 · {pct(ALL_RESOLVED, ALL_TASKS)}", "ref", anchor="middle"
    )
    bar_h, step = 46, 150
    bottom = rows_top + step * (len(TASK_SOURCES) - 1) + 42 + bar_h + 6
    fig.path(f"M{ref_x:.1f},{rows_top - 14:.1f} V{bottom:.1f}", INK, 2.4, dash="6 6")
    for i, (name, solved, total, usd) in enumerate(TASK_SOURCES):
        ry = rows_top + i * step
        fig.text(left, ry + 24, name, "row")
        fig.text(left + inner, ry + 24, f"${usd:.2f} / task", "row-2", anchor="end")
        by = ry + 42
        fig.rect(left, by, inner, bar_h, rx=7, fill=UNRESOLVED)
        w = solved / total * inner
        fig.rect(left, by, w, bar_h, rx=7, fill=ACCENT)
        fig.text(
            left + w - 16, by + bar_h / 2 + 8, pct(solved, total), "pct", anchor="end"
        )
        fig.text(left, by + bar_h + 30, f"{solved} of {total} resolved", "row-2")
    y = bottom + 56
    fig.path(f"M{left},{y - 26} H{left + inner}", LINE, 1.2)
    foot = fig.wrap(CHART_FOOT, "foot", inner)
    fig.lines(left, y + 10, foot, "foot", 28)
    y += 10 + 28 * len(foot) + 12
    src = fig.wrap(CHART_SRC, "src", inner)
    fig.lines(left, y, src, "src", 26)
    fig.height = math.ceil(y + 26 * (len(src) - 1) + 36)
    return fig.svg(CHART_TITLE, CHART_DESC)


FIGURES = {
    "contextbench-search-handoff.svg": search_handoff_wide,
    "contextbench-search-handoff-narrow.svg": search_handoff_narrow,
    "contextbench-task-sources.svg": task_sources_wide,
    "contextbench-task-sources-narrow.svg": task_sources_narrow,
}


def main() -> None:
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, render in FIGURES.items():
        path = args.out / name
        path.write_text(render(), encoding="utf-8")
        print(
            f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}"
            f"  {path.stat().st_size // 1024} KiB"
        )


if __name__ == "__main__":
    main()
