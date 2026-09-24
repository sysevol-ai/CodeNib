#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Compare embedding and embedding-free routes on the same aligned Base labels."""

from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codenib.types import NodeInfo
from scripts.analyze_jev_routes import coverage_summary, distribution
from scripts.audit_jev_base_labels import rerank_metrics
from scripts.benchmark_jev_base import (
    candidate_coverage,
    digest,
    load_cases,
    metrics,
    paired_bootstrap,
    write_json,
)

JEV = "typesafe/jev-1.13"
QWEN = "Qwen/Qwen3-Reranker-4B"


def align_cases(cases: list, audit: list) -> list:
    """Only apply an audit to the exact published coordinates it inspected."""
    mapping = {
        (a["instance_id"], a["file"], a["symbol"], a["change_type"]): a for a in audit
    }
    if len(mapping) != len(audit):
        raise ValueError("Duplicate audit identities")
    aligned = copy.deepcopy(cases)
    used = set()
    for case in aligned:
        for block in case["target_blocks"]:
            key = (
                case["instance_id"],
                block["file"],
                block["symbol"],
                block["change_type"],
            )
            row = mapping[key]
            if row["published"] != [block["start"], block["end"]]:
                raise ValueError("Label coordinates differ from the audit")
            block["start"], block["end"] = row["base"]
            used.add(key)
    if used != set(mapping):
        raise ValueError("Audit and evaluated targets differ")
    return aligned


def require_matched_cases(reference: list, other: list) -> None:
    keys = (
        "instance_id",
        "query",
        "repo",
        "language_group",
        "base_commit",
        "git_tree",
        "selected_blobs_sha256",
        "target_files",
        "target_blocks",
        "corpus_chunks",
    )
    if len(reference) != len(other):
        raise ValueError("Routes contain different numbers of cases")
    if any(
        len({c["instance_id"] for c in group}) != len(group)
        for group in (reference, other)
    ):
        raise ValueError("Duplicate instances must not inflate the sample")
    for left, right in zip(reference, other, strict=True):
        for key in keys:
            if left[key] != right[key]:
                raise ValueError(f"Routes differ in {key}: {left['instance_id']}")


def mean_metrics(values: dict) -> dict:
    return {
        k: float(np.mean([v[k] for v in values.values()]))
        for k in next(iter(values.values()))
    }


