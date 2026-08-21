# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""MCP-compatible query surface for multimodal repository knowledge."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .media_knowledge import (
    find_visual_code_links,
    get_visual_evidence,
    search_visual_context,
)

_MAX_QUERY_BYTES = 4096
_MAX_PATH_BYTES = 4096
_MAX_LIMIT = 20

MULTIMODAL_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "name": "search_visual_context",
        "description": "Search repository visual artifacts, facts, and source bindings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_visual_evidence",
        "description": "Return one visual evidence entry by repository-relative artifact path.",
        "input_schema": {
            "type": "object",
            "properties": {"artifact_path": {"type": "string"}},
            "required": ["artifact_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_visual_code_links",
        "description": "Find visual artifacts grounded to a source file and optional symbol.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string"},
                "symbol": {"type": "string"},
            },
            "required": ["source_path"],
            "additionalProperties": False,
        },
    },
)


@dataclass(frozen=True)
class MultimodalKnowledgeToolRouter:
    """Small tool router that mirrors the future MCP surface."""

    view: Mapping[str, Any]

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [dict(schema) for schema in MULTIMODAL_TOOL_SCHEMAS]

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise ValueError("multimodal tool arguments must be an object")
        if name == "search_visual_context":
            query = _bounded_text(arguments.get("query"), label="query")
            limit = _limit(arguments.get("limit", 5))
            return {
                "results": search_visual_context(self.view, query, limit=limit),
            }
        if name == "get_visual_evidence":
            artifact_path = _bounded_text(
                arguments.get("artifact_path"),
                label="artifact_path",
                max_bytes=_MAX_PATH_BYTES,
            )
            return {"evidence": get_visual_evidence(self.view, artifact_path)}
        if name == "find_visual_code_links":
            source_path = _bounded_text(
                arguments.get("source_path"),
                label="source_path",
                max_bytes=_MAX_PATH_BYTES,
            )
            symbol = _bounded_text(
                arguments.get("symbol", ""),
                label="symbol",
                max_bytes=_MAX_PATH_BYTES,
                allow_empty=True,
            )
            return {
                "links": find_visual_code_links(
                    self.view,
                    source_path,
                    symbol=symbol,
                )
            }
        raise ValueError(f"unknown multimodal knowledge tool: {name}")


def _bounded_text(
    value: Any,
    *,
    label: str,
    max_bytes: int = _MAX_QUERY_BYTES,
    allow_empty: bool = False,
) -> str:
    text = str(value or "").strip()
    if not text and not allow_empty:
        raise ValueError(f"{label} is required")
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError(f"{label} exceeds the byte limit")
    if any(ord(character) < 0x20 for character in text):
        raise ValueError(f"{label} contains control characters")
    return text


def _limit(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("limit must be an integer")
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if not 1 <= limit <= _MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_LIMIT}")
    return limit


__all__ = ["MULTIMODAL_TOOL_SCHEMAS", "MultimodalKnowledgeToolRouter"]
