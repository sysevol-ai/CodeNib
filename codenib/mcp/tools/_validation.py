# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared request bounds for MCP tools exposed to coding agents."""

from __future__ import annotations

MAX_TOOL_RESULTS = 100
MAX_TOOL_TEXT_CHARS = 16_000
MAX_GRAPH_DEPTH = 10
MAX_GRAPH_NODES = 200
MAX_ROUTE_SYMBOLS = 32


def bounded_int(
    value: int,
    *,
    name: str,
    minimum: int = 1,
    maximum: int = MAX_TOOL_RESULTS,
) -> int:
    """Return an integer within an explicit tool boundary."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}.")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def bounded_text(
    value: str,
    *,
    name: str,
    maximum: int = MAX_TOOL_TEXT_CHARS,
    strip: bool = True,
) -> str:
    """Normalize non-empty tool text while rejecting oversized requests."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string.")
    if not value.strip():
        raise ValueError(f"{name} must not be empty.")
    if len(value) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters.")
    return value.strip() if strip else value
