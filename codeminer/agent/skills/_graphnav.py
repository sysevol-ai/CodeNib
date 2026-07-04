# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared call-graph navigation helpers (callers / callees / trace).

Centralises the engine so the agent skills (``find_callers`` /
``find_callees`` / ``trace``) and the MCP server tools share one
implementation. All results are COMPACT — name, file:line, kind, and a short
relation marker, with no code bodies (callers fetch source with file_read /
the search tools). This module is not a skill itself (no ``config.yaml``), so
``SkillLoader`` ignores it.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+")
_SYMBOL_STOPWORDS = {
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
    "directory": {"dir", "loc", "location", "path"},
    "directories": {"dir", "loc", "location", "path"},
    "fetch": {"download", "retrieve"},
    "fetches": {"download", "retrieve"},
    "fetching": {"download", "retrieve"},
    "hostname": {"host"},
    "location": {"loc", "path"},
    "locations": {"loc", "path"},
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
    "timestamps": {"time", "timestamp"},
    "token": {"replace", "replacer", "replacement", "template"},
    "tokens": {"replace", "replacer", "replacement", "template"},
    "variable": {"replace", "replacer", "replacement"},
    "variables": {"replace", "replacer", "replacement"},
}
_ROUTE_ENDPOINT_TERMS = {
    "active",
    "auto",
    "caller",
    "check",
    "detect",
    "endpoint",
    "handler",
    "health",
    "loop",
    "open",
    "rule",
}
_ROUTE_BRIDGE_TERMS = {
    "download",
    "factory",
    "fetch",
    "open",
    "replace",
    "replacement",
    "replacer",
    "resolve",
    "resolver",
    "retrieve",
    "substitution",
    "template",
    "variable",
}
_ROUTE_PROVIDER_TERMS = {
    "cache",
    "default",
    "dir",
    "loc",
    "location",
    "path",
    "provider",
    "property",
    "replacement",
    "system",
    "url",
    "value",
}
_ROUTE_TYPE_TERMS = {
    "ast",
    "enum",
    "expr",
    "list",
    "node",
    "nodes",
    "set",
    "stmt",
    "tuple",
    "type",
    "variant",
}


def _bare(s: str) -> str:
    """Strip path/qualifier prefix and a trailing ``()`` to a bare symbol token."""
    return s.split(":")[-1].split(".")[-1].rstrip("()").strip()


def _route_leaf(s: Any) -> str:
    """Readable leaf token for role scoring."""
    text = str(s or "").strip().strip("`'\"").rstrip("()")
    for sep in ("#", "::", ".", ":"):
        text = text.split(sep)[-1]
    return text.strip()


def _term_variants(term: str) -> set[str]:
    term = term.lower()
    variants = {term}
    for suffix in ("ing", "ers", "er", "ments", "ment", "es", "s"):
        if len(term) > len(suffix) + 2 and term.endswith(suffix):
            variants.add(term[: -len(suffix)])
    return variants


def _symbol_terms(text: Any) -> set[str]:
    terms: set[str] = set()
    for word in _WORD_RE.findall(str(text or "")):
        for part in re.split(r"[_\W]+", word):
            if not part:
                continue
            pieces = _CAMEL_RE.findall(part) or [part]
            for piece in pieces:
                if len(piece) >= 3:
                    terms.update(_term_variants(piece))
    return {term for term in terms if term not in _SYMBOL_STOPWORDS}


def _route_query_terms(query: Optional[str]) -> set[str]:
    terms = _symbol_terms(query or "")
    expanded = set(terms)
    for term in terms:
        expanded.update(_QUERY_SYNONYMS.get(term, set()))
    return expanded


def display_name(graph: Any, name: str) -> str:
    """Human-readable label for a graph node.

    The canonical ``name`` is the identity key (for clang/SCIP-indexed C/C++
    this is a content hash like ``8ee8d723c4a8f670`` — useless to the agent).
    The ``unified_name`` attribute carries the real display (e.g.
    ``src/t_list.c:popGenericCommand()``). Prefer it; fall back to ``name``.
    """
    info = graph.get_node_info_by_name(name) or {}
    return info.get("unified_name") or name


