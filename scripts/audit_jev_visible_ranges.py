#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Offline sensitivity audit: synthetic chunk headers are not source lines."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codenib.types import NODE_TYPE_METHOD, NodeInfo
from scripts.analyze_jev_dense import align_cases, read_run
from scripts.benchmark_jev_base import digest, load_cases, metrics, write_json


def corrected_node(raw: dict) -> NodeInfo:
    node = NodeInfo.model_validate(raw)
    lines = (node.content or "").splitlines()
    if not lines or lines[0] != node.node_id:
        raise ValueError("Expected the frozen chunk's node-ID header")
    metadata_lines = 1 + int(
        node.type == NODE_TYPE_METHOD
        and len(lines) > 1
        and lines[1].startswith("class ")
    )
    if len(lines) <= metadata_lines:
        raise ValueError("Frozen candidate contains no visible source")
    node.end_line = min(
        node.end_line, node.start_line + len(lines) - metadata_lines - 1
    )
    return node


def audit(prepared: Path, run: Path, label_report: Path) -> dict:
    _, published = load_cases(prepared)
    labels = json.loads(label_report.read_text())
    cases = align_cases(published, labels["base_aligned_sensitivity"]["label_audit"])
    _, rows, _, run_hashes, _ = read_run(run, prepared, published, cases)
    before, after = {}, {}
    changed_ranges = candidates = 0
    changed_cases = []
    for case in cases:
        original, corrected = {}, {}
        for raw in case["candidates"]:
            key = (raw["node_id"], raw["start_line"], raw["end_line"])
            original[key] = NodeInfo.model_validate(raw)
            corrected[key] = corrected_node(raw)
            candidates += 1
            changed_ranges += corrected[key].end_line != raw["end_line"]
        for route, ordering in (
            ("grep", case["candidates"]),
            ("grep_jev", rows[case["instance_id"]]["ranking"]),
        ):
            keys = [(n["node_id"], n["start_line"], n["end_line"]) for n in ordering]
            old = metrics(case, [original[key] for key in keys])["span_recall@5"]
            new = metrics(case, [corrected[key] for key in keys])["span_recall@5"]
            before.setdefault(route, []).append(old)
            after.setdefault(route, []).append(new)
            if old != new:
                changed_cases.append(
                    {
                        "instance_id": case["instance_id"],
                        "route": route,
                        "before": old,
                        "after": new,
                    }
                )
    return {
        "scope": (
            "Offline range correction with frozen candidates, text, model scores "
            "and rankings; not a rerun of candidate generation or the production route."
        ),
        "cases": len(cases),
        "candidates": candidates,
        "changed_ranges": changed_ranges,
        "before_recall_at_5": {k: mean(v) for k, v in before.items()},
        "after_recall_at_5": {k: mean(v) for k, v in after.items()},
        "changed_case_scores": changed_cases,
        "input_sha256": {
            "prepared_manifest": digest(prepared / "manifest.json"),
            "label_report": digest(label_report),
            **run_hashes,
        },
        "audit_script_sha256": digest(Path(__file__)),
        "billed_api_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--label-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_json(args.output, audit(args.prepared, args.run, args.label_report))


if __name__ == "__main__":
    main()
