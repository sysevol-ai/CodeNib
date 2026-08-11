# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Configurable MCP tool visibility for agent-surface experiments."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from .explore_bounds import MAX_EXPLORE_CALL_RESULT_BYTES, call_tool_result_bytes

TOOL_SURFACE_FULL = "full"
TOOL_SURFACE_EXPLORE = "explore"
TOOL_SURFACES = frozenset({TOOL_SURFACE_FULL, TOOL_SURFACE_EXPLORE})
_EXPLORE_TOOLS = frozenset({"explore_context"})


def normalize_tool_surface(value: str | None) -> str:
    surface = (value or TOOL_SURFACE_FULL).strip().lower()
    if surface not in TOOL_SURFACES:
        raise ValueError(
            f"unknown MCP tool surface {value!r}; supported: {sorted(TOOL_SURFACES)}"
        )
    return surface


class ToolSurfaceMCPServer(MCPServer):
    """MCP server that can hide and reject tools outside an experiment arm."""

    def __init__(
        self, *args: Any, tool_surface: str = TOOL_SURFACE_FULL, **kwargs: Any
    ):
        super().__init__(*args, **kwargs)
        self._tool_surface = normalize_tool_surface(tool_surface)

    @property
    def tool_surface(self) -> str:
        return self._tool_surface

    def set_tool_surface(self, value: str) -> str:
        """Set the active surface and return its normalized name."""

        self._tool_surface = normalize_tool_surface(value)
        return self._tool_surface

    def _tool_is_visible(self, name: str) -> bool:
        return self._tool_surface == TOOL_SURFACE_FULL or name in _EXPLORE_TOOLS

    async def list_tools(self):
        tools = await super().list_tools()
        return [tool for tool in tools if self._tool_is_visible(tool.name)]

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None):
        if not self._tool_is_visible(name):
            raise ValueError(
                f"tool {name!r} is unavailable under the "
                f"{self._tool_surface!r} MCP tool surface"
            )
        result = await super().call_tool(name, arguments, context)
        if (
            name == "explore_context"
            and call_tool_result_bytes(result) > MAX_EXPLORE_CALL_RESULT_BYTES
        ):
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=(
                            "explore_context exceeded CodeNib's MCP response "
                            "safety limit; no oversized result was delivered"
                        ),
                    )
                ],
                is_error=True,
            )
        return result


__all__ = [
    "TOOL_SURFACE_EXPLORE",
    "TOOL_SURFACE_FULL",
    "TOOL_SURFACES",
    "ToolSurfaceMCPServer",
    "normalize_tool_surface",
]
