# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Draft sources for speculative decoding.

A drafter proposes a :class:`~codenib.serving.types.DraftTree` of candidate tokens for
the target model to verify. CodeNib serving ships three:

* :class:`~codenib.serving.drafter.copy.CopyDrafter` — exact repeated spans (CopySpec).
* :class:`~codenib.serving.drafter.retrieval.RetrievalDrafter` — retrieved code (RASD).
* :class:`~codenib.serving.drafter.suffix_automaton.SuffixAutomatonDrafter` — exact
  longest-suffix match over context + corpus (SAM-Decoding / SuffixDecoding).
* fusion via :func:`~codenib.serving.drafter.fusion.fuse` — merge several draft trees.
"""

from codenib.serving.drafter.base import Drafter
from codenib.serving.drafter.copy import (
    DEFAULT_MAX_MATCH,
    DEFAULT_MIN_MATCH,
    CopyDrafter,
)
from codenib.serving.drafter.fusion import fuse
from codenib.serving.drafter.retrieval import (
    DEFAULT_RETRIEVAL_K,
    CodeNibBackend,
    NgramRetrievalBackend,
    RetrievalBackend,
    RetrievalDrafter,
)
from codenib.serving.drafter.suffix_automaton import SuffixAutomatonDrafter

__all__ = [
    "Drafter",
    "CopyDrafter",
    "RetrievalDrafter",
    "RetrievalBackend",
    "NgramRetrievalBackend",
    "CodeNibBackend",
    "SuffixAutomatonDrafter",
    "fuse",
    "DEFAULT_MIN_MATCH",
    "DEFAULT_MAX_MATCH",
    "DEFAULT_RETRIEVAL_K",
]
