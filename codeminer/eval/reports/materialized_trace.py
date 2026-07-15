# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Snapshot-clustered summaries for the mixed-operation materialization trace."""

from __future__ import annotations

import math
import random
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence


def aggregate_materialized_trace(
    plan: Mapping[str, Any],
    reports: Mapping[str, Mapping[str, Any]],
    *,
    bootstrap_samples: int = 10_000,
    seed: int = 20260713,
    consumer_counts: Sequence[int] = (1, 5, 10, 20, 50, 100),
) -> dict[str, Any]:
    """Aggregate complete reports with one repository snapshot as one sample."""

    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be positive")
    counts = sorted({int(value) for value in consumer_counts})
    if not counts or counts[0] <= 0:
        raise ValueError("consumer_counts must be positive")

    snapshots = plan.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        raise ValueError("plan has no snapshots")
    expected_ids = [str(row["instance_id"]) for row in snapshots]
    if len(expected_ids) != len(set(expected_ids)):
        raise ValueError("plan contains duplicate instance ids")
    missing = sorted(set(expected_ids) - set(reports))
    extra = sorted(set(reports) - set(expected_ids))
    if missing or extra:
        raise ValueError(f"report identity mismatch: missing={missing}, extra={extra}")

    plan_protocol = plan.get("protocol") or {}
    rows = [
        _snapshot_row(
            snapshot,
            reports[str(snapshot["instance_id"])],
            plan_protocol=plan_protocol,
        )
        for snapshot in snapshots
    ]
    rng = random.Random(seed)
    overall = _aggregate_rows(rows, bootstrap_samples=bootstrap_samples, rng=rng)
    by_language = {
        language: _aggregate_rows(
            [row for row in rows if row["language"] == language],
            bootstrap_samples=bootstrap_samples,
            rng=rng,
        )
        for language in sorted({row["language"] for row in rows})
    }

    amortization_curve = []
    for consumers in counts:
        requests = []
        process_isolated = {
            "projected_total_seconds": [],
            "materialization_seconds_per_consumer": [],
            "load_seconds_per_consumer": [],
            "service_seconds_per_consumer": [],
            "effective_seconds_per_consumer": [],
        }
        shared_runtime = {key: [] for key in process_isolated}
        for row in rows:
            scale = consumers / row["source_queries"]
            service_total = scale * row["service_seconds"]
            isolated_total = (
                row["materialization_seconds"]
                + consumers * row["load_seconds"]
                + service_total
            )
            resident_total = (
                row["materialization_seconds"] + row["load_seconds"] + service_total
            )
            requests.append(scale * row["request_count"])
            for values, total, load_per_consumer in (
                (process_isolated, isolated_total, row["load_seconds"]),
                (
                    shared_runtime,
                    resident_total,
                    row["load_seconds"] / consumers,
                ),
            ):
                values["projected_total_seconds"].append(total)
                values["materialization_seconds_per_consumer"].append(
                    row["materialization_seconds"] / consumers
                )
                values["load_seconds_per_consumer"].append(load_per_consumer)
                values["service_seconds_per_consumer"].append(
                    row["service_seconds"] / row["source_queries"]
                )
                values["effective_seconds_per_consumer"].append(total / consumers)
        amortization_curve.append(
            {
                "consumer_count": consumers,
                "request_count_per_snapshot": _summary(
                    requests, bootstrap_samples=bootstrap_samples, rng=rng
                ),
                "deployment_models": {
                    "artifact_shared_process_isolated": _summarize_curve_model(
                        process_isolated,
                        bootstrap_samples=bootstrap_samples,
                        rng=rng,
                    ),
                    "shared_resident_runtime": _summarize_curve_model(
                        shared_runtime,
                        bootstrap_samples=bootstrap_samples,
                        rng=rng,
                    ),
                },
            }
        )

    protocol = plan.get("protocol") or {}
    return {
        "schema_version": 2,
        "protocol": {
            "workload_kind": protocol.get("workload_kind"),
            "claim_boundary": protocol.get("claim_boundary"),
            "statistical_unit": "repository snapshot",
            "snapshot_count": len(rows),
            "source_query_count": sum(row["source_queries"] for row in rows),
            "request_count": sum(row["request_count"] for row in rows),
            "measured_row_count": sum(row["measured_rows"] for row in rows),
            "bootstrap": {
                "estimand": "median across snapshot-level summaries",
                "samples": bootstrap_samples,
                "seed": seed,
                "interval": "percentile 95%",
            },
            "amortization_assumption": (
                "the fixed per-snapshot operation mix scales linearly with the "
                "number of sequential source-query sessions; concurrency and "
                "queueing are not modeled"
            ),
            "primary_deployment_model": "artifact_shared_process_isolated",
            "deployment_models": {
                "artifact_shared_process_isolated": (
                    "one materialized artifact is shared, but each consumer hydrates "
                    "its own process: B + qL + (q/N)S"
                ),
                "shared_resident_runtime": (
                    "one materialized artifact and one hydrated service process are "
                    "shared: B + L + (q/N)S; reported as an optimistic serving bound"
                ),
            },
            "output_guard": (
                "complete canonical JSON fingerprints are stable across measured "
                "repetitions; this is not cross-provider semantic equivalence"
            ),
        },
        "overall": overall,
        "by_language": by_language,
        "amortization_curve": amortization_curve,
        "snapshot_rows": rows,
    }


