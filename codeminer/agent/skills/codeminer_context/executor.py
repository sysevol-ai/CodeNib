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

from typing import Any, Callable, Dict, List


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
    from .._graphnav import neighbors

    def execute(
        query: str,
        max_results: int = 30,
        seeds: int = 5,
        **kwargs: Any,
    ) -> List[Any]:
        seeds = max(1, int(seeds or 5))
        max_results = max(seeds, int(max_results or 30))

        # 1) SEARCH for entry-point symbols (compact; no bodies).
        found: List[Any] = []
        if retrieve is not None and getattr(retrieve, "bm25", None) is not None:
            found += to_queried_nodes(
                retrieve.bm25.search(
                    query=query,
                    top_k=seeds * 2,
                    return_code_content=False,
                    wrap_with_ln=False,
                    filter_test=True,
                )
            )
        if retrieve is not None and getattr(retrieve, "vector_store", None) is not None:
            level = getattr(retrieve, "default_level", "l2")
            found += to_queried_nodes(
                retrieve.vector_store.search(query=query, top_k=seeds * 2, level=level)
            )
        entry = _dedup_by_id(found)[:seeds]

        # 2) GRAPH-EXPAND each seed deterministically (callers + callees).
        results: List[Any] = list(entry)
        graph = getattr(expand, "code_graph", None) if expand is not None else None
        if graph is not None:
            per = max(2, (max_results - len(entry)) // max(1, len(entry)))
            for s in entry:
                name = getattr(s, "node_name", None)
                if not name:
                    continue
                try:
                    results += neighbors(graph, name, "both", top_k=per)
                except ValueError:
                    continue  # unresolved seed — skip, keep the rest

        return _dedup_by_id(results)[:max_results]

    return execute
