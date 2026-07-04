# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, List, Optional

if TYPE_CHECKING:
    from ....ops.expand import ExpandContext
    from ....types import QueriedNode


def create_executor(context: "ExpandContext") -> Callable[..., List["QueriedNode"]]:
    """Return a semantic route-map implementation over the static graph."""
    from .._graphnav import lsp_route

    def execute(
        symbols: List[str],
        query: Optional[str] = None,
        top_k: int = 12,
        include_neighbors: bool = True,
        **kwargs: Any,
    ) -> List["QueriedNode"]:
        if context is None or context.code_graph is None:
            raise RuntimeError("Symbol graph not available")
        return lsp_route(
            context.code_graph,
            symbols=list(symbols or []),
            query=query,
            top_k=int(top_k or 12),
            include_neighbors=bool(include_neighbors),
        )

    return execute