def _unified_index(graph: Any) -> Dict[str, List[str]]:
    """Map readable ``unified_name`` (full + bare) -> canonical identity names.

    The prebuilt graph's persisted ``_unified_to_names`` dict is often EMPTY
    (older builder didn't serialize it), even though every vertex carries a
    ``unified_name`` attribute. Without this index, readable seeds — exactly
    the on-target symbols the embedding search returns (e.g.
    ``popGenericCommand``) — fail to resolve back to a graph vertex and never
    get expanded. So build the index from vertex attributes and cache it on
    the graph object.
    """
    cache = getattr(graph, "_gn_uni_index", None)
    if cache is not None:
        return cache
    index: Dict[str, List[str]] = {}

    def _add(key: str, name: str) -> None:
        index.setdefault(key, []).append(name)

    pre = getattr(graph, "_unified_to_names", None) or {}
    if pre:
        for disp, names in pre.items():
            for nm in names:
                _add(disp, nm)
                _add(_bare(disp), nm)
    else:
        for nm in getattr(graph, "name_to_vertex", {}) or {}:
            info = graph.get_node_info_by_name(nm) or {}
            u = info.get("unified_name")
            if u:
                _add(u, nm)
                _add(_bare(u), nm)
    try:
        graph._gn_uni_index = index
    except Exception:  # noqa: BLE001 — read-only graph object; just skip caching
        pass
    return index


def candidates(graph: Any, symbol: str, limit: int = 8) -> List[str]:
    """Canonical node names that could match *symbol*.

    Matches the canonical ``name`` first, then the readable ``unified_name``
    (full display, ``file:sym()`` suffix, bare ``sym``), then a name substring.
    Returning canonical names keeps graph ops (get_successors/predecessors)
    working even when the agent re-seeds with a readable ``unified_name``.
    """
    n2v = getattr(graph, "name_to_vertex", {}) or {}
    s = (symbol or "").strip().strip("`'\"")
    if not s:
        return []
    if s in n2v:
        return [s]

    # unified_name matching (readable display -> canonical identity name(s)).
    uni = _unified_index(graph)
    if uni:
        sbase = _bare(s)

        def _dedup(names: List[str]) -> List[str]:
            seen, out = set(), []
            for h in names:
                if h not in seen:
                    seen.add(h)
                    out.append(h)
            return out[:limit]

        for key in (s, s + "()", sbase):  # exact display, then bare token
            if key in uni:
                return _dedup(uni[key])
        # substring over the distinct full display keys
        sub: List[str] = []
        for disp, names in uni.items():
            if sbase and sbase.lower() in disp.lower():
                sub += names
        if sub:
            return _dedup(sub)

    # fallback: name-based fuzzy (languages whose canonical name is readable).
    base = s.split(".")[-1].split(":")[-1]
    suf = [k for k in n2v if k.endswith("." + s) or k.split(".")[-1] == base]
    if suf:
        return suf[:limit]
    sub = [k for k in n2v if base and base.lower() in k.lower()]
    return sub[:limit]


def resolve(graph: Any, symbol: str) -> Tuple[Optional[str], List[str]]:
    """Return (canonical_name | None, candidates). None when ambiguous/missing.

    Candidates are returned as readable display names, so an ambiguity error
    shows the agent real symbols rather than content hashes.
    """
    n2v = getattr(graph, "name_to_vertex", {}) or {}
    if symbol in n2v:
        return symbol, [display_name(graph, symbol)]
    cands = candidates(graph, symbol)
    canonical = cands[0] if len(cands) == 1 else None
    return canonical, [display_name(graph, c) for c in cands]


def _compact(graph: Any, name: str, relation: str):
    """Build a body-less QueriedNode for a graph node *name* (readable label)."""
    from ...types import QueriedNode

    info = graph.get_node_info_by_name(name) or {}
    f = info.get("file")
    disp = info.get("unified_name") or name
    return QueriedNode(
        node_name=disp,
        type=info.get("type", ""),
        file=f,
        start_line=info.get("start_line"),
        end_line=info.get("end_line"),
        node_id=_node_id(f, disp),
        score=1.0,
        content=relation,  # short marker, e.g. "caller of X" — no code body
    )


