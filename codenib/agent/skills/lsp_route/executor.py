# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, List, Optional

if TYPE_CHECKING:
    from ....ops.expand import ExpandContext
    from ....types import QueriedNode


def create_executor(context: "ExpandContext") -> Callable[..., List["QueriedNode"]]:
    """Return a semantic route-map implementation over the static graph."""
    from ...lsp_provider import resolve_lsp_provider

    def execute(
        symbols: List[str],
        query: Optional[str] = None,
        top_k: int = 12,
        include_neighbors: bool = True,
        **kwargs: Any,
    ) -> List["QueriedNode"]:
        return resolve_lsp_provider(context).route(
            symbols=list(symbols or []),
            query=query,
            top_k=int(top_k or 12),
            include_neighbors=bool(include_neighbors),
        )

    return execute
