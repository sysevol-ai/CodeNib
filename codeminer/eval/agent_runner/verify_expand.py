# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Graph-backed answer verification helpers for agent-runner evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from codeminer.eval.agent_runner.symbols import symbol_leaf
from codeminer.eval.retrieval_eval import normalize_file_path, parse_answer_spans


def _answer_symbol_names(answer: str) -> List[str]:
    """Names from the agent's ``Symbols:`` line."""
    try:
        from codeminer.eval.retrieval_eval import _answer_symbols

        return list(_answer_symbols(answer or ""))
    except Exception:  # noqa: BLE001 - parser is best-effort
        return []


class GraphNav:
    """1-hop neighbour navigation over a symbol graph.

    Holds the graph plus two small maps built once:

    * ``key_to_idx``: ``(normalized_file, leaf_symbol) -> vertex index``
    * ``idx_to_span``: ``vertex index -> (file, start_1based, end_1based, name)``
    """

    def __init__(self, graph: Any) -> None:
        self.g = graph
        self.key_to_idx: Dict[Tuple[str, str], int] = {}
        self.idx_to_span: Dict[int, Tuple[str, int, int, str]] = {}
        for vertex in graph.vs:
            attrs = vertex.attributes()
            file_path = attrs.get("file")
            start = attrs.get("start_line")
            end = attrs.get("end_line")
            if file_path is None or start is None or end is None:
                continue
            normalized_file = normalize_file_path(file_path)
            if not normalized_file:
                continue
            self.idx_to_span[vertex.index] = (
                normalized_file,
                int(start) + 1,
                int(end) + 1,
                attrs.get("name"),
            )
            for label in (attrs.get("name"), attrs.get("unified_name")):
                leaf = symbol_leaf(label)
                if leaf:
                    self.key_to_idx.setdefault((normalized_file, leaf), vertex.index)

    def resolves(self, file_path: str, leaf: str) -> bool:
        """Return true when ``file_path:leaf`` maps to a graph vertex."""
        return (normalize_file_path(file_path), leaf) in self.key_to_idx

    def neighbors(
        self, seeds: List[Tuple[str, str]], max_nodes: int = 10
    ) -> List[Dict[str, Any]]:
        """Return 1-hop symbol neighbours of ``(file, leaf)`` seed keys."""
        out: List[Dict[str, Any]] = []
        seen: set = set()
        for file_path, leaf in seeds:
            idx = self.key_to_idx.get((normalize_file_path(file_path), leaf))
            if idx is None:
                continue
            for neighbor_idx in self.g.neighbors(idx):
                span = self.idx_to_span.get(neighbor_idx)
                if not span:
                    continue
                key = (span[0], span[1], span[2])
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "file": span[0],
                        "start_line": span[1],
                        "end_line": span[2],
                        "name": span[3],
                    }
                )
                if len(out) >= max_nodes:
                    return out
        return out


@dataclass
class Verdict:
    """Graph anchoring result for a committed localization answer."""

    ok: bool
    n_named: int
    n_resolved: int
    seeds: List[Tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class VerifyExpandRun:
    """Result of an optional verify-expand retry."""

    result: Any
    triggered: bool = False
    resolved: Optional[int] = None


def graph_verify(answer: str, nav: GraphNav) -> Verdict:
    """Check whether an answer is anchored to graph-resolvable code.

    ``ok`` means the answer either cites at least one symbol that resolves to the
    graph or includes a structured ``Locations:`` span. ``seeds`` contains the
    resolved graph keys that can be used for a bounded expansion retry.
    """
    named = _answer_symbol_names(answer)
    resolved: List[Tuple[str, str]] = []
    for raw in named:
        if ":" not in str(raw):
            continue
        file_part, symbol = str(raw).split(":", 1)
        file_path = normalize_file_path(file_part)
        leaf = symbol_leaf(symbol)
        if file_path and leaf and nav.resolves(file_path, leaf):
            resolved.append((file_path, leaf))
    has_locations = bool(parse_answer_spans(answer))
    return Verdict(
        ok=bool(resolved) or has_locations,
        n_named=len(named),
        n_resolved=len(resolved),
        seeds=resolved,
    )


def render_expansion(candidates: List[Dict[str, Any]]) -> str:
    """Render graph-neighbour candidates as a retry prompt preamble."""
    if not candidates:
        return ""
    lines = [
        "Related code (1-hop call-graph neighbours of the likely targets). Your "
        "previous answer did not resolve to a known symbol - re-examine these "
        "exact locations and commit a corrected answer:",
    ]
    for candidate in candidates:
        name = candidate.get("name") or ""
        lines.append(
            f"- {candidate['file']}:{candidate['start_line']}-"
            f"{candidate['end_line']} {name}".rstrip()
        )
    return "\n".join(lines)


def expansion_seeds_from_candidates(
    candidates: List[Dict[str, Any]],
) -> List[Tuple[str, str]]:
    """Return ``(file, leaf)`` expansion seeds from candidate dictionaries."""
    seeds: List[Tuple[str, str]] = []
    for candidate in candidates or []:
        file_path = normalize_file_path(candidate.get("file"))
        leaf = symbol_leaf(candidate.get("name") or candidate.get("symbol"))
        if file_path and leaf:
            seeds.append((file_path, leaf))
    return seeds


def run_verify_expand(
    result: Any,
    *,
    query: str,
    nav: Optional[GraphNav],
    preload_candidates: List[Dict[str, Any]],
    rerun: Callable[[str], Any],
    enabled: bool = True,
    max_nodes: int = 10,
) -> VerifyExpandRun:
    """Optionally verify an answer and rerun with graph-neighbour context.

    This is evaluation-harness feedback, not a runtime default. It does not
    rewrite answers or score outputs; it only decides whether to make one
    bounded retry and returns both the final result and audit flags.
    """
    if not enabled or nav is None:
        return VerifyExpandRun(result=result)

    verdict = graph_verify(getattr(result, "answer", None) or "", nav)
    if verdict.ok:
        return VerifyExpandRun(result=result, resolved=verdict.n_resolved)

    seeds = verdict.seeds or expansion_seeds_from_candidates(preload_candidates)
    extra = render_expansion(nav.neighbors(seeds, max_nodes=max_nodes))
    if not extra:
        return VerifyExpandRun(result=result, resolved=verdict.n_resolved)

    return VerifyExpandRun(
        result=rerun(f"{query}\n\n{extra}"),
        triggered=True,
        resolved=verdict.n_resolved,
    )
