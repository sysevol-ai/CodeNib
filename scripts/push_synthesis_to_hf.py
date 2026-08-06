#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
"""Push a directory of synthesized-query JSONs to HuggingFace as one config
of a multi-config dataset.

Each row of the resulting dataset is one query entry (the schema produced by
``scripts/multiply_behavior_queries.py:_to_compact_record``). All ``*.json``
files in ``--input-dir`` are flattened into rows.

First push creates the repo. Subsequent pushes with a different
``--config-name`` add new configurations under the same dataset and downstream
users load them as ``load_dataset(repo_id, config_name)``.

Auth: ``hf auth login`` (or ``huggingface-cli login``) before running.

Example
-------
    python scripts/push_synthesis_to_hf.py \\
        --input-dir synthesis_output_behavior_x20/ \\
        --repo-id sysevol-ai/codenib-synthesis \\
        --config-name behavioral_x20
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from datasets import Dataset
from huggingface_hub import DatasetCard, DatasetCardData


def _load_rows(input_dir: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    files = sorted(input_dir.glob("*.json"))
    if not files:
        raise FileNotFoundError(f"No JSON files in {input_dir}")
    for f in files:
        with f.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        entries = data if isinstance(data, list) else [data]
        rows.extend(entries)
    return rows


def _ensure_license(repo_id: str, license_id: str) -> None:
    """Set the dataset card license without disturbing the configs block."""
    try:
        card = DatasetCard.load(repo_id, repo_type="dataset")
    except Exception:
        card = DatasetCard(content="", data=DatasetCardData())
    card.data.license = license_id
    card.push_to_hub(repo_id, repo_type="dataset")
    print(f"License set to {license_id}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--input-dir", required=True, type=Path)
    p.add_argument("--repo-id", default="sysevol-ai/codenib-synthesis")
    p.add_argument(
        "--config-name",
        required=True,
        help="HuggingFace config name (e.g. behavioral_x20, symbol_hint_v1).",
    )
    p.add_argument("--split", default="test")
    p.add_argument(
        "--private",
        action="store_true",
        help="Make the repo private (default: public).",
    )
    p.add_argument("--license", default="apache-2.0")
    p.add_argument(
        "--commit-message",
        default=None,
        help="Override the auto-generated commit message.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the Dataset locally but don't push.",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()

    rows = _load_rows(args.input_dir.expanduser())
    print(f"Loaded {len(rows)} rows from {args.input_dir}")
    if not rows:
        raise ValueError("No rows to upload.")

    n_instances = len({r.get("instance_id") for r in rows if r.get("instance_id")})
    print(f"  spans {n_instances} instance(s)")

    ds = Dataset.from_list(rows)
    print(f"  first row keys: {list(rows[0].keys())}")

    if args.dry_run:
        print("--dry-run set; not pushing.")
        return

    commit_msg = args.commit_message or f"Add {args.config_name} ({len(rows)} rows)"
    print(
        f"Pushing to {args.repo_id} "
        f"(config={args.config_name}, split={args.split}, private={args.private})..."
    )
    ds.push_to_hub(
        args.repo_id,
        config_name=args.config_name,
        split=args.split,
        private=args.private,
        commit_message=commit_msg,
    )
    _ensure_license(args.repo_id, args.license)
    print(f"Done. https://huggingface.co/datasets/{args.repo_id}")


if __name__ == "__main__":
    main()
