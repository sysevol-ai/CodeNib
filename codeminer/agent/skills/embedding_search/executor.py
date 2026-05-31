# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, List, Optional, Set

if TYPE_CHECKING:
    from ....ops.retrieve import RetrieveContext
    from ....types import QueriedNode


def create_executor(context: "RetrieveContext") -> Callable[..., List["QueriedNode"]]:
    """Factory: returns a callable that performs vector embedding search.

    Parameters
    ----------
    context:
        A ``RetrieveContext`` instance (from ``codeminer.ops.retrieve``)
        that carries the backing ``CodeVectorStore``.
    """

    def execute(query: str, top_k: int = 20, **kwargs: Any) -> List["QueriedNode"]:
        if context is None or context.vector_store is None:
            raise RuntimeError("Vector store not available")
        store = context.vector_store

        level: str = kwargs.get("level", context.default_level)
        return_content: bool = kwargs.get("return_content", True)
        score_threshold: Optional[float] = kwargs.get("score_threshold")
        mask_name: Optional[str] = kwargs.get("mask_name")

        mask_ids: Optional[Set[str]] = None
        if mask_name:
            mask_ids = context.masks.get(mask_name)

        if return_content:
            results = store.search_with_content(
                query=query,
                top_k=top_k,
                level=level,
                mask_node_ids=mask_ids,
                score_threshold=score_threshold,
            )
        else:
            results = store.search(
                query=query,
                top_k=top_k,
                level=level,
                mask_node_ids=mask_ids,
                score_threshold=score_threshold,
            )

        return results

    return execute
