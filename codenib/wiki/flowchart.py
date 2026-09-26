# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Serve-time cleanup for the section flow diagram a Wiki page carries.

``AgentWiki`` draws a page's planned call path as a Mermaid flowchart and also
lists each relation under its section as an ``**Interactions**`` row.  When a
row repeats an arrow the flow already draws, the reader sees the same call
twice.  The quality gates count those rows at generation time, so the rows are
dropped only when a page is served, never from the cached page itself.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Mapping, Set, Tuple

_MERMAID_OPEN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*mermaid\b")
_NODE = re.compile(r'^\s*([A-Za-z_][\w-]*)\s*\[\s*"([^"]*)"\s*\]\s*$')
_EDGE = re.compile(
    r"^\s*([A-Za-z_][\w-]*)\s*-->\s*(?:\|[^|]*\|\s*)?([A-Za-z_][\w-]*)\s*$"
)
_ROW = re.compile(r"^\s*[-*]\s+`([^`]+)`\s*→\s*`([^`]+)`")
_HEADER = re.compile(r"^\s*\*\*Interactions\*\*\s*$")


def _name(label: str) -> str:
    """Normalise a symbol label so ``Foo.bar()`` and ``Foo.bar`` compare equal."""

    text = label.strip().strip("`").strip()
    return re.sub(r"\(\)$", "", text)


def flow_edges(markdown: str) -> Set[Tuple[str, str]]:
    """Return the ``(source, target)`` symbol pairs every Mermaid flow draws."""

    edges: Set[Tuple[str, str]] = set()
    lines = (markdown or "").split("\n")
    index = 0
    while index < len(lines):
        match = _MERMAID_OPEN.match(lines[index])
        if not match:
            index += 1
            continue
        marker = match.group(1)
        labels = {}
        pairs: List[Tuple[str, str]] = []
        index += 1
        while index < len(lines) and not lines[index].strip().startswith(marker):
            node = _NODE.match(lines[index])
            if node:
                labels[node.group(1)] = node.group(2)
            edge = _EDGE.match(lines[index])
            if edge:
                pairs.append((edge.group(1), edge.group(2)))
            index += 1
        for source, target in pairs:
            if source in labels and target in labels:
                edges.add((_name(labels[source]), _name(labels[target])))
        index += 1
    return edges


def drop_flow_duplicate_interactions(markdown: str) -> str:
    """Remove ``**Interactions**`` rows whose arrow the page's flow already draws.

    A header left with no rows is removed with its trailing blank line, so the
    section does not end on an empty label.
    """

    drawn = flow_edges(markdown)
    if not drawn:
        return markdown
    lines = markdown.split("\n")
    kept: List[str] = []
    index = 0
    while index < len(lines):
        if not _HEADER.match(lines[index]):
            kept.append(lines[index])
            index += 1
            continue
        header = lines[index]
        end = index + 1
        rows: List[str] = []
        while end < len(lines) and _ROW.match(lines[end]):
            rows.append(lines[end])
            end += 1
        remaining = [
            row
            for row in rows
            if (_name(_ROW.match(row).group(1)), _name(_ROW.match(row).group(2)))
            not in drawn
        ]
        if remaining:
            kept.append(header)
            kept.extend(remaining)
        elif (
            kept
            and not kept[-1].strip()
            and end < len(lines)
            and not lines[end].strip()
        ):
            end += 1
        index = end
    return "\n".join(kept)


_RECORDED_CLAIM = "Each arrow is a call site recorded in the index."


def _symbol(qualified: str) -> str:
    """Drop the ``file:`` prefix a relation endpoint carries."""

    text = str(qualified or "")
    head, sep, tail = text.partition(":")
    # ``src/a.rs:Type::method()``: the prefix is a path, the rest the symbol.
    if sep and ("/" in head or "." in head) and not tail.startswith(":"):
        text = tail
    return _name(text)


def recorded_edges(relations: Iterable[Mapping]) -> Set[Tuple[str, str]]:
    """Return the ``(source, target)`` pairs the index recorded with a call site."""

    pairs: Set[Tuple[str, str]] = set()
    for relation in relations or []:
        if not relation.get("anchors"):
            continue
        pairs.add((_symbol(relation.get("source")), _symbol(relation.get("target"))))
    return pairs


def qualify_flow_caption(markdown: str, relations: Iterable[Mapping]) -> str:
    """Keep the flow caption's "recorded call site" claim true.

    The generator writes that every arrow is a recorded call site, but a
    planned step can name a call the index never recorded.  When some arrows
    lack a record the caption says which ones are recorded; when none has
    one, it says the arrows are the page's description.
    """

    if _RECORDED_CLAIM not in (markdown or ""):
        return markdown
    drawn = flow_edges(markdown)
    if not drawn:
        return markdown
    recorded = drawn & recorded_edges(relations)
    if recorded == drawn:
        return markdown
    if recorded:
        claim = "Arrows with a line number are call sites recorded in the index."
    else:
        claim = "The index records none of these calls; the arrows follow the page's reading."
    return markdown.replace(_RECORDED_CLAIM, claim)


__all__ = [
    "drop_flow_duplicate_interactions",
    "flow_edges",
    "qualify_flow_caption",
    "recorded_edges",
]
