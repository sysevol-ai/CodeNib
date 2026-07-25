# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
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

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Sequence

from .boundary import is_line_bearing, to_agent_repr

_BACKTICK = re.compile(r"`([^`\n]{2,100})`")
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}(?:\.[A-Za-z_][A-Za-z0-9_]{2,})*")
_CODEISH = re.compile(r"_|[a-z][A-Z]|[0-9][A-Z]|[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_]")
_IDENT_STOP = {"BUG", "TODO", "FIXME", "NOTE", "XXX", "HACK", "WARNING", "ERROR"}
_SEED_POLICIES = frozenset({"all", "specific"})
_CAMEL_SEED = re.compile(r"[a-z][A-Z]")
_SIMPLE_CALL = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:[.:#][A-Za-z_][A-Za-z0-9_]*)*\(\)$"
)
_DOMAIN_HINTS = {
    "com",
    "dev",
    "docs",
    "github",
    "html",
    "http",
    "https",
    "io",
    "net",
    "org",
    "readthedocs",
    "sh",
    "www",
}


@dataclass(frozen=True)
class LSPRouteContext:
    """Rendered startup context produced by the static graph route."""

    seeds: tuple[str, ...]
    nodes: tuple[Any, ...]
    text: str = ""
    arguments: Mapping[str, Any] | None = None
    route_fingerprint: str | None = None


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


def normalize_lsp_route_seed_policy(seed_policy: str | None = "all") -> str:
    """Normalize and validate the startup route seed policy."""

    policy = (seed_policy or "all").strip().lower()
    if policy not in _SEED_POLICIES:
        raise ValueError(
            f"Unsupported lsp_route seed_policy {seed_policy!r}; "
            f"supported values are {sorted(_SEED_POLICIES)}"
        )
    return policy


def is_specific_lsp_symbol_seed(seed: str) -> bool:
    """Return whether *seed* is specific enough for gated startup routing."""

    text = (seed or "").strip().strip("`'\"")
    if not text:
        return False
    if not _is_symbol_seed_shape(text):
        return False
    check = text[:-2] if text.endswith("()") else text
    if "/" in check or "::" in check or "#" in check:
        return True
    if "." in check:
        if _looks_like_domain_fragment(check):
            return False
        return any(_is_specific_name_part(part) for part in check.split("."))
    if "_" in check:
        return True
    if _CAMEL_SEED.search(text):
        return True
    if text.isupper() and len(text) >= 3:
        return True
    if (
        any(ch.isalpha() for ch in text)
        and any(ch.isdigit() for ch in text)
        and not text.islower()
    ):
        return True
    return False


def filter_lsp_symbol_seeds(
    seeds: Iterable[str],
    *,
    seed_policy: str | None = "all",
) -> List[str]:
    """Apply a route startup seed policy while preserving seed order."""

    policy = normalize_lsp_route_seed_policy(seed_policy)
    out = list(seeds)
    if policy == "all":
        return out
    return [seed for seed in out if is_specific_lsp_symbol_seed(seed)]


def build_lsp_route_context(
    executor: Any,
    query: str,
    *,
    explicit_seeds: Any = None,
    seed_limit: int = 8,
    seed_policy: str | None = "all",
    query_fallback: bool = False,
    top_k: int = 12,
    include_neighbors: bool = True,
) -> LSPRouteContext:
    """Run an ``lsp_route`` executor and render startup context."""

    seeds = extract_lsp_symbol_seeds(
        query,
        explicit=explicit_seeds,
        limit=seed_limit,
    )
    seeds = filter_lsp_symbol_seeds(seeds, seed_policy=seed_policy)
    if not seeds and not query_fallback:
        return LSPRouteContext(seeds=(), nodes=(), text="")

    route_args = canonical_lsp_route_args(
        symbols=seeds,
        query=query,
        top_k=top_k,
        include_neighbors=include_neighbors,
    )
    nodes = tuple(
        executor(
            **route_args,
        )
        or ()
    )
    return LSPRouteContext(
        seeds=tuple(seeds),
        nodes=nodes,
        text=render_lsp_route_context(seeds, nodes),
        arguments=route_args,
        route_fingerprint=fingerprint_lsp_route_nodes(nodes),
    )


def canonical_lsp_route_args(
    *,
    symbols: Iterable[Any],
    query: Any = None,
    top_k: Any = 12,
    include_neighbors: Any = True,
) -> dict[str, Any]:
    """Return the canonical argument shape for a static LSP route call."""

    return {
        "symbols": [str(symbol) for symbol in symbols or []],
        "query": str(query) if query is not None else None,
        "top_k": int(top_k or 12),
        "include_neighbors": bool(include_neighbors),
    }


def fingerprint_lsp_route_nodes(nodes: Sequence[Any]) -> str:
    """Return a stable fingerprint for an ordered route-node result."""

    payload = summarize_lsp_route_nodes(nodes, max_nodes=len(nodes))
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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

    seed_line = "Seeds: " + ", ".join(seeds) if seeds else "Seed source: query text"
    lines = [
        "# Static LSP route hints (unverified)",
        "These graph anchors come from explicit symbol-like names in the task. "
        "If no symbol seed was available, query text was used to find candidate "
        "graph anchors. Use them to choose the first one or two files to inspect; "
        "they are not a checklist. Do not cite them until you read the file.",
        seed_line,
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


def summarize_lsp_route_nodes(
    nodes: Sequence[Any],
    *,
    max_nodes: int = 5,
) -> List[dict[str, str]]:
    """Return a small trace-safe preview of route nodes."""

    preview: List[dict[str, str]] = []
    for node in list(nodes)[: max(0, int(max_nodes or 0))]:
        data = _node_dict(node)
        item = {
            "location": _location(data),
            "symbol": str(data.get("node_name") or data.get("node_id") or ""),
            "type": str(data.get("type") or ""),
        }
        relation = str(data.get("content") or "").strip()
        if relation:
            item["relation"] = relation
        preview.append(item)
    return preview


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


def _is_symbol_seed_shape(text: str) -> bool:
    if any(ch.isspace() for ch in text):
        return False
    if any(ch in text for ch in "\"'`[]{}"):
        return False
    if "(" in text or ")" in text:
        return bool(_SIMPLE_CALL.fullmatch(text))
    if "://" in text or "@" in text:
        return False
    return True


def _looks_like_domain_fragment(text: str) -> bool:
    parts = [part for part in re.split(r"[./]", text.lower()) if part]
    return any(part in _DOMAIN_HINTS for part in parts)


def _is_specific_name_part(text: str) -> bool:
    if not text:
        return False
    if "_" in text:
        return True
    if _CAMEL_SEED.search(text):
        return True
    if text.isupper() and len(text) >= 3:
        return True
    return (
        any(ch.isalpha() for ch in text)
        and any(ch.isdigit() for ch in text)
        and not text.islower()
    )


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
    "canonical_lsp_route_args",
    "extract_lsp_symbol_seeds",
    "filter_lsp_symbol_seeds",
    "fingerprint_lsp_route_nodes",
    "is_specific_lsp_symbol_seed",
    "normalize_lsp_route_seed_policy",
    "render_lsp_route_context",
    "summarize_lsp_route_nodes",
]
