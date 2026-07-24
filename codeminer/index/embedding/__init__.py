# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Embedding module for CodeNib.
Provides vector storage and semantic search capabilities for code chunks.
"""

from .builders import VectorStoreBuilder, build_hierarchical_vector_store
from .vector_store import CodeVectorStore, create_code_vector_store

__all__ = [
    "CodeVectorStore",
    "create_code_vector_store",
    "build_hierarchical_vector_store",
    "VectorStoreBuilder",
]
