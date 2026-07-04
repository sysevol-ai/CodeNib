# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic verify-expand for the runtime probe (Layer 4).

After the agent commits an answer, check — against the compiled graph, with NO
LLM — whether the answer is *anchored* to real code (its named symbols resolve to
graph nodes). If not, the agent likely drifted or hallucinated, so deterministically
inject the 1-hop graph neighbours of the best available seeds (the pre-load
candidates and any resolved symbols) and let it answer once more. Bounded.

This is the only layer that needs the loop; everything it does (resolution check,
neighbour expansion) is a graph lookup, so it adds cost only as bounded extra
agent turns and only when verification fails. See .claude/design/agent-runtime.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from codeminer.eval.retrieval_eval import normalize_file_path, parse_answer_spans


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return max(a_start, b_start) <= min(a_end, b_end)


def _leaf(name: Any) -> str:
    """Trailing symbol leaf, matching harness.build_symbol_span_index keys."""
    s = str(name or "").strip()
    if not s:
        return ""
    if ":" in s:
        s = s.rsplit(":", 1)[1]
    if "]" in s:
        s = s.rsplit("]", 1)[1]
    s = s.replace("#", ".")
    s = s.split("(", 1)[0]
    return s.split("/")[-1].split(".")[-1].strip()


def _answer_symbol_names(answer: str) -> List[str]:
    """Names from the agent's ``Symbols:`` line (reuses the eval parser if present)."""
    try:
        from codeminer.eval.retrieval_eval import _answer_symbols

        return list(_answer_symbols(answer or ""))
    except Exception:  # noqa: BLE001 — parser is best-effort
        return []


