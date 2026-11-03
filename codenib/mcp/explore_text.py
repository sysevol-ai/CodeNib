# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Readable text for an ``explore_context`` result.

MCP clients such as Claude Code show the model a tool result's text block,
not its ``structuredContent``. The JSON copy spent most of that text on
plan internals, fingerprints and repeated provenance, and gave source as
escaped strings without line numbers. This rendering keeps what an agent
acts on: line-numbered source it can cite or edit from, the static
references around the main symbols, and one-line notes. The structured
result is unchanged for programmatic clients.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_OMITTED_LINES = re.compile(r"\.\.\. (\d+) source lines omitted \.\.\.")
# Graph names disambiguate same-named definitions with their position.
_POSITION_SUFFIX = re.compile(r"\(_codenib_l\d+_c\d+\)")
_TRUNCATED = "... source excerpt truncated ..."
_MAX_NEIGHBORS = 12
_MAX_ANCHOR_LABELS = 3
# Codes whose detail refers to JSON fields; the text gives the meaning instead.
_NOTE_TEXT = {
    "response_budget_applied": (
        "Some lower-ranked source or relationships were shortened to fit this "
        "response; ask a narrower question to see more."
    ),
    "response_budget_exhausted": (
        "Nothing beyond this summary fit the response; ask a narrower question."
    ),
}
# Implied by a graph_preparing or graph_unavailable note.
_GRAPH_PENDING_CODES = frozenset({"route_unavailable", "dependency_unavailable"})


def _symbol_label(name: Any) -> str:
    """Drop the ``path.py:`` prefix from an indexed symbol name."""

    text = _POSITION_SUFFIX.sub("", str(name or ""))
    prefix, separator, rest = text.partition(":")
    if (
        separator
        and rest
        and not rest.startswith(":")
        and ("/" in prefix or "." in prefix)
    ):
        return rest
    return text


def _symbol_file(name: Any) -> str | None:
    prefix, separator, rest = str(name or "").partition(":")
    if separator and rest and ("/" in prefix or "." in prefix):
        return prefix
    return None


def _numbered(content: str, start_line: int) -> list[str]:
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    rendered: list[str] = []
    number = start_line
    for line in lines:
        omitted = _OMITTED_LINES.fullmatch(line.strip())
        if omitted:
            count = int(omitted.group(1))
            rendered.append(f"      ... {count} lines omitted ...")
            number += count
            continue
        if line.strip() == _TRUNCATED:
            rendered.append("      ... excerpt truncated ...")
            continue
        rendered.append(f"{number:>6}\t{line}")
        number += 1
    return rendered


def _context_lines(file_path: str, context: Mapping[str, Any]) -> list[str]:
    start = context.get("start_line")
    end = context.get("end_line")
    span = f"lines {start}-{end}" if start and end and start != end else f"line {start}"
    anchors = context.get("anchors") or []
    labels: list[str] = []
    for anchor in anchors:
        if isinstance(anchor, Mapping) and anchor.get("symbol"):
            label = _symbol_label(anchor["symbol"])
            if label and label not in labels:
                labels.append(label)
    header = f"{file_path}:{start}" if start else file_path
    title = ", ".join(labels[:_MAX_ANCHOR_LABELS])
    if len(labels) > _MAX_ANCHOR_LABELS:
        title += f" +{len(labels) - _MAX_ANCHOR_LABELS} more"
    heading = f"--- {header} ({span})" + (f" {title}" if title else "")

    content = context.get("content")
    dedup = context.get("session_dedup")
    if context.get("content_kind") == "session_pointer" or (
        isinstance(dedup, Mapping) and dedup.get("content_retained") is False
    ):
        source_call = dedup.get("source_call") if isinstance(dedup, Mapping) else None
        where = f"call {source_call}" if source_call else "an earlier call"
        return [heading, f"      (same source as returned in {where}; not repeated)"]
    if not isinstance(content, str) or not content:
        return [heading, "      (source omitted to fit the response)"]
    lines = [heading]
    if context.get("verified") is not True:
        lines.append("      (indexed copy; not verified against the current checkout)")
    lines.extend(_numbered(content, int(start) if start else 1))
    return lines


def _node_label(node: Mapping[str, Any]) -> str:
    label = _symbol_label(node.get("name"))
    file_path, line = node.get("file"), node.get("line")
    if file_path and line:
        return f"{label} ({file_path}:{line})"
    if file_path:
        return f"{label} ({file_path})"
    return label


def _neighbor_line(prefix: str, labels: Sequence[str]) -> str:
    shown = ", ".join(labels[:_MAX_NEIGHBORS])
    if len(labels) > _MAX_NEIGHBORS:
        shown += f", +{len(labels) - _MAX_NEIGHBORS} more"
    return f"  {prefix}: {shown}"