def read_run(path: Path, prepared: Path, published: list, aligned: list):
    config = json.loads((path / "config.json").read_text())
    rows = json.loads((path / "rows.json").read_text())
    if config["prepared_manifest_sha256"] != digest(prepared / "manifest.json"):
        raise ValueError("Reranker saw different candidates")
    expected = {(c["instance_id"], 0) for c in published}
    if (
        config["repeats"] != 1
        or len(rows) != len(expected)
        or {(r["instance_id"], r["rep"]) for r in rows} != expected
    ):
        raise ValueError("Incomplete, repeated, or duplicate reranker observations")
    by_id = {r["instance_id"]: r for r in rows}
    for case in published:
        row = by_id[case["instance_id"]]
        if rerank_metrics(case, row) != row["metrics"]:
            raise ValueError("Cannot reproduce recorded ranking metrics")
    aligned_metrics = {
        c["instance_id"]: rerank_metrics(c, by_id[c["instance_id"]]) for c in aligned
    }
    hashes = {name: digest(path / name) for name in ("config.json", "rows.json")}
    api = None
    if config["backend"] == "jev":
        calls = json.loads((path / "calls.json").read_text())
        if sorted(i for r in rows for i in r["call_indices"]) != list(
            range(len(calls))
        ):
            raise ValueError("API call trace differs from the observations")
        hashes["calls.json"] = digest(path / "calls.json")
        api = {
            "requests": len(calls),
            "resolved_models": dict(Counter(c["model"] for c in calls if "model" in c)),
            "errors_by_status": dict(
                Counter(
                    str(c.get("http_status", "non_http")) for c in calls if "error" in c
                )
            ),
            "reported_input_tokens": sum(
                c.get("usage", {}).get("input_tokens", 0) for c in calls
            ),
            "reported_output_tokens": sum(
                c.get("usage", {}).get("output_tokens", 0) for c in calls
            ),
            "failed_cost_note": "Missing usage for failed calls remains unknown",
        }
    return config, by_id, aligned_metrics, hashes, api


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bm25-prepared", type=Path, required=True)
    parser.add_argument("--grep-prepared", type=Path, required=True)
    parser.add_argument("--dense-prepared", type=Path, required=True)
    parser.add_argument("--hybrid-prepared", type=Path, required=True)
    parser.add_argument("--runs", type=Path, nargs="+", required=True)
    parser.add_argument("--label-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-csv", type=Path, required=True)
    args = parser.parse_args()
    original = json.loads(args.label_report.read_text())
    audit = original["base_aligned_sensitivity"]["label_audit"]
    roots = {
        name: getattr(args, name + "_prepared")
        for name in ("bm25", "grep", "dense", "hybrid")
    }
    manifests, published, aligned, hashes = {}, {}, {}, {}
    for name, root in roots.items():
        manifests[name], published[name] = load_cases(root)
        require_matched_cases(published["bm25"], published[name])
        aligned[name] = align_cases(published[name], audit)
        hashes[digest(root / "manifest.json")] = name
    cases = aligned["bm25"]
    if len(cases) != 100 or len({c["repo"] for c in cases}) != 25 or len(audit) != 151:
        raise ValueError("Expected the complete 100-issue, 25-repository Base split")
    result = {
        "dataset": manifests["bm25"]["dataset"],
        "dataset_revision": manifests["bm25"]["dataset_revision"],
        "instances": len(cases),
        "repositories": 25,
        "label_frame": "base-aligned",
        "label_report_sha256": digest(args.label_report),
        "script_sha256": digest(Path(__file__)),
        "embedding_config": manifests["dense"]["embedding_config"],
        "timing_note": (
            "Warm components measured separately and summed per issue. "
            "BM25 search remeasured during dense preparation. Grep includes "
            "successful planning; excludes initial 429 failures and recovery "
            "waits. Offline builds excluded; these are not production "
            "end-to-end timings."
        ),
        "routes": {},
        "paired_comparisons": {},
    }
    per_case, rows_by_arm = {}, {}
    csv_rows = []
    for route, route_cases in aligned.items():
        values = {
            c["instance_id"]: metrics(
                c, [NodeInfo.model_validate(n) for n in c["candidates"]]
            )
            for c in route_cases
        }
        per_case[(route, "unreranked")] = values
        retrieval = {c["instance_id"]: c["retrieval_ms"] for c in route_cases}
        if route == "bm25":
            retrieval = {
                c["instance_id"]: c["online_components_ms"]["bm25"]
                for c in aligned["dense"]
            }
        planner_cost = sum(
            c.get("grep", {}).get("recorded_cost_usd", 0) for c in route_cases
        )
        result["routes"][route] = {
            "manifest_sha256": digest(roots[route] / "manifest.json"),
            "candidate_count": distribution(
                [len(c["candidates"]) for c in route_cases]
            ),
            "coverage": coverage_summary([candidate_coverage(c) for c in route_cases]),
            "unreranked": mean_metrics(values),
            "retrieval_ms": distribution(list(retrieval.values())),
            "planner_reported_cost_usd": planner_cost,
            "arms": {},
        }
        for case in route_cases:
            iid = case["instance_id"]
            csv_rows.append(
                {
                    "instance_id": iid,
                    "repo": case["repo"],
                    "language_group": case["language_group"],
                    "route": route,
                    "arm": "unreranked",
                    "candidate_count": len(case["candidates"]),
                    "candidate_span_coverage": candidate_coverage(case)["span_recall"],
                    **values[iid],
                    "retrieval_ms": retrieval[iid],
                    "rerank_ms": 0.0,
                    "stage_sum_ms": retrieval[iid],
                    "rerank_recorded_cost_usd": 0.0,
                    "failed_calls": 0,
                }
            )

    for path in args.runs:
        config = json.loads((path / "config.json").read_text())
        route = hashes[config["prepared_manifest_sha256"]]
        config, rows, values, artifacts, api = read_run(
            path, roots[route], published[route], aligned[route]
        )
        model = config["model"]
        if model not in (JEV, QWEN) or (route, model) in per_case:
            raise ValueError("Unexpected or duplicate reranker arm")
        per_case[(route, model)] = values
        rows_by_arm[(route, model)] = rows
        retrieval = {
            r["instance_id"]: r["retrieval_ms"]
            for r in csv_rows
            if r["route"] == route and r["arm"] == "unreranked"
        }
        result["routes"][route]["arms"][model] = {
            "config": config,
            "raw_artifacts": {"directory": str(path), "sha256": artifacts},
            "metrics": mean_metrics(values),
            "rerank_ms": distribution([r["rerank_ms"] for r in rows.values()]),
            "stage_sum_ms": distribution(
                [retrieval[i] + rows[i]["rerank_ms"] for i in rows]
            ),
            "recorded_rerank_cost_usd": sum(
                r["recorded_cost_usd"] for r in rows.values()
            ),
            "failed_calls": sum(r["failed_calls"] for r in rows.values()),
            "failed_observations": sum(
                bool(r["error"] or r["failed_calls"]) for r in rows.values()
            ),
            "api": api,
            "per_language": {
                lang: mean_metrics(
                    {
                        c["instance_id"]: values[c["instance_id"]]
                        for c in cases
                        if c["language_group"] == lang
                    }
                )
                for lang in sorted({c["language_group"] for c in cases})
            },
        }
        if route in ("bm25", "grep"):
            if not all(
                np.isclose(
                    v,
                    original["base_aligned_sensitivity"]["routes"][route]["arms"][
                        model
                    ][k],
                )
                for k, v in mean_metrics(values).items()
            ):
                raise ValueError(
                    "Reused results differ from the existing aligned report"
                )
        for case in aligned[route]:
            iid = case["instance_id"]
            row = rows[iid]
            csv_rows.append(
                {
                    "instance_id": iid,
                    "repo": case["repo"],
                    "language_group": case["language_group"],
                    "route": route,
                    "arm": model,
                    "candidate_count": len(case["candidates"]),
                    "candidate_span_coverage": candidate_coverage(case)["span_recall"],
                    **values[iid],
                    "retrieval_ms": retrieval[iid],
                    "rerank_ms": row["rerank_ms"],
                    "stage_sum_ms": retrieval[iid] + row["rerank_ms"],
                    "rerank_recorded_cost_usd": row["recorded_cost_usd"],
                    "failed_calls": row["failed_calls"],
                }
            )
    expected = {(route, model) for route in roots for model in (JEV, QWEN)}
    if set(rows_by_arm) != expected:
        raise ValueError("Every route needs both reranker controls")
    for model in (JEV, QWEN):
        configs = [result["routes"][route]["arms"][model]["config"] for route in roots]
        for key in ("backend", "model", "revision", "batch_size", "qwen_max_length"):
            if any(c[key] != configs[0][key] for c in configs):
                raise ValueError(f"Reranker configurations differ in {key}")
    comparisons = [
        (("grep", JEV), (route, QWEN)) for route in ("dense", "hybrid", "bm25")
    ]
    comparisons += [(("bm25", JEV), (route, QWEN)) for route in ("dense", "hybrid")]
    comparisons += [((route, JEV), (route, QWEN)) for route in roots]
    comparisons += [((route, JEV), (route, "unreranked")) for route in roots]
    for left, right in comparisons:
        a, b = per_case[left], per_case[right]
        differences = {i: a[i]["span_recall@5"] - b[i]["span_recall@5"] for i in a}
        result["paired_comparisons"]["/".join(left) + " minus " + "/".join(right)] = {
            "metrics": {
                key: paired_bootstrap(cases, a, b, key)
                for key in (
                    "span_recall@1",
                    "span_recall@5",
                    "span_recall@10",
                    "span_mrr",
                )
            },
            "win_tie_loss": {
                "win": sum(d > 1e-9 for d in differences.values()),
                "tie": sum(abs(d) <= 1e-9 for d in differences.values()),
                "loss": sum(d < -1e-9 for d in differences.values()),
            },
        }
    prep = [c["dense_preparation"] for c in aligned["dense"]]
    result["offline_preparation"] = {
        "new_document_texts": sum(c["new_document_texts"] for c in prep),
        "document_encode_ms": sum(c["document_encode_ms"] for c in prep),
        "faiss_build_ms": sum(c["faiss_build_ms"] for c in prep),
        "query_tokens": distribution([c["query_tokens"] for c in prep]),
        "truncated_queries": sum(c["query_truncated"] for c in prep),
        "max_document_tokens": max(c["document_max_tokens"] for c in prep),
        "truncated_document_occurrences_across_snapshots": sum(
            c["documents_truncated"] for c in prep
        ),
    }
    args.case_csv.parent.mkdir(parents=True, exist_ok=True)
    exported_metrics = {
        "span_recall@1",
        "span_recall@5",
        "span_recall@10",
        "span_mrr",
        "span_all@5",
        "file_recall@5",
    }
    metric_keys = set(result["routes"]["bm25"]["unreranked"])
    fields = [k for k in csv_rows[0] if k not in metric_keys or k in exported_metrics]
    with args.case_csv.open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=fields, lineterminator="\n", extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(csv_rows)
    result["per_case_csv_sha256"] = digest(args.case_csv)
    write_json(args.output, result)
    print(
        json.dumps(
            {
                "routes": {
                    name: {
                        "coverage": r["coverage"],
                        "unreranked": r["unreranked"]["span_recall@5"],
                        "reranked": {
                            m: a["metrics"]["span_recall@5"]
                            for m, a in r["arms"].items()
                        },
                    }
                    for name, r in result["routes"].items()
                },
                "paired": result["paired_comparisons"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
