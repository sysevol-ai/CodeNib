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
import json
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

# Change in input tokens with the prepared context against the agent reading
# the thread with gh, and its 95% interval, from docs/threads.md:
# (agent, model and tasks, change, low, high).
TAKEOVERS = [
    ("Codex", "gpt-6.1-sol · 76 pull requests", -21, -27, -14),
    ("Codex", "gpt-6.1-sol · 47 issues", -25, -31, -19),
    ("Claude Code", "Claude Opus 5.5 · 76 pull requests", -45, -57, -30),
]
# The scale of that figure runs from this change to zero, with these ticks.
SCALE_MIN = -70
TICKS = (-60, -40, -20, 0)

# The recorded Claude Code session the homepage replays.
RECORD = ROOT / "landing" / "assets" / "demos" / "codegraph-claude.json"

_BACKGROUND = re.compile(r'<rect width="\d+" height="\d+" fill="#[0-9a-fA-F]+"/>\n?')


def _percent(value: int) -> str:
    return f"−{-value}%" if value < 0 else f"{value}%" if value else "0"


def thread_tokens(c: dict[str, str]) -> str:
    """Change in input tokens with the prepared context, per agent and task."""
    styles = {
        "agent": {"size": 16, "weight": 620, "fill": c["ink"]},
        "tasks": {"size": 13, "fill": c["muted"]},
        "change": {"size": 21, "weight": 650, "spacing": -0.02, "fill": c["accent"]},
        "tick": {"size": 13, "fill": c["muted"]},
    }
    x0, x1, row = 286, 800, 78
    bottom = row * len(TAKEOVERS)
    fig = Figure(WIDTH, bottom + 34, styles)

    def at(value: float) -> float:
        return x1 - (x1 - x0) * value / SCALE_MIN

    for i, (agent, tasks, change, low, high) in enumerate(TAKEOVERS):
        top = i * row
        fig.path(f"M 0 {top + 0.5} H {WIDTH}", stroke=c["rule"], sw=1)
        fig.text(0, top + 35, agent, "agent")
        fig.text(0, top + 56, tasks, "tasks")
        y = top + 52
        fig.path(f"M {at(low):.1f} {y} H {at(high):.1f}", stroke=c["accent"], sw=3)
        fig.circle(at(change), y, 8, c["accent"])
        fig.text(at(change), top + 31, _percent(change), "change", anchor="middle")
    fig.path(f"M 0 {bottom + 0.5} H {WIDTH}", stroke=c["rule"], sw=1)
    # Zero is the agent reading the thread itself: the line each row is read
    # against.
    fig.path(f"M {x1} 0.5 V {bottom + 6}", stroke=c["base"], sw=1)
    for tick in TICKS:
        if tick:
            fig.path(f"M {at(tick):.1f} {bottom} v 6", stroke=c["base"], sw=1)
        fig.text(at(tick), bottom + 25, _percent(tick), "tick", anchor="middle")
    fig.text(0, bottom + 25, "Change in input tokens", "tick")
    return fig.svg(
        "Change in input tokens when the agent is handed the thread context",
        "Three results, each a dot with its 95% interval on a scale from "
        "−70% to 0, where 0 is the agent reading the thread with gh. "
        + " ".join(
            f"{agent}, {tasks.replace(' · ', ', ')}: {_percent(change)} "
            f"({_percent(low)} to {_percent(high)})."
            for agent, tasks, change, low, high in TAKEOVERS
        ),
    )


def _symbol(name: str) -> tuple[str, str]:
    """Split ``src/requests/api.py:Session.get()`` into file stem and symbol."""
    file, _, symbol = name.rpartition(":")
    return Path(file).stem, symbol


def _entry_label(name: str) -> str:
    """A method keeps its class; a module function is named by its module."""
    stem, symbol = _symbol(name)
    return symbol if "." in symbol else f"{stem}.{symbol}"


def _chain_label(name: str) -> str:
    """``Class.method()`` when it is short, otherwise the method alone."""
    symbol = _symbol(name)[1]
    return symbol if len(symbol) <= 18 else symbol.rpartition(".")[2]


def caller_path(c: dict[str, str]) -> str:
    """What one dependency_subgraph call returned in the recorded session."""
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    result = record["dependency_result"]
    nodes = sorted(result["nodes"], key=lambda node: -node["depth"])
    deepest = nodes[0]["depth"]
    entries = [node for node in nodes if node["depth"] == deepest]
    chain = [node for node in nodes if node["depth"] < deepest]
    if [node["depth"] for node in chain] != list(range(deepest - 1, 0, -1)):
        raise ValueError("the recorded callers are not one path")
    anchor = next(a for a in record["source_anchors"] if a["symbol"] == result["root"])
    chain.append(
        {"name": result["root"], "file": anchor["file"], "line": anchor["start_line"]}
    )

    styles = {
        "entry": {"family": "mono", "size": 13, "fill": c["muted"]},
        "name": {"family": "mono", "size": 14, "weight": 500, "fill": c["ink"]},
        "root": {"family": "mono", "size": 14, "weight": 680, "fill": c["ink"]},
        "where": {"family": "mono", "size": 13, "fill": c["muted"]},
    }
    pitch, pad = 23, 10
    # The entry points meet at ``join``, left of the first name on the path.
    label_x, dot_x, join = 150, 163, 236
    xs = [312, 464, 616, 766]
    if len(chain) != len(xs):
        raise ValueError("the figure is laid out for a path of four functions")
    mid = pad + (len(entries) - 1) * pitch / 2
    fig = Figure(WIDTH, int(2 * mid + 1), styles)

    for i, node in enumerate(entries):
        y = pad + i * pitch
        fig.text(label_x, y + 4.5, _entry_label(node["name"]), "entry", anchor="end")
        bend = (dot_x + join) / 2
        fig.path(
            f"M {dot_x} {y} C {bend} {y} {bend} {mid} {join} {mid}",
            stroke=c["base"],
            sw=1.25,
        )
        fig.circle(dot_x, y, 3, c["base"])
    fig.path(f"M {join} {mid} H {xs[-1]}", stroke=c["muted"], sw=1.5)
    for i, (x, node) in enumerate(zip(xs, chain, strict=True)):
        last = i == len(chain) - 1
        tip = x - (16 if last else 13)
        fig.path(
            f"M {tip - 6} {mid - 5} L {tip} {mid} L {tip - 6} {mid + 5}",
            stroke=c["muted"],
            sw=1.5,
        )
        fig.circle(x, mid, 8 if last else 5, c["accent"] if last else c["muted"])
        style = "root" if last else "name"
        fig.text(x, mid - 20, _chain_label(node["name"]), style, anchor="middle")
        where = f"{Path(node['file']).name}:{node['line']}"
        fig.text(x, mid + 31, where, "where", anchor="middle")
    names = [_chain_label(node["name"]) for node in chain]
    return fig.svg(
        f"The callers of {names[-1]} in Requests, from one dependency_subgraph call",
        f"{len(entries)} public entry points, "
        + ", ".join(_entry_label(node["name"]) for node in entries)
        + f", each reach {names[0]}. From there one path leads to {names[-1]}: "
        + ", then ".join(
            f"{name} at {Path(node['file']).name} line {node['line']}"
            for name, node in zip(names, chain, strict=True)
        )
        + ".",
    )


FIGURES = {"thread-tokens": thread_tokens, "caller-path": caller_path}


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