class GraphNav:
    """1-hop neighbour navigation over a prebuilt symbol graph.

    Holds the igraph plus two small maps built once per instance:
      ``key_to_idx``  : (norm_file, leaf) -> vertex index
      ``idx_to_span`` : vertex index -> (norm_file, start_1based, end_1based, name)
    so neighbours of the few answer/pre-load seeds are computed on demand (no full
    O(V*deg) materialisation).
    """

    def __init__(self, graph: Any) -> None:
        self.g = graph
        self.key_to_idx: Dict[Tuple[str, str], int] = {}
        self.idx_to_span: Dict[int, Tuple[str, int, int, str]] = {}
        self.file_to_idx: Dict[str, List[int]] = {}
        self.leaf_to_idx: Dict[str, List[int]] = {}
        for v in graph.vs:
            a = v.attributes()
            f, s, e = a.get("file"), a.get("start_line"), a.get("end_line")
            if f is None or s is None or e is None:
                continue
            nf = normalize_file_path(f)
            if not nf:
                continue
            self.idx_to_span[v.index] = (nf, int(s) + 1, int(e) + 1, a.get("name"))
            self.file_to_idx.setdefault(nf, []).append(v.index)
            seen_leaves: set[str] = set()
            for label in (a.get("name"), a.get("unified_name")):
                leaf = _leaf(label)
                if leaf:
                    self.key_to_idx.setdefault((nf, leaf), v.index)
                    if leaf not in seen_leaves:
                        self.leaf_to_idx.setdefault(leaf, []).append(v.index)
                        seen_leaves.add(leaf)

    def resolves(self, file: str, leaf: str) -> bool:
        return (normalize_file_path(file), leaf) in self.key_to_idx

    def neighbors(
        self, seeds: List[Tuple[str, str]], max_nodes: int = 10
    ) -> List[Dict]:
        """1-hop symbol neighbours of the seed (file, leaf) keys, as span dicts."""
        out: List[Dict[str, Any]] = []
        seen: set = set()
        for file, leaf in seeds:
            idx = self.key_to_idx.get((normalize_file_path(file), leaf))
            if idx is None:
                continue
            for ni in self.g.neighbors(idx):
                sp = self.idx_to_span.get(ni)
                if not sp:  # file-type / span-less node
                    continue
                key = (sp[0], sp[1], sp[2])
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "file": sp[0],
                        "start_line": sp[1],
                        "end_line": sp[2],
                        "name": sp[3],
                    }
                )
                if len(out) >= max_nodes:
                    return out
        return out

    def seeds_from_spans(
        self, candidates: List[Dict[str, Any]], max_seeds: int = 5
    ) -> List[Tuple[str, str]]:
        """Resolve retrieval/preload spans back to graph seed keys.

        Preload candidates often carry only ``file/start/end`` because prompt
        snippets are span-oriented. For fan-out expansion, recover the graph
        vertex whose range overlaps that span and use its ``(file, leaf)`` key.
        """
        seeds: List[Tuple[str, str]] = []
        seen: set[Tuple[str, str]] = set()
        for c in candidates or []:
            nf = normalize_file_path(c.get("file"))
            if not nf:
                continue
            try:
                start = int(c.get("start") or c.get("start_line"))
                end = int(c.get("end") or c.get("end_line") or start)
            except (TypeError, ValueError):
                continue
            for idx in self.file_to_idx.get(nf, []):
                sp = self.idx_to_span.get(idx)
                if not sp or not _overlaps(start, end, sp[1], sp[2]):
                    continue
                leaf = _leaf(sp[3])
                key = (sp[0], leaf)
                if leaf and key not in seen:
                    seen.add(key)
                    seeds.append(key)
                    if len(seeds) >= max_seeds:
                        return seeds
                break
        return seeds

    def definitions_for_symbols(
        self, symbols: List[str], max_nodes: int = 5
    ) -> List[Dict[str, Any]]:
        """Definition spans for exact symbol leaf hints from read output."""
        out: List[Dict[str, Any]] = []
        deferred: List[Dict[str, Any]] = []
        seen: set[Tuple[str, int, int, str]] = set()
        lower_index: Optional[Dict[str, List[int]]] = None

        def _candidate(idx: int) -> Optional[Dict[str, Any]]:
            sp = self.idx_to_span.get(idx)
            if not sp:
                return None
            return {
                "file": sp[0],
                "start_line": sp[1],
                "end_line": sp[2],
                "name": sp[3],
            }

        def _rank(idx: int, leaf: str) -> Tuple[int, int, str, int]:
            sp = self.idx_to_span.get(idx)
            if not sp:
                return (99, 0, "", 0)
            file, start, end, name = sp
            span_len = max(0, int(end) - int(start))
            exact_nodes_def = file.endswith("nodes.rs") and str(name).endswith(
                f"/nodes/{leaf}"
            )
            if exact_nodes_def:
                tier = 0
            elif file.endswith("nodes.rs"):
                tier = 1
            else:
                tier = 2
            return (tier, span_len, file, int(start))

        def _append(target: List[Dict[str, Any]], candidate: Dict[str, Any]) -> bool:
            key = (
                candidate["file"],
                candidate["start_line"],
                candidate["end_line"],
                candidate["name"],
            )
            if key in seen:
                return False
            seen.add(key)
            target.append(candidate)
            return True

        for raw in symbols or []:
            leaf = _leaf(raw)
            if not leaf:
                continue
            idxs = self.leaf_to_idx.get(leaf)
            if not idxs:
                if lower_index is None:
                    lower_index = {}
                    for key, values in self.leaf_to_idx.items():
                        lower_index.setdefault(key.lower(), []).extend(values)
                idxs = lower_index.get(leaf.lower(), [])
            ranked = sorted(idxs, key=lambda idx: _rank(idx, leaf))
            first = True
            for idx in ranked:
                candidate = _candidate(idx)
                if not candidate:
                    continue
                target = out if first else deferred
                _append(target, candidate)
                first = False
                if len(out) >= max_nodes:
                    return out
        for candidate in deferred:
            out.append(candidate)
            if len(out) >= max_nodes:
                return out
        return out

    def route_for_symbols(
        self,
        symbols: List[str],
        *,
        max_nodes: int = 5,
        query: Optional[str] = None,
        pool_nodes: Optional[int] = None,
        neighbor_nodes: int = 12,
    ) -> List[Dict[str, Any]]:
        """Static-LSP route anchors for read-symbol scheduling.

        ``GraphNav`` is a lightweight igraph wrapper used by the synthesis
        harness, so the first implementation delegates to the same role-quota
        selector the scheduler already validates. This gives the live loop a
        named ``lsp_route`` operation and keeps the route contract consistent
        while the shared implementation is factored out.
        """
        from scripts.agent_compile.lib.harness import (
            scheduled_definition_candidates_for_symbols,
        )

        return scheduled_definition_candidates_for_symbols(
            self,
            symbols,
            max_nodes=max_nodes,
            query=query,
            pool_nodes=pool_nodes,
            neighbor_nodes=neighbor_nodes,
        )


