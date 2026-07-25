# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

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
    *,
    fusion: str = "weighted",
    rrf_k: int = 60,
) -> List[QueriedNode]:
    """Merge multiple retrieval branches with weighted scoring or RRF."""
    return _merge_rankings(
        branches,
        weights=weights,
        top_k=top_k,
        fusion=fusion,
        rrf_k=rrf_k,
        key_fn=queried_node_key,
    )


def merge_file_rankings(
    branches: List[List[QueriedNode]],
    weights: Optional[List[float]] = None,
    top_k: Optional[int] = None,
    *,
    rrf_k: int = 60,
) -> List[QueriedNode]:
    """Fuse ranked branches by unique file using weighted RRF.

    Each branch contributes at most once per file. Stable relative file names
    embedded in ``node_id`` take precedence over machine-specific absolute
    paths, which keeps graph and vector artifacts comparable across snapshots.
    """
    unique_branches = [dedup_queried_files(branch) for branch in branches]
    return _merge_rankings(
        unique_branches,
        weights=weights,
        top_k=top_k,
        fusion="rrf",
        rrf_k=rrf_k,
        key_fn=queried_file_key,
    )


def _merge_rankings(
    branches: List[List[QueriedNode]],
    weights: Optional[List[float]],
    top_k: Optional[int],
    *,
    fusion: str,
    rrf_k: int,
    key_fn: Callable[[QueriedNode], Tuple[Any, ...]],
) -> List[QueriedNode]:
    if not branches:
        return []

    if not weights or len(weights) != len(branches):
        weights = [1.0] * len(branches)
    if rrf_k <= 0:
        raise ValueError("rrf_k must be positive.")

    normalized_fusion = _normalize_fusion(fusion)

    accumulator: Dict[Tuple[Any, ...], QueriedNode] = {}

    for weight, results in zip(weights, branches, strict=True):
        for rank, item in enumerate(results, start=1):
            key = key_fn(item)
            weighted = _fusion_score(
                item,
                rank=rank,
                weight=weight,
                fusion=normalized_fusion,
                rrf_k=rrf_k,
            )

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

    if top_k is not None:
        merged = merged[:top_k]
    return merged


def queried_node_key(item: QueriedNode) -> Tuple[Any, ...]:
    """Stable identity for deduping candidate nodes across pipeline stages."""
    node_id = (item.node_id or "").strip()
    if node_id:
        return ("node_id", node_id)
    return (
        "span",
        item.file,
        item.node_name,
        item.start_line,
        item.end_line,
    )


def queried_file_key(item: QueriedNode) -> Tuple[Any, ...]:
    """Stable file identity across relative graph and absolute index paths."""
    file_path = (item.file or "").strip().replace("\\", "/")
    node_id = (item.node_id or "").strip().replace("\\", "/")
    if node_id and ":" in node_id:
        node_file = node_id.split(":", 1)[0].removeprefix("./")
        if (
            node_file
            and file_path
            and (file_path == node_file or file_path.endswith("/" + node_file))
        ):
            return ("file", node_file)
    if file_path:
        return ("file", file_path)
    return ("node", *queried_node_key(item))


def dedup_queried_nodes(nodes: Sequence[QueriedNode]) -> List[QueriedNode]:
    """Deduplicate nodes while preserving first-seen rank order.

    Retrieval pipelines often concatenate dense hits with graph-expanded
    neighbors. The dense order is the ranking signal; duplicates should not
    consume multiple top-k slots. If a later duplicate carries code content and
    the first copy does not, keep the first rank but attach the content.
    """
    by_key: Dict[Tuple[Any, ...], int] = {}
    out: List[QueriedNode] = []
    for node in nodes:
        key = queried_node_key(node)
        existing_idx = by_key.get(key)
        if existing_idx is None:
            by_key[key] = len(out)
            out.append(node)
            continue
        existing = out[existing_idx]
        if not existing.content and node.content:
            out[existing_idx] = _with_score(
                existing,
                existing.score or 0.0,
                content_override=node.content,
            )
    return out


def dedup_queried_files(nodes: Sequence[QueriedNode]) -> List[QueriedNode]:
    """Keep the first representative node for each stable file identity."""
    seen: Set[Tuple[Any, ...]] = set()
    out: List[QueriedNode] = []
    for node in nodes:
        key = queried_file_key(node)
        if key in seen:
            continue
        seen.add(key)
        out.append(node)
    return out


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


def _fusion_score(
    item: QueriedNode,
    *,
    rank: int,
    weight: float,
    fusion: str,
    rrf_k: int,
) -> float:
    if fusion == "rrf":
        return weight / float(rrf_k + rank)

    base_score = item.score or 0.0
    if base_score == 0.0:
        base_score = 1.0 / rank
    return weight * base_score


def _normalize_fusion(value: str) -> str:
    normalized = (value or "weighted").strip().lower().replace("-", "_")
    aliases = {
        "linear": "weighted",
        "linear_combination": "weighted",
        "weighted_sum": "weighted",
        "none": "weighted",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"weighted", "rrf"}:
        raise ValueError("fusion must be 'weighted' or 'rrf'.")
    return normalized


def _dump_model(model: object) -> Dict[str, object]:
    if hasattr(model, "model_dump"):
        return dict(model.model_dump())
    if hasattr(model, "dict"):
        return dict(model.dict())
    raise TypeError(f"Object {model} does not support model dumping.")
