#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Generate one CodeMiner synthesis config replacement with per-category counts."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from codeminer.dataset.codeminer_base import CodeMinerBaseDataset
from codeminer.dataset.codeminer_synthesis import (
    ALL_CONFIGS,
    EXPECTED_BASE_CATEGORY_COUNTS,
    EXPECTED_CATEGORY_COUNTS_WITH_TRAVERSAL,
    normalize_synthesis_record,
    validate_synthesis_records,
)


def _parse_counts(raw: str) -> Dict[str, int]:
    if not raw:
        return dict(EXPECTED_BASE_CATEGORY_COUNTS)
    counts: Dict[str, int] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"Bad --counts entry {chunk!r}; expected category=N")
        name, value = chunk.split("=", 1)
        counts[name.strip()] = int(value.strip())
    return counts


def _stance_counts_for_total(total: int) -> str:
    stances = ("trace_back", "trace_down", "bridge")
    base, extra = divmod(total, len(stances))
    counts = {
        stance: base + (1 if idx < extra else 0) for idx, stance in enumerate(stances)
    }
    return ",".join(f"{stance}={count}" for stance, count in counts.items() if count)


def _parse_total(raw: str) -> int:
    total = 0
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"Bad stance count entry {chunk!r}; expected stance=N")
        _, value = chunk.split("=", 1)
        total += int(value.strip())
    return total


def _read_json_list(path: Path) -> List[Dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"Expected list in {path}, got {type(data).__name__}")
    return data


