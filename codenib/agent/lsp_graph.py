# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Graph-backed LSP-shaped navigation helpers.

These helpers expose static CodeGraph data through familiar LSP concepts:
definition, references, and a compact route map over related symbols. They are
pure graph utilities used by both agent skills and MCP tools; they do not know
about SWE-bench scoring, answer formats, or experiment feedback gates.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from ..types import (
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    QueriedNode,
    is_symbol_node,
    node_has_definition,
    node_is_reference_only,
)

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+")
_STOPWORDS = {
    "and",
    "arg",
    "args",
    "bool",
    "case",
    "class",
    "ctx",
    "data",
    "dict",
    "else",
    "err",
    "error",
    "false",
    "file",
    "func",
    "int",
    "kwargs",
    "list",
    "none",
    "null",
    "object",
    "return",
    "self",
    "str",
    "string",
    "true",
    "type",
}
_QUERY_SYNONYMS = {
    "cached": {"cache"},
    "caching": {"cache"},
    "directory": {"dir", "location", "path"},
    "directories": {"dir", "location", "path"},
    "fetch": {"download", "retrieve"},
    "fetches": {"download", "retrieve"},
    "fetching": {"download", "retrieve"},
    "location": {"path"},
    "locations": {"path"},
    "placeholder": {"replace", "replacer", "replacement", "template"},
    "placeholders": {"replace", "replacer", "replacement", "template"},
    "property": {"provider", "value"},
    "properties": {"provider", "value"},
    "retrieval": {"retrieve", "download", "cache"},
    "retrieve": {"download", "cache"},
    "retrieves": {"download", "cache"},
    "retrieving": {"download", "cache"},
    "resolution": {"resolve", "resolver", "replace", "replacer"},
    "resolved": {"resolve", "resolver", "replace", "replacer"},
    "substitution": {"replace", "replacer", "replacement"},
    "substitute": {"replace", "replacer", "replacement"},
    "token": {"replace", "replacer", "replacement", "template"},
    "tokens": {"replace", "replacer", "replacement", "template"},
    "variable": {"replace", "replacer", "replacement"},
    "variables": {"replace", "replacer", "replacement"},
}
_BRIDGE_TERMS = {
    "adapter",
    "build",
    "builder",
    "download",
    "factory",
    "fetch",
    "make",
    "new",
    "open",
    "replace",
    "replacement",
    "resolver",
    "retrieve",
    "template",
}
_PROVIDER_TERMS = {
    "cache",
    "config",
    "default",
    "directory",
    "env",
    "location",
    "path",
    "provider",
    "property",
    "setting",
    "url",
    "value",
}
_ENDPOINT_TERMS = {
    "apply",
    "check",
    "detect",
    "handle",
    "handler",
    "main",
    "process",
    "run",
    "validate",
}
_QUERY_SEED_MIN_OVERLAP = 2
_ROUTE_SEED_NODE_TYPES = frozenset(
    {NODE_TYPE_CLASS, NODE_TYPE_FIELD, NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
)


def display_name(graph: Any, name: str) -> str:
    """Return a readable graph-node label, preferring ``unified_name``."""
    info = graph.get_node_info_by_name(name) or {}
    return info.get("unified_name") or name


def _bare(symbol: str) -> str:
    return symbol.split(":")[-1].split(".")[-1].split("#")[-1].rstrip("()").strip()


def _node_id(file_path: Optional[str], display: str) -> str:
    if not file_path:
        return display
    if "/" in display or ":" in display:
        return display
    return f"{file_path}:{display}"


def _terms(text: Any) -> set[str]:
    out: set[str] = set()
    for word in _WORD_RE.findall(str(text or "")):
        for part in re.split(r"[_\W]+", word):
            if not part:
                continue
            for piece in _CAMEL_RE.findall(part) or [part]:
                lowered = piece.lower()
                if len(lowered) >= 3 and lowered not in _STOPWORDS:
                    out.add(lowered)
                    for suffix in ("ing", "ers", "er", "ments", "ment", "es", "s"):
                        if len(lowered) > len(suffix) + 2 and lowered.endswith(suffix):
                            out.add(lowered[: -len(suffix)])
    return out


def _query_terms(query: Optional[str]) -> set[str]:
    terms = _terms(query or "")
    expanded = set(terms)
    for term in terms:
        expanded.update(_QUERY_SYNONYMS.get(term, set()))
    return expanded


def _unified_index(graph: Any) -> dict[str, list[str]]:
    cache = getattr(graph, "_lsp_graph_unified_index", None)
    if cache is not None:
        return cache

    index: dict[str, list[str]] = {}

    def add(key: str, name: str) -> None:
        if key:
            index.setdefault(key, []).append(name)

    prebuilt = getattr(graph, "_unified_to_names", None) or {}
    if prebuilt:
        for display, names in prebuilt.items():
            for name in names:
                add(str(display), str(name))
                add(_bare(str(display)), str(name))
    else:
        for name in getattr(graph, "name_to_vertex", {}) or {}:
            info = graph.get_node_info_by_name(name) or {}
            display = info.get("unified_name")
            if display:
                add(str(display), str(name))
                add(_bare(str(display)), str(name))

    try:
        graph._lsp_graph_unified_index = index
    except Exception:  # noqa: BLE001 - graph may disallow dynamic attributes
        pass
    return index


def resolve_symbol_candidates(graph: Any, symbol: str, limit: int = 8) -> list[str]:
    """Return canonical node names that match a user-facing symbol seed."""
    names = getattr(graph, "name_to_vertex", {}) or {}
    seed = (symbol or "").strip().strip("`'\"")
    if not seed:
        return []
    if seed in names:
        return [seed]

    if hasattr(graph, "resolve_symbol"):
        canonical, candidates = graph.resolve_symbol(seed)
        if canonical:
            return [canonical]
        if candidates:
            out: list[str] = []
            for candidate in candidates:
                text = str(candidate)
                if text in names:
                    out.append(text)
                    continue
                out.extend(_names_from_unified(graph, text))
            if out:
                return _dedupe_names(out, limit)

    for key in (seed, seed + "()", _bare(seed)):
        matches = _unified_index(graph).get(key)
        if matches:
            return _dedupe_names(matches, limit)

    bare = _bare(seed)
    suffix = [
        name for name in names if name.endswith("." + seed) or _bare(name) == bare
    ]
    if suffix:
        return suffix[:limit]
    substring = [name for name in names if bare and bare.lower() in name.lower()]
    return substring[:limit]


def _names_from_unified(graph: Any, display: str) -> list[str]:
    out: list[str] = []
    index = _unified_index(graph)
    for key in (display, display + "()", _bare(display)):
        out.extend(index.get(key) or [])
    return out


def _dedupe_names(names: Iterable[str], limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= limit:
            break
    return out


def _resolve_one(graph: Any, symbol: str) -> tuple[Optional[str], list[str]]:
    candidates = resolve_symbol_candidates(graph, symbol)
    if len(candidates) == 1:
        return candidates[0], [display_name(graph, candidates[0])]
    return None, [display_name(graph, candidate) for candidate in candidates]


def _compact_node(
    graph: Any,
    name: str,
    relation: str,
    *,
    score: float = 1.0,
    use_selection_line: bool = False,
):
    info = graph.get_node_info_by_name(name) or {}
    file_path = info.get("file")
    display = info.get("unified_name") or name
    selection_line = info.get("selection_line")
    start_line = (
        selection_line
        if use_selection_line and selection_line is not None
        else info.get("start_line")
    )
    end_line = start_line if use_selection_line else info.get("end_line")
    if is_symbol_node(info.get("type")) and node_is_reference_only(info):
        file_path = None
        start_line = None
        end_line = None
    return QueriedNode(
        node_name=display,
        type=info.get("type", ""),
        file=file_path,
        node_id=_node_id(file_path, display),
        start_line=start_line,
        end_line=end_line,
        score=score,
        content=relation,
    )


def _compact_reference(
    graph: Any,
    *,
    target_name: str,
    source_vid: Optional[int],
    file_path: Optional[str],
    line: Optional[int],
):
    target_label = display_name(graph, target_name)
    source_label = ""
    if source_vid is not None:
        source = graph.get_node_info_by_id(source_vid) or {}
        source_label = source.get("unified_name") or source.get("name") or ""
    label = source_label or target_label
    display_line = int(line) + 1 if line is not None else None
    node_id = (
        f"{file_path}:{display_line}:ref:{target_label}"
        if file_path and display_line is not None
        else label
    )
    return QueriedNode(
        node_name=label,
        type="reference",
        file=file_path,
        start_line=line,
        end_line=line,
        node_id=node_id,
        score=1.0,
        content=f"reference to {target_label}",
    )


def _dedupe_nodes(nodes: Iterable[QueriedNode], limit: int) -> list[QueriedNode]:
    seen: set[tuple[Any, ...]] = set()
    out: list[QueriedNode] = []
    for node in nodes:
        key = (node.file, node.start_line, node.end_line, node.node_name, node.content)
        if key in seen:
            continue
        seen.add(key)
        out.append(node)
        if len(out) >= limit:
            break
    return out


def _ensure_range_index(graph: Any) -> None:
    if not hasattr(graph, "build_range_indexes"):
        return
    if getattr(graph, "_file_nodes", None) and getattr(
        graph, "_file_edge_anchors", None
    ):
        return
    graph.build_range_indexes()


def _node_name_for_vid(graph: Any, vid: int) -> Optional[str]:
    info = graph.get_node_info_by_id(vid) or {}
    return info.get("name")


def _definitions_for_symbol(graph: Any, symbol: str, limit: int) -> list[QueriedNode]:
    name, candidates = _resolve_one(graph, symbol)
    if name is None:
        if candidates:
            raise ValueError(
                f"symbol {symbol!r} is ambiguous; candidates: {candidates}. "
                "Call again with one exact name."
            )
        raise ValueError(
            f"symbol {symbol!r} not found in the code graph. Use a name from "
            "a search result or read output."
        )
    if not node_has_definition(graph.get_node_info_by_name(name) or {}):
        raise ValueError(f"symbol {symbol!r} has no indexed definition")
    label = display_name(graph, name)
    return [
        _compact_node(
            graph,
            name,
            f"definition of {label}",
            use_selection_line=True,
        )
    ][:limit]


def lsp_definition(
    graph: Any,
    *,
    file_path: Optional[str] = None,
    line: Optional[int] = None,
    character: Optional[int] = None,
    symbol: Optional[str] = None,
    top_k: int = 8,
) -> list[QueriedNode]:
    """Static-index analogue of LSP ``textDocument/definition``.

    ``line`` is the graph's 0-based line number. Agent-facing skill schemas mark
    line inputs as 1-based, so the runner performs the boundary conversion.
    """
    limit = max(1, int(top_k or 8))
    if symbol:
        return _definitions_for_symbol(graph, symbol, limit)
    if not file_path or line is None:
        raise ValueError("lsp_definition requires either symbol or file_path + line")
    if not hasattr(graph, "query_range"):
        raise ValueError("code graph does not support range queries")

    _ensure_range_index(graph)
    query = graph.query_range(file_path, int(line), int(line))

    target_names: list[str] = []
    for edge in getattr(query, "outgoing", []) or []:
        name = _node_name_for_vid(graph, edge.target_vid)
        if name:
            target_names.append(name)

    defined = sorted(
        list(getattr(query, "defined", []) or []),
        key=lambda node: (
            (getattr(node, "end_line", 0) or 0) - (getattr(node, "start_line", 0) or 0),
            getattr(node, "start_line", 0) or 0,
        ),
    )
    defined_names = [node.name for node in defined if getattr(node, "name", None)]
    if not target_names:
        target_names = defined_names
    elif character is not None:
        # A declaration line can also contain receiver/type references. Exact
        # character lookup must consider both sets before token filtering.
        target_names.extend(defined_names)

    if character is not None:
        target_names = _filter_targets_by_character(
            graph,
            target_names,
            file_path=file_path,
            line=int(line),
            character=int(character),
        )

    if not target_names:
        if character is not None:
            raise ValueError(
                "no indexed definition token at "
                f"{file_path}:{int(line) + 1}:{int(character)}"
            )
        raise ValueError(f"no indexed definition at {file_path}:{int(line) + 1}")

    target_names = [
        name
        for name in target_names
        if node_has_definition(graph.get_node_info_by_name(name) or {})
    ]
    if not target_names:
        raise ValueError(f"no indexed definition at {file_path}:{int(line) + 1}")

    nodes = [
        _compact_node(
            graph,
            name,
            f"definition of {display_name(graph, name)}",
            use_selection_line=True,
        )
        for name in target_names
    ]
    return _dedupe_nodes(nodes, limit)


def _filter_targets_by_character(
    graph: Any,
    target_names: Sequence[str],
    *,
    file_path: str,
    line: int,
    character: int,
) -> list[str]:
    if not target_names:
        return []
    token = _source_token_at_character(graph, file_path, line, character)
    if not token:
        return []
    return [
        name for name in target_names if token in _symbol_anchor_tokens(graph, name)
    ]


def _source_token_at_character(
    graph: Any, file_path: str, line: int, character: int
) -> Optional[str]:
    source_line = _source_line(graph, file_path, line)
    if source_line is None:
        return None
    for match in _WORD_RE.finditer(source_line):
        if match.start() <= character < match.end():
            return match.group(0)
    return None


def _source_line(graph: Any, file_path: str, line: int) -> Optional[str]:
    root = getattr(graph, "project_root", None)
    path = Path(file_path)
    if not path.is_absolute():
        if not root:
            return None
        path = Path(root) / path
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[line]
    except (OSError, IndexError):
        return None


def _symbol_anchor_tokens(graph: Any, name: str) -> set[str]:
    info = graph.get_node_info_by_name(name) or {}
    candidates = {
        name,
        display_name(graph, name),
        str(info.get("name") or ""),
        str(info.get("unified_name") or ""),
    }
    return {_bare(candidate) for candidate in candidates if _bare(candidate)}


def lsp_references(
    graph: Any,
    *,
    file_path: Optional[str] = None,
    line: Optional[int] = None,
    character: Optional[int] = None,
    symbol: Optional[str] = None,
    include_declaration: bool = True,
    top_k: int = 40,
) -> list[QueriedNode]:
    """Static-index analogue of LSP ``textDocument/references``."""
    limit = max(1, int(top_k or 40))
    targets: list[str] = []
    if symbol:
        name, candidates = _resolve_one(graph, symbol)
        if name is None:
            if candidates:
                raise ValueError(
                    f"symbol {symbol!r} is ambiguous; candidates: {candidates}. "
                    "Call again with one exact name."
                )
            raise ValueError(
                f"symbol {symbol!r} not found in the code graph. Use a name "
                "from a search result or read output."
            )
        targets = [name]
    else:
        if not file_path or line is None:
            raise ValueError(
                "lsp_references requires either symbol or file_path + line"
            )
        definitions = lsp_definition(
            graph,
            file_path=file_path,
            line=line,
            character=character,
            top_k=limit,
        )
        for definition in definitions:
            name, _ = _resolve_one(graph, definition.node_name)
            if name:
                targets.append(name)

    nodes: list[QueriedNode] = []
    for name in targets:
        label = display_name(graph, name)
        if include_declaration and node_has_definition(
            graph.get_node_info_by_name(name) or {}
        ):
            nodes.append(
                _compact_node(
                    graph,
                    name,
                    f"definition of {label}",
                    use_selection_line=True,
                )
            )

        vid = getattr(graph, "name_to_vertex", {}).get(name)
        if vid is None or not hasattr(graph, "graph"):
            continue
        for eid in graph.graph.incident(vid, mode="in"):
            edge = graph.graph.es[eid]
            attrs = edge.attributes()
            if attrs.get("type") != EDGE_TYPE_REFERENCE:
                continue
            nodes.append(
                _compact_reference(
                    graph,
                    target_name=name,
                    source_vid=edge.source,
                    file_path=attrs.get("anchor_file"),
                    line=attrs.get("anchor_line"),
                )
            )
    return _dedupe_nodes(nodes, limit)


def _graph_names_for_ids(graph: Any, vertex_ids: Sequence[int]) -> list[str]:
    out = []
    for vid in vertex_ids:
        name = _node_name_for_vid(graph, vid)
        if name:
            out.append(name)
    return out


def _node_text(graph: Any, name: str) -> str:
    info = graph.get_node_info_by_name(name) or {}
    return f"{info.get('file') or ''} {display_name(graph, name)} {name}"


def _span_len(graph: Any, name: str) -> int:
    info = graph.get_node_info_by_name(name) or {}
    try:
        start = int(info.get("start_line") or 0)
        end = int(info.get("end_line") or start)
    except (TypeError, ValueError):
        return 0
    return max(0, end - start + 1)


def _role(graph: Any, name: str, *, query_terms: set[str]) -> str:
    info = graph.get_node_info_by_name(name) or {}
    node_type = str(info.get("type") or "")
    label = display_name(graph, name)
    leaf = _bare(label or name)
    lower_leaf = leaf.lower()
    terms = _terms(_node_text(graph, name))
    overlap = terms & query_terms

    if node_type in {NODE_TYPE_CLASS, NODE_TYPE_FIELD}:
        return "type"
    if lower_leaf.startswith(("get", "default")) or (
        terms & _PROVIDER_TERMS and overlap
    ):
        return "provider"
    if lower_leaf.startswith(("new", "make", "build")) or (
        terms & _BRIDGE_TERMS and overlap
    ):
        return "bridge"
    if node_type in {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD} and (
        _span_len(graph, name) > 1 or terms & _ENDPOINT_TERMS
    ):
        return "endpoint"
    return "support"


def _candidate_score(
    graph: Any,
    candidate: dict[str, Any],
    *,
    query_terms: set[str],
) -> float:
    name = str(candidate.get("name") or "")
    role = str(candidate.get("role") or "support")
    source = str(candidate.get("source") or "")
    terms = _terms(_node_text(graph, name))
    score = 5.0 * len(terms & query_terms)

    if source in {"direct_seed", "query_seed"}:
        score += max(0, 8 - int(candidate.get("direct_rank") or 0))
    else:
        score += 3.0

    if role == "endpoint":
        score += 4.0
    elif role == "bridge":
        score += 3.0
    elif role == "provider":
        score += 2.0
    elif role == "type":
        score += 1.0

    return score


def _query_seed_candidates(
    graph: Any,
    *,
    query_terms: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    if not query_terms:
        return []

    candidates: list[dict[str, Any]] = []
    for name in getattr(graph, "name_to_vertex", {}) or {}:
        info = graph.get_node_info_by_name(name) or {}
        node_type = str(info.get("type") or "")
        if node_type and node_type not in _ROUTE_SEED_NODE_TYPES:
            continue
        # Query fallback intentionally ignores file paths for seed selection:
        # repo/package names tend to be broad issue text noise.
        seed_text = f"{display_name(graph, name)} {name}"
        overlap = _terms(seed_text) & query_terms
        if len(overlap) < _QUERY_SEED_MIN_OVERLAP:
            continue
        role = _role(graph, name, query_terms=query_terms)
        candidate = {
            "name": name,
            "role": role,
            "source": "query_seed",
            "seed": ", ".join(sorted(overlap)[:4]),
            "direct_rank": len(candidates),
        }
        candidate["score"] = _candidate_score(
            graph, candidate, query_terms=query_terms
        ) + 6.0 * len(overlap)
        candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            -float(item.get("score") or 0.0),
            display_name(graph, str(item.get("name") or "")),
        )
    )
    return candidates[:limit]


def _route_neighbors(graph: Any, name: str) -> list[tuple[str, str]]:
    neighbors: list[tuple[str, str]] = []
    if hasattr(graph, "get_successors"):
        neighbors.extend(
            (neighbor, "successor")
            for neighbor in _graph_names_for_ids(graph, graph.get_successors(name))
        )
    if hasattr(graph, "get_predecessors"):
        neighbors.extend(
            (neighbor, "predecessor")
            for neighbor in _graph_names_for_ids(graph, graph.get_predecessors(name))
        )
    return neighbors


def _include_neighbor(
    graph: Any, name: str, *, role: str, query_terms: set[str]
) -> bool:
    if role not in {"endpoint", "bridge", "provider", "type"}:
        return False
    if not query_terms:
        return role in {"endpoint", "bridge"}
    return bool(_terms(_node_text(graph, name)) & query_terms) or role in {
        "endpoint",
        "bridge",
    }


def _route_node(graph: Any, candidate: dict[str, Any]) -> QueriedNode:
    name = str(candidate.get("name") or "")
    role = str(candidate.get("role") or "support")
    source = str(candidate.get("source") or "direct_seed")
    seed = str(candidate.get("seed") or display_name(graph, name))
    via = str(candidate.get("via") or "")
    direction = str(candidate.get("direction") or "")
    if source == "direct_seed":
        relation = f"route {role}: direct seed {seed}"
    elif source == "query_seed":
        relation = f"route {role}: query match {seed}"
    elif via:
        relation = f"route {role}: {direction} via {via}"
    else:
        relation = f"route {role}: graph neighbor"
    return _compact_node(
        graph,
        name,
        relation,
        score=float(candidate.get("score") or 0.0),
    )


def _append_route_seed(
    graph: Any,
    candidates: list[dict[str, Any]],
    name: str,
    *,
    seed: str,
    rank: int,
    source: str,
    query_terms: set[str],
    include_neighbors: bool,
) -> None:
    role = _role(graph, name, query_terms=query_terms)
    candidate = {
        "name": name,
        "role": role,
        "source": source,
        "seed": seed,
        "direct_rank": rank,
    }
    candidate["score"] = _candidate_score(graph, candidate, query_terms=query_terms)
    candidates.append(candidate)

    if not include_neighbors:
        return
    for neighbor, direction in _route_neighbors(graph, name):
        neighbor_role = _role(graph, neighbor, query_terms=query_terms)
        if not _include_neighbor(
            graph, neighbor, role=neighbor_role, query_terms=query_terms
        ):
            continue
        neighbor_candidate = {
            "name": neighbor,
            "role": neighbor_role,
            "source": "neighbor",
            "via": display_name(graph, name),
            "direction": direction,
            "direct_rank": rank + len(candidates),
        }
        neighbor_candidate["score"] = _candidate_score(
            graph, neighbor_candidate, query_terms=query_terms
        )
        candidates.append(neighbor_candidate)


def lsp_route(
    graph: Any,
    *,
    symbols: Sequence[str],
    query: Optional[str] = None,
    top_k: int = 12,
    include_neighbors: bool = True,
) -> list[QueriedNode]:
    """Return compact graph route anchors for one or more symbol seeds.

    Route roles are derived from node type, query-term overlap, and local graph
    neighborhood. This function intentionally avoids benchmark instance names or
    scorer-specific answer ordering.
    """
    limit = max(1, int(top_k or 12))
    query_terms = _query_terms(query)
    candidates: list[dict[str, Any]] = []

    for rank, seed in enumerate(symbols or []):
        for name in resolve_symbol_candidates(graph, str(seed), limit=4):
            _append_route_seed(
                graph,
                candidates,
                name,
                seed=str(seed),
                rank=rank,
                source="direct_seed",
                query_terms=query_terms,
                include_neighbors=include_neighbors,
            )

    if not candidates:
        for rank, candidate in enumerate(
            _query_seed_candidates(graph, query_terms=query_terms, limit=limit)
        ):
            _append_route_seed(
                graph,
                candidates,
                str(candidate.get("name") or ""),
                seed=str(candidate.get("seed") or "query"),
                rank=rank,
                source="query_seed",
                query_terms=query_terms,
                include_neighbors=include_neighbors,
            )

    source_rank = {"direct_seed": 0, "query_seed": 0, "neighbor": 1}
    role_rank = {"endpoint": 0, "bridge": 1, "provider": 2, "type": 3, "support": 4}
    candidates.sort(
        key=lambda item: (
            source_rank.get(str(item.get("source") or ""), 2),
            -float(item.get("score") or 0.0),
            role_rank.get(str(item.get("role") or "support"), 5),
            int(item.get("direct_rank") or 0),
            display_name(graph, str(item.get("name") or "")),
        )
    )

    seen: set[str] = set()
    selected: list[QueriedNode] = []
    for candidate in candidates:
        name = str(candidate.get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        selected.append(_route_node(graph, candidate))
        if len(selected) >= limit:
            break
    return selected
