#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Compare frozen BM25 and LLM-grep retrieval with matched reranker arms."""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codenib.types import NodeInfo
from scripts.benchmark_jev_base import (
    analyze,
    candidate_coverage,
    digest,
    load_cases,
    paired_bootstrap,
    pool_coverage,
    write_json,
)
from scripts.benchmark_model_grep import interleave


def distribution(values) -> dict:
    return {
        "min": float(np.min(values)),
        "p50": float(np.median(values)),
        "p95": float(np.percentile(values, 95)),
        "max": float(np.max(values)),
        "mean": float(np.mean(values)),
    }


def coverage_summary(values) -> dict:
    return {
        "metrics": {k: float(np.mean([v[k] for v in values])) for k in values[0]},
        "instances_without_target_block": sum(v["span_recall"] == 0 for v in values),
        "instances_with_all_target_blocks": sum(v["span_all"] == 1 for v in values),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bm25-prepared", type=Path, required=True)
    parser.add_argument("--bm25-sweep", type=Path, required=True)
    parser.add_argument("--grep-prepared", type=Path, required=True)
    parser.add_argument("--bm25-runs", type=Path, nargs="+", required=True)
    parser.add_argument("--grep-runs", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-csv", type=Path, required=True)
    args = parser.parse_args()
    _, bm25 = load_cases(args.bm25_prepared)
    _, grep = load_cases(args.grep_prepared)
    sweep_manifest, sweep = load_cases(args.bm25_sweep)
    groups = {"bm25": bm25, "grep": grep}
    if [c["instance_id"] for c in bm25] != [c["instance_id"] for c in grep] or [
        c["instance_id"] for c in bm25
    ] != [c["instance_id"] for c in sweep]:
        raise ValueError("Retrieval routes must use the same instances and order")
    for a, b, s in zip(bm25, grep, sweep, strict=True):
        for key in (
            "query",
            "repo",
            "base_commit",
            "git_tree",
            "selected_blobs_sha256",
            "target_files",
            "target_blocks",
        ):
            if a[key] != b[key] or a[key] != s[key]:
                raise ValueError(f"Routes differ in {key}")
        if a["candidates"] != s["candidates"][: len(a["candidates"])]:
            raise ValueError("BM25 candidates are not an exact sweep prefix")
    result = {
        "bm25_sweep_manifest_sha256": digest(args.bm25_sweep / "manifest.json"),
        "bm25_coverage_curve": {
            str(k): coverage_summary([c["coverage_curve"][str(k)] for c in sweep])
            for k in sweep_manifest["coverage_ks"]
        },
        "full_corpus_coverage": coverage_summary([c["corpus_coverage"] for c in sweep]),
        "routes": {},
    }
    with tempfile.TemporaryDirectory(prefix="codenib-route-analysis-") as temporary:
        for route, prepared, runs in (
            ("bm25", args.bm25_prepared, args.bm25_runs),
            ("grep", args.grep_prepared, args.grep_runs),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                result["routes"][route] = analyze(
                    SimpleNamespace(
                        prepared=prepared,
                        runs=runs,
                        output=Path(temporary) / f"{route}.json",
                        case_csv=None,
                        baseline_name=route,
                    )
                )
            result["routes"][route]["candidate_count_distribution"] = distribution(
                [len(c["candidates"]) for c in groups[route]]
            )
    grep_by_id = {c["instance_id"]: c for c in grep}
    plans = []
    for c in grep:
        path = args.grep_prepared / "plans" / f"{c['instance_id']}.json"
        if digest(path) != c["grep"]["plan_sha256"]:
            raise ValueError("Frozen planner trace changed")
        plans.append(json.loads(path.read_text()))
    attempts = []
    for plan in plans:
        attempts.extend(plan.get("attempts", [plan]))
        if "prior_run_attempt" in plan:
            attempts.append(plan["prior_run_attempt"])
    result["planner"] = {
        "requests": len(attempts),
        "statuses": dict(
            Counter(str(a.get("http_status", "non_http")) for a in attempts)
        ),
        "failed_final_plans": sum(bool(p.get("error")) for p in plans),
        "successful_plans_reused": sum("reused_from_sha256" in p for p in plans),
        "successful_providers": dict(
            Counter(p.get("provider") for p in plans if not p.get("error"))
        ),
        "reported_cost_usd": sum(a.get("usage", {}).get("cost", 0.0) for a in attempts),
        "reported_prompt_tokens": sum(
            a.get("usage", {}).get("prompt_tokens", 0) for a in attempts
        ),
        "reported_completion_tokens": sum(
            a.get("usage", {}).get("completion_tokens", 0) for a in attempts
        ),
        "final_planning_latency_ms": distribution(
            [c["grep"]["planner_ms"] for c in grep]
        ),
        "grep_and_context_latency_ms": distribution(
            [c["grep"]["search_and_context_ms"] for c in grep]
        ),
        "bm25_top100_control_latency_ms": distribution(
            [c["grep"]["bm25_control_retrieval_ms"] for c in grep]
        ),
        "grep_actions": sum(len(c["grep"]["actions"]) for c in grep),
        "grep_action_errors": sum(
            "error" in a for c in grep for a in c["grep"]["actions"]
        ),
        "grep_actions_capped_at_500_lines": sum(
            a["capped_at_500_lines"] for c in grep for a in c["grep"]["actions"]
        ),
        "latency_note": (
            "Final planning call/retry latency; prior-run 429 attempts and "
            "offline resume scheduling are NOT included. Initial run used "
            "4 workers; resumed run used 1. This is not a production "
            "availability or end-to-end latency benchmark."
        ),
    }
    result["paired_grep_minus_bm25"] = {}
    for arm in result["routes"]["bm25"]["arms"]:
        if arm not in result["routes"]["grep"]["arms"]:
            raise ValueError("Missing matched reranker arm")
        left = {
            iid: c["arms"][arm]
            for iid, c in result["routes"]["grep"]["per_case"].items()
        }
        right = {
            iid: c["arms"][arm]
            for iid, c in result["routes"]["bm25"]["per_case"].items()
        }
        result["paired_grep_minus_bm25"][arm] = {
            key: paired_bootstrap(bm25, left, right, key)
            for key in (
                "span_recall@1",
                "span_recall@5",
                "span_recall@10",
                "file_recall@5",
                "span_mrr",
            )
        }
        differences = [
            left[iid]["span_recall@5"] - right[iid]["span_recall@5"] for iid in left
        ]
        result["paired_grep_minus_bm25"][arm]["win_tie_loss"] = {
            "win": sum(d > 1e-9 for d in differences),
            "tie": sum(abs(d) <= 1e-9 for d in differences),
            "loss": sum(d < -1e-9 for d in differences),
        }
    union = {}
    for a, b in zip(bm25, grep, strict=True):
        nodes = interleave(
            [[NodeInfo.model_validate(n) for n in c["candidates"]] for c in (a, b)], 100
        )
        union[a["instance_id"]] = pool_coverage(a, nodes)
    result["interleaved_union_top100_coverage_only"] = coverage_summary(
        list(union.values())
    )
    result["paired_candidate_coverage"] = paired_bootstrap(
        bm25,
        {c["instance_id"]: candidate_coverage(c) for c in grep},
        {c["instance_id"]: candidate_coverage(c) for c in bm25},
        "span_recall",
    )
    for route, runs in (("bm25", args.bm25_runs), ("grep", args.grep_runs)):
        for path in runs:
            config = json.loads((path / "config.json").read_text())
            rows = json.loads((path / "rows.json").read_text())
            totals = [
                r["rerank_ms"]
                + (
                    grep_by_id[r["instance_id"]]["retrieval_ms"]
                    if route == "grep"
                    else grep_by_id[r["instance_id"]]["grep"][
                        "bm25_control_retrieval_ms"
                    ]
                )
                for r in rows
            ]
            result["routes"][route]["arms"][config["model"]]["offline_stage_sum_ms"] = (
                distribution(totals)
            )
    args.case_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.case_csv.open("w", newline="") as stream:
        writer = None
        for c in sweep:
            iid = c["instance_id"]
            flat = {
                "instance_id": iid,
                "repo": c["repo"],
                "language_group": c["language_group"],
            }
            for k, value in c["coverage_curve"].items():
                flat[f"bm25_span_coverage@{k}"] = value["span_recall"]
            flat["grep_candidate_count"] = len(grep_by_id[iid]["candidates"])
            flat["grep_span_coverage"] = candidate_coverage(grep_by_id[iid])[
                "span_recall"
            ]
            flat["union100_span_coverage"] = union[iid]["span_recall"]
            flat["planner_ms"] = grep_by_id[iid]["grep"]["planner_ms"]
            for route in groups:
                for arm, values in result["routes"][route]["per_case"][iid][
                    "arms"
                ].items():
                    for key in (
                        "span_recall@1",
                        "span_recall@5",
                        "span_recall@10",
                        "file_recall@5",
                        "span_mrr",
                    ):
                        flat[f"{route}/{arm}/{key}"] = values[key]
            if writer is None:
                writer = csv.DictWriter(
                    stream, fieldnames=list(flat), lineterminator="\n"
                )
                writer.writeheader()
            writer.writerow(flat)
    for route in groups:
        result["routes"][route].pop("per_case")
    result["case_csv"] = str(args.case_csv)
    result["script_sha256"] = digest(Path(__file__))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, result)
    print(
        json.dumps(
            {
                "coverage": result["bm25_coverage_curve"],
                "grep": result["routes"]["grep"]["candidate_coverage"],
                "planner": result["planner"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
