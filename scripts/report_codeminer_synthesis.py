#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Report and gate quality for the CodeMiner synthesis Hugging Face dataset."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping

import datasets

from codeminer.dataset.codeminer_synthesis import (
    DEFAULT_DATASET,
    DEFAULT_SPLIT,
    EXPECTED_BASE_CATEGORY_COUNTS,
    normalize_synthesis_record,
    parse_config_list,
    validate_synthesis_records,
)


def _parse_expected_counts(raw: str) -> Dict[str, int]:
    if not raw:
        return dict(EXPECTED_BASE_CATEGORY_COUNTS)
    out: Dict[str, int] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"Bad expected count {chunk!r}; expected category=N")
        name, value = chunk.split("=", 1)
        out[name.strip()] = int(value.strip())
    return out


def _split_info(dataset_name: str, configs: List[str]) -> Dict[str, Dict[str, Any]]:
    info: Dict[str, Dict[str, Any]] = {}
    for config_name in configs:
        try:
            splits = datasets.get_dataset_split_names(dataset_name, config_name)
        except Exception as exc:
            info[config_name] = {"available": [], "error": str(exc)}
            continue
        info[config_name] = {"available": splits}
    return info


def _load_rows(
    *,
    dataset_name: str,
    configs: List[str],
    expected_split: str,
    inspect_fallback_split: bool,
) -> tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    split_report = _split_info(dataset_name, configs)
    rows: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []

    for config_name in configs:
        entry = split_report[config_name]
        available = list(entry.get("available") or [])
        if entry.get("error"):
            issues.append(
                {
                    "severity": "error",
                    "code": "split_lookup_failed",
                    "source_config": config_name,
                    "reason": entry["error"],
                }
            )
            continue

        if expected_split in available:
            load_split = expected_split
        else:
            issues.append(
                {
                    "severity": "error",
                    "code": "missing_expected_split",
                    "source_config": config_name,
                    "expected_split": expected_split,
                    "available_splits": available,
                }
            )
            if inspect_fallback_split and available:
                load_split = available[0]
            else:
                continue

        entry["loaded_split"] = load_split
        ds = datasets.load_dataset(dataset_name, config_name, split=load_split)
        rows.extend(normalize_synthesis_record(row, config_name) for row in ds)
    return rows, split_report, issues


def build_report(args: argparse.Namespace) -> Dict[str, Any]:
    configs = parse_config_list(args.configs)
    expected_counts = _parse_expected_counts(args.expected_category_counts)
    rows, split_report, split_issues = _load_rows(
        dataset_name=args.dataset,
        configs=configs,
        expected_split=args.split,
        inspect_fallback_split=args.inspect_fallback_split,
    )
    quality = validate_synthesis_records(
        rows,
        expected_category_counts=expected_counts,
        compare_category_distribution=not args.allow_category_drift,
    )
    issues = split_issues + quality["issues"]
    language_counts = Counter(row.get("language_group") or "" for row in rows)
    return {
        "dataset": args.dataset,
        "expected_split": args.split,
        "configs": configs,
        "split_report": split_report,
        "row_count": len(rows),
        "by_language": dict(sorted(language_counts.items())),
        "by_config": quality["by_config"],
        "by_category": quality["by_category"],
        "by_config_category": quality["by_config_category"],
        "issues": issues,
        "error_count": sum(1 for issue in issues if issue["severity"] == "error"),
        "warning_count": sum(1 for issue in issues if issue["severity"] == "warning"),
    }


def _table(headers: List[str], rows: List[List[Any]]) -> str:
    values = [[str(cell) for cell in row] for row in rows]
    widths = [
        (
            max(len(headers[i]), *(len(row[i]) for row in values))
            if values
            else len(headers[i])
        )
        for i in range(len(headers))
    ]
    lines = [
        "  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))),
        "  ".join("-" * widths[i] for i in range(len(headers))),
    ]
    for row in values:
        lines.append("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(lines)


def format_text_report(report: Mapping[str, Any], *, max_issues: int) -> str:
    lines = [
        f"Dataset: {report['dataset']}",
        f"Expected split: {report['expected_split']}",
        f"Rows inspected: {report['row_count']}",
        f"Errors: {report['error_count']}  Warnings: {report['warning_count']}",
        "",
        "Splits:",
    ]
    split_rows = []
    for config_name, info in report["split_report"].items():
        split_rows.append(
            [
                config_name,
                ",".join(info.get("available") or []),
                info.get("loaded_split") or "",
            ]
        )
    lines.append(_table(["config", "available", "loaded"], split_rows))

    lines.extend(["", "Category Counts:"])
    categories = sorted(report["by_category"])
    cat_rows = []
    for config_name, counts in report["by_config_category"].items():
        cat_rows.append([config_name] + [counts.get(cat, 0) for cat in categories])
    lines.append(_table(["config"] + categories, cat_rows))

    lines.extend(["", "Language Counts:"])
    lines.append(
        _table(
            ["language_group", "rows"],
            [[name, count] for name, count in report["by_language"].items()],
        )
    )

    issues = list(report["issues"])
    if issues:
        lines.extend(["", f"Issues (first {min(max_issues, len(issues))}):"])
        issue_rows = []
        for issue in issues[:max_issues]:
            issue_rows.append(
                [
                    issue.get("severity", ""),
                    issue.get("code", ""),
                    issue.get("source_config", ""),
                    issue.get("query_id", ""),
                    ",".join(issue.get("files") or []),
                ]
            )
        lines.append(
            _table(["severity", "code", "config", "query_id", "files"], issue_rows)
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--split", default=DEFAULT_SPLIT)
    p.add_argument("--configs", default="all")
    p.add_argument(
        "--inspect-fallback-split",
        action="store_true",
        help=(
            "If --split is absent on HF, inspect the first available split while "
            "still reporting the missing expected split as an error."
        ),
    )
    p.add_argument(
        "--expected-category-counts",
        default="",
        help=(
            "Comma-separated category=N expectations per config. Defaults to the "
            "base 50-row synthesis mix."
        ),
    )
    p.add_argument(
        "--allow-category-drift",
        action="store_true",
        help="Do not warn when configs have different category distributions.",
    )
    p.add_argument("--json-output", default="")
    p.add_argument("--max-issues", type=int, default=40)
    p.add_argument("--no-fail", action="store_true", help="Always exit zero.")
    p.add_argument(
        "--fail-on-warnings",
        action="store_true",
        help="Exit nonzero on warnings as well as errors.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    report = build_report(args)
    print(format_text_report(report, max_issues=args.max_issues))
    if args.json_output:
        path = Path(args.json_output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    should_fail = report["error_count"] > 0 or (
        args.fail_on_warnings and report["warning_count"] > 0
    )
    return 0 if args.no_fail or not should_fail else 1


if __name__ == "__main__":
    sys.exit(main())
