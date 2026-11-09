#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Render the homepage's agent replay from a recorded CodeGraph session.

Every line and graph node comes from ``landing/assets/demos/codegraph-claude.json``.
This makes no model call and writes no text of its own beyond short labels. The
page shows the whole transcript without JavaScript; ``agent-replay.js`` only
reveals what is already there.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
RECORD = _PROJECT_ROOT / "landing" / "assets" / "demos" / "codegraph-claude.json"

BEGIN_MARKER = "<!-- BEGIN CODENIB_AGENT_REPLAY -->"
END_MARKER = "<!-- END CODENIB_AGENT_REPLAY -->"
CHAPTERS = ("Connect", "Ask", "Trace")
INDENT = "      "

GRAPH_WIDTH = 640
GRAPH_HEIGHT = 452
ENTRY_Y = 48
CHAIN_Y = {3: 160, 2: 246, 1: 332, 0: 414}
CENTER_X = 320


def _symbol(name: str) -> str:
    return name.split(":")[-1]


def _label(name: str) -> str:
    """Keep ``Class.method()`` when it fits; otherwise show the method alone."""

    short = _symbol(name)
    return short if len(short) <= 24 else short.split(".")[-1]


def _method(name: str) -> str:
    return _symbol(name).split(".")[-1]


def _source_url(record: dict, file: str, line: int) -> str:
    return f"{record['repository']}/blob/{record['source_commit']}/{file}#L{line}"


def _location(file: str, line: int) -> str:
    return f"{file.split('/')[-1]}:{line}"


def _line(
    chapter: int,
    wait: int,
    body: str,
    kind: str = "",
    light: int | None = None,
    typed: int = 0,
) -> str:
    parts = ("line", kind, "typed" if typed else "")
    classes = " ".join(part for part in parts if part)
    attrs = f'class="{classes}" data-chapter="{chapter}" data-wait="{wait}"'
    if light is not None:
        attrs += f' data-light="{light}"'
    if typed:
        attrs += f' style="--chars:{typed}"'
    return f"<p {attrs}>{body}</p>"


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _source_link(record: dict, file: str, line: int, text: str) -> str:
    return f'<a href="{html.escape(_source_url(record, file, line))}">{_esc(text)}</a>'


def _answer_paragraphs(record: dict) -> list[str]:
    paragraphs = record["answer"].replace("**", "").replace("`", "").split("\n\n")
    # The label before the chain repeats what the graph already says.
    return [part for part in paragraphs if not part.rstrip().endswith(":")]


def _transcript(record: dict) -> list[str]:
    explore, trace = record["tool_calls"]
    explore_tool = explore["tool_name"].split("__")[-1]
    trace_tool = trace["tool_name"].split("__")[-1]
    query = explore["tool_input"]["query"]
    command = record["setup_command"]
    lines = [
        _line(
            0,
            300,
            f'<span class="dim">$</span> {_esc(command)}',
            "cmd",
            typed=len(command) + 2,
        )
    ]
    for index, output in enumerate(record["setup_output_excerpt"]):
        lines.append(_line(0, 900 if index == 0 else 240, _esc(output), "out"))

    prompt = record["prompt"].split(" Cite files")[0]
    lines.append(_line(1, 1000, f'<span class="dim">&gt;</span> {_esc(prompt)}', "ask"))
    lines.append(
        _line(
            1,
            1000,
            f'<span class="tool">{_esc(explore_tool)}</span> '
            f'<span class="dim">{_esc(json.dumps(query))}</span>',
            typed=len(explore_tool) + len(query) + 3,
        )
    )
    anchors = sorted(
        record["source_anchors"], key=lambda item: item["start_line"], reverse=True
    )
    for index, anchor in enumerate(anchors):
        span = f"{_location(anchor['file'], anchor['start_line'])}–{anchor['end_line']}"
        link = _source_link(record, anchor["file"], anchor["start_line"], span)
        lines.append(
            _line(
                1,
                1100 if index == 0 else 320,
                f"{link} {_esc(_label(anchor['symbol']))}",
                "hit",
            )
        )

    graph = record["dependency_result"]
    root = _label(graph["root"])
    depth = trace["tool_input"]["depth"]
    direction = trace["tool_input"]["direction"]
    lines.append(
        _line(
            2,
            1000,
            f'<span class="tool">{_esc(trace_tool)}</span> '
            f'<span class="dim">{_esc(root)} {direction} depth {depth}</span>',
            light=0,
            typed=len(trace_tool) + len(root) + len(direction) + 11,
        )
    )
    for hop in range(1, depth + 1):
        found = [node for node in graph["nodes"] if node["depth"] == hop]
        if len(found) == 1:
            detail = _source_link(
                record,
                found[0]["file"],
                found[0]["line"],
                _location(found[0]["file"], found[0]["line"]),
            )
            text = f"{detail} {_esc(_label(found[0]['name']))}"
        else:
            text = f"{len(found)} public entry points"
        lines.append(
            _line(
                2,
                1000 if hop == 1 else 620,
                f'<span class="dim">hop {hop}</span> {text}',
                "hit",
                light=hop,
            )
        )
    for index, paragraph in enumerate(_answer_paragraphs(record)):
        lines.append(_line(2, 1000 if index == 0 else 500, _esc(paragraph), "answer"))
    stats = (
        f"{record['seconds']['agent']:.1f} s · {record['agent_turns']} turns · "
        f"{len(record['tool_calls'])} tools · "
        f"${record['agent_estimated_cost_usd']:.3f} list-price estimate · "
        + ("source unchanged" if record["source_unchanged"] else "source changed")
    )
    lines.append(_line(2, 600, _esc(stats), "stats"))
    return lines


