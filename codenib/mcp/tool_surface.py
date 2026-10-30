# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Configurable MCP tool visibility for agent-surface experiments."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from .context import IndexedQueryError
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
        self,
        *args: Any,
        tool_surface: str = TOOL_SURFACE_FULL,
        preload_explore: bool = False,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self._tool_surface = normalize_tool_surface(tool_surface)
        self._preload_explore = preload_explore

    @property
    def tool_surface(self) -> str:
        return self._tool_surface

    @property
    def preload_explore(self) -> bool:
        return self._preload_explore

    def set_tool_surface(self, value: str, *, preload_explore: bool = False) -> str:
        """Set the active surface and return its normalized name."""

        self._tool_surface = normalize_tool_surface(value)
        self._preload_explore = preload_explore
        return self._tool_surface

    def _tool_is_visible(self, name: str) -> bool:
        return self._tool_surface == TOOL_SURFACE_FULL or name in _EXPLORE_TOOLS

    async def list_tools(self):
        tools = await super().list_tools()
        visible = [tool for tool in tools if self._tool_is_visible(tool.name)]
        if self._tool_surface == TOOL_SURFACE_EXPLORE and self._preload_explore:
            # Claude Code may defer schemas behind ToolSearch. Request visibility
            # for this explicit single-entry mode without changing full mode or
            # mutating the SDK's retained tool definitions. Client settings win.
            visible = [
                tool.model_copy(
                    update={"meta": {**(tool.meta or {}), "anthropic/alwaysLoad": True}}
                )
                for tool in visible
            ]
        return visible

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None):
        if not self._tool_is_visible(name):
            raise ValueError(
                f"tool {name!r} is unavailable under the "
                f"{self._tool_surface!r} MCP tool surface"
            )
        try:
            result = await super().call_tool(name, arguments, context)
        except Exception as error:
            # MCP 2.3 masks raised tool exceptions. Return a protocol-level
            # tool failure for our known source/reload rejections instead of
            # losing the recovery action. The SDK wraps the original exception
            # in ToolError; keep all other errors on its existing error path.
            cause: BaseException | None = error
            seen: set[int] = set()
            while cause is not None and id(cause) not in seen:
                if isinstance(cause, IndexedQueryError):
                    return CallToolResult(
                        content=[TextContent(type="text", text=cause.public_message)],
                        is_error=True,
                    )
                seen.add(id(cause))
                cause = cause.__cause__
            raise
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
