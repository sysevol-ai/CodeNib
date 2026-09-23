#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Rescore frozen retrieval rankings against base-commit symbol locations.

The published dataset stores modified symbol ranges after applying the patch.
This offline sensitivity audit resolves the same named symbols before the
patch, without changing any query, candidate, plan, score, or ranking.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codenib.code_chunking import create_chunker
from codenib.languages import extension_to_language_map
from codenib.types import NodeInfo
from scripts.analyze_jev_routes import coverage_summary
from scripts.benchmark_jev_base import (
    candidate_coverage,
    digest,
    git,
    load_cases,
    metrics,
    paired_bootstrap,
    pool_coverage,
    write_json,
)
from scripts.benchmark_model_grep import interleave


def align_blocks(case: dict, repo: Path, chunkers: dict) -> tuple[list, list]:
    languages = extension_to_language_map("gt")
    files, blocks, audit = {}, [], []
    for block in case["target_blocks"]:
        name = block["file"]
        if name not in files:
            language = languages[Path(name).suffix]
            if language not in chunkers:
                # GTLocator uses these defaults: depth 2, no line splitting.
                chunkers[language] = create_chunker(language)
            source = git(repo, "show", f"{case['base_commit']}:{name}")
            source = (
                source.decode("utf-8", errors="replace")
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )
            files[name] = chunkers[language].chunk_source(
                source, file_path=name, relative_path=name
            )
        symbol = name + ":" + block["symbol"]
        matches = [c for c in files[name] if c.node_id == symbol]
        if not matches:
            raise ValueError(
                f"Cannot resolve base symbol: {case['instance_id']} {symbol}"
            )
        # Match GTLocator.extract_symbols_from_file's last-wins dictionary.
        # Retain every match in the audit rather than hiding ambiguous names.
        chosen = matches[-1]
        aligned = {**block, "start": chosen.start_line + 1, "end": chosen.end_line + 1}
        blocks.append(aligned)
        audit.append(
            {
                "instance_id": case["instance_id"],
                "file": name,
                "symbol": block["symbol"],
                "change_type": block["change_type"],
                "published": [block["start"], block["end"]],
                "base": [aligned["start"], aligned["end"]],
                "all_base_matches": [
                    [c.start_line + 1, c.end_line + 1] for c in matches
                ],
            }
        )
    return blocks, audit


