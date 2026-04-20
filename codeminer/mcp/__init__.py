"""
MCP (Model Context Protocol) server implementation for CodeMiner.

Exposes CodeMiner's backbone capabilities (semantic indexing, CodeGraph,
hybrid retrieval) as MCP tools over stdio for external agent frameworks.
"""

__all__ = ["ServerContext"]

from .context import ServerContext