def _graph(record: dict) -> str:
    graph = record["dependency_result"]
    root = next(a for a in record["source_anchors"] if a["symbol"] == graph["root"])
    root_node = {
        "name": graph["root"],
        "file": root["file"],
        "line": root["start_line"],
        "depth": 0,
    }
    nodes = [root_node, *graph["nodes"]]
    deepest = max(node["depth"] for node in nodes)
    entries = [node for node in nodes if node["depth"] == deepest]
    spacing = (GRAPH_WIDTH - 80) / max(len(entries) - 1, 1)
    points = {}
    for index, node in enumerate(entries):
        points[node["name"]] = (40 + index * spacing, ENTRY_Y)
    for node in nodes:
        if node["depth"] != deepest:
            points[node["name"]] = (CENTER_X, CHAIN_Y[node["depth"]])

    depth_of = {node["name"]: node["depth"] for node in nodes}
    parts = []
    for edge in graph["edges"]:
        x1, y1 = points[edge["source"]]
        x2, y2 = points[edge["target"]]
        middle = (y1 + y2) / 2
        path = f"M{x1:.1f} {y1 + 6}C{x1:.1f} {middle} {x2} {middle} {x2} {y2 - 6}"
        parts.append(
            f'<path class="g-edge g-item" data-depth="{depth_of[edge["source"]]}" '
            f'pathLength="1" d="{path}"/>'
        )
    for node in nodes:
        x, y = points[node["name"]]
        url = html.escape(_source_url(record, node["file"], node["line"]))
        location = _location(node["file"], node["line"])
        title = f"<title>{_esc(_symbol(node['name']))} at {_esc(location)}</title>"
        if node["depth"] == deepest:
            text = (
                f'<text x="{x:.1f}" y="{y - 18}" text-anchor="middle">'
                f"{_esc(_method(node['name']))}</text>"
            )
        else:
            text = (
                f'<text x="{x + 18}" y="{y + 4}">{_esc(_label(node["name"]))}'
                f'<tspan class="g-loc" dx="10">{_esc(location)}</tspan></text>'
            )
        kind = "g-root" if node["depth"] == 0 else "g-node"
        radius = 7 if node["depth"] == 0 else 5
        parts.append(
            f'<a href="{url}" class="{kind} g-item" data-depth="{node["depth"]}">'
            f'{title}<circle cx="{x:.1f}" cy="{y}" r="{radius}"/>{text}</a>'
        )
    label = (
        f"Recorded caller chain: {len(entries)} public entry points reach "
        f"{_label(graph['root'])}. Each node links to its pinned source."
    )
    body = "\n".join(f"{INDENT}      {part}" for part in parts)
    return (
        f'<svg viewBox="0 0 {GRAPH_WIDTH} {GRAPH_HEIGHT}" role="group" '
        f'aria-label="{html.escape(label)}">\n{body}\n{INDENT}    </svg>'
    )


def render_replay(record: dict | None = None) -> str:
    """Return the replay's HTML, without its markers."""

    record = record or json.loads(RECORD.read_text(encoding="utf-8"))
    graph = record["dependency_result"]
    chapters = "".join(
        f'<button type="button" data-seek="{index}" '
        f'aria-pressed="{"true" if index == 0 else "false"}">{name}</button>'
        for index, name in enumerate(CHAPTERS)
    )
    transcript = "\n".join(f"{INDENT}      {line}" for line in _transcript(record))
    trace = record["tool_calls"][1]
    return "\n".join(
        [
            f'{INDENT}<div class="replay" data-replay>',
            f'{INDENT}  <div class="replay-bar">',
            f"{INDENT}    <span>Requests · Claude Code + CodeNib</span>",
            f'{INDENT}    <div class="replay-controls">'
            f'<div class="replay-chapters" role="group" aria-label="Jump to a step">'
            f"{chapters}</div>"
            f'<button class="replay-toggle" type="button">Pause</button></div>',
            f"{INDENT}  </div>",
            f'{INDENT}  <div class="replay-body">',
            f'{INDENT}    <div class="replay-log" tabindex="0" role="region" '
            f'aria-label="Recorded command-line output">',
            transcript,
            f"{INDENT}    </div>",
            f'{INDENT}    <div class="replay-graph">',
            f'{INDENT}      <p class="replay-graph-title">'
            f"{_esc(trace['tool_name'].split('__')[-1])} "
            f'<span>who reaches {_esc(_label(graph["root"]))}</span></p>',
            f"{INDENT}    {_graph(record)}",
            f"{INDENT}    </div>",
            f"{INDENT}  </div>",
            f"{INDENT}</div>",
        ]
    )


def replace_replay(text: str, replay: str) -> str:
    """Replace the generated replay between the homepage's markers."""

    before, begin, tail = text.partition(BEGIN_MARKER)
    if not begin:
        raise ValueError(f"missing marker: {BEGIN_MARKER}")
    _, end, after = tail.partition(END_MARKER)
    if not end:
        raise ValueError(f"missing marker: {END_MARKER}")
    return f"{before}{begin}\n{replay}\n{INDENT}{end}{after}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", type=Path, help="Update the replay in a page")
    parser.add_argument("--check", type=Path, help="Fail if a page's replay is stale")
    args = parser.parse_args(argv)
    replay = render_replay()
    if args.write:
        text = args.write.read_text(encoding="utf-8")
        args.write.write_text(replace_replay(text, replay), encoding="utf-8")
    elif args.check:
        text = args.check.read_text(encoding="utf-8")
        if text != replace_replay(text, replay):
            print(f"{args.check} agent replay is out of date", file=sys.stderr)
            return 1
    else:
        print(replay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