def load_graph_nav(prebuilt_dir: str, instance_id: str) -> Optional[GraphNav]:
    path = os.path.join(prebuilt_dir, instance_id, "graph.pkl")
    if not os.path.exists(path):
        return None
    try:
        from scripts.agent_compile.lib.prebuilt import load_prebuilt_code_graph

        graph = load_prebuilt_code_graph(prebuilt_dir, instance_id)
    except Exception:  # noqa: BLE001 — missing/corrupt graph just disables verify
        return None
    g = getattr(graph, "graph", None)
    return GraphNav(g) if g is not None else None


@dataclass
class Verdict:
    ok: bool
    n_named: int
    n_resolved: int
    seeds: List[Tuple[str, str]] = field(default_factory=list)


def graph_verify(answer: str, nav: GraphNav) -> Verdict:
    """Is the committed answer anchored to real graph code?

    ok ⟺ the answer cites at least one symbol that resolves to a graph node, OR a
    structured ``Locations:`` span. Otherwise it is unanchored (drift/hallucination)
    and a graph-grounded retry is warranted. ``seeds`` = the resolved (file, leaf)
    keys, used as expansion roots.
    """
    named = _answer_symbol_names(answer)
    resolved: List[Tuple[str, str]] = []
    for raw in named:
        if ":" not in str(raw):
            continue
        file_part, sym = str(raw).split(":", 1)
        file = normalize_file_path(file_part)
        leaf = _leaf(sym)
        if file and leaf and nav.resolves(file, leaf):
            resolved.append((file, leaf))
    has_locations = bool(parse_answer_spans(answer))
    return Verdict(
        ok=bool(resolved) or has_locations,
        n_named=len(named),
        n_resolved=len(resolved),
        seeds=resolved,
    )


def _candidate_tags(candidate: Dict[str, Any]) -> str:
    tags = []
    if candidate.get("role"):
        tags.append(str(candidate["role"]))
    if candidate.get("source"):
        tags.append(str(candidate["source"]))
    if candidate.get("via"):
        tags.append(f"via {candidate['via']}")
    return f" [{'; '.join(tags)}]" if tags else ""


def render_expansion(candidates: List[Dict[str, Any]]) -> str:
    """Opening-prompt preamble injecting graph neighbours to verify against."""
    if not candidates:
        return ""
    lines = [
        "Related code (1-hop call-graph neighbours of the likely targets). Your "
        "previous answer did not resolve to a known symbol — re-examine these "
        "exact locations and commit a corrected answer:",
    ]
    for c in candidates:
        nm = c.get("name") or ""
        suffix = _candidate_tags(c)
        lines.append(
            f"- {c['file']}:{c['start_line']}-{c['end_line']} {nm}{suffix}".rstrip()
        )
    return "\n".join(lines)


def render_fanout_expansion(
    candidates: List[Dict[str, Any]], *, reason: str = "search_fanout"
) -> str:
    """Prompt preamble for proactive graph expansion under search pressure."""
    if not candidates:
        return ""
    lines = [
        "Related code (bounded 1-hop call-graph neighbours selected because the "
        f"previous search trace showed {reason}). Read only the relevant spans, "
        "then commit a final answer grounded in locations you actually read:",
    ]
    for c in candidates:
        nm = c.get("name") or ""
        suffix = _candidate_tags(c)
        lines.append(
            f"- {c['file']}:{c['start_line']}-{c['end_line']} {nm}{suffix}".rstrip()
        )
    return "\n".join(lines)