def _summarize_curve_model(
    values: Mapping[str, Sequence[float]],
    *,
    bootstrap_samples: int,
    rng: random.Random,
) -> dict[str, Any]:
    return {
        key: _summary(
            samples,
            bootstrap_samples=bootstrap_samples,
            rng=rng,
        )
        for key, samples in values.items()
    }


def _snapshot_row(
    snapshot: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    plan_protocol: Mapping[str, Any],
) -> dict:
    instance_id = str(snapshot["instance_id"])
    manifest = report.get("manifest") or {}
    expected_repo = Path(str(snapshot["repo_path"])).resolve()
    actual_repo = Path(str(manifest.get("repository", ""))).resolve()
    if actual_repo != expected_repo:
        raise ValueError(f"{instance_id}: report repository does not match plan")
    if manifest.get("commit") != snapshot.get("commit"):
        raise ValueError(f"{instance_id}: report commit does not match plan")
    trace = report.get("trace") or {}
    if trace.get("sha256") != snapshot.get("trace_sha256"):
        raise ValueError(f"{instance_id}: report trace hash does not match plan")

    protocol = report.get("protocol") or {}
    if protocol.get("hydration_process_boundary") != (
        "fresh process after materialization checkpoint"
    ):
        raise ValueError(
            f"{instance_id}: manifest hydration was not measured in a fresh process"
        )
    request_count = int(protocol.get("request_count", 0))
    expected_requests = int(snapshot["total_requests"])
    if request_count != expected_requests:
        raise ValueError(
            f"{instance_id}: expected {expected_requests} requests, got {request_count}"
        )
    repetitions = int(protocol.get("measured_repetitions", 0))
    measured_rows = report.get("rows")
    if not isinstance(measured_rows, list) or len(measured_rows) != (
        request_count * repetitions
    ):
        raise ValueError(f"{instance_id}: incomplete measured rows")
    _validate_measured_rows(
        instance_id,
        measured_rows,
        request_count=request_count,
        repetitions=repetitions,
        report_protocol=protocol,
        plan_protocol=plan_protocol,
        expected_guarded_requests=int(snapshot.get("lsp_requests", 0)),
    )
    materialization = report.get("materialization")
    if not isinstance(materialization, Mapping):
        raise ValueError(f"{instance_id}: missing materialization record")
    resources = report.get("resources")
    if not isinstance(resources, Mapping):
        raise ValueError(f"{instance_id}: missing resource peaks")

    operation_values: dict[str, list[float]] = {}
    operation_nonempty: dict[str, list[float]] = {}
    for measured in measured_rows:
        operation = str(measured["operation"])
        operation_values.setdefault(operation, []).append(
            float(measured["elapsed_seconds"])
        )
        operation_nonempty.setdefault(operation, []).append(
            float(int(measured.get("result_count", 0) > 0))
        )
    operation_p50 = {
        operation: statistics.median(values)
        for operation, values in sorted(operation_values.items())
    }
    operation_nonempty_fraction = {
        operation: statistics.fmean(values)
        for operation, values in sorted(operation_nonempty.items())
    }

    phases = report.get("phases") or {}
    load_seconds = _finite_nonnegative(phases.get("load_seconds"), "load_seconds")
    materialization_seconds = _finite_nonnegative(
        materialization.get("wall_seconds"), "materialization wall_seconds"
    )
    measured_request_seconds = _finite_nonnegative(
        phases.get("measured_request_seconds"), "measured_request_seconds"
    )
    service_seconds = measured_request_seconds / repetitions
    source_queries = int(snapshot["synthesis_queries"])
    if source_queries <= 0:
        raise ValueError(f"{instance_id}: source query count must be positive")
    setup_seconds = materialization_seconds + load_seconds

    return {
        "instance_id": instance_id,
        "snapshot_id": str(snapshot["snapshot_id"]),
        "repository": str(snapshot["repository"]),
        "commit": str(snapshot["commit"]),
        "language": str(snapshot["language_group"]),
        "source_queries": source_queries,
        "request_count": request_count,
        "measured_rows": len(measured_rows),
        "materialization_seconds": materialization_seconds,
        "load_seconds": load_seconds,
        "setup_seconds": setup_seconds,
        "service_seconds": service_seconds,
        "total_observed_workload_seconds": setup_seconds + service_seconds,
        "setup_seconds_per_source_query": setup_seconds / source_queries,
        "setup_seconds_per_request": setup_seconds / request_count,
        "service_seconds_per_request": service_seconds / request_count,
        "artifact_bytes": int(materialization["artifact_bytes"]),
        "per_index_seconds": {
            str(key): _finite_nonnegative(value, f"per-index {key}")
            for key, value in sorted(materialization["per_index_seconds"].items())
        },
        "operation_p50_seconds": operation_p50,
        "operation_nonempty_fraction": operation_nonempty_fraction,
        "process_max_rss_bytes": int(resources["process_max_rss_bytes"]),
        "max_child_rss_bytes": int(resources["max_child_rss_bytes"]),
        "cuda_peak_allocated_bytes": _optional_int(
            resources.get("cuda_peak_allocated_bytes")
        ),
        "cuda_peak_reserved_bytes": _optional_int(
            resources.get("cuda_peak_reserved_bytes")
        ),
    }


