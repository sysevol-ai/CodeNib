# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared request bounds for MCP tools that return repository context."""

from __future__ import annotations

from typing import Any

MAX_TOOL_RESULTS = 100
MAX_GRAPH_DEPTH = 8
MAX_DEPENDENCY_EDGES = 2_000
MAX_ROUTE_SYMBOLS = 32
MAX_LSP_POSITION = 2**31 - 1
MAX_SOURCE_PATH_CHARS = 4_096
MAX_SOURCE_WINDOW_LINES = 200
MAX_SOURCE_CONTENT_CHARS = 16_000


def required_text(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty.")
    return value.strip()


def bounded_integer(
    value: Any,
    *,
    name: str,
    minimum: int = 1,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value
