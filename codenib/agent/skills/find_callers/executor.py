# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, List

if TYPE_CHECKING:
    from ....ops.expand import ExpandContext
    from ....types import QueriedNode


def create_executor(context: "ExpandContext") -> Callable[..., List["QueriedNode"]]:
    """Who calls this symbol? (incoming call-graph edges, compact)."""
    from .._graphnav import neighbors

    def execute(symbol: str, top_k: int = 40, **kwargs: Any) -> List["QueriedNode"]:
        if context is None or context.code_graph is None:
            raise RuntimeError("Symbol graph not available")
        return neighbors(context.code_graph, symbol, "callers", top_k=int(top_k or 40))

    return execute