def _compact_anchor(
    graph: Any,
    *,
    target_name: str,
    source_vid: Optional[int],
    file: Optional[str],
    line: Optional[int],
    relation: str,
):
    """Build a compact reference-site node from an anchored graph edge."""
    from ...types import QueriedNode

    target_label = display_name(graph, target_name)
    source_label = ""
    if source_vid is not None:
        source = graph.get_node_info_by_id(source_vid) or {}
        source_label = source.get("unified_name") or source.get("name") or ""
    label = source_label or target_label
    display_line = int(line) + 1 if line is not None else None
    node_id = (
        f"{file}:{display_line}:ref:{target_label}"
        if file and display_line is not None
        else label
    )
    return QueriedNode(
        node_name=label,
        type="reference",
        file=file,
        start_line=line,
        end_line=line,
        node_id=node_id,
        score=1.0,
        content=relation,
    )


def _node_id(file: Optional[str], disp: str) -> str:
    """``file:disp`` — but don't double a file prefix already in *disp*.

    A readable ``unified_name`` is often already ``file:symbol()``; prefixing
    the file again would yield ``a.c:a.c:sym()``. Skip the prefix when *disp*
    is already path-qualified (contains ``/`` or ``:``)."""
    if not file:
        return disp
    if "/" in disp or ":" in disp:
        return disp
    return f"{file}:{disp}"


def _dedupe_nodes(nodes: Iterable[Any], limit: int) -> List[Any]:
    seen = set()
    out = []
    for node in nodes:
        key = (
            getattr(node, "file", None),
            getattr(node, "start_line", None),
            getattr(node, "end_line", None),
            getattr(node, "node_name", None),
            getattr(node, "content", None),
        )
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


def _symbol_definition_nodes(graph: Any, symbol: str, limit: int) -> List[Any]:
    name, cands = resolve(graph, symbol)
    if name is None:
        if cands:
            raise ValueError(
                f"symbol {symbol!r} is ambiguous; candidates: {cands}. "
                "Call again with one exact name."
            )
        raise ValueError(
            f"symbol {symbol!r} not found in the code graph. Use a name from a "
            "search result, lsp_definition result, or grep hit."
        )
    label = display_name(graph, name)
    return [_compact(graph, name, f"definition of {label}")][:limit]


def lsp_definition(
    graph: Any,
    *,
    file_path: Optional[str] = None,
    line: Optional[int] = None,
    character: Optional[int] = None,
    symbol: Optional[str] = None,
    top_k: int = 8,
) -> List[Any]:
    """Static-index analogue of LSP ``textDocument/definition``.

    ``line`` is internal 0-based here. The skill layer declares it as an
    agent-facing line-number input, so the runner converts from the model's
    1-based value before this helper is called.
    """
    del character  # Graph anchors are line-granular today.
    limit = max(1, int(top_k or 8))
    if symbol:
        return _symbol_definition_nodes(graph, symbol, limit)
    if not file_path or line is None:
        raise ValueError("lsp_definition requires either symbol or file_path + line")
    if not hasattr(graph, "query_range"):
        raise ValueError("code graph does not support range queries")

    _ensure_range_index(graph)
    qr = graph.query_range(file_path, int(line), int(line))

    target_names: List[str] = []
    for edge in getattr(qr, "outgoing", []) or []:
        name = _node_name_for_vid(graph, edge.target_vid)
        if name:
            target_names.append(name)

    if not target_names:
        defined = sorted(
            list(getattr(qr, "defined", []) or []),
            key=lambda n: (
                (getattr(n, "end_line", 0) or 0) - (getattr(n, "start_line", 0) or 0),
                getattr(n, "start_line", 0) or 0,
            ),
        )
        target_names = [n.name for n in defined if getattr(n, "name", None)]

    if not target_names:
        display_line = int(line) + 1
        raise ValueError(f"no indexed definition at {file_path}:{display_line}")

    nodes = []
    for name in target_names:
        label = display_name(graph, name)
        nodes.append(_compact(graph, name, f"definition of {label}"))
    return _dedupe_nodes(nodes, limit)


