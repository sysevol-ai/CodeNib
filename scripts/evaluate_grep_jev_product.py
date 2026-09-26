#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Evaluate fresh product planning and Jev on the complete, pinned Base split.

Requires explicit billed-call opt-in. Serial execution shares a reported-cost
stop across cases; missing usage stops the entire run. This is not a provider
billing cap: the last in-flight call can cross the limit. No automatic retries,
resumption, selected-case filters, or label-dependent retrieval are supported.
Full source traces stay in the operator-selected output directory.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from statistics import mean
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codenib.agent.runtime import grep_jev as runtime
from codenib.source_fingerprint import capture_repository_source
from scripts.analyze_jev_dense import align_cases
from scripts.audit_grep_jev_product import materialize_commit
from scripts.benchmark_jev_base import (
    digest,
    load_cases,
    metrics,
    paired_bootstrap,
    write_json,
)


def evaluate_case(case: dict, prebuilt: Path, remaining_cost: float) -> dict:
    """Observe, without replacing, the real planner and scoring responses."""
    candidates = []
    scorer = runtime.decide_code_relevance
    planner = runtime._plan
    trace = {}

    def observe_plan(*args):
        plan = planner(*args)
        trace["search_plan"] = plan.model_dump()
        return plan

    def observe(client, query, batch):
        # Capture original order before the product assigns scores and sorts.
        candidates.extend(node.model_copy(deep=True) for _, node in batch)
        try:
            return scorer(client, query, batch)
        except Exception as exc:
            # Never serialize provider bodies, headers, URLs or exception text.
            trace["provider_failure"] = {"type": type(exc).__name__}
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if isinstance(status, int) and 100 <= status < 600:
                trace["provider_failure"]["http_status"] = status
            raise

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="codenib-product-evaluation-") as temp:
        root = Path(temp) / case["repo"].split("/")[-1]
        root.mkdir()
        trace["snapshot"] = materialize_commit(
            prebuilt / case["instance_id"] / "repo",
            case["base_commit"],
            case["git_tree"],
            root,
        )
        retrieval_started = time.monotonic()

        def deadline():
            if time.monotonic() - retrieval_started >= 90:
                raise runtime.GrepJevError("Product evaluation deadline exceeded")

        try:
            with (
                patch.object(runtime, "_plan", observe_plan),
                patch.object(runtime, "decide_code_relevance", observe),
                capture_repository_source(root, check_cancelled=deadline) as source,
            ):
                result = runtime.GrepJevRetriever(
                    runtime.GrepJevConfig(
                        max_cost_usd=min(0.10, remaining_cost),
                        timeout=max(0.001, 90 - (time.monotonic() - retrieval_started)),
                    )
                ).search(source, case["query"], top_k=100, check_cancelled=deadline)
            if len(candidates) != result.plan["candidate_count"]:
                raise ValueError("Scoring observer missed product candidates")
            trace.update(
                status="complete",
                plan=result.plan,
                candidates=[n.model_dump() for n in candidates],
                ranking=[n.model_dump() for n in result.nodes],
                grep_metrics=metrics(case, candidates),
                reranked_metrics=metrics(case, result.nodes),
            )
        except runtime.GrepJevError as exc:
            trace.update(status="error", error=str(exc), plan=exc.provider_usage)
            trace["observed_candidates"] = [n.model_dump() for n in candidates]
    trace["seconds"] = time.monotonic() - started
    return trace


def stop_reason(rows: list[dict], limit: float) -> str | None:
    """Unknown charges prohibit another case, including after failed requests."""
    plans = [r.get("plan") for r in rows if r["status"] != "not_run"]
    if any(not p or p.get("unreported_call_cost") for p in plans):
        return "unreported_call_cost"
    if sum(p["reported_cost_usd"] for p in plans) >= limit:
        return "reported_cost_limit"
    return None


