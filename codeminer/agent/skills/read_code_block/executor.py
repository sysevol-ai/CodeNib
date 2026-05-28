# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, Callable


def create_executor(context: Any) -> Callable[..., str]:
    """Read a symbol's code block by its graph node (LocAgent-style, no file_read).

    Resolves *symbol* to its canonical node, then reads the node's line span from
    the file (relative to the process cwd, which the runner sets to the repo).
    """

    def execute(symbol: str, **kwargs: Any) -> str:
        graph = getattr(context, "code_graph", None)
        if graph is None:
            raise RuntimeError("Symbol graph not available")
        canonical, cands = graph.resolve_symbol(symbol)
        if canonical is None:
            raise ValueError(
                f"symbol {symbol!r} unresolved"
                + (f"; candidates: {cands}" if cands else " in the code graph")
            )
        info = graph.get_node_info_by_name(canonical) or {}
        disp = info.get("unified_name") or canonical
        path = info.get("file")
        if not path:
            raise ValueError(f"{disp} has no associated file")
        try:
            with open(path, "r", errors="replace") as fh:
                lines = fh.readlines()
        except FileNotFoundError:
            raise ValueError(f"file not found for {disp}: {path}") from None

        start = info.get("start_line")
        end = info.get("end_line")
        if start:  # mirror CodeGraph.get_node_content's 1-based span slice
            body = "".join(lines[max(0, start - 1) : (end or start)])
            header = f"{disp}  [{path}:{start}-{end or start}]"
        else:
            body = "".join(lines[:80])
            header = f"{disp}  [{path}]"
        return f"{header}\n{body}"

    return execute