def _relationship_lines(relationship: Mapping[str, Any]) -> list[str]:
    root = relationship.get("root") or relationship.get("seed")
    nodes = {
        node.get("name"): node
        for node in relationship.get("nodes") or []
        if isinstance(node, Mapping) and node.get("kind") != "file"
    }
    root_node = nodes.get(root)
    if root_node:
        heading = _node_label(root_node)
    else:
        root_file = _symbol_file(root)
        heading = _symbol_label(root) + (f" ({root_file})" if root_file else "")
    lines = [heading]
    outgoing: list[str] = []
    incoming: list[str] = []
    deeper = 0
    for edge in relationship.get("edges") or []:
        if not isinstance(edge, Mapping):
            continue
        source, target = edge.get("source"), edge.get("target")
        if source == root and target in nodes:
            label = _node_label(nodes[target])
            if label not in outgoing:
                outgoing.append(label)
        elif target == root and source in nodes:
            label = _node_label(nodes[source])
            if label not in incoming:
                incoming.append(label)
    for node in nodes.values():
        depth = node.get("depth")
        if isinstance(depth, int) and depth > 1:
            deeper += 1
    if outgoing:
        lines.append(_neighbor_line("references", outgoing))
    if incoming:
        lines.append(_neighbor_line("referenced by", incoming))
    if deeper:
        lines.append(f"  {deeper} more symbols two or more steps away")
    projection = relationship.get("response_projection")
    if isinstance(projection, Mapping):
        lines.append(
            "  (neighbors omitted to fit the response: "
            f"{projection.get('original_nodes', 0)} symbols, "
            f"{projection.get('original_edges', 0)} references)"
        )
    elif not outgoing and not incoming and not deeper:
        note = relationship.get("note")
        lines.append(f"  {note}" if note else "  no indexed references found")
    return lines


def _chain_lines(item: Mapping[str, Any]) -> list[str]:
    root = _symbol_label(item.get("root") or item.get("seed"))
    chains = [chain for chain in item.get("chains") or [] if isinstance(chain, Mapping)]
    if not chains:
        note = item.get("note")
        return [f"{root}: {note}" if note else f"{root}: no indexed referrers found"]
    via = item.get("via")
    if not via:
        heading = f"How {root} is reached:"
    elif item.get("via_reason") == "callee":
        heading = (
            f"Nothing indexed references {root}; it calls {_symbol_label(via)}, "
            "which is reached like this:"
        )
    else:
        heading = (
            f"How {root} is reached (through the same-named {_symbol_label(via)}, "
            "which dynamic dispatch may route to it):"
        )
    lines = [heading]
    for number, chain in enumerate(chains, 1):
        steps = [step for step in chain.get("steps") or [] if isinstance(step, Mapping)]
        for index, step in enumerate(steps):
            label = _node_label(step)
            if not index:
                lines.append(f"  {number}. {label}")
                continue
            site = step.get("referenced_at")
            where = ""
            if isinstance(site, Mapping) and site.get("file"):
                line = site.get("line")
                where = f", referenced at {site['file']}" + (f":{line}" if line else "")
            lines.append(f"     -> {label}{where}")
        if chain.get("complete") is False:
            lines.append(
                "     (no entry reached within the search depth; the earliest "
                "referrer found is shown first)"
            )
    if item.get("truncated"):
        lines.append("  (search bounds reached; other chains may exist)")
    return lines


def render_explore_text(response: Mapping[str, Any]) -> str:
    """Return the agent-facing text for one bounded explore response."""

    summary = (
        response.get("summary") if isinstance(response.get("summary"), Mapping) else {}
    )
    source = (
        response.get("source") if isinstance(response.get("source"), Mapping) else {}
    )
    files = [
        group for group in response.get("files") or [] if isinstance(group, Mapping)
    ]
    relationships = [
        item
        for item in response.get("relationships") or []
        if isinstance(item, Mapping)
    ]
    diagnostics = [
        item for item in response.get("diagnostics") or [] if isinstance(item, Mapping)
    ]
    call_chains = [
        item for item in response.get("call_chains") or [] if isinstance(item, Mapping)
    ]
    chain_count = sum(len(item.get("chains") or []) for item in call_chains)

    windows = sum(len(group.get("contexts") or []) for group in files)
    lines = [
        f"{len(files)} files, {windows} source windows, "
        f"{len(relationships)} relationship groups"
        + (f", {chain_count} call chains." if chain_count else ".")
    ]
    commit = str(source.get("commit") or "")[:12]
    if source.get("verified") is True:
        lines.append(
            "Source is read from the current checkout"
            + (f" (HEAD {commit})" if commit else "")
            + "; line numbers are 1-based and safe to cite."
        )
    else:
        lines.append(
            "Source could not be verified against the current checkout; treat "
            "excerpts as indexed copies and read the file before relying on them."
        )

    codes = {str(item.get("code")) for item in diagnostics}
    graph_note = bool(codes & {"graph_preparing", "graph_unavailable"})
    for item in diagnostics:
        code = str(item.get("code") or "")
        if graph_note and code in _GRAPH_PENDING_CODES:
            continue
        detail = _NOTE_TEXT.get(code, str(item.get("detail") or "").strip())
        lines.append(f"Note ({code}): {detail}" if detail else f"Note ({code})")

    if call_chains:
        lines.append("")
        lines.append(
            "Call chains (entry first; an entry is code nothing indexed "
            "references, such as a command, route or framework hook; each arrow "
            "is a static reference, so confirm it in the source):"
        )
        for item in call_chains:
            lines.extend(_chain_lines(item))

    for group in files:
        file_path = str(group.get("file") or "")
        for context in group.get("contexts") or []:
            if isinstance(context, Mapping):
                lines.append("")
                lines.extend(_context_lines(file_path, context))

    if relationships:
        lines.append("")
        lines.append(
            "Relationships (static references: calls, field and type uses, "
            "function values; confirm a runtime call in the source):"
        )
        for relationship in relationships:
            lines.extend(_relationship_lines(relationship))

    session_call = summary.get("session_call")
    if isinstance(session_call, int):
        lines.append("")
        lines.append(f"(explore_context call {session_call} in this session)")
    return "\n".join(lines) + "\n"


__all__ = ["render_explore_text"]
