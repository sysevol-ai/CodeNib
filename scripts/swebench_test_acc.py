#!/usr/bin/env python

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

import argparse
import json
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Set


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recompute top-k symbol accuracy for retrieval results."
    )
    parser.add_argument(
        "--results",
        type=Path,
        required=True,
        help="Path to evaluator JSON (e.g., nomic_retrieval_only_new_eval.json).",
    )
    parser.add_argument(
        "--k",
        type=int,
        nargs="*",
        help="Optional list of k values to evaluate. "
        "Defaults to the symbol metric keys or list length.",
    )
    return parser.parse_args()


def load_entries(path: Path) -> List[dict]:
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, list):
        raise ValueError(f"Expected list at top level, got {type(data).__name__}")
    return data


def collect_default_ks(entries: Sequence[dict]) -> List[int]:
    ks: Set[int] = set()
    for entry in entries:
        metrics = entry.get("metrics", {}).get("symbols", {})
        for key in metrics.keys():
            try:
                ks.add(int(key))
            except (TypeError, ValueError):
                continue
    if not ks:
        max_len = max(
            (len(entry.get("metric_k_node_ids", [])) for entry in entries), default=0
        )
        if max_len > 0:
            ks.add(max_len)
    return sorted(k for k in ks if k > 0)


def normalize_symbol(symbol: str) -> str:
    return symbol.strip()


def compute_metrics(
    predictions: Sequence[str], targets: Sequence[str]
) -> Dict[str, float]:
    """Compute accuracy (hit@k), precision, recall, and hits for a single instance."""
    hits = sum(1 for value in predictions if value in targets)
    target_set = set(targets)
    accuracy = 1.0 if target_set and target_set.issubset(set(predictions)) else 0.0
    precision = hits / max(len(predictions), 1)
    recall = hits / max(len(targets), 1) if targets else 0.0
    return {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "hits": hits,
    }


def aggregate_metrics(
    entries: Sequence[dict], ks: Iterable[int]
) -> Dict[int, Dict[str, float]]:
    aggregates: Dict[int, Dict[str, float]] = {}
    counts: Dict[int, int] = {}

    for entry in entries:
        targets = {
            normalize_symbol(sym)
            for sym in entry.get("symbols_modified", [])
            if isinstance(sym, str) and sym.strip()
        }
        if not targets:
            continue

        predictions = [
            normalize_symbol(sym)
            for sym in entry.get("metric_k_node_ids", [])
            if isinstance(sym, str) and sym.strip()
        ]
        if not predictions:
            continue

        target_list = list(targets)
        for k in ks:
            if k <= 0:
                continue
            scoped = aggregates.setdefault(
                k, {"accuracy": 0.0, "precision": 0.0, "recall": 0.0, "hits": 0.0}
            )
            metrics = compute_metrics(predictions[:k], target_list)
            for key in ("accuracy", "precision", "recall", "hits"):
                scoped[key] += metrics[key]
            counts[k] = counts.get(k, 0) + 1

    averaged: Dict[int, Dict[str, float]] = {}
    for k, stats in aggregates.items():
        count = counts.get(k, 0)
        if count == 0:
            continue
        averaged[k] = {
            "accuracy": stats["accuracy"] / count,
            "precision": stats["precision"] / count,
            "recall": stats["recall"] / count,
            "avg_hits": stats["hits"] / count,
            "evaluated": count,
        }
    return averaged


def main() -> None:
    args = parse_args()
    entries = load_entries(args.results)
    ks = args.k or collect_default_ks(entries)
    if not ks:
        raise ValueError("No valid k values provided or inferred.")

    stats = aggregate_metrics(entries, ks)

    if not stats:
        print("No valid instances with targets and predictions were found.")
        return

    for k in sorted(stats.keys()):
        scoped = stats[k]
        evaluated = scoped["evaluated"]
        print(f"Top-{k} over {evaluated} evaluated instances:")
        print(
            f"  accuracy={scoped['accuracy']:.4f} "
            f"precision={scoped['precision']:.4f} "
            f"recall={scoped['recall']:.4f} "
            f"avg_hits={scoped['avg_hits']:.2f}"
        )


if __name__ == "__main__":
    main()
