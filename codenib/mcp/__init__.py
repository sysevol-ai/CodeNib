# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CodeNib MCP server - exposes backbone capabilities over stdio.

Provides MCP tools for semantic indexing, CodeGraph, and hybrid retrieval
(vector, BM25, regex, Zoekt trigram) for external agent frameworks.
"""

__all__ = ["ServerContext"]

from .context import ServerContext
