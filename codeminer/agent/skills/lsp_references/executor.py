# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, List, Optional

if TYPE_CHECKING:
    from ....ops.expand import ExpandContext
    from ....types import QueriedNode


def create_executor(context: "ExpandContext") -> Callable[..., List["QueriedNode"]]:
    """Return a static-index implementation of textDocument/references."""
    from ...lsp_graph import lsp_references

    def execute(
        file_path: Optional[str] = None,
        line: Optional[int] = None,
        character: Optional[int] = None,
        symbol: Optional[str] = None,
        include_declaration: bool = True,
        top_k: int = 40,
        **kwargs: Any,
    ) -> List["QueriedNode"]:
        if context is None or context.code_graph is None:
            raise RuntimeError("Symbol graph not available")
        return lsp_references(
            context.code_graph,
            file_path=file_path,
            line=line,
            character=character,
            symbol=symbol,
            include_declaration=bool(include_declaration),
            top_k=int(top_k or 40),
        )

    return execute
