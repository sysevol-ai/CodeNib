# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""BM25 keyword/identifier search over indexed code nodes.

``custom`` skill: takes a typed :class:`ComposerContexts` bundle. The
``retrieve`` context carries the backing ``BM25CodeIndexer``; the optional
``expand`` context carries the symbol graph used to relabel raw content-hash
identity names to their readable ``unified_name`` (so the agent can navigate
and read by them). ``names_only=True`` returns compact NAME tags with no code
bodies — the LocAgent-style entry point that ``bm25_names`` used to provide.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, List

if TYPE_CHECKING:
    from ....types import QueriedNode
    from ..context import ComposerContexts


def create_executor(
    contexts: "ComposerContexts",
) -> Callable[..., List["QueriedNode"]]:
    """Factory: returns a callable that performs BM25 search."""
    from ....ops.retrieve import to_queried_nodes
    from .._graphnav import _node_id

    retrieve = contexts.retrieve
    expand = contexts.expand

    def execute(
        query: str,
        top_k: int = 20,
        names_only: bool = False,
        **kwargs: Any,
    ) -> List["QueriedNode"]:
        if retrieve is None or retrieve.bm25 is None:
            raise RuntimeError("BM25 index not available")

        filter_test = bool(kwargs.get("filter_test", False))
        if names_only:
            # Symbol NAME tags only — no bodies, no line-number wrapping.
            return_content = False
            wrap_with_ln = False
        else:
            return_content = bool(kwargs.get("return_content", True))
            wrap_with_ln = bool(kwargs.get("wrap_with_line_numbers", True))

        nodes = to_queried_nodes(
            retrieve.bm25.search(
                query=query,
                top_k=int(top_k or 20),
                return_code_content=return_content,
                wrap_with_ln=wrap_with_ln,
                filter_test=filter_test,
            )
        )

        # Relabel raw identity names (content-hash for SCIP/clang) to their
        # readable ``unified_name`` whenever a graph is available.
        graph = expand.code_graph if expand is not None else None
        for n in nodes:
            if names_only:
                n.content = None  # ensure no body leaks through
            nm = getattr(n, "node_name", None)
            if graph is not None and nm:
                disp = graph.display_name(nm)
                if disp and disp != nm:
                    n.node_name = disp
                    # ``disp`` (unified_name) is already ``file:Symbol``, so use
                    # _node_id, which skips re-prefixing the file (matching the
                    # codenib_context composer); a bare ``f"{f}:{disp}"`` would
                    # double-prefix to ``src/x.c:src/x.c:foo()``.
                    n.node_id = _node_id(getattr(n, "file", None), disp)
        return nodes

    return execute
