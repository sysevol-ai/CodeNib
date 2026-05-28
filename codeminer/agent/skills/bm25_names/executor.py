# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Compact BM25 that returns symbol NAME tags only (LocAgent-style entry).

``custom`` skill: takes the full contexts dict so it can use the retrieve
context's bm25 index AND the expand context's graph to relabel content-hash
identity names to their readable ``unified_name`` — the agent navigates and
reads by those names, so they must be readable. No code bodies are returned.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List


def create_executor(contexts: Dict[str, Any]) -> Callable[..., List[Any]]:
    retrieve = (contexts or {}).get("retrieve")
    expand = (contexts or {}).get("expand")

    from ....ops.retrieve import to_queried_nodes

    def execute(query: str, top_k: int = 20, **kwargs: Any) -> List[Any]:
        if retrieve is None or getattr(retrieve, "bm25", None) is None:
            raise RuntimeError("BM25 index not available")
        nodes = to_queried_nodes(
            retrieve.bm25.search(
                query=query,
                top_k=int(top_k or 20),
                return_code_content=False,  # NAME tags only — no bodies
                wrap_with_ln=False,
                filter_test=kwargs.get("filter_test", True),
            )
        )
        graph = getattr(expand, "code_graph", None) if expand is not None else None
        for n in nodes:
            n.content = None  # ensure no body leaks through
            nm = getattr(n, "node_name", None)
            if graph is not None and nm:
                disp = graph.display_name(nm)
                if disp and disp != nm:
                    n.node_name = disp
                    f = getattr(n, "file", None)
                    n.node_id = f"{f}:{disp}" if f else disp
        return nodes

    return execute