def _write_seed_file(args: argparse.Namespace) -> Path:
    ds = CodeMinerBaseDataset(
        dataset=args.source_dataset,
        split=args.split,
        filter_instance=f"^({re.escape(args.instance_id)})$",
        root=args.cache_dir or None,
        repo_root=args.repo_cache_dir or None,
        log=True,
    )
    rows = [dict(row) for row in ds.load()]
    if len(rows) != 1:
        raise ValueError(
            f"Expected exactly one codeminer-base row for {args.instance_id!r}, "
            f"got {len(rows)}."
        )
    row = rows[0]
    seed = {
        "repo": row["repo"],
        "instance_id": row["instance_id"],
        "base_commit": row["base_commit"],
        "language_group": row.get("language_group"),
    }
    path = args.output_dir / f"{args.config}_{args.instance_id}_seed.json"
    path.write_text(json.dumps([seed], indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _common_multiplier_args(args: argparse.Namespace) -> List[str]:
    cmd = [
        "--model",
        args.model_name,
        "--judge-model",
        args.judge_model,
        "--max-turns",
        str(args.max_turns),
        "--sampling-seed",
        str(args.sampling_seed),
        "--assignment-seed",
        str(args.assignment_seed),
        "--simple-ratio",
        str(args.simple_ratio),
        "--behavioral-consensus-runs",
        str(args.behavioral_consensus_runs),
        "--post-fix-max-retries",
        str(args.post_fix_max_retries),
    ]
    if args.cache_dir:
        cmd.extend(["--cache-dir", args.cache_dir])
    if args.repo_cache_dir:
        cmd.extend(["--repo-cache-dir", args.repo_cache_dir])
    return cmd


def _common_traversal_args(args: argparse.Namespace) -> List[str]:
    cmd = [
        "--model",
        args.model_name,
        "--judge-model",
        args.judge_model,
        "--max-turns",
        str(args.max_turns),
        "--sampling-seed",
        str(args.sampling_seed),
        "--judge-retries",
        str(args.traversal_judge_retries),
        "--post-fix-max-retries",
        str(args.post_fix_max_retries),
    ]
    if args.cache_dir:
        cmd.extend(["--cache-dir", args.cache_dir])
    if args.repo_cache_dir:
        cmd.extend(["--repo-cache-dir", args.repo_cache_dir])
    return cmd


def _run_behavioral(args: argparse.Namespace, seed_file: Path, count: int) -> Path:
    out_dir = args.output_dir / "behavioral_x20"
    out_path = out_dir / f"synthesized_queries_{args.instance_id}.json"
    if args.skip_existing and out_path.exists():
        return out_path
    cmd = [
        sys.executable,
        "scripts/multiply_behavior_queries.py",
        "--seed-file",
        str(seed_file),
        "--output-dir",
        str(out_dir),
        "--n-queries",
        str(count),
        *_common_multiplier_args(args),
    ]
    print("running:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return out_path


def _run_mixed(
    args: argparse.Namespace,
    seed_file: Path,
    anchor_file: Path,
    counts: Dict[str, int],
) -> Path:
    out_dir = args.output_dir / "mixed_x30"
    out_path = out_dir / f"synthesized_queries_{args.instance_id}.json"
    if args.skip_existing and out_path.exists():
        return out_path
    type_counts = ",".join(f"{name}={count}" for name, count in counts.items() if count)
    cmd = [
        sys.executable,
        "scripts/multiply_queries.py",
        "--seed-file",
        str(seed_file),
        "--anchor-file",
        str(anchor_file),
        "--output-dir",
        str(out_dir),
        "--type-counts",
        type_counts,
        *_common_multiplier_args(args),
    ]
    print("running:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return out_path


def _run_traversal(args: argparse.Namespace, seed_file: Path, count: int) -> Path:
    out_dir = args.output_dir / "traversal_x10"
    out_path = out_dir / f"synthesized_queries_{args.instance_id}.json"
    if args.skip_existing and out_path.exists():
        return out_path
    stance_counts = args.traversal_stance_counts or _stance_counts_for_total(count)
    stance_total = _parse_total(stance_counts)
    if stance_total != count:
        raise ValueError(
            f"--traversal-stance-counts sums to {stance_total}, "
            f"but traversal count is {count}."
        )
    cmd = [
        sys.executable,
        "scripts/multiply_traversal_queries.py",
        "--seed-file",
        str(seed_file),
        "--output-dir",
        str(out_dir),
        "--stance-counts",
        stance_counts,
        *_common_traversal_args(args),
    ]
    print("running:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return out_path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True, choices=ALL_CONFIGS)
    p.add_argument("--instance-id", required=True)
    p.add_argument("--split", default="test")
    p.add_argument("--source-dataset", default="fishmingyu/codeminer-base-dataset")
    p.add_argument(
        "--counts",
        default="",
        help=(
            "Comma-separated category=N counts. Defaults to the base 50-row mix."
            " Include traversal=10 to generate traversal rows."
        ),
    )
    p.add_argument(
        "--include-traversal",
        action="store_true",
        help="Shortcut for the base 50-row mix plus traversal=10.",
    )
    p.add_argument(
        "--traversal-stance-counts",
        default="",
        help=(
            "Optional traversal stance counts. Defaults to an even split, "
            "so traversal=10 becomes trace_back=4,trace_down=3,bridge=3."
        ),
    )
    p.add_argument("--traversal-judge-retries", type=int, default=2)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--combined-file", default="")
    p.add_argument("--model-name", default="opus")
    p.add_argument("--judge-model", default="opus")
    p.add_argument("--max-turns", type=int, default=10)
    p.add_argument("--behavioral-consensus-runs", type=int, default=3)
    p.add_argument("--post-fix-max-retries", type=int, default=3)
    p.add_argument("--sampling-seed", type=int, default=42)
    p.add_argument("--assignment-seed", type=int, default=0)
    p.add_argument("--simple-ratio", type=float, default=0.5)
    p.add_argument("--cache-dir", default="")
    p.add_argument("--repo-cache-dir", default="")
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="Reuse existing per-category JSON files in --output-dir.",
    )
    p.add_argument(
        "--allow-quality-errors",
        action="store_true",
        help="Write combined output even if validation finds errors.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    args.output_dir = args.output_dir.expanduser()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    counts = (
        dict(EXPECTED_CATEGORY_COUNTS_WITH_TRAVERSAL)
        if args.include_traversal and not args.counts
        else _parse_counts(args.counts)
    )
    seed_file = _write_seed_file(args)

    combined: List[Dict[str, Any]] = []
    behavioral_count = counts.get("behavioral", 0)
    traversal_count = counts.get("traversal", 0)
    if behavioral_count <= 0 and traversal_count <= 0:
        raise ValueError("The standard synthesis pipeline requires behavioral > 0.")
    behavioral_path: Path | None = None
    if behavioral_count > 0:
        behavioral_path = _run_behavioral(args, seed_file, behavioral_count)
        combined.extend(
            normalize_synthesis_record(row, args.config)
            for row in _read_json_list(behavioral_path)
        )

    mixed_counts = {
        k: v
        for k, v in counts.items()
        if k not in {"behavioral", "traversal"} and v > 0
    }
    if mixed_counts:
        if behavioral_path is None:
            raise ValueError("Mixed categories require behavioral anchors.")
        mixed_path = _run_mixed(args, seed_file, behavioral_path, mixed_counts)
        combined.extend(
            normalize_synthesis_record(row, args.config)
            for row in _read_json_list(mixed_path)
        )

    if traversal_count > 0:
        traversal_path = _run_traversal(args, seed_file, traversal_count)
        combined.extend(
            normalize_synthesis_record(row, args.config)
            for row in _read_json_list(traversal_path)
        )

    combined_path = (
        Path(args.combined_file).expanduser()
        if args.combined_file
        else args.output_dir / f"{args.config}_combined.json"
    )
    combined_path.write_text(
        json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    report = validate_synthesis_records(
        combined,
        expected_category_counts=counts,
        compare_category_distribution=False,
    )
    report_path = combined_path.with_suffix(".quality.json")
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"combined -> {combined_path}")
    print(
        f"quality -> {report_path} "
        f"({report['error_count']} error(s), {report['warning_count']} warning(s))"
    )
    if report["error_count"] and not args.allow_quality_errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
