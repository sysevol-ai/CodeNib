# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""MCP-compatible query surface for multimodal repository knowledge."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

from .media_knowledge import (
    find_visual_code_links,
    get_visual_evidence,
    search_visual_context,
)

_MAX_QUERY_BYTES = 4096
_MAX_PATH_BYTES = 4096
_MAX_LIMIT = 20
_NONEMPTY_TEXT_PATTERN = r"^(?=.*\S)[^\u0000-\u001F\u007F]*$"
_OPTIONAL_TEXT_PATTERN = r"^[^\u0000-\u001F\u007F]*$"
_RELATIVE_PATH_PATTERN = (
    r"^(?=.*\S)(?!/)(?!.*//)(?!.*(?:^|/)\.\.?(?:/|$))(?!.*\/$)"
    r"[^\\\u0000-\u001F\u007F]+$"
)

_MULTIMODAL_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "name": "search_visual_context",
        "description": "Search repository visual artifacts, facts, and source bindings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": _MAX_QUERY_BYTES,
                    "pattern": _NONEMPTY_TEXT_PATTERN,
                    "description": (
                        "Nonempty query without control characters; the UTF-8 "
                        f"payload is limited to {_MAX_QUERY_BYTES} bytes."
                    ),
                },
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
            "properties": {
                "artifact_path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": _MAX_PATH_BYTES,
                    "pattern": _RELATIVE_PATH_PATTERN,
                    "description": (
                        "Canonical repository-relative POSIX path without control "
                        f"characters; limited to {_MAX_PATH_BYTES} UTF-8 bytes."
                    ),
                }
            },
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
                "source_path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": _MAX_PATH_BYTES,
                    "pattern": _RELATIVE_PATH_PATTERN,
                    "description": (
                        "Canonical repository-relative POSIX path without control "
                        f"characters; limited to {_MAX_PATH_BYTES} UTF-8 bytes."
                    ),
                },
                "symbol": {
                    "type": "string",
                    "maxLength": _MAX_PATH_BYTES,
                    "pattern": _OPTIONAL_TEXT_PATTERN,
                    "description": (
                        "Optional symbol without control characters; limited to "
                        f"{_MAX_PATH_BYTES} UTF-8 bytes."
                    ),
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT},
            },
            "required": ["source_path"],
            "additionalProperties": False,
        },
    },
)


def multimodal_tool_schemas() -> list[dict[str, Any]]:
    """Return independent mutable copies of the stable tool schemas."""

    return list(copy.deepcopy(_MULTIMODAL_TOOL_SCHEMAS))


@dataclass(frozen=True)
class MultimodalKnowledgeToolRouter:
    """Small tool router that mirrors the future MCP surface."""

    view: Mapping[str, Any]

    def tool_schemas(self) -> list[dict[str, Any]]:
        return multimodal_tool_schemas()

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if type(name) is not str:
            raise ValueError("unknown multimodal knowledge tool")
        if name == "search_visual_context":
            arguments = _arguments(arguments, allowed={"query", "limit"})
            query = _bounded_text(arguments.get("query"), label="query")
            limit = _limit(arguments.get("limit", 5))
            return {
                "results": search_visual_context(self.view, query, limit=limit),
            }
        if name == "get_visual_evidence":
            arguments = _arguments(arguments, allowed={"artifact_path"})
            artifact_path = _relative_path(
                arguments.get("artifact_path"),
                label="artifact_path",
            )
            return {"evidence": get_visual_evidence(self.view, artifact_path)}
        if name == "find_visual_code_links":
            arguments = _arguments(
                arguments,
                allowed={"source_path", "symbol", "limit"},
            )
            source_path = _relative_path(
                arguments.get("source_path"),
                label="source_path",
            )
            symbol = _bounded_text(
                arguments.get("symbol", ""),
                label="symbol",
                max_bytes=_MAX_PATH_BYTES,
                allow_empty=True,
            )
            limit = _limit(arguments.get("limit", _MAX_LIMIT))
            return {
                "links": find_visual_code_links(
                    self.view,
                    source_path,
                    symbol=symbol,
                )[:limit]
            }
        raise ValueError("unknown multimodal knowledge tool")


def _arguments(
    value: Any,
    *,
    allowed: set[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("multimodal tool arguments must be an object")
    if len(value) > len(allowed) or any(
        type(key) is not str or key not in allowed for key in value
    ):
        raise ValueError("multimodal tool arguments contain unexpected properties")
    return value


def _bounded_text(
    value: Any,
    *,
    label: str,
    max_bytes: int = _MAX_QUERY_BYTES,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a string")
    text = value.strip()
    if not text and not allow_empty:
        raise ValueError(f"{label} is required")
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError(f"{label} exceeds the byte limit")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
        raise ValueError(f"{label} contains control characters")
    return text


def _relative_path(value: Any, *, label: str) -> str:
    text = _bounded_text(value, label=label, max_bytes=_MAX_PATH_BYTES)
    if "\\" in text:
        raise ValueError(f"{label} must be a repository-relative path")
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != text
    ):
        raise ValueError(f"{label} must be a repository-relative path")
    return path.as_posix()


def _limit(value: Any) -> int:
    if type(value) is not int:
        raise ValueError("limit must be an integer")
    if not 1 <= value <= _MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_LIMIT}")
    return value


__all__ = ["MultimodalKnowledgeToolRouter", "multimodal_tool_schemas"]
