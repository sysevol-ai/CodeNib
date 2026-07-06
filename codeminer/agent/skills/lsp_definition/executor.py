# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, List, Optional

if TYPE_CHECKING:
    from ....ops.expand import ExpandContext
    from ....types import QueriedNode


def create_executor(context: "ExpandContext") -> Callable[..., List["QueriedNode"]]:
    """Return a static-index implementation of textDocument/definition."""
    from ...lsp_provider import StaticLSPProvider

    def execute(
        file_path: Optional[str] = None,
        line: Optional[int] = None,
        character: Optional[int] = None,
        symbol: Optional[str] = None,
        top_k: int = 8,
        **kwargs: Any,
    ) -> List["QueriedNode"]:
        if context is None or context.code_graph is None:
            raise RuntimeError("Symbol graph not available")
        return StaticLSPProvider(context.code_graph).definition(
            file_path=file_path,
            line=line,
            character=character,
            symbol=symbol,
            top_k=int(top_k or 8),
        )

    return execute
