# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, List

if TYPE_CHECKING:
    from ....ops.rerank import RerankContext
    from ....types import QueriedNode


def create_executor(context: "RerankContext") -> Callable[..., List["QueriedNode"]]:
    """Create an LLM rerank executor bound to the given RerankContext."""

    def execute(
        query: str, candidates: List[Any], top_k: int = 5, **kwargs: Any
    ) -> List["QueriedNode"]:
        agent = context.ensure_agent()
        candidate_top_k = kwargs.get("candidate_top_k", context.candidate_top_k)
        window_size = kwargs.get("window_size", context.window_size)
        window_step = kwargs.get("window_step", context.window_step)
        include_content = kwargs.get("return_content", False)

        nodes = candidates[:candidate_top_k] if candidate_top_k else candidates

        return agent.rerank_nodes(
            query=query,
            nodes=nodes,
            top_k=top_k,
            window_size=window_size,
            window_step=window_step,
            include_content=include_content,
        )

    return execute
