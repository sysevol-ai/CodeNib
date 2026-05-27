# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, Callable, List


def create_executor(context: Any) -> Callable[..., List[Any]]:
    """Transitive impact (callers) / dependencies (callees) over the call graph."""
    from ....graph.dependency import DependencyAnalyzer
    from ....types import QueriedNode

    def execute(
        symbol: str,
        direction: str = "impact",
        max_depth: int = 3,
        **kwargs: Any,
    ) -> List[Any]:
        if getattr(context, "code_graph", None) is None:
            raise RuntimeError("Symbol graph not available")
        analyzer = DependencyAnalyzer(context.code_graph)
        depth = int(max_depth or 3)
        if str(direction).lower().startswith("dep"):
            result = analyzer.dependencies(symbol, max_depth=depth)
        else:
            result = analyzer.impact(symbol, max_depth=depth)
        if not result.nodes and result.note:
            # unresolved/ambiguous symbol — surface candidates so the agent re-seeds
            raise ValueError(result.note)
        out: List[Any] = []
        for n in result.nodes:
            out.append(
                QueriedNode(
                    node_name=n.name,
                    type=n.kind,
                    file=n.file,
                    start_line=n.line,
                    node_id=f"{n.file}:{n.name}" if n.file else n.name,
                    score=1.0,
                    content=f"depth-{n.depth} {result.direction} of {result.root}",
                )
            )
        return out

    return execute