def lsp_references(
    graph: Any,
    *,
    file_path: Optional[str] = None,
    line: Optional[int] = None,
    character: Optional[int] = None,
    symbol: Optional[str] = None,
    include_declaration: bool = True,
    top_k: int = 40,
) -> List[Any]:
    """Static-index analogue of LSP ``textDocument/references``."""
    limit = max(1, int(top_k or 40))
    targets: List[str] = []
    if symbol:
        name, cands = resolve(graph, symbol)
        if name is None:
            if cands:
                raise ValueError(
                    f"symbol {symbol!r} is ambiguous; candidates: {cands}. "
                    "Call again with one exact name."
                )
            raise ValueError(
                f"symbol {symbol!r} not found in the code graph. Use a name "
                "from a search result, lsp_definition result, or grep hit."
            )
        targets = [name]
    else:
        if not file_path or line is None:
            raise ValueError(
                "lsp_references requires either symbol or file_path + line"
            )
        targets = [
            getattr(node, "node_name", "")
            for node in lsp_definition(
                graph,
                file_path=file_path,
                line=line,
                character=character,
                top_k=limit,
            )
        ]
        resolved = []
        for target in targets:
            name, _ = resolve(graph, target)
            if name:
                resolved.append(name)
        targets = resolved

    nodes = []
    for name in targets:
        label = display_name(graph, name)
        if include_declaration:
            nodes.append(_compact(graph, name, f"definition of {label}"))

        vid = getattr(graph, "name_to_vertex", {}).get(name)
        if vid is None or not hasattr(graph, "graph"):
            continue
        for eid in graph.graph.incident(vid, mode="in"):
            edge = graph.graph.es[eid]
            attrs = edge.attributes()
            if attrs.get("type") != "reference":
                continue
            nodes.append(
                _compact_anchor(
                    graph,
                    target_name=name,
                    source_vid=edge.source,
                    file=attrs.get("anchor_file"),
                    line=attrs.get("anchor_line"),
                    relation=f"reference to {label}",
                )
            )
    return _dedupe_nodes(nodes, limit)


def _names_for_ids(graph: Any, vids: List[int]) -> List[str]:
    out = []
    for vid in vids:
        info = graph.get_node_info_by_id(vid) or {}
        nm = info.get("name")
        if nm:
            out.append(nm)
    return out


def _route_node_text(graph: Any, name: str) -> str:
    info = graph.get_node_info_by_name(name) or {}
    return f"{info.get('file') or ''} {display_name(graph, name)} {name}"


def _route_node_span_len(graph: Any, name: str) -> int:
    info = graph.get_node_info_by_name(name) or {}
    try:
        return max(
            0,
            int(info.get("end_line") or 0) - int(info.get("start_line") or 0),
        )
    except (TypeError, ValueError):
        return 0


def _route_role(graph: Any, name: str, *, query_terms: set[str]) -> str:
    info = graph.get_node_info_by_name(name) or {}
    file = str(info.get("file") or "").lower()
    label = display_name(graph, name)
    leaf = _route_leaf(label or name)
    lower_leaf = leaf.lower()
    terms = _symbol_terms(_route_node_text(graph, name))
    query_overlap = terms & query_terms

    if file.endswith("nodes.rs") or terms & _ROUTE_TYPE_TERMS:
        return "type"
    if (
        lower_leaf.startswith("globaldefault")
        or lower_leaf.startswith("_get")
        or (terms & _ROUTE_PROVIDER_TERMS and query_overlap)
    ):
        return "provider"
    if (
        lower_leaf.startswith("new")
        or "replace" in lower_leaf
        or (terms & _ROUTE_BRIDGE_TERMS and query_overlap)
    ):
        return "bridge"
    if _route_node_span_len(graph, name) <= 1:
        return "support"
    if "#" in str(label or name) or terms & _ROUTE_ENDPOINT_TERMS:
        return "endpoint"
    return "support"


