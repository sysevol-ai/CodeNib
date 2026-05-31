# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, List

if TYPE_CHECKING:
    from ....ops.expand import ExpandContext
    from ....types import QueriedNode


def create_executor(context: "ExpandContext") -> Callable[..., List["QueriedNode"]]:
    """Shortest call path from one symbol to another (compact hops)."""
    from .._graphnav import trace as _trace

    def execute(
        from_symbol: str, to_symbol: str, max_hops: int = 10, **kwargs: Any
    ) -> List["QueriedNode"]:
        if context is None or context.code_graph is None:
            raise RuntimeError("Symbol graph not available")
        return _trace(
            context.code_graph, from_symbol, to_symbol, max_hops=int(max_hops or 10)
        )

    return execute
