# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Utility functions for evaluating retrieval outputs against labeled targets."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ..types import QueriedNode


def normalize_file_path(value: Optional[str]) -> Optional[str]:
    """Normalize file paths emitted by retrieval to canonical posix format."""
    if not value:
        return None
    normalized = str(Path(value).as_posix())
    return normalized.lstrip("./")


def normalize_symbol_identifier(value: Optional[str]) -> Optional[str]:
    """Normalize `file:Symbol` identifiers to ensure file portion matches the retrieved format."""
    if not value:
        return None
    if ":" not in value:
        return value
    file_part, symbol_part = value.split(":", 1)
    normalized_file = normalize_file_path(file_part)
    if normalized_file:
        return f"{normalized_file}:{symbol_part}"
    return value


def collect_targets(
    instance: Mapping[str, object], simplified_symbols: bool = True
) -> Tuple[List[str], List[str]]:
    """Aggregate and normalize file + symbol labels from a dataset instance.
    If simplified_symbols is True, use symbols_modified and symbols_deleted, exclude symbols_added.
    If simplified_symbols is False, use all symbols_modified, symbols_added and symbols_deleted.
    """
    target_files = instance.get("target_files") or []

    if simplified_symbols:
        # Use symbols_modified and symbols_deleted, exclude symbols_added
        target_symbols_raw = list(instance.get("symbols_modified") or [])
        target_symbols_raw.extend(instance.get("symbols_deleted") or [])
        # Filter out class nodes (symbols without parentheses)
        target_symbols = [s for s in target_symbols_raw if "(" in s]
    else:
        target_symbols: List[str] = []
        for key in ("symbols_modified", "symbols_added", "symbols_deleted"):
            target_symbols.extend(instance.get(key) or [])

    normalized_files = [
        path for path in (normalize_file_path(value) for value in target_files) if path
    ]
    normalized_symbols = [
        symbol
        for symbol in (normalize_symbol_identifier(value) for value in target_symbols)
        if symbol
    ]
    return normalized_files, normalized_symbols


def build_symbol_prediction(node: QueriedNode) -> Optional[str]:
    """Format a retrieved node into `file:node_name` from node_id."""
    # Use node_id (format: "file.py:symbol" or "file.py" for headers)
    if node.node_id and ":" in node.node_id:
        return normalize_symbol_identifier(node.node_id)
    return None


def extract_predictions(
    nodes: Sequence[QueriedNode],
) -> Tuple[List[str], List[str]]:
    """Extract unique files and symbols from retrieved nodes."""
    seen_files = set()
    unique_files_ordered = []
    for node in nodes:
        if node.node_id:
            file_path = (
                node.node_id.split(":")[0] if ":" in node.node_id else node.node_id
            )
            normalized = normalize_file_path(file_path)
            if normalized and normalized not in seen_files:
                seen_files.add(normalized)
                unique_files_ordered.append(normalized)

    normalized_symbols = [
        value
        for value in (build_symbol_prediction(node) for node in nodes)
        if value is not None
    ]

    return unique_files_ordered, normalized_symbols


def compute_metrics(
    predictions: Sequence[str],
    targets: Sequence[str],
) -> Dict[str, float]:
    """Compute accuracy (hit@K), precision, recall, and hit counts for a single scope."""
    hits = sum(1 for value in predictions if value in targets)
    # Use issubset logic: accuracy=1.0 only if ALL targets are in predictions
    accuracy = 1.0 if targets and set(targets).issubset(set(predictions)) else 0.0
    precision = hits / max(len(predictions), 1)
    recall = hits / max(len(targets), 1) if targets else 0.0
    return {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "hits": hits,
    }


def evaluate_predictions(
    nodes: Sequence[QueriedNode],
    target_files: Sequence[str],
    target_symbols: Sequence[str],
    ks: Sequence[int],
) -> Dict[str, Dict[int, Dict[str, float]]]:
    """Evaluate retrieved nodes against targets for multiple cutoffs."""
    unique_files_ordered, normalized_symbols = extract_predictions(nodes)

    metrics = {"files": {}, "symbols": {}}
    for k in ks:
        # File-level: evaluate against top-k unique files
        metrics["files"][k] = compute_metrics(unique_files_ordered[:k], target_files)
        # Symbol-level: evaluate against top-k nodes/symbols
        metrics["symbols"][k] = compute_metrics(normalized_symbols[:k], target_symbols)
    return metrics


def aggregate_metrics(
    aggregate: Dict[str, Dict[int, Dict[str, float]]],
    instance_metrics: Dict[str, Dict[int, Dict[str, float]]],
) -> None:
    """Accumulate per-instance metrics into a running aggregate."""
    for scope in ("files", "symbols"):
        for k, stats in instance_metrics[scope].items():
            scoped = aggregate.setdefault(scope, {}).setdefault(
                k, {"accuracy": 0.0, "precision": 0.0, "recall": 0.0, "hits": 0.0}
            )
            scoped["accuracy"] += stats["accuracy"]
            scoped["precision"] += stats["precision"]
            scoped["recall"] += stats["recall"]
            scoped["hits"] += stats["hits"]


def average_metrics(
    aggregate: Dict[str, Dict[int, Dict[str, float]]], count: int
) -> Dict[str, Dict[int, Dict[str, float]]]:
    """Convert accumulated metrics into dataset-level averages."""
    if count == 0:
        return aggregate
    averaged: Dict[str, Dict[int, Dict[str, float]]] = {}
    for scope, per_k in aggregate.items():
        averaged[scope] = {}
        for k, stats in per_k.items():
            averaged[scope][k] = {
                "accuracy": stats["accuracy"] / count,
                "precision": stats["precision"] / count,
                "recall": stats["recall"] / count,
                "avg_hits": stats["hits"] / count,
            }
    return averaged


def summarize_predictions(
    nodes: Sequence[QueriedNode],
    limit: Optional[int] = None,
) -> List[Dict[str, object]]:
    """Return a serializable view of ranked nodes for downstream inspection."""
    summary = []
    for rank, node in enumerate(nodes):
        if limit is not None and rank >= limit:
            break
        summary.append(
            {
                "rank": rank + 1,
                "node_name": node.node_name,
                "file": normalize_file_path(node.file),
                "score": node.score,
            }
        )
    return summary
