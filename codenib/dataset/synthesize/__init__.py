# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Query synthesis tools for SWE-bench multiplexed indexing.

Heavy synthesis components depend on the optional Claude Agent SDK. Keep package
import lightweight so tests and utility modules can import ``_types`` /
``vocab_guard`` without installing that optional runtime.
"""

from importlib import import_module
from typing import TYPE_CHECKING

from codenib.dataset.utils import (
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

if TYPE_CHECKING:  # pragma: no cover - imported only by static analyzers
    from .context_loader import ContextLoader
    from .query_curator import QueryCurator
    from .query_synthesizer import ClaudeQuerySynthesizer
    from .verifier import Verifier

_LAZY_EXPORTS = {
    "ClaudeQuerySynthesizer": ".query_synthesizer",
    "ContextLoader": ".context_loader",
    "QueryCurator": ".query_curator",
    "Verifier": ".verifier",
}


def __getattr__(name: str):
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(_LAZY_EXPORTS[name], __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


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
