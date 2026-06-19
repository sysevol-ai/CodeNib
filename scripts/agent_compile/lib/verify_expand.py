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
import pickle
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from codeminer.eval.retrieval_eval import normalize_file_path, parse_answer_spans


def _leaf(name: Any) -> str:
    """Trailing symbol leaf, matching harness.build_symbol_span_index keys."""
    s = str(name or "").strip()
    if not s:
        return ""
    if ":" in s:
        s = s.rsplit(":", 1)[1]
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
        for v in graph.vs:
            a = v.attributes()
            f, s, e = a.get("file"), a.get("start_line"), a.get("end_line")
            if f is None or s is None or e is None:
                continue
            nf = normalize_file_path(f)
            if not nf:
                continue
            self.idx_to_span[v.index] = (nf, int(s) + 1, int(e) + 1, a.get("name"))
            for label in (a.get("name"), a.get("unified_name")):
                leaf = _leaf(label)
                if leaf:
                    self.key_to_idx.setdefault((nf, leaf), v.index)

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


def load_graph_nav(prebuilt_dir: str, instance_id: str) -> Optional[GraphNav]:
    path = os.path.join(prebuilt_dir, instance_id, "graph.pkl")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            bundle = pickle.load(f)
    except Exception:  # noqa: BLE001 — missing/corrupt graph just disables verify
        return None
    g = bundle.get("graph") if isinstance(bundle, dict) else None
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
        lines.append(f"- {c['file']}:{c['start_line']}-{c['end_line']} {nm}".rstrip())
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