def _validate_measured_rows(
    instance_id: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    request_count: int,
    repetitions: int,
    report_protocol: Mapping[str, Any],
    plan_protocol: Mapping[str, Any],
    expected_guarded_requests: int,
) -> None:
    """Reject complete-looking reports that violate the replay contract."""

    expected_repetitions = list(range(repetitions))
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        request_id = str(row.get("request_id") or "").strip()
        if not request_id:
            raise ValueError(f"{instance_id}: measured row has no request id")
        groups.setdefault(request_id, []).append(row)
    if len(groups) != request_count:
        raise ValueError(
            f"{instance_id}: expected {request_count} unique requests, "
            f"got {len(groups)}"
        )

    operation_counts: dict[str, int] = {}
    guarded_requests = 0
    for request_id, group in sorted(groups.items()):
        observed_repetitions = [row.get("repetition") for row in group]
        if (
            any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in observed_repetitions
            )
            or sorted(observed_repetitions) != expected_repetitions
        ):
            raise ValueError(
                f"{instance_id}: {request_id} has invalid measured repetitions"
            )

        operations = {str(row.get("operation") or "").strip() for row in group}
        if len(operations) != 1 or "" in operations:
            raise ValueError(
                f"{instance_id}: {request_id} changes operation across repetitions"
            )
        operation = operations.pop()
        operation_counts[operation] = operation_counts.get(operation, 0) + 1

        fingerprints = {row.get("response_fingerprint") for row in group}
        if len(fingerprints) != 1 or not _is_sha256(next(iter(fingerprints))):
            raise ValueError(
                f"{instance_id}: {request_id} has an unstable response fingerprint"
            )
        result_counts = {row.get("result_count") for row in group}
        if len(result_counts) != 1 or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in result_counts
        ):
            raise ValueError(
                f"{instance_id}: {request_id} has an unstable result count"
            )
        if any(row.get("error") not in (None, "") for row in group):
            raise ValueError(f"{instance_id}: {request_id} contains an error row")
        for row in group:
            _finite_nonnegative(
                row.get("elapsed_seconds"),
                f"{instance_id}: {request_id} elapsed_seconds",
            )

        contract_presence = ["contract_fingerprint" in row for row in group]
        if any(contract_presence) != all(contract_presence):
            raise ValueError(
                f"{instance_id}: {request_id} has incomplete contract fingerprints"
            )
        if all(contract_presence):
            guarded_requests += 1
            contract_fingerprints = {row.get("contract_fingerprint") for row in group}
            if len(contract_fingerprints) != 1 or not _is_sha256(
                next(iter(contract_fingerprints))
            ):
                raise ValueError(
                    f"{instance_id}: {request_id} has an unstable contract fingerprint"
                )

    expected_mix = plan_protocol.get("per_snapshot")
    if isinstance(expected_mix, Mapping):
        normalized_mix = {
            str(operation): int(count) for operation, count in expected_mix.items()
        }
        if operation_counts != normalized_mix:
            raise ValueError(
                f"{instance_id}: operation mix does not match the plan: "
                f"expected={normalized_mix}, actual={operation_counts}"
            )

    reported_guarded_requests = int(
        report_protocol.get("semantic_guarded_request_count", 0)
    )
    if reported_guarded_requests != expected_guarded_requests:
        raise ValueError(f"{instance_id}: semantic guard count does not match the plan")
    if guarded_requests != reported_guarded_requests:
        raise ValueError(f"{instance_id}: guarded row groups do not match the protocol")
    if report_protocol.get("output_guard") != (
        "canonical JSON SHA-256 stable across repetitions"
    ):
        raise ValueError(f"{instance_id}: unexpected output guard")
    if (
        operation_counts.get("dense", 0)
        and report_protocol.get("dense_query_cache")
        != "cleared before each independent dense request"
    ):
        raise ValueError(f"{instance_id}: dense requests reused a query embedding")


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _aggregate_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    bootstrap_samples: int,
    rng: random.Random,
) -> dict[str, Any]:
    scalar_keys = (
        "materialization_seconds",
        "load_seconds",
        "setup_seconds",
        "service_seconds",
        "total_observed_workload_seconds",
        "setup_seconds_per_source_query",
        "setup_seconds_per_request",
        "service_seconds_per_request",
        "artifact_bytes",
        "process_max_rss_bytes",
        "max_child_rss_bytes",
        "cuda_peak_allocated_bytes",
        "cuda_peak_reserved_bytes",
    )
    result = {
        key: _summary(
            [float(row[key]) for row in rows if row.get(key) is not None],
            bootstrap_samples=bootstrap_samples,
            rng=rng,
        )
        for key in scalar_keys
    }
    result["per_index_seconds"] = _nested_summary(
        rows,
        "per_index_seconds",
        bootstrap_samples=bootstrap_samples,
        rng=rng,
    )
    result["operation_p50_seconds"] = _nested_summary(
        rows,
        "operation_p50_seconds",
        bootstrap_samples=bootstrap_samples,
        rng=rng,
    )
    result["operation_nonempty_fraction"] = _nested_summary(
        rows,
        "operation_nonempty_fraction",
        bootstrap_samples=bootstrap_samples,
        rng=rng,
    )
    return result


