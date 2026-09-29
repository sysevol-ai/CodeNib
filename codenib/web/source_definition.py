# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""The definition a source slice starts inside.

A slice around a call site or a marked line begins in the middle of a
function. Readers need that function's signature to know where they are, so
the slice carries it as ``definition`` (1-based lines) and the frontend draws
it above a gap row.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Optional

from ..types import NODE_TYPE_CLASS, NODE_TYPE_FUNCTION, NODE_TYPE_METHOD
from ..wiki.source_excerpt import MERGE_GAP, signature_span

_KINDS = {NODE_TYPE_CLASS, NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
# Leading comments plus a long multi-line signature fit in this many lines.
_HEAD_LINES = 12


def attach_definition(
    result: dict[str, Any],
    graph: Any,
    read_lines: Callable[[int, int], Optional[Sequence[str]]],
) -> dict[str, Any]:
    """Return *result* with the signature of the definition it starts inside.

    *result* is a 1-based source slice; *read_lines(start, end)* returns the
    same file's lines, 1-based and inclusive. The graph's spans are 0-based.
    When the signature ends within :data:`MERGE_GAP` lines of the slice, the
    slice is widened to begin at the signature instead of drawing a gap.
    """

    start = result.get("start_line")
    if graph is None or not isinstance(start, int) or start <= 1:
        return result
    try:
        found = graph.query_range(result["file"], start - 1, start - 1)
    except Exception:  # noqa: BLE001 - a missing file or span keeps the slice
        return result
    definitions = [node for node in found.defined if node.kind in _KINDS]
    # A slice that opens on a definition (or its decorators) already shows it.
    if any(0 <= node.start_line - (start - 1) <= MERGE_GAP for node in definitions):
        return result
    enclosing = [node for node in definitions if node.start_line < start - 1]
    if not enclosing:
        return result
    node = max(enclosing, key=lambda item: item.start_line)
    first = node.start_line + 1
    head = read_lines(first, min(first + _HEAD_LINES, start) - 1) or []
    offset, length = signature_span(head)
    if not length:
        return result
    signature_first = first + offset
    signature_last = signature_first + length - 1
    if start - signature_last - 1 <= MERGE_GAP:
        before = read_lines(signature_first, start - 1) or []
        return {
            **result,
            "start_line": signature_first,
            "content": "".join(f"{line}\n" for line in before) + result["content"],
        }
    return {
        **result,
        "definition": {
            "start_line": signature_first,
            "end_line": signature_last,
            "content": "".join(f"{line}\n" for line in head[offset : offset + length]),
        },
    }