def render_lsp_route_context(
    candidates: List[Dict[str, Any]],
    *,
    reason: str = "read_symbols",
    symbols: Optional[List[str]] = None,
) -> str:
    """Prompt preamble for scheduled static-LSP route context."""
    if not candidates:
        return ""
    symbol_text = ""
    if symbols:
        symbol_text = " for " + ", ".join(symbols[:6])
    lines = [
        "Related route anchors (static graph/LSP route context selected "
        f"because the read trace showed {reason}{symbol_text}). These are "
        "candidate answer anchors, not just hints: read the relevant spans and "
        "use the role/via tags to preserve route order. Keep the first five "
        "comma-separated entries in the final `Locations:` line for route "
        "endpoints, bridge/factory definitions, "
        "provider/value definitions, and type/base-variant anchors; do not put "
        "small helper methods ahead of those core locations. The final answer "
        "must use plain `Files:`, `Symbols:`, and single-line `Locations:` "
        "contract lines, not markdown bullets:",
    ]
    if any(str(c.get("role") or "") == "type" for c in candidates):
        lines.append(
            "For type-heavy routes, type/base-variant definitions are evidence. "
            "If the question asks how a value is recognized as a tuple, list, "
            "set, name, call, or AST expression, reserve a final Locations slot "
            "for the relevant type/base-variant definition; do not cite only "
            "the rule or helper that pattern-matches it. Read the base type "
            "anchor first and only one named variant when needed; do not read "
            "every variant or chase semantic helpers unless the query asks for "
            "that layer. If a rule endpoint calls builtin or semantic-resolution "
            "helpers, cite the endpoint lines that call them instead of reading "
            "the semantic model implementation."
        )
    for c in candidates:
        nm = c.get("name") or ""
        suffix = _candidate_tags(c)
        lines.append(
            f"- {c['file']}:{c['start_line']}-{c['end_line']} {nm}{suffix}".rstrip()
        )
    return "\n".join(lines)


def render_lsp_definition_context(
    candidates: List[Dict[str, Any]],
    *,
    reason: str = "read_symbols",
    symbols: Optional[List[str]] = None,
) -> str:
    """Prompt preamble for scheduled static-LSP definition context."""
    if not candidates:
        return ""
    symbol_text = ""
    if symbols:
        symbol_text = " for " + ", ".join(symbols[:6])
    lines = [
        "Related definitions (static graph/LSP go-to-definition context selected "
        f"because the read trace showed {reason}{symbol_text}). These are "
        "candidate answer anchors, not just hints: read the relevant spans and, "
        "when they explain the requested traversal, include their exact ranges "
        "in the final Locations line instead of mentioning the symbols only in "
        "prose. Keep the first five comma-separated entries in the final "
        "`Locations:` line for route endpoints, "
        "bridge/factory definitions, and provider/value definitions; do not put "
        "small helper methods ahead of those core locations. Avoid exact "
        "file:line ranges for helper methods in explanatory prose unless they "
        "also belong in the final Locations shortlist. The final answer must "
        "use plain `Files:`, `Symbols:`, and single-line `Locations:` contract "
        "lines, not markdown bullets:",
    ]
    for c in candidates:
        nm = c.get("name") or ""
        suffix = _candidate_tags(c)
        lines.append(
            f"- {c['file']}:{c['start_line']}-{c['end_line']} {nm}{suffix}".rstrip()
        )
    return "\n".join(lines)


def expansion_seeds_from_candidates(
    preload_candidates: List[Dict[str, Any]],
) -> List[Tuple[str, str]]:
    """(file, leaf) seeds from pre-load candidates, for when the answer resolved
    nothing of its own — expand around what retrieval proposed."""
    seeds: List[Tuple[str, str]] = []
    for c in preload_candidates or []:
        f = normalize_file_path(c.get("file"))
        leaf = _leaf(c.get("name") or c.get("symbol"))
        if f and leaf:
            seeds.append((f, leaf))
    return seeds


def expansion_seeds_from_candidate_spans(
    nav: GraphNav,
    preload_candidates: List[Dict[str, Any]],
    *,
    max_seeds: int = 5,
) -> List[Tuple[str, str]]:
    """Best-effort graph seeds from span-only preload candidates."""
    return nav.seeds_from_spans(preload_candidates, max_seeds=max_seeds)
