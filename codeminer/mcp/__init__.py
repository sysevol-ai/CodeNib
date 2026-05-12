"""CodeMiner MCP server - exposes backbone capabilities over stdio.

Provides MCP tools for semantic indexing, CodeGraph, and hybrid retrieval
(vector, BM25, regex, Zoekt trigram) for external agent frameworks.
"""

__all__ = ["ServerContext"]

from .context import ServerContext
