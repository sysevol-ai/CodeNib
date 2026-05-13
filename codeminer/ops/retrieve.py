# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..index.embedding.vector_store import CodeVectorStore
from ..index.regex_idx.regex_idx import RegexNodeIndex
from ..index.sparse_idx.bm25_index import BM25CodeIndexer
from ..log_utils import get_logger
from ..types import NodeInfo, QueriedNode

logger = get_logger(__name__)


@dataclass
class RetrieveContext:
    """Handles backing indexes for retrieval operations."""

    bm25: Optional[BM25CodeIndexer] = None
    vector_store: Optional[CodeVectorStore] = None
    regex_index: Optional[RegexNodeIndex] = None
    default_top_k: int = 10
    default_level: str = "l2"  # "l0" (file skeletons) or "l2" (functions/methods)
    masks: Dict[str, Set[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers (used by skill executors and pipeline)
# ---------------------------------------------------------------------------


def to_queried_nodes(results: Sequence[object]) -> List[QueriedNode]:
    """Normalize retrieval results to QueriedNode list."""
    converted: List[QueriedNode] = []
    for rank, item in enumerate(results):
        if isinstance(item, QueriedNode):
            converted.append(item)
            continue
        if isinstance(item, NodeInfo):
            data = _dump_model(item)
            score = data.get("score") or 0.0
            if not score:
                score = 1.0 / (rank + 1)
            data["score"] = score
            converted.append(QueriedNode(**data))
            continue
        if isinstance(item, dict):
            data = dict(item)
            score = data.get("score") or 0.0
            if not score:
                score = 1.0 / (rank + 1)
            data["score"] = score
            data.setdefault("node_name", data.get("name", ""))
            data.setdefault("content", data.get("content"))
            converted.append(QueriedNode(**data))
            continue
        raise TypeError(f"Unsupported result type for normalization: {type(item)}")
    return converted


def merge_hybrid(
    branches: List[List[QueriedNode]],
    weights: Optional[List[float]] = None,
    top_k: Optional[int] = None,
) -> List[QueriedNode]:
    """Merge multiple retrieval branches with weighted scoring."""
    if not branches:
        return []

    if not weights or len(weights) != len(branches):
        weights = [1.0] * len(branches)

    accumulator: Dict[
        Tuple[Optional[str], str, Optional[int], Optional[int]], QueriedNode
    ] = {}

    for weight, results in zip(weights, branches, strict=True):
        for rank, item in enumerate(results):
            base_score = item.score or 0.0
            if base_score == 0.0:
                base_score = 1.0 / (rank + 1)

            key = (item.file, item.node_name, item.start_line, item.end_line)
            weighted = weight * base_score

            if key not in accumulator:
                accumulator[key] = _with_score(item, weighted)
            else:
                existing = accumulator[key]
                new_score = existing.score + weighted
                content = existing.content or item.content
                accumulator[key] = _with_score(existing, new_score, content)

    merged = sorted(
        accumulator.values(),
        key=lambda node: node.score,
        reverse=True,
    )

    if top_k:
        merged = merged[:top_k]
    return merged


def _with_score(
    item: QueriedNode, score: float, content_override: Optional[str] = None
) -> QueriedNode:
    update = {"score": score}
    if content_override is not None:
        update["content"] = content_override
    if hasattr(item, "model_copy"):
        return item.model_copy(update=update)
    if hasattr(item, "copy"):
        return item.copy(update=update)
    data = _dump_model(item)
    data.update(update)
    return QueriedNode(**data)


def _dump_model(model: object) -> Dict[str, object]:
    if hasattr(model, "model_dump"):
        return dict(model.model_dump())
    if hasattr(model, "dict"):
        return dict(model.dict())
    raise TypeError(f"Object {model} does not support model dumping.")
