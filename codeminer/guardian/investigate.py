# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Investigate step: attach ranked code-location evidence to a signal.

Phase 1 uses CodeMiner's deterministic cascade retrieval
(:class:`~codeminer.model.hybrid_retrieve_pipeline.HybridRetrievePipeline`) —
no LLM, no API keys. Given a hotspot, we ask the pipeline for the most relevant
nodes and map them into :class:`Evidence`. The pipeline is duck-typed (anything
with ``query(str, top_k=int) -> list[QueriedNode]``) so tests can inject a fake.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Protocol, Sequence

from ..log_utils import get_logger
from .signals import Hotspot

logger = get_logger(__name__)


@dataclass
class Evidence:
    """A single ranked code location supporting a finding."""

    file: str
    node_name: str
    type: str
    start_line: Optional[int]
    end_line: Optional[int]
    score: Optional[float]


class _Retriever(Protocol):
    """Minimal surface Guardian needs from a retrieval pipeline."""

    def query(self, query: str, top_k: Optional[int] = ...) -> Sequence: ...


def hotspot_query(hotspot: Hotspot) -> str:
    """Build a natural-language retrieval query from a hotspot path.

    Uses the file stem plus its parent directory so BM25 has identifier-like
    keywords to latch onto (e.g. ``"runner agent"`` for ``agent/runner.py``).
    """
    stem = os.path.splitext(os.path.basename(hotspot.path))[0]
    parent = os.path.basename(os.path.dirname(hotspot.path))
    keywords = " ".join(t for t in (stem, parent) if t)
    return f"code in {hotspot.path} ({keywords})"


def investigate_hotspot(
    hotspot: Hotspot,
    retriever: _Retriever,
    *,
    top_k: int = 5,
) -> List[Evidence]:
    """Return up to ``top_k`` evidence locations for a hotspot.

    Degrades gracefully: if retrieval raises (e.g. an index failed to build),
    we log and return an empty list rather than aborting the whole cycle.
    """
    query = hotspot_query(hotspot)
    try:
        nodes = retriever.query(query, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 — a bad query must not kill the cycle
        logger.warning("investigate_hotspot: retrieval failed for %s: %s", query, exc)
        return []

    evidence: List[Evidence] = []
    for n in nodes or []:
        evidence.append(
            Evidence(
                file=getattr(n, "file", None) or "",
                node_name=getattr(n, "node_name", "") or "",
                type=getattr(n, "type", "") or "",
                start_line=getattr(n, "start_line", None),
                end_line=getattr(n, "end_line", None),
                score=getattr(n, "score", None),
            )
        )
    return evidence