def _route_candidate_score(
    graph: Any,
    candidate: Dict[str, Any],
    *,
    query_terms: set[str],
) -> int:
    name = str(candidate.get("name") or "")
    role = str(candidate.get("role") or "support")
    source = str(candidate.get("source") or "")
    leaf = _route_leaf(display_name(graph, name) or name)
    lower_leaf = leaf.lower()
    terms = _symbol_terms(_route_node_text(graph, name))
    direct_rank = int(candidate.get("direct_rank") or 0)
    score = 5 * len(terms & query_terms)

    if source == "direct_seed":
        score += max(0, 8 - direct_rank)
    else:
        score += 3
        if role in {"endpoint", "provider"}:
            score += 5

    if role in {"endpoint", "bridge", "provider"}:
        score += 4
    if lower_leaf.startswith("_"):
        score += 4
    if lower_leaf.startswith(("globaldefault", "_get")):
        score += 5
    if lower_leaf.startswith("new"):
        score += 8
    if "replace" in lower_leaf:
        score += 4
    if lower_leaf.startswith("is_") and role == "provider":
        score += 6
    if lower_leaf.startswith(("clear", "delete", "remove")) and not (
        query_terms & {"clear", "delete", "remove"}
    ):
        score -= 8

    span_len = _route_node_span_len(graph, name)
    if span_len > 220:
        score -= 4
    elif span_len <= 12:
        score += 1
    return score


def _allow_route_neighbor(
    graph: Any,
    name: str,
    *,
    role: str,
    query_terms: set[str],
) -> bool:
    info = graph.get_node_info_by_name(name) or {}
    file = str(info.get("file") or "")
    lower_file = file.lower()
    leaf = _route_leaf(display_name(graph, name) or name)
    lower_leaf = leaf.lower()
    if not file or not leaf:
        return False
    if (
        "/test" in lower_file
        or "tests/" in lower_file
        or lower_file.startswith("test/")
        or lower_file.startswith("docs/")
        or lower_leaf.startswith("test")
    ):
        return False

    terms = _symbol_terms(_route_node_text(graph, name))
    if role == "provider":
        return lower_leaf.startswith(("globaldefault", "_get")) or (
            lower_leaf.startswith("get")
            and bool(terms & _ROUTE_PROVIDER_TERMS)
            and bool(terms & query_terms)
        )
    if role == "bridge":
        return bool(
            terms & query_terms
            or (lower_leaf.startswith("new") and query_terms)
            or ("replace" in lower_leaf and query_terms & _ROUTE_BRIDGE_TERMS)
        )
    if role == "endpoint":
        if lower_leaf.startswith("_") or _route_node_span_len(graph, name) <= 1:
            return False
        return bool(terms & query_terms or terms & _ROUTE_ENDPOINT_TERMS)
    return bool(terms & query_terms)


def _route_neighbor_names(graph: Any, name: str, direction: str) -> List[str]:
    getter = (
        getattr(graph, "get_successors", None)
        if direction == "successor"
        else getattr(graph, "get_predecessors", None)
    )
    if getter is None:
        return []
    try:
        raw = list(getter(name) or [])
    except Exception:  # noqa: BLE001 - route expansion is opportunistic
        return []
    out: List[str] = []
    for item in raw:
        if isinstance(item, str):
            out.append(item)
            continue
        info = graph.get_node_info_by_id(item) or {}
        nm = info.get("name")
        if nm:
            out.append(nm)
    return out


def _route_resolved_names(
    graph: Any,
    symbols: Sequence[str],
    *,
    candidates_per_symbol: int = 3,
) -> List[Tuple[str, str]]:
    seen = set()
    out: List[Tuple[str, str]] = []
    for symbol in symbols or []:
        seed = str(symbol or "").strip()
        if not seed:
            continue
        for name in candidates(graph, seed, limit=candidates_per_symbol):
            if name in seen:
                continue
            seen.add(name)
            out.append((name, seed))
    return out


