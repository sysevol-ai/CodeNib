#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Measure exact repository-snapshot reuse in benchmark datasets."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codeminer.compiler.snapshot_store import SourceSnapshot  # noqa: E402
from codeminer.compiler.snapshot_store import normalize_repo


def summarize_snapshot_reuse(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    records = [row for row in rows if row.get("repo") and row.get("base_commit")]
    snapshot_ids = [
        SourceSnapshot(str(row["repo"]), str(row["base_commit"])).snapshot_id
        for row in records
    ]
    instances = {str(row.get("instance_id") or "") for row in records}
    instances.discard("")
    repos = {normalize_repo(str(row["repo"])) for row in records}
    counts = Counter(snapshot_ids)
    per_snapshot = sorted(counts.values())
    unique_snapshots = len(counts)
    record_count = len(records)
    return {
        "records": record_count,
        "instances": len(instances),
        "repositories": len(repos),
        "unique_snapshots": unique_snapshots,
        "duplicate_records": record_count - unique_snapshots,
        "snapshot_reuse_fraction": (
            1.0 - unique_snapshots / record_count if record_count else 0.0
        ),
        "records_per_snapshot_mean": (
            record_count / unique_snapshots if unique_snapshots else 0.0
        ),
        "records_per_snapshot_median": (
            statistics.median(per_snapshot) if per_snapshot else 0.0
        ),
        "records_per_snapshot_max": max(per_snapshot, default=0),
    }


def _load_hf(dataset: str, configs: Sequence[str], split: str) -> list[dict[str, Any]]:
    from datasets import get_dataset_config_names, load_dataset

    selected = list(configs)
    if not selected:
        available = get_dataset_config_names(dataset)
        selected = available if len(available) > 1 else [""]
    rows: list[dict[str, Any]] = []
    for config in selected:
        loaded = load_dataset(dataset, config or None, split=split)
        rows.extend(dict(row) for row in loaded)
    return rows


def _load_json(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"expected a JSON array at {path}")
    return [dict(row) for row in payload]


def _markdown(label: str, summary: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            f"## {label}",
            "",
            "| records | instances | repos | snapshots | avoided builds "
            "| reuse | records/snapshot |",
            "|---:|---:|---:|---:|---:|---:|---:|",
            (
                f"| {summary['records']} | {summary['instances']} "
                f"| {summary['repositories']} | {summary['unique_snapshots']} "
                f"| {summary['duplicate_records']} "
                f"| {summary['snapshot_reuse_fraction']:.1%} "
                f"| {summary['records_per_snapshot_mean']:.2f} |"
            ),
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--dataset",
        action="append",
        help="Hugging Face dataset name; repeat to measure cross-dataset reuse",
    )
    source.add_argument("--input", type=Path, help="JSON or JSONL records")
    parser.add_argument("--config", action="append", default=[])
    parser.add_argument("--split", default="test")
    parser.add_argument("--label", default=None)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args(argv)

    if args.dataset:
        rows = []
        for dataset in args.dataset:
            rows.extend(_load_hf(dataset, args.config, args.split))
        label = args.label or " + ".join(args.dataset)
    else:
        rows = _load_json(args.input)
        label = args.label or str(args.input)
    summary = summarize_snapshot_reuse(rows)
    summary["label"] = label
    print(_markdown(label, summary))
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
