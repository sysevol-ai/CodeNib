# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""One-call, graph-aware context composer (the codegraph_context analogue).

The agent calls this ONCE with the task; the composer does the work
internally — search for entry-point symbols (bm25 + embedding), then
*deterministically* expand each along the call graph (callers + callees), then
return a compact, deduped, budget-capped set. This is the graph-aware harness
piece: the graph is used regardless of whether the model would have chosen a
graph tool itself, while staying token-cheap (names + file:line + relation, no
code bodies — the agent file_reads the few it needs).

Skill type is ``custom`` so the loader hands it the full ``contexts`` dict
(it needs both the ``retrieve`` and ``expand`` contexts).
"""

from __future__ import annotations

import os
import re
from itertools import zip_longest
from typing import Any, Callable, Dict, List

# Ablation knob (#133): set CODEMINER_COMPOSER_NO_GRAPH=1 to return only the
# search seeds (no call-graph expansion). Used to isolate how much the graph/LSP
# expansion contributes on top of plain bm25+embedding search.
_NO_GRAPH = os.environ.get("CODEMINER_COMPOSER_NO_GRAPH") == "1"

# A token is "code-ish" (a likely symbol/identifier the user named) if it has an
# underscore, an internal camelCase hump, or is an ALL_CAPS word (>=3). This
# excludes plain English Titlecase ("Null", "Array") that misleads bm25.
_CODEISH = re.compile(r"_|[a-z][A-Z]")
_BACKTICK = re.compile(r"`([^`\n]{2,60})`")
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
# Common bug-report ALL_CAPS noise that is never a code symbol.
_IDENT_STOP = {"BUG", "TODO", "FIXME", "NOTE", "XXX", "HACK", "WARNING", "ERROR"}


def _query_identifiers(query: str, limit: int = 4) -> List[str]:
    """Extract code-like identifiers the user explicitly named in the query.

    Bug reports usually mention the API/command/symbol involved (``LPOP``,
    ``popGenericCommand``, ``foo_bar``). bm25 over the full NL text latches onto
    literal English tokens; searching these identifiers directly seeds the graph
    from the right place even when the prose misleads plain search.
    """
    out: List[str] = []
    for m in _BACKTICK.findall(query or ""):
        out.append(m.strip())
    for tok in _TOKEN.findall(query or ""):
        if _CODEISH.search(tok) or (tok.isupper() and len(tok) >= 3):
            out.append(tok)
    seen: set = set()
    res: List[str] = []
    for t in out:
        t = t.strip("`.,():;").strip()
        if t and t.upper() not in _IDENT_STOP and t.lower() not in seen:
            seen.add(t.lower())
            res.append(t)
    return res[:limit]


def _interleave(*seqs: List[Any]) -> List[Any]:
    """Round-robin merge: seqs[0][0], seqs[1][0], seqs[0][1], ... (no source
    is allowed to crowd the others out of the seed budget)."""
    out: List[Any] = []
    for tup in zip_longest(*seqs):
        out.extend(x for x in tup if x is not None)
    return out


def _dedup_by_id(nodes: List[Any]) -> List[Any]:
    seen, out = set(), []
    for n in nodes:
        key = getattr(n, "node_id", None) or getattr(n, "node_name", None) or id(n)
        if key not in seen:
            seen.add(key)
            out.append(n)
    return out


def create_executor(contexts: Dict[str, Any]) -> Callable[..., List[Any]]:
    retrieve = (contexts or {}).get("retrieve")
    expand = (contexts or {}).get("expand")

    from ....ops.retrieve import to_queried_nodes
    from .._graphnav import _node_id, display_name, neighbors

    def _readable(graph: Any, node: Any) -> Any:
        """Relabel a search-seed node with its readable unified_name.

        Search indexes are keyed by the canonical identity name (a content
        hash for clang/SCIP-indexed C/C++). Surface the readable display so
        the agent can ground / grep / re-seed it.
        """
        if graph is None:
            return node
        nm = getattr(node, "node_name", None)
        if not nm:
            return node
        disp = display_name(graph, nm)
        if disp and disp != nm:
            node.node_name = disp
            node.node_id = _node_id(getattr(node, "file", None), disp)
        return node

    def execute(
        query: str,
        max_results: int = 30,
        seeds: int = 5,
        **kwargs: Any,
    ) -> List[Any]:
        seeds = max(1, int(seeds or 5))
        max_results = max(seeds, int(max_results or 30))

        # 1) SEARCH for entry-point symbols (compact; no bodies).
        #    Keep the two sources SEPARATE and INTERLEAVE them — concatenating
        #    bm25-then-embedding lets one source crowd out the other. On NL bug
        #    reports bm25 latches onto literal tokens (e.g. "null array" ->
        #    response-encoding constants) while embedding finds the actual edit
        #    site (the command handler); round-robin guarantees both are seeded.
        bm: List[Any] = []
        ve: List[Any] = []
        ident: List[Any] = []
        has_bm25 = retrieve is not None and getattr(retrieve, "bm25", None) is not None
        if has_bm25:
            bm = to_queried_nodes(
                retrieve.bm25.search(
                    query=query,
                    top_k=seeds * 2,
                    return_code_content=False,
                    wrap_with_ln=False,
                    filter_test=True,
                )
            )
            # Identifier seeding: bm25 each code-token the user named directly,
            # so the graph expands from the right symbol even when the NL prose
            # misleads the full-text query (the redis "null array" -> wrong-seed
            # miss). These lead the interleave — they are the most precise.
            for tok in _query_identifiers(query):
                ident += to_queried_nodes(
                    retrieve.bm25.search(
                        query=tok,
                        top_k=2,
                        return_code_content=False,
                        wrap_with_ln=False,
                        filter_test=True,
                    )
                )
        if retrieve is not None and getattr(retrieve, "vector_store", None) is not None:
            level = getattr(retrieve, "default_level", "l2")
            ve = to_queried_nodes(
                retrieve.vector_store.search(query=query, top_k=seeds * 2, level=level)
            )
        entry = _dedup_by_id(_interleave(ident, ve, bm))[:seeds]

        # 2) GRAPH-EXPAND each seed deterministically (callers + callees).
        #    Expand on the canonical (hash) name FIRST — it resolves exactly —
        #    then relabel the seed itself to its readable display.
        graph = getattr(expand, "code_graph", None) if expand is not None else None
        expanded: List[Any] = []
        if graph is not None and not _NO_GRAPH:
            per = max(2, (max_results - len(entry)) // max(1, len(entry)))
            for s in entry:
                name = getattr(s, "node_name", None)
                if not name:
                    continue
                try:
                    expanded += neighbors(graph, name, "both", top_k=per)
                except ValueError:
                    continue  # unresolved seed — skip, keep the rest

        results: List[Any] = [_readable(graph, s) for s in entry] + expanded
        return _dedup_by_id(results)[:max_results]

    return execute
