# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent-boundary line-numbering conversion (issue #153).

Internal representations are uniformly **0-based**: BM25 docs, FAISS
metadata, symbol-graph anchors, and the tree-sitter ``CodeChunk`` all count
lines from 0. The *agent boundary* is **1-based outward** — line numbers
shown to, and accepted back from, the LLM are 1-based, mirroring the
``CodeLocation`` convention already used at the dataset/HuggingFace
boundary (see ``_chunk_to_code_block`` in ``dataset/gt_locate.py``, which
does the same ``+1`` once).

Without a single conversion site there is a silent 0/1 split *inside one
result*: a ``bm25_search`` hit renders its ``content`` gutter 1-based while
its structured ``start_line`` stays 0-based, so an LLM that reads "line 42"
from the snippet and feeds it back as a 0-based seed hits an off-by-one
that never raises.

Two helpers, one conversion each:

- :func:`to_agent_repr` — serialize a result node for the LLM (``+1``).
- :func:`from_agent_repr` — parse a line number coming back (``-1``).

Internals stay 0-based; only these two functions touch the offset.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# The LLM sees 1-based lines; every internal index is 0-based.
AGENT_LINE_OFFSET = 1

# Structured fields carrying a line number on a result node.
_LINE_FIELDS = ("start_line", "end_line")


def is_line_bearing(obj: Any) -> bool:
    """Whether *obj* is a result node that carries line-number fields.

    Used to decide whether :func:`to_agent_repr` should be applied during
    serialization; non-node results (bare strings, scores, dicts without
    line fields) are left untouched.
    """
    if isinstance(obj, dict):
        return "start_line" in obj or "end_line" in obj
    return hasattr(obj, "start_line") or hasattr(obj, "end_line")


def to_agent_repr(node: Any) -> Dict[str, Any]:
    """Serialize a result node to a 1-based dict for the LLM.

    Accepts a pydantic ``NodeInfo`` / ``QueriedNode``, a plain object with
    ``__dict__``, or a mapping. Returns a plain dict with ``start_line`` /
    ``end_line`` shifted ``+1`` (0-based → 1-based). ``None`` line values
    are preserved as ``None``; all other fields pass through unchanged.
    """
    data = _as_dict(node)
    for fld in _LINE_FIELDS:
        val = data.get(fld)
        # bool is an int subclass — exclude it defensively.
        if isinstance(val, int) and not isinstance(val, bool):
            data[fld] = val + AGENT_LINE_OFFSET
    return data


def from_agent_repr(line: Optional[int]) -> Optional[int]:
    """Convert a single 1-based line number from the LLM to 0-based.

    ``None`` passes through. A malformed seed (the LLM emitting ``0`` or a
    negative, which is not a valid 1-based line) is clamped at ``0`` so it
    can never produce a negative internal index.
    """
    if line is None:
        return None
    converted = int(line) - AGENT_LINE_OFFSET
    return converted if converted >= 0 else 0


def from_agent_repr_arg(value: Any) -> Any:
    """Apply :func:`from_agent_repr` across a whole tool argument.

    Handles the shapes a line-bearing skill argument can take:

    - a scalar line number,
    - a list/tuple of line numbers,
    - a list of ``[start, end]`` pairs, or
    - ``{"start_line": ..., "end_line": ...}`` range dicts.

    Any other shape passes through unchanged. This is the input-side
    counterpart wired into :class:`AgentRunner` for skill inputs marked
    ``is_line_number`` (e.g. ``graph_expand.ranges``, ``file_read``).
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return from_agent_repr(value)
    if isinstance(value, (list, tuple)):
        return type(value)(from_agent_repr_arg(v) for v in value)
    if isinstance(value, dict):
        out = dict(value)
        for fld in _LINE_FIELDS:
            if isinstance(out.get(fld), int) and not isinstance(out.get(fld), bool):
                out[fld] = from_agent_repr(out[fld])
        return out
    return value


def _as_dict(node: Any) -> Dict[str, Any]:
    """Best-effort conversion of a result node to a mutable dict."""
    if isinstance(node, dict):
        return dict(node)
    if hasattr(node, "model_dump"):
        return node.model_dump(exclude_none=True)
    if hasattr(node, "__dict__"):
        return dict(node.__dict__)
    return {"value": node}