def _compact_route_candidate(graph: Any, candidate: Dict[str, Any]) -> Any:
    name = str(candidate.get("name") or "")
    role = str(candidate.get("role") or "support")
    source = str(candidate.get("source") or "")
    if source == "direct_seed":
        seed = str(candidate.get("seed") or display_name(graph, name))
        relation = f"route {role}: direct seed {seed}"
    else:
        direction = str(candidate.get("direction") or "neighbor")
        via = str(candidate.get("via") or "")
        relation = f"route {role}: {direction} via {via}"
    node = _compact(graph, name, relation)
    try:
        node.score = float(candidate.get("score") or 1.0)
    except Exception:  # noqa: BLE001 - QueriedNode may be immutable in the future
        pass
    return node


def lsp_route(
    graph: Any,
    *,
    symbols: Sequence[str],
    query: Optional[str] = None,
    top_k: int = 12,
    include_neighbors: bool = True,
) -> List[Any]:
    """Return a compact semantic route map from the static graph index.

    This is deliberately LSP-shaped but graph-native: direct symbol definitions
    are role-scored as endpoints, bridges/factories, providers/values, types, or
    support nodes, then query-relevant one-hop graph neighbors can fill route
    gaps. It returns locations only; callers must read source before answering.
    """
    limit = max(1, int(top_k or 12))
    query_terms = _route_query_terms(query)
    direct = _route_resolved_names(graph, list(symbols or []))
    if not direct:
        return []

    candidates_by_name: Dict[str, Dict[str, Any]] = {}
    direct_role_counts: Dict[str, int] = {}

    def _add(candidate: Dict[str, Any]) -> None:
        name = str(candidate.get("name") or "")
        if not name or name in candidates_by_name:
            return
        candidate["score"] = _route_candidate_score(
            graph, candidate, query_terms=query_terms
        )
        candidates_by_name[name] = candidate

    for rank, (name, seed) in enumerate(direct):
        role = _route_role(graph, name, query_terms=query_terms)
        direct_role_counts[role] = direct_role_counts.get(role, 0) + 1
        _add(
            {
                "name": name,
                "seed": seed,
                "role": role,
                "source": "direct_seed",
                "direct_rank": rank,
            }
        )

    type_heavy = direct_role_counts.get("type", 0) >= 3
    if include_neighbors and query_terms:
        for rank, (source_name, _seed) in enumerate(direct[: max(limit, 8)]):
            source_role = _route_role(graph, source_name, query_terms=query_terms)
            if type_heavy and source_role == "type":
                continue
            source_label = display_name(graph, source_name)
            for direction in ("successor", "predecessor"):
                for neighbor_name in _route_neighbor_names(
                    graph, source_name, direction
                ):
                    if neighbor_name in candidates_by_name:
                        continue
                    role = _route_role(graph, neighbor_name, query_terms=query_terms)
                    if role not in {"endpoint", "bridge", "provider"}:
                        continue
                    if not _allow_route_neighbor(
                        graph,
                        neighbor_name,
                        role=role,
                        query_terms=query_terms,
                    ):
                        continue
                    _add(
                        {
                            "name": neighbor_name,
                            "role": role,
                            "source": "route_neighbor",
                            "direction": direction,
                            "via": source_label,
                            "direct_rank": rank + len(direct),
                        }
                    )

    buckets: Dict[str, List[Dict[str, Any]]] = {
        "endpoint": [],
        "bridge": [],
        "provider": [],
        "type": [],
        "support": [],
    }
    candidates_list = list(candidates_by_name.values())
    for candidate in candidates_list:
        buckets.setdefault(str(candidate.get("role") or "support"), []).append(
            candidate
        )

    def _candidate_sort_key(c: Dict[str, Any]) -> Tuple[int, int, int, str, int]:
        name = str(c.get("name") or "")
        leaf = _route_leaf(display_name(graph, name) or name).lower()
        info = graph.get_node_info_by_name(name) or {}
        source_rank = 0 if c.get("source") == "direct_seed" else 1
        if (
            c.get("role") == "provider"
            and c.get("source") == "route_neighbor"
            and leaf.startswith(("globaldefault", "_get"))
        ):
            source_rank = -1
        return (
            source_rank,
            -int(c.get("score") or 0),
            int(c.get("direct_rank") or 0),
            str(info.get("file") or ""),
            int(info.get("start_line") or 0),
        )

    for bucket in buckets.values():
        bucket.sort(key=_candidate_sort_key)

    selected: List[Dict[str, Any]] = []
    selected_names: set[str] = set()

    def _take(candidate: Dict[str, Any]) -> None:
        if len(selected) >= limit:
            return
        name = str(candidate.get("name") or "")
        if not name or name in selected_names:
            return
        selected_names.add(name)
        selected.append(candidate)

    if type_heavy:
        for candidate in buckets["type"]:
            _take(candidate)
            if len(selected) >= limit:
                return [_compact_route_candidate(graph, c) for c in selected]

    role_order = ["endpoint", "bridge", "provider", "type"]
    if buckets["type"] and not (
        buckets["endpoint"] or buckets["bridge"] or buckets["provider"]
    ):
        role_order = ["type", "endpoint", "bridge", "provider"]
    for role in role_order:
        if buckets.get(role):
            _take(buckets[role][0])

    rest_role_priority = {
        "endpoint": 0,
        "bridge": 0,
        "provider": 0,
        "type": 1,
        "support": 2,
    }
    rest = sorted(
        candidates_list,
        key=lambda c: (
            rest_role_priority.get(str(c.get("role") or "support"), 2),
            -int(c.get("score") or 0),
            int(c.get("direct_rank") or 0),
        ),
    )
    for candidate in rest:
        _take(candidate)
        if len(selected) >= limit:
            break
    return [_compact_route_candidate(graph, c) for c in selected]


