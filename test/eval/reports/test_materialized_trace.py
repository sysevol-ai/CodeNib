# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from codeminer.eval.reports.materialized_trace import aggregate_materialized_trace


def _plan(tmp_path: Path) -> dict:
    snapshots = []
    for index, language in enumerate(("Python", "Rust")):
        repo = tmp_path / f"repo-{index}"
        repo.mkdir()
        snapshots.append(
            {
                "snapshot_id": f"snapshot-{index}",
                "repository": f"org/repo-{index}",
                "commit": f"commit-{index}",
                "instance_id": f"instance-{index}",
                "language_group": language,
                "repo_path": str(repo),
                "synthesis_queries": 20,
                "lsp_requests": 0,
                "total_requests": 2,
                "trace_sha256": f"trace-{index}",
            }
        )
    return {
        "protocol": {
            "workload_kind": "controlled mixed-operation trace",
            "claim_boundary": "not an observed agent call distribution",
            "per_snapshot": {"dense": 1, "bm25": 1},
        },
        "snapshots": snapshots,
    }


def _report(snapshot: dict, scale: float) -> dict:
    measured = []
    for repetition in range(2):
        measured.extend(
            [
                {
                    "request_id": "dense",
                    "operation": "dense",
                    "repetition": repetition,
                    "elapsed_seconds": 0.1 * scale,
                    "response_fingerprint": "a" * 64,
                    "result_count": 5,
                },
                {
                    "request_id": "bm25",
                    "operation": "bm25",
                    "repetition": repetition,
                    "elapsed_seconds": 0.01 * scale,
                    "response_fingerprint": "b" * 64,
                    "result_count": 0,
                },
            ]
        )
    return {
        "manifest": {
            "repository": snapshot["repo_path"],
            "commit": snapshot["commit"],
        },
        "trace": {"sha256": snapshot["trace_sha256"]},
        "protocol": {
            "request_count": 2,
            "measured_repetitions": 2,
            "semantic_guarded_request_count": 0,
            "output_guard": "canonical JSON SHA-256 stable across repetitions",
            "dense_query_cache": "cleared before each independent dense request",
            "hydration_process_boundary": (
                "fresh process after materialization checkpoint"
            ),
        },
        "phases": {
            "load_seconds": 2.0 * scale,
            "measured_request_seconds": 0.22 * scale,
        },
        "rows": measured,
        "materialization": {
            "wall_seconds": 100.0 * scale,
            "per_index_seconds": {
                "bm25": 10.0 * scale,
                "vector": 80.0 * scale,
            },
            "artifact_bytes": int(1000 * scale),
        },
        "resources": {
            "process_max_rss_bytes": int(500 * scale),
            "max_child_rss_bytes": int(200 * scale),
            "cuda_peak_allocated_bytes": int(300 * scale),
            "cuda_peak_reserved_bytes": int(400 * scale),
        },
    }


def test_aggregate_uses_snapshot_level_summaries(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    reports = {
        row["instance_id"]: _report(row, scale=index + 1)
        for index, row in enumerate(plan["snapshots"])
    }

    result = aggregate_materialized_trace(
        plan, reports, bootstrap_samples=100, seed=7, consumer_counts=(20,)
    )

    assert result["protocol"]["snapshot_count"] == 2
    assert result["protocol"]["request_count"] == 4
    assert result["overall"]["materialization_seconds"]["median"] == 150.0
    assert result["overall"]["operation_p50_seconds"]["dense"]["n"] == 2
    assert result["overall"]["operation_nonempty_fraction"]["bm25"]["median"] == 0.0
    curve = result["amortization_curve"][0]
    assert curve["consumer_count"] == 20
    assert curve["request_count_per_snapshot"]["median"] == 2.0
    models = curve["deployment_models"]
    assert models["artifact_shared_process_isolated"]["projected_total_seconds"][
        "median"
    ] == pytest.approx(210.165)
    assert models["shared_resident_runtime"]["projected_total_seconds"][
        "median"
    ] == pytest.approx(153.165)
    assert result["protocol"]["primary_deployment_model"] == (
        "artifact_shared_process_isolated"
    )
    assert set(result["by_language"]) == {"Python", "Rust"}


def test_aggregate_rejects_snapshot_identity_mismatch(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    reports = {row["instance_id"]: _report(row, scale=1.0) for row in plan["snapshots"]}
    reports["instance-0"]["manifest"]["commit"] = "wrong"

    with pytest.raises(ValueError, match="commit does not match"):
        aggregate_materialized_trace(plan, reports, bootstrap_samples=10)


def test_aggregate_requires_every_planned_snapshot(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    reports = {"instance-0": _report(plan["snapshots"][0], scale=1.0)}

    with pytest.raises(ValueError, match="missing=\\['instance-1'\\]"):
        aggregate_materialized_trace(plan, reports, bootstrap_samples=10)


def test_aggregate_rejects_same_process_hydration(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    reports = {row["instance_id"]: _report(row, scale=1.0) for row in plan["snapshots"]}
    reports["instance-0"]["protocol"][
        "hydration_process_boundary"
    ] = "same process as materialization"

    with pytest.raises(ValueError, match="not measured in a fresh process"):
        aggregate_materialized_trace(plan, reports, bootstrap_samples=10)


def test_aggregate_rejects_duplicate_repetitions(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    reports = {row["instance_id"]: _report(row, scale=1.0) for row in plan["snapshots"]}
    reports["instance-0"]["rows"][2]["repetition"] = 0

    with pytest.raises(ValueError, match="invalid measured repetitions"):
        aggregate_materialized_trace(plan, reports, bootstrap_samples=10)


def test_aggregate_rejects_unstable_fingerprints(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    reports = {row["instance_id"]: _report(row, scale=1.0) for row in plan["snapshots"]}
    reports["instance-0"]["rows"][2]["response_fingerprint"] = "c" * 64

    with pytest.raises(ValueError, match="unstable response fingerprint"):
        aggregate_materialized_trace(plan, reports, bootstrap_samples=10)


def test_aggregate_rejects_operation_mix_drift(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    reports = {row["instance_id"]: _report(row, scale=1.0) for row in plan["snapshots"]}
    for row in reports["instance-0"]["rows"]:
        if row["request_id"] == "bm25":
            row["operation"] = "dense"

    with pytest.raises(ValueError, match="operation mix does not match"):
        aggregate_materialized_trace(plan, reports, bootstrap_samples=10)


def test_aggregate_rejects_incomplete_contract_fingerprints(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    reports = {row["instance_id"]: _report(row, scale=1.0) for row in plan["snapshots"]}
    plan["snapshots"][0]["lsp_requests"] = 1
    reports["instance-0"]["protocol"]["semantic_guarded_request_count"] = 1
    reports["instance-0"]["rows"][0]["contract_fingerprint"] = "d" * 64

    with pytest.raises(ValueError, match="incomplete contract fingerprints"):
        aggregate_materialized_trace(plan, reports, bootstrap_samples=10)
