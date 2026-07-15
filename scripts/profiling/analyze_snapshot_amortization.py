#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Measure exact-snapshot amortization from a workload and build profiles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codeminer.eval.reports.snapshot_amortization import (  # noqa: E402
    analyze_snapshot_amortization,
    render_markdown,
)


def _load_dataset(name: str, split: str) -> list[dict[str, Any]]:
    from datasets import get_dataset_config_names, load_dataset

    configs = get_dataset_config_names(name)
    selected = configs if len(configs) > 1 else [None]
    rows = []
    for config in selected:
        rows.extend(dict(row) for row in load_dataset(name, config, split=split))
    return rows


def _load_rows(path: Path) -> list[dict[str, Any]]:
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset")
    source.add_argument("--input", type=Path)
    parser.add_argument("--split", default="test")
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--embedding-model", required=True)
    parser.add_argument("--profile-tag", default="codeminer_base_test")
    parser.add_argument("--level", choices=("l0", "l2"), default="l2")
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    args = parser.parse_args(argv)

    dataset_rows = (
        _load_dataset(args.dataset, args.split)
        if args.dataset
        else _load_rows(args.input)
    )
    profiles = []
    for path in sorted(args.profile_dir.glob("*.json")):
        profile = json.loads(path.read_text(encoding="utf-8"))
        if (
            profile.get("embedding_model") == args.embedding_model
            and profile.get("profile_tag") == args.profile_tag
        ):
            profiles.append(profile)

    report = analyze_snapshot_amortization(
        dataset_rows,
        profiles,
        level=args.level,
    )
    markdown = render_markdown(report)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
    if args.output_markdown:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