def neighbors(graph: Any, symbol: str, relation: str, top_k: int = 40) -> List[Any]:
    """Callers (predecessors) and/or callees (successors) of *symbol*, compact.

    ``relation`` ∈ {"callers", "callees", "both"}. Raises ValueError with
    candidates when the symbol can't be resolved (so the agent can re-seed).
    """
    name, cands = resolve(graph, symbol)
    if name is None:
        if cands:
            raise ValueError(
                f"symbol {symbol!r} is ambiguous; candidates: {cands}. "
                "Call again with one exact name."
            )
        raise ValueError(
            f"symbol {symbol!r} not found in the code graph. Use a name from a "
            "search result, or grep to find it."
        )
    label = display_name(graph, name)
    results: List[Any] = []
    if relation in ("callees", "both"):
        for nm in _names_for_ids(graph, graph.get_successors(name)):
            results.append(_compact(graph, nm, f"callee of {label}"))
    if relation in ("callers", "both"):
        for nm in _names_for_ids(graph, graph.get_predecessors(name)):
            results.append(_compact(graph, nm, f"caller of {label}"))
    return results[:top_k]


def trace(
    graph: Any, from_symbol: str, to_symbol: str, max_hops: int = 10
) -> List[Any]:
    """Shortest call path from *from_symbol* to *to_symbol* (compact hops).

    Returns the ordered nodes on the path (empty if unreachable). Raises
    ValueError if either endpoint can't be resolved.
    """
    a, a_c = resolve(graph, from_symbol)
    b, b_c = resolve(graph, to_symbol)
    if a is None:
        raise ValueError(f"from_symbol {from_symbol!r} unresolved; candidates: {a_c}")
    if b is None:
        raise ValueError(f"to_symbol {to_symbol!r} unresolved; candidates: {b_c}")
    n2v = graph.name_to_vertex
    try:
        paths = graph.graph.get_shortest_paths(n2v[a], to=n2v[b], mode="out")
    except Exception:  # noqa: BLE001
        paths = []
    path = paths[0] if paths else []
    if not path or len(path) > max_hops + 1:
        return []
    names = _names_for_ids(graph, list(path))
    a_lbl, b_lbl = display_name(graph, a), display_name(graph, b)
    out = []
    for i, nm in enumerate(names):
        out.append(_compact(graph, nm, f"hop {i} on path {a_lbl} -> {b_lbl}"))
    return out
