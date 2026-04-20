"""
MCP tools for CodeMiner.

Each tool is a thin wrapper delegating to existing backbone APIs.
"""

__all__ = ["search_semantic"]

from .search import search_semantic