def rerank_metrics(case: dict, row: dict) -> dict:
    nodes = [NodeInfo.model_validate(n) for n in case["candidates"]]
    lookup = {(n.node_id, n.start_line, n.end_line): n for n in nodes}
    order = [(n["node_id"], n["start_line"], n["end_line"]) for n in row["ranking"]]
    if (
        len(lookup) != len(nodes)
        or len(order) != len(nodes)
        or set(order) != set(lookup)
    ):
        raise ValueError("Recorded ranking changed candidate identity")
    return metrics(case, [lookup[k] for k in order])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prebuilt-root", type=Path, required=True)
    parser.add_argument("--bm25-prepared", type=Path, required=True)
    parser.add_argument("--grep-prepared", type=Path, required=True)
    parser.add_argument("--bm25-sweep", type=Path, required=True)
    parser.add_argument("--runs", type=Path, nargs="+", required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--case-csv", type=Path, required=True)
    args = parser.parse_args()
    result = json.loads(args.results.read_text())
    _, bm25 = load_cases(args.bm25_prepared)
    _, grep = load_cases(args.grep_prepared)
    sweep_manifest, sweep = load_cases(args.bm25_sweep)
    audit, aligned, chunkers = [], {}, {}
    for case in bm25:
        blocks, records = align_blocks(
            case, args.prebuilt_root / case["instance_id"] / "repo", chunkers
        )
        aligned[case["instance_id"]] = blocks
        audit.extend(records)
    for cases in (bm25, grep, sweep):
        for case in cases:
            case["target_blocks"] = aligned[case["instance_id"]]
    routes = {"bm25": bm25, "grep": grep}
    by_id = {
        route: {c["instance_id"]: c for c in cases} for route, cases in routes.items()
    }
    hashes = {
        digest(args.bm25_prepared / "manifest.json"): "bm25",
        digest(args.grep_prepared / "manifest.json"): "grep",
    }
    per_arm = {route: {} for route in routes}
    for path in args.runs:
        config = json.loads((path / "config.json").read_text())
        route = hashes[config["prepared_manifest_sha256"]]
        rows = json.loads((path / "rows.json").read_text())
        expected = {
            (c["instance_id"], r)
            for c in routes[route]
            for r in range(config["repeats"])
        }
        if {(r["instance_id"], r["rep"]) for r in rows} != expected or len(rows) != len(
            expected
        ):
            raise ValueError("Incomplete or duplicated run")
        arm = config["model"]
        if arm in per_arm[route]:
            raise ValueError("Duplicate model run")
        items = {iid: [] for iid in by_id[route]}
        for row in rows:
            items[row["instance_id"]].append(
                rerank_metrics(by_id[route][row["instance_id"]], row)
            )
        per_arm[route][arm] = {
            iid: {k: float(np.mean([r[k] for r in values])) for k in values[0]}
            for iid, values in items.items()
        }
    for route in routes:
        if set(per_arm[route]) != set(result["routes"][route]["arms"]):
            raise ValueError("Sensitivity audit is missing model arms")
    sensitivity = {
        "note": (
            "Offline rescore only; resolve published symbols in base_commit "
            "with the GTLocator chunker defaults and last-wins collision "
            "policy. Queries, candidates, model outputs, and rankings are "
            "unchanged. This repairs coordinates, not semantic label correctness."
        ),
        "script_sha256": digest(Path(__file__)),
        "blocks": len(audit),
        "blocks_with_changed_range": sum(r["published"] != r["base"] for r in audit),
        "instances_with_changed_range": len(
            {r["instance_id"] for r in audit if r["published"] != r["base"]}
        ),
        "ambiguous_symbols": [r for r in audit if len(r["all_base_matches"]) > 1],
        "label_audit": audit,
        "bm25_coverage_curve": {
            str(k): coverage_summary(
                [
                    pool_coverage(
                        c, [NodeInfo.model_validate(n) for n in c["candidates"][:k]]
                    )
                    for c in sweep
                ]
            )
            for k in sweep_manifest["coverage_ks"]
        },
        "routes": {},
        "paired_grep_minus_bm25": {},
    }
    for route, cases in routes.items():
        sensitivity["routes"][route] = {
            "candidate_coverage": coverage_summary(
                [candidate_coverage(c) for c in cases]
            ),
            "arms": {
                arm: {
                    k: float(np.mean([v[k] for v in items.values()]))
                    for k in next(iter(items.values()))
                }
                for arm, items in per_arm[route].items()
            },
        }
        jev = next(a for a in per_arm[route] if a.startswith("typesafe/"))
        sensitivity["routes"][route]["paired_jev_minus"] = {
            arm: paired_bootstrap(cases, per_arm[route][jev], values, "span_recall@5")
            for arm, values in per_arm[route].items()
            if arm != jev
        }
    for arm in per_arm["bm25"]:
        sensitivity["paired_grep_minus_bm25"][arm] = paired_bootstrap(
            bm25, per_arm["grep"][arm], per_arm["bm25"][arm], "span_recall@5"
        )
    sensitivity["interleaved_union_top100_coverage_only"] = coverage_summary(
        [
            pool_coverage(
                a,
                interleave(
                    [
                        [NodeInfo.model_validate(n) for n in c["candidates"]]
                        for c in (a, b)
                    ],
                    100,
                ),
            )
            for a, b in zip(bm25, grep, strict=True)
        ]
    )
    with args.case_csv.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        for route in routes:
            row[f"base_aligned/{route}/candidate_span_coverage"] = candidate_coverage(
                by_id[route][row["instance_id"]]
            )["span_recall"]
            for arm, values in per_arm[route].items():
                row[f"base_aligned/{route}/{arm}/span_recall@5"] = values[
                    row["instance_id"]
                ]["span_recall@5"]
    with args.case_csv.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    result["base_aligned_sensitivity"] = sensitivity
    result["primary_quality_frame"] = "base_aligned_sensitivity"
    result["published_quality_frame"] = "routes"
    write_json(args.results, result)
    print(
        json.dumps(
            {k: v for k, v in sensitivity.items() if k not in ("label_audit",)},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
