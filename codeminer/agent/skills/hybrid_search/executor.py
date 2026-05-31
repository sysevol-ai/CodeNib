# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from ....ops.retrieve import RetrieveContext
    from ....types import QueriedNode


def _get_score(item: Any) -> float:
    """Extract score from a result item, handling various object shapes."""
    if hasattr(item, "score"):
        return item.score or 0.0
    if isinstance(item, dict):
        return item.get("score") or 0.0
    return 0.0


def _get_key(item: Any) -> Tuple[str, str, Optional[int], Optional[int]]:
    """Build a deduplication key from a result item."""
    if hasattr(item, "file"):
        return (item.file, item.node_name, item.start_line, item.end_line)
    if isinstance(item, dict):
        return (
            item.get("file", ""),
            item.get("node_name", ""),
            item.get("start_line"),
            item.get("end_line"),
        )
    return ("", "", None, None)


def _with_score(item: Any, score: float, content_override: Optional[str] = None) -> Any:
    """Return a copy of *item* with an updated score (and optional content)."""
    update: Dict[str, Any] = {"score": score}
    if content_override is not None:
        update["content"] = content_override

    # Pydantic v2
    if hasattr(item, "model_copy"):
        return item.model_copy(update=update)
    # Pydantic v1
    if hasattr(item, "copy"):
        return item.copy(update=update)
    # Dict fallback
    if isinstance(item, dict):
        merged = dict(item)
        merged.update(update)
        return merged
    return item


def create_executor(context: "RetrieveContext") -> Callable[..., List["QueriedNode"]]:
    """Factory: returns a callable that performs weighted score fusion.

    Unlike the other retrieval executors, the hybrid executor does not call
    an index directly.  Instead it accepts pre-computed candidate lists from
    upstream retrievers and fuses them using weighted scoring.

    Parameters
    ----------
    context:
        A ``RetrieveContext`` instance.  Not directly used for index access
        but accepted for interface consistency with other executor factories.
    """

    def execute(
        candidates: List[List[Any]],
        top_k: int = 20,
        weights: Optional[List[float]] = None,
        **kwargs: Any,
    ) -> List["QueriedNode"]:
        if not candidates:
            return []

        # Default to uniform weights when unspecified or length-mismatched.
        effective_weights: List[float]
        if not weights or len(weights) != len(candidates):
            effective_weights = [1.0] * len(candidates)
        else:
            effective_weights = list(weights)

        accumulator: Dict[Tuple[str, str, Optional[int], Optional[int]], Any] = {}

        for weight, results in zip(effective_weights, candidates, strict=True):
            for rank, item in enumerate(results):
                base_score = _get_score(item)
                if base_score == 0.0:
                    # Reciprocal-rank fallback for unscored results.
                    base_score = 1.0 / (rank + 1)

                key = _get_key(item)
                weighted = weight * base_score

                if key not in accumulator:
                    accumulator[key] = _with_score(item, weighted)
                    continue

                # Merge: sum weighted scores, prefer non-None content.
                existing = accumulator[key]
                existing_score = _get_score(existing)
                merged_score = existing_score + weighted

                existing_content = (
                    getattr(existing, "content", None)
                    if hasattr(existing, "content")
                    else existing.get("content") if isinstance(existing, dict) else None
                )
                new_content = (
                    getattr(item, "content", None)
                    if hasattr(item, "content")
                    else item.get("content") if isinstance(item, dict) else None
                )
                content = existing_content or new_content

                accumulator[key] = _with_score(
                    existing, merged_score, content_override=content
                )

        merged = sorted(
            accumulator.values(),
            key=lambda node: _get_score(node),
            reverse=True,
        )

        return merged[:top_k]

    return execute
