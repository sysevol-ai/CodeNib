#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Rebuild the CodeMiner synthesis dataset into a Hugging Face-ready folder.

This script assembles one parquet file per language config, writes dataset-card
metadata with the benchmark split set to ``test``, and runs the local quality
gate before an optional upload.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

import datasets
from huggingface_hub import HfApi

from codeminer.dataset.codeminer_synthesis import (
    ALL_CONFIGS,
    DEFAULT_DATASET,
    DEFAULT_SPLIT,
    EXPECTED_BASE_CATEGORY_COUNTS,
    EXPECTED_CATEGORY_COUNTS_WITH_TRAVERSAL,
    SYNTHESIS_FEATURES,
    normalize_synthesis_record,
    parse_config_list,
    validate_synthesis_records,
)


def _load_hf_config(
    dataset_name: str, config_name: str, split: str
) -> List[Dict[str, Any]]:
    ds = datasets.load_dataset(dataset_name, config_name, split=split)
    return [normalize_synthesis_record(row, config_name) for row in ds]


def _read_json_records(path: Path) -> List[Mapping[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        rows: List[Mapping[str, Any]] = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("rows", "data", "instances", "queries"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return [payload]
    raise ValueError(f"Unsupported JSON payload in {path}: {type(payload).__name__}")


def _load_replacement(path: Path, config_name: str) -> List[Dict[str, Any]]:
    paths: List[Path]
    if path.is_dir():
        # The per-instance generation tree holds the canonical, complete record
        # set in ``<iid>/<config>_combined.json`` PLUS its per-category sources
        # (behavioral_x20/, mixed_x30/, traversal_x10/) and seed/quality files.
        # Reading everything double-counts every query (duplicate_query_id) and
        # mixes in non-record files — so prefer the combined files, which each
        # already union behavioral + mixed + traversal for one repo.
        combined = sorted(path.rglob("*_combined.json"))
        if combined:
            paths = combined
        else:
            paths = sorted(
                p for p in path.rglob("*") if p.suffix.lower() in {".json", ".jsonl"}
            )
    else:
        paths = [path]
    if not paths:
        raise ValueError(f"No JSON/JSONL replacement files found under {path}")

    records: List[Dict[str, Any]] = []
    for file_path in paths:
        records.extend(
            normalize_synthesis_record(row, config_name)
            for row in _read_json_records(file_path)
        )
    return records


def _parse_config_paths(values: Iterable[str]) -> Dict[str, List[Path]]:
    paths_by_config: Dict[str, List[Path]] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(
                f"Bad config path {value!r}; expected CONFIG=/path/to/json"
            )
        config_name, path = value.split("=", 1)
        config_name = config_name.strip()
        if config_name not in ALL_CONFIGS:
            raise ValueError(
                f"Unknown config {config_name!r}; "
                f"valid configs: {', '.join(ALL_CONFIGS)}"
            )
        paths_by_config.setdefault(config_name, []).append(Path(path).expanduser())
    return paths_by_config


def _parse_replacements(values: Iterable[str]) -> Dict[str, Path]:
    parsed = _parse_config_paths(values)
    replacements: Dict[str, Path] = {}
    for config_name, paths in parsed.items():
        if len(paths) > 1:
            raise ValueError(f"Only one --replacement is allowed for {config_name}.")
        replacements[config_name] = paths[0]
    return replacements


# Build artifacts / type stubs are not valid localization targets: the real
# logic lives in the source copy (lib/), not the bundle (dist/) or the .d.ts
# declaration. Rows whose ground truth is ENTIRELY such files are dropped so the
# benchmark never asks a retriever to land on generated code.
_NON_SOURCE_GT_RE = re.compile(
    r"(^|/)(dist|build|out|node_modules|vendor)/|(\.min\.js|\.d\.[cm]?ts|\.map)$",
    re.I,
)


def _has_source_gt(row: Dict[str, Any]) -> bool:
    files = row.get("gt_files") or row.get("target_files") or []
    if not files:
        return True  # no file constraint to judge; keep (handled elsewhere)
    return any(not _NON_SOURCE_GT_RE.search(str(f)) for f in files)


def _drop_non_source_gt(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [row for row in records if _has_source_gt(row)]


def _filter_base_distribution(
    records: List[Dict[str, Any]],
    *,
    keep_extra_categories: bool,
) -> List[Dict[str, Any]]:
    if keep_extra_categories:
        return records
    expected = set(EXPECTED_BASE_CATEGORY_COUNTS)
    return [row for row in records if row.get("category") in expected]


def _sort_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        records,
        key=lambda row: (
            str(row.get("source_config") or ""),
            str(row.get("category") or ""),
            str(row.get("query_id") or ""),
        ),
    )


def _write_config_parquet(
    records: List[Dict[str, Any]],
    *,
    output_dir: Path,
    config_name: str,
    split: str,
) -> Path:
    config_dir = output_dir / config_name
    config_dir.mkdir(parents=True, exist_ok=True)
    out_path = config_dir / f"{split}-00000-of-00001.parquet"
    ds = datasets.Dataset.from_list(_sort_records(records), features=SYNTHESIS_FEATURES)
    ds.to_parquet(str(out_path))
    return out_path


def _read_existing_card(dataset_name: str) -> str:
    try:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(dataset_name, "README.md", repo_type="dataset")
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].lstrip()
    return text


def _write_readme(
    *,
    output_dir: Path,
    dataset_name: str,
    configs: List[str],
    split: str,
    existing_body: str,
) -> Path:
    lines = [
        "---",
        "license: apache-2.0",
        "configs:",
    ]
    for config_name in configs:
        lines.extend(
            [
                f"- config_name: {config_name}",
                "  data_files:",
                f"  - split: {split}",
                f"    path: {config_name}/{split}-*",
            ]
        )
    lines.append("---")
    if existing_body.strip():
        lines.append(existing_body.rstrip())
    else:
        lines.extend(
            [
                "",
                "# CodeMiner Synthesis",
                "",
                "Synthetic code-search benchmark queries for CodeNib.",
            ]
        )
    path = output_dir / "README.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_gitattributes(output_dir: Path) -> None:
    (output_dir / ".gitattributes").write_text(
        "*.parquet filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8"
    )


def _summarize(records_by_config: Mapping[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    return {
        config_name: {
            "rows": len(records),
            "categories": dict(Counter(row.get("category") or "" for row in records)),
            "repos": sorted({row.get("repo") for row in records if row.get("repo")}),
        }
        for config_name, records in records_by_config.items()
    }


def build_dataset(args: argparse.Namespace) -> Dict[str, Any]:
    configs = parse_config_list(args.configs)
    replacements = _parse_replacements(args.replacement)
    appends = _parse_config_paths(args.append)
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    records_by_config: Dict[str, List[Dict[str, Any]]] = {}
    for config_name in configs:
        if config_name in replacements:
            records = _load_replacement(replacements[config_name], config_name)
            source = str(replacements[config_name])
        else:
            records = _load_hf_config(args.dataset, config_name, args.source_split)
            source = f"{args.dataset}/{config_name}:{args.source_split}"
        records = _filter_base_distribution(
            records,
            keep_extra_categories=args.keep_extra_categories,
        )
        before = len(records)
        records = _drop_non_source_gt(records)
        dropped = before - len(records)
        if dropped:
            print(f"  {config_name}: dropped {dropped} row(s) with non-source GT")
        for append_path in appends.get(config_name, []):
            appended = _load_replacement(append_path, config_name)
            records.extend(appended)
            source += f" + {append_path}"
        records_by_config[config_name] = records
        print(f"{config_name}: {len(records)} row(s) from {source}")

    all_records = [row for records in records_by_config.values() for row in records]
    expected_counts = (
        EXPECTED_CATEGORY_COUNTS_WITH_TRAVERSAL
        if args.include_traversal
        else EXPECTED_BASE_CATEGORY_COUNTS
    )
    quality = validate_synthesis_records(
        all_records,
        expected_category_counts=expected_counts if args.enforce_base_counts else None,
        compare_category_distribution=not args.allow_category_drift,
    )

    for config_name, records in records_by_config.items():
        _write_config_parquet(
            records,
            output_dir=output_dir,
            config_name=config_name,
            split=args.output_split,
        )
    _write_readme(
        output_dir=output_dir,
        dataset_name=args.dataset,
        configs=configs,
        split=args.output_split,
        existing_body=(
            "" if args.no_reuse_card_body else _read_existing_card(args.dataset)
        ),
    )
    _write_gitattributes(output_dir)

    report = {
        "dataset": args.dataset,
        "source_split": args.source_split,
        "output_split": args.output_split,
        "configs": configs,
        "replacements": {k: str(v) for k, v in replacements.items()},
        "appends": {
            k: [str(path) for path in paths] for k, paths in sorted(appends.items())
        },
        "summary": _summarize(records_by_config),
        "quality": quality,
    }
    report_path = output_dir / "quality_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Wrote {output_dir}")
    print(
        "Quality: "
        f"{quality['error_count']} error(s), {quality['warning_count']} warning(s) "
        f"-> {report_path}"
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument(
        "--source-split",
        default=DEFAULT_SPLIT,
        help="Split to read from the current/source HF dataset.",
    )
    p.add_argument(
        "--output-split",
        default=DEFAULT_SPLIT,
        help="Split to write into the rebuilt dataset folder.",
    )
    p.add_argument("--configs", default="all")
    p.add_argument(
        "--replacement",
        action="append",
        default=[],
        help="Replace one config with local rows: CONFIG=/path/to/file-or-dir.",
    )
    p.add_argument(
        "--append",
        action="append",
        default=[],
        help="Append local rows to one config: CONFIG=/path/to/file-or-dir.",
    )
    p.add_argument(
        "--keep-extra-categories",
        action="store_true",
        help="Keep categories outside the base synthesis mix, such as traversal.",
    )
    p.add_argument(
        "--include-traversal",
        action="store_true",
        help="Expect the base 50-row mix plus 10 traversal rows per config.",
    )
    p.add_argument(
        "--no-enforce-base-counts",
        dest="enforce_base_counts",
        action="store_false",
        help="Do not warn when each config differs from the 50-row base mix.",
    )
    p.set_defaults(enforce_base_counts=True)
    p.add_argument(
        "--allow-category-drift",
        action="store_true",
        help="Do not warn when configs have different category distributions.",
    )
    p.add_argument(
        "--allow-quality-errors",
        action="store_true",
        help="Write output even when the quality gate finds errors.",
    )
    p.add_argument("--output-dir", required=True)
    p.add_argument(
        "--push-to-hub",
        default="",
        help="Optional dataset repo id to update after the quality gate passes.",
    )
    p.add_argument(
        "--private",
        action="store_true",
        help="Create/update the target HF dataset repo as private.",
    )
    p.add_argument(
        "--no-reuse-card-body",
        action="store_true",
        help="Do not copy the existing README body from the source dataset.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    report = build_dataset(args)
    quality = report["quality"]
    if quality["error_count"] and not args.allow_quality_errors:
        print(
            "Quality gate failed; inspect quality_report.json or rerun with "
            "--allow-quality-errors for a draft-only build.",
            file=sys.stderr,
        )
        return 1

    if args.push_to_hub:
        api = HfApi()
        api.create_repo(
            repo_id=args.push_to_hub,
            repo_type="dataset",
            private=args.private,
            exist_ok=True,
        )
        api.upload_folder(
            repo_id=args.push_to_hub,
            repo_type="dataset",
            folder_path=str(Path(args.output_dir).expanduser()),
            delete_patterns=["*/train-*", "train-*"],
            commit_message=(
                "Rebuild CodeMiner synthesis dataset with test split and QA gate"
            ),
        )
        print(f"Pushed dataset to https://huggingface.co/datasets/{args.push_to_hub}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
