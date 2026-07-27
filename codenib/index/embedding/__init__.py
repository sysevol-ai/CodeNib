# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Embedding module for CodeNib.
Provides vector storage and semantic search capabilities for code chunks.
"""

from __future__ import annotations

from typing import Any

from ..._lazy import exported_dir, load_export

_EXPORTS = {
    "CodeVectorStore": (
        "codenib.index.embedding.vector_store",
        "CodeVectorStore",
    ),
    "create_code_vector_store": (
        "codenib.index.embedding.vector_store",
        "create_code_vector_store",
    ),
    "build_hierarchical_vector_store": (
        "codenib.index.embedding.builders",
        "build_hierarchical_vector_store",
    ),
    "VectorStoreBuilder": (
        "codenib.index.embedding.builders",
        "VectorStoreBuilder",
    ),
}

__all__ = [
    "CodeVectorStore",
    "create_code_vector_store",
    "build_hierarchical_vector_store",
    "VectorStoreBuilder",
]


def __getattr__(name: str) -> Any:
    return load_export(globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return exported_dir(globals(), _EXPORTS)
