# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, Callable, List


def create_executor(context: Any) -> Callable[..., List[Any]]:
    """Factory: returns a callable that performs BM25 search.

    Parameters
    ----------
    context:
        A ``RetrieveContext`` instance (from ``codeminer.ops.retrieve``)
        that carries the backing ``BM25CodeIndexer``.
    """

    def execute(query: str, top_k: int = 20, **kwargs: Any) -> List[Any]:
        if context.bm25 is None:
            raise RuntimeError("BM25 index not available")

        filter_test = kwargs.get("filter_test", False)
        return_content = kwargs.get("return_content", True)
        wrap_with_ln = kwargs.get("wrap_with_line_numbers", True)

        results = context.bm25.search(
            query=query,
            top_k=top_k,
            return_code_content=return_content,
            wrap_with_ln=wrap_with_ln,
            filter_test=filter_test,
        )
        return results

    return execute
