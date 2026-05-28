# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, Callable, List


def create_executor(context: Any) -> Callable[..., List[Any]]:
    """Who calls this symbol? (incoming call-graph edges, compact)."""
    from .._graphnav import neighbors

    def execute(symbol: str, top_k: int = 40, **kwargs: Any) -> List[Any]:
        if context.code_graph is None:
            raise RuntimeError("Symbol graph not available")
        return neighbors(context.code_graph, symbol, "callers", top_k=int(top_k or 40))

    return execute
