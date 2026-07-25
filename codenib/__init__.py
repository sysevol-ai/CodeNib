# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Public CodeNib API.

Exports are resolved lazily so importing :mod:`codenib` or starting the CLI
does not initialize optional graph, embedding, and agent runtimes.
"""

from __future__ import annotations

from typing import Any

from ._lazy import exported_dir, load_export

_EXPORTS = {
    "LSIndexer": ("codenib.ls_router", "LSIndexer"),
    "CodeGraph": ("codenib.graph.code_graph", "CodeGraph"),
    "BM25CodeIndexer": (
        "codenib.index.sparse_idx.bm25_index",
        "BM25CodeIndexer",
    ),
    "KeywordExtractor": ("codenib.agent.extract_agent", "KeywordExtractor"),
    "CodeSearchEngine": ("codenib.search", "CodeSearchEngine"),
    "RerankAgent": ("codenib.agent.rerank_agent", "RerankAgent"),
    "CodeChunker": ("codenib.code_chunker", "CodeChunker"),
    "RepoChunkingConfig": ("codenib.code_chunker", "RepoChunkingConfig"),
    "RegexNodeIndex": ("codenib.index.regex_idx", "RegexNodeIndex"),
    "CodeVectorStore": (
        "codenib.index.embedding.vector_store",
        "CodeVectorStore",
    ),
    "create_code_vector_store": (
        "codenib.index.embedding.vector_store",
        "create_code_vector_store",
    ),
}

__all__ = [
    "LSIndexer",
    "CodeGraph",
    "BM25CodeIndexer",
    "KeywordExtractor",
    "CodeSearchEngine",
    "RerankAgent",
    "CodeChunker",
    "RepoChunkingConfig",
    "RegexNodeIndex",
    "CodeVectorStore",
    "create_code_vector_store",
]


def __getattr__(name: str) -> Any:
    return load_export(globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return exported_dir(globals(), _EXPORTS)
