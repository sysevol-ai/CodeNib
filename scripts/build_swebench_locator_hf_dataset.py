#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build a HuggingFace-ready SWE-bench locator dataset from selected instances."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

import datasets
import pyarrow as pa

from codenib.log_utils import get_logger

logger = get_logger(__name__)

DROP_FIELDS = {"changed_loc", "total_in_repo"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a HF dataset from pre-enriched selected_instances.json.",
    )
    parser.add_argument(
        "--selected-instances",
        type=str,
        required=True,
        help="Path to selected_instances.json (with GT fields already computed).",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="test",
        help="Dataset split name (default: test).",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        required=True,
        help="Output directory for the dataset (Parquet format).",
    )
    parser.add_argument(
        "--output-jsonl",
        type=str,
        default=None,
        help="Optional path for JSONL export.",
    )
    parser.add_argument(
        "--output-parquet",
        type=str,
        default=None,
        help="Optional path for parquet export.",
    )
    parser.add_argument(
        "--push-to-hub",
        type=str,
        default=None,
        help="HuggingFace Hub repo ID to push to (e.g. 'org/dataset-name').",
    )
    parser.add_argument(
        "--private",
        action="store_true",
        help="Make the Hub dataset private (only with --push-to-hub).",
    )
    return parser


def _load_instances(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"Expected list in {path}, got {type(data).__name__}")
    return [dict(item) for item in data]


def _prepare_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Drop useless fields and add derived ones."""
    out = {k: v for k, v in row.items() if k not in DROP_FIELDS}
    out["gt_code_blocks_count"] = len(row.get("gt_code_blocks", []))
    return out


def _write_jsonl(rows: List[Dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    args = build_parser().parse_args()

    selected_path = Path(args.selected_instances).expanduser()
    raw_rows = _load_instances(selected_path)
    rows = [_prepare_row(r) for r in raw_rows]
    logger.info("Loaded %d instance(s) from %s", len(rows), selected_path)

    if not rows:
        raise ValueError("No instances found in input file.")

    # Build the Arrow table directly so we control struct field order for
    # gt_code_blocks (Arrow/Parquet sorts struct fields alphabetically by
    # default when inferring from Python dicts).
    _CODE_BLOCK_FIELDS = [
        "change_type",
        "start_line",
        "end_line",
        "file_path",
        "symbol",
        "symbol_type",
    ]

    def _reorder_code_blocks(blocks: list) -> list:
        """Return code_block dicts with keys in the canonical order."""
        return [{k: b[k] for k in _CODE_BLOCK_FIELDS} for b in blocks]

    for row in rows:
        if row.get("gt_code_blocks"):
            row["gt_code_blocks"] = _reorder_code_blocks(row["gt_code_blocks"])

    # Let HF infer schema, then rebuild the gt_code_blocks column with
    # an explicit struct type to lock in the field order.
    ds = datasets.Dataset.from_list(rows)
    table = ds.data
    code_block_type = pa.struct(
        [
            ("change_type", pa.string()),
            ("start_line", pa.int64()),
            ("end_line", pa.int64()),
            ("file_path", pa.string()),
            ("symbol", pa.string()),
            ("symbol_type", pa.string()),
        ]
    )
    idx = table.schema.get_field_index("gt_code_blocks")
    old_col = table.column(idx)
    new_col = old_col.cast(pa.list_(code_block_type))
    table = table.set_column(
        idx, pa.field("gt_code_blocks", pa.list_(code_block_type)), new_col
    )
    ds = datasets.Dataset(table)

    # Save Parquet (default) — HF Hub native format
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = output_dir / f"{args.split}-00000-of-00001.parquet"
    ds.to_parquet(str(parquet_path))
    logger.info("Saved Parquet (%d rows) to %s", len(ds), parquet_path)

    if args.output_jsonl:
        jsonl_path = Path(args.output_jsonl).expanduser()
        _write_jsonl(rows, jsonl_path)
        logger.info("Saved JSONL export to %s", jsonl_path)

    if args.output_parquet:
        extra_parquet = Path(args.output_parquet).expanduser()
        extra_parquet.parent.mkdir(parents=True, exist_ok=True)
        ds.to_parquet(str(extra_parquet))
        logger.info("Saved extra Parquet export to %s", extra_parquet)

    if args.push_to_hub:
        ds_dict = datasets.DatasetDict({args.split: ds})
        ds_dict.push_to_hub(args.push_to_hub, private=args.private)
        logger.info(
            "Pushed dataset to https://huggingface.co/datasets/%s", args.push_to_hub
        )

    # Summary
    by_lang = {}
    for r in rows:
        lang = r.get("language_group", "unknown")
        by_lang[lang] = by_lang.get(lang, 0) + 1
    gt_count = sum(1 for r in rows if r.get("gt_code_blocks_count", 0) > 0)
    logger.info(
        "Summary: total=%d  gt_nonempty=%d  by_language=%s",
        len(rows),
        gt_count,
        by_lang,
    )


if __name__ == "__main__":
    main()
