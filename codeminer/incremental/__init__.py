# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Incremental indexing layer for CodeMiner.

Provides patch-level change detection and selective re-embedding so that
only changed code chunks are re-embedded when the repository is updated.

Pipeline:
    git diff detection → changed files → per-file rechunk → incremental embedding update
"""

from .chunk_store import IncrementalChunkStore, VersionedChunk
from .embeddings_cache import EmbeddingsCache
from .git_diff import GitDiffDetector, RepoChanges
from .index_updater import IncrementalIndexUpdater, UpdateResult
from .state import IncrementalState

__all__ = [
    "GitDiffDetector",
    "RepoChanges",
    "IncrementalChunkStore",
    "VersionedChunk",
    "EmbeddingsCache",
    "IncrementalIndexUpdater",
    "UpdateResult",
    "IncrementalState",
]
