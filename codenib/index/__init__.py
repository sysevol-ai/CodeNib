# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Index-related components: sparse, dense, and regex."""

from __future__ import annotations

from typing import Any

from .._lazy import exported_dir, load_export

_EXPORTS = {
    "CodeVectorStore": (
        "codenib.index.embedding.vector_store",
        "CodeVectorStore",
    ),
    "create_code_vector_store": (
        "codenib.index.embedding.vector_store",
        "create_code_vector_store",
    ),
    "RegexNodeIndex": ("codenib.index.regex_idx", "RegexNodeIndex"),
    "BM25CodeIndexer": (
        "codenib.index.sparse_idx.bm25_index",
        "BM25CodeIndexer",
    ),
}

__all__ = [
    "CodeVectorStore",
    "create_code_vector_store",
    "RegexNodeIndex",
    "BM25CodeIndexer",
]


def __getattr__(name: str) -> Any:
    return load_export(globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return exported_dir(globals(), _EXPORTS)