def summarize(rows: list[dict]) -> dict:
    """Keep failed and unattempted cases in the fixed evaluation denominator."""
    complete = [r for r in rows if r["status"] == "complete"]
    paired = [
        (
            (
                r["grep_metrics"]["span_recall@5"],
                r["reranked_metrics"]["span_recall@5"],
            )
            if r["status"] == "complete"
            else (0.0, 0.0)
        )
        for r in rows
    ]
    calls = [c for r in rows for c in (r.get("plan") or {}).get("provider_calls", [])]
    differences = [p[1] - p[0] for p in paired]
    interval = None
    if len(rows) == 100:
        interval = paired_bootstrap(
            rows,
            {
                r["instance_id"]: {"recall": p[1]}
                for r, p in zip(rows, paired, strict=True)
            },
            {
                r["instance_id"]: {"recall": p[0]}
                for r, p in zip(rows, paired, strict=True)
            },
            "recall",
        )
    return {
        "cases": len(rows),
        "completed_cases": len(complete),
        "status_counts": dict(Counter(r["status"] for r in rows)),
        "model_evaluation_complete": len(complete) == len(rows),
        "failure_metric_policy": "failed or unattempted cases score zero",
        "grep_recall_at_5": mean(p[0] for p in paired),
        "reranked_recall_at_5": mean(p[1] for p in paired),
        "paired": {
            "difference": mean(differences),
            "wins": sum(v > 0 for v in differences),
            "ties": sum(v == 0 for v in differences),
            "losses": sum(v < 0 for v in differences),
            "repository_bootstrap": interval,
        },
        "reported_cost_usd": sum(c["usage"]["cost"] for c in calls),
        "reported_provider_calls": len(calls),
        "unreported_call_cost": any(
            r["status"] == "error"
            and (not r.get("plan") or r["plan"].get("unreported_call_cost"))
            for r in rows
        ),
        "resolved_models": dict(Counter(c["model"] for c in calls)),
    }


def evaluate(args) -> dict:
    if not args.allow_billed_calls:
        raise ValueError("Explicit --allow-billed-calls is required")
    if not math.isfinite(args.max_cost_usd) or args.max_cost_usd <= 0:
        raise ValueError("--max-cost-usd must be positive and finite")
    manifest, published = load_cases(args.prepared)
    labels = json.loads(args.labels.read_text())
    cases = align_cases(published, labels["base_aligned_sensitivity"]["label_audit"])
    if len(cases) != 100 or len({c["repo"] for c in cases}) != 25:
        raise ValueError("The complete 100-case, 25-repository split is required")
    inputs = {
        "prepared_manifest": digest(args.prepared / "manifest.json"),
        "label_report": digest(args.labels),
    }
    prior = json.loads((ROOT / "docs/assets/grep_jev_range_audit.json").read_text())
    if any(prior["input_sha256"][k] != v for k, v in inputs.items()):
        raise ValueError("Inputs differ from the published experiment")
    # Validate key presence without recording it or making a metadata request.
    runtime.GrepJevConfig().credential()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "cases").mkdir()
    configuration = {
        "scope": "Fresh source-only product planning and Jev; no agent evaluation",
        "dataset_revision": manifest["dataset_revision"],
        "input_sha256": inputs,
        "product_sha256": digest(ROOT / "codenib/agent/runtime/grep_jev.py"),
        "script_sha256": digest(Path(__file__)),
        "requested_models": [runtime.PLANNER_MODEL, runtime.JEV_MODEL],
        "max_cost_usd": args.max_cost_usd,
        "budget_scope": "Stop subsequent calls; in-flight cost may cross limit",
        "per_case_timeout_seconds": 90,
        "per_case_reported_cost_limit_usd": 0.10,
    }
    write_json(args.output / "config.json", configuration)
    rows = []
    for case in cases:
        row = {
            "instance_id": case["instance_id"],
            "repo": case["repo"],
            "language_group": case["language_group"],
            "base_commit": case["base_commit"],
        }
        stop = stop_reason(rows, args.max_cost_usd)
        if stop:
            trace = {"status": "not_run", "reason": stop}
        else:
            spent = sum((r.get("plan") or {}).get("reported_cost_usd", 0) for r in rows)
            try:
                trace = evaluate_case(
                    case, args.prebuilt_root, args.max_cost_usd - spent
                )
            except Exception as exc:
                # A non-route exception can lose usage accounting; fail closed.
                trace = {"status": "error", "error_type": type(exc).__name__}
        write_json(
            args.output / "cases" / f"{case['instance_id']}.json", {**row, **trace}
        )
        row.update(
            {
                k: v
                for k, v in trace.items()
                if k not in {"candidates", "ranking", "observed_candidates"}
            }
        )
        rows.append(row)
        report = {**configuration, **summarize(rows), "per_case": rows}
        # Partial reports cannot masquerade as a complete dataset evaluation.
        report["model_evaluation_complete"] = report["completed_cases"] == 100
        write_json(args.output / "summary.json", report)
        print(
            json.dumps(
                {
                    "done": len(rows),
                    "instance_id": case["instance_id"],
                    "status": trace["status"],
                    "reported_cost_usd": report["reported_cost_usd"],
                }
            ),
            flush=True,
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--prebuilt-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-billed-calls", action="store_true")
    parser.add_argument("--max-cost-usd", type=float, required=True)
    result = evaluate(parser.parse_args())
    if not result["model_evaluation_complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
