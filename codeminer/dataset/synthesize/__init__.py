# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Query synthesis tools for SWE-bench multiplexed indexing."""

from codeminer.dataset.utils import (
    CodeLocation,
    CodeSearchDataset,
    CodeSearchQuery,
    GroundTruth,
    QueryType,
    get_prompt_for_query_type,
)

from ._types import QuerySynthesisResult
from .vocab_guard import (
    DEFAULT_OVERLAP_THRESHOLD,
    VocabOverlapResult,
    VocabularyOverlapGuard,
    gt_identifier_tokens,
)

__all__ = [
    "ClaudeQuerySynthesizer",
    "ContextLoader",
    "QueryCurator",
    "QuerySynthesisResult",
    "Verifier",
    "VocabularyOverlapGuard",
    "VocabOverlapResult",
    "gt_identifier_tokens",
    "DEFAULT_OVERLAP_THRESHOLD",
    "CodeLocation",
    "CodeSearchDataset",
    "CodeSearchQuery",
    "GroundTruth",
    "QueryType",
    "get_prompt_for_query_type",
]


def __getattr__(name: str):
    """Lazy-load synthesis helpers that require optional Claude SDK deps."""
    if name == "ClaudeQuerySynthesizer":
        from .query_synthesizer import ClaudeQuerySynthesizer

        return ClaudeQuerySynthesizer
    if name == "ContextLoader":
        from .context_loader import ContextLoader

        return ContextLoader
    if name == "QueryCurator":
        from .query_curator import QueryCurator

        return QueryCurator
    if name == "Verifier":
        from .verifier import Verifier

        return Verifier
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