def _nested_summary(
    rows: Sequence[Mapping[str, Any]],
    key: str,
    *,
    bootstrap_samples: int,
    rng: random.Random,
) -> dict[str, Any]:
    names = sorted({name for row in rows for name in row[key]})
    summaries = {}
    for name in names:
        values = [float(row[key][name]) for row in rows if name in row[key]]
        if len(values) != len(rows):
            raise ValueError(f"{key}.{name} is missing from some snapshots")
        summaries[name] = _summary(values, bootstrap_samples=bootstrap_samples, rng=rng)
    return summaries


def _summary(
    values: Sequence[float],
    *,
    bootstrap_samples: int,
    rng: random.Random,
) -> dict[str, Any] | None:
    if not values:
        return None
    clean = [_finite_nonnegative(value, "summary value") for value in values]
    n = len(clean)
    medians = [
        statistics.median(clean[rng.randrange(n)] for _ in range(n))
        for _ in range(bootstrap_samples)
    ]
    return {
        "n": n,
        "median": statistics.median(clean),
        "p25": _percentile(clean, 0.25),
        "p75": _percentile(clean, 0.75),
        "ci95_low": _percentile(medians, 0.025),
        "ci95_high": _percentile(medians, 0.975),
    }


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = quantile * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _finite_nonnegative(value: Any, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return number


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
