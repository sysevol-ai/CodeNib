# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Initial static-graph route context for agent runs.

This module keeps LSP-shaped routing generic and runtime-facing: it extracts
symbol-like seeds already present in the task, runs a caller-provided
``lsp_route`` executor, and renders compact, unverified hints for the opening
agent prompt. It does not read ground truth, score answers, or know about a
dataset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Sequence

from .boundary import is_line_bearing, to_agent_repr

_BACKTICK = re.compile(r"`([^`\n]{2,100})`")
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}(?:\.[A-Za-z_][A-Za-z0-9_]{2,})*")
_CODEISH = re.compile(r"_|[a-z][A-Z]|[0-9][A-Z]|[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_]")
_IDENT_STOP = {"BUG", "TODO", "FIXME", "NOTE", "XXX", "HACK", "WARNING", "ERROR"}


@dataclass(frozen=True)
class LSPRouteContext:
    """Rendered startup context produced by the static graph route."""

    seeds: tuple[str, ...]
    nodes: tuple[Any, ...]
    text: str = ""


def extract_lsp_symbol_seeds(
    *texts: Any,
    explicit: Any = None,
    limit: int = 8,
) -> List[str]:
    """Extract explicit symbol-like seeds from task text.

    The extractor is intentionally conservative. It keeps user-supplied explicit
    seeds first, then adds backtick-delimited code names and code-like tokens
    from the task text. It does not inspect repository contents or benchmark
    labels.
    """

    seeds: List[str] = []
    _add_seed_values(seeds, explicit)

    text = "\n".join(_flatten_text_values(texts))
    for match in _BACKTICK.findall(text):
        _add_seed_values(seeds, match)
    for token in _TOKEN.findall(text):
        if token.upper() in _IDENT_STOP:
            continue
        if _CODEISH.search(token) or (token.isupper() and len(token) >= 3):
            _add_seed_values(seeds, token)

    seen: set[str] = set()
    out: List[str] = []
    max_items = max(1, int(limit or 8))
    for seed in seeds:
        key = seed.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(seed)
        if len(out) >= max_items:
            break
    return out


def build_lsp_route_context(
    executor: Any,
    query: str,
    *,
    explicit_seeds: Any = None,
    seed_limit: int = 8,
    top_k: int = 12,
    include_neighbors: bool = True,
) -> LSPRouteContext:
    """Run an ``lsp_route`` executor and render startup context."""

    seeds = extract_lsp_symbol_seeds(
        query,
        explicit=explicit_seeds,
        limit=seed_limit,
    )
    if not seeds:
        return LSPRouteContext(seeds=(), nodes=(), text="")

    nodes = tuple(
        executor(
            symbols=list(seeds),
            query=query,
            top_k=int(top_k or 12),
            include_neighbors=bool(include_neighbors),
        )
        or ()
    )
    return LSPRouteContext(
        seeds=tuple(seeds),
        nodes=nodes,
        text=render_lsp_route_context(seeds, nodes),
    )


def render_lsp_route_context(
    seeds: Sequence[str],
    nodes: Sequence[Any],
    *,
    max_nodes: int | None = None,
) -> str:
    """Render route nodes as compact, unverified prompt context."""

    selected = list(nodes)
    if max_nodes is not None:
        selected = selected[: max(0, int(max_nodes))]
    if not selected:
        return ""

    lines = [
        "# Static LSP route hints (unverified)",
        "These graph anchors come from explicit symbol-like names in the task. "
        "Use them to choose what to read; do not cite them until you read the "
        "file.",
        "Seeds: " + ", ".join(seeds),
        "",
    ]
    for index, node in enumerate(selected, 1):
        data = _node_dict(node)
        location = _location(data)
        symbol = str(data.get("node_name") or data.get("node_id") or "(unknown)")
        node_type = str(data.get("type") or "symbol")
        relation = str(data.get("content") or "").strip()
        suffix = f" - {relation}" if relation else ""
        lines.append(f"{index}. {location} {symbol} [{node_type}]{suffix}")
    return "\n".join(lines)


def _flatten_text_values(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if text:
                out.append(text)
        elif isinstance(value, Mapping):
            for item in value.values():
                out.extend(_flatten_text_values([item]))
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            out.extend(_flatten_text_values(value))
        else:
            text = str(value).strip()
            if text:
                out.append(text)
    return out


def _add_seed_values(seeds: List[str], value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str):
        text = value.strip().strip("`'\"")
        if text:
            seeds.append(text)
        return
    if isinstance(value, Mapping):
        for item in value.values():
            _add_seed_values(seeds, item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            _add_seed_values(seeds, item)


def _node_dict(node: Any) -> dict[str, Any]:
    if is_line_bearing(node):
        return to_agent_repr(node)
    if isinstance(node, Mapping):
        return dict(node)
    if hasattr(node, "model_dump"):
        return node.model_dump(exclude_none=True)
    if hasattr(node, "__dict__"):
        return dict(node.__dict__)
    return {"node_name": str(node)}


def _location(data: Mapping[str, Any]) -> str:
    file_path = str(data.get("file") or data.get("file_path") or "(unknown)")
    start = data.get("start_line")
    end = data.get("end_line")
    if start is None:
        return file_path
    if end is None:
        return f"{file_path}:{start}"
    return f"{file_path}:{start}-{end}"


__all__ = [
    "LSPRouteContext",
    "build_lsp_route_context",
    "extract_lsp_symbol_seeds",
    "render_lsp_route_context",
]
