#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Aggregate strict incremental-maintenance results for paper analysis."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codeminer.eval.maintenance_manifest import load_manifest  # noqa: E402


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = q * (len(ordered) - 1)
    lo, hi = math.floor(rank), math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def _distribution(values: Iterable[Any]) -> dict[str, int | float | None]:
    measured = [value for raw in values if (value := _number(raw)) is not None]
    positive = [value for value in measured if value > 0]
    return {
        "count": len(measured),
        "p25": _percentile(measured, 0.25),
        "p50": _percentile(measured, 0.50),
        "p75": _percentile(measured, 0.75),
        "geomean": (
            math.exp(sum(math.log(value) for value in positive) / len(positive))
            if positive
            else None
        ),
        "min": min(measured) if measured else None,
        "max": max(measured) if measured else None,
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        rows.append(payload)
    return rows


def _transition_key(row: Mapping[str, Any]) -> tuple[str, int] | None:
    instance_id = row.get("instance_id")
    step = row.get("step")
    if not isinstance(instance_id, str) or not isinstance(step, int) or step < 1:
        return None
    return instance_id, step


def _arm_guard(row: Mapping[str, Any], arm: str) -> bool:
    fidelity = (row.get(arm) or {}).get("fidelity") or {}
    return bool(
        fidelity.get("status") == "ok"
        and fidelity.get("equivalence_guardrail_pass") is True
    )


def _ratio(numerator: Any, denominator: Any) -> float | None:
    top, bottom = _number(numerator), _number(denominator)
    return (
        top / bottom if top is not None and bottom is not None and bottom > 0 else None
    )


def _graph_arm_reliability(
    rows: Sequence[Mapping[str, Any]], arm: str
) -> dict[str, Any]:
    payloads = [row.get(arm) or {} for row in rows]
    attempted = [payload for payload in payloads if payload]
    succeeded = [payload for payload in attempted if payload.get("status") == "ok"]

    def failures(payload: Mapping[str, Any]) -> Sequence[Any]:
        return (
            payload.get("attempt_failures")
            or payload.get("prior_attempt_failures")
            or ()
        )

    return {
        "attempted_rows": len(attempted),
        "succeeded_rows": len(succeeded),
        "failed_rows": len(attempted) - len(succeeded),
        "recovered_rows": sum(bool(failures(payload)) for payload in succeeded),
        "failed_attempts": sum(len(failures(payload)) for payload in attempted),
        "strategy_attempts": _distribution(
            payload.get("strategy_attempts") for payload in attempted
        ),
        "recovery_s": _distribution(payload.get("recovery_s") for payload in attempted),
    }


def _graph_arm_fidelity(rows: Sequence[Mapping[str, Any]], arm: str) -> dict[str, Any]:
    denominator = len(rows)
    behavior_exact = 0
    raw_whole_exact = 0
    raw_changed_exact = 0
    strict_exact = 0
    fallback_rows = 0
    for row in rows:
        payload = row.get(arm) or {}
        fidelity = payload.get("fidelity") or {}
        behavior = fidelity.get("behavior") or {}
        raw_graph = fidelity.get("raw_graph") or {}
        behavior_exact += bool(
            fidelity.get("behavior_exact") is True or behavior.get("exact") is True
        )
        raw_whole_exact += raw_graph.get("whole_exact") is True
        raw_changed_exact += raw_graph.get("changed_exact") is True
        strict_exact += _arm_guard(row, arm)
        stats = payload.get("stats") or {}
        fallback_rows += int(stats.get("files_symbol_identity_fallback") or 0) > 0

    def rate(count: int) -> float | None:
        return count / denominator if denominator else None

    def metric(name: str, field: str) -> Iterable[Any]:
        for row in rows:
            fidelity = (row.get(arm) or {}).get("fidelity") or {}
            metrics = (fidelity.get("raw_graph") or {}).get("metrics") or {}
            yield (metrics.get(name) or {}).get(field)

    return {
        "rows": denominator,
        "behavior_exact": behavior_exact,
        "behavior_exact_rate": rate(behavior_exact),
        "raw_whole_exact": raw_whole_exact,
        "raw_whole_exact_rate": rate(raw_whole_exact),
        "raw_changed_exact": raw_changed_exact,
        "raw_changed_exact_rate": rate(raw_changed_exact),
        "strict_exact": strict_exact,
        "strict_exact_rate": rate(strict_exact),
        "fallback_rows": fallback_rows,
        "behavior_agreement": _distribution(
            (((row.get(arm) or {}).get("fidelity") or {}).get("behavior") or {}).get(
                "equivalent_fraction"
            )
            for row in rows
        ),
        "edge_f1": _distribution(metric("edges", "f1")),
        "reference_edge_f1": _distribution(metric("reference_edges", "f1")),
        "changed_edge_f1": _distribution(metric("changed_edges", "f1")),
    }


def _graph_group(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    source_rows = [row for row in rows if int(row.get("delta_files") or 0) > 0]
    symbol_admitted = [
        row for row in source_rows if _arm_guard(row, "symbol-level-patch")
    ]
    file_admitted = [row for row in source_rows if _arm_guard(row, "file-level-patch")]
    paired_admitted = [
        row
        for row in source_rows
        if _arm_guard(row, "symbol-level-patch") and _arm_guard(row, "file-level-patch")
    ]

    def measured(row: Mapping[str, Any], arm: str) -> bool:
        return bool(
            (row.get("fully-rebuild") or {}).get("status") == "ok"
            and (row.get(arm) or {}).get("status") == "ok"
        )

    symbol_measured = [
        row for row in source_rows if measured(row, "symbol-level-patch")
    ]
    file_measured = [row for row in source_rows if measured(row, "file-level-patch")]
    paired_measured = [
        row
        for row in source_rows
        if measured(row, "symbol-level-patch") and measured(row, "file-level-patch")
    ]

    def full_time(row: Mapping[str, Any]) -> Any:
        return (row.get("fully-rebuild") or {}).get("t_s")

    def arm_time(row: Mapping[str, Any], arm: str, *, amortized: bool) -> Any:
        payload = row.get(arm) or {}
        return payload.get("amortized_t_s") if amortized else payload.get("t_s")

    def fault_inclusive_time(row: Mapping[str, Any], arm: str) -> Any:
        payload = row.get(arm) or {}
        if arm == "fully-rebuild":
            return payload.get("fault_inclusive_t_s", payload.get("t_s"))
        return payload.get(
            "fault_inclusive_amortized_t_s",
            payload.get("amortized_t_s", payload.get("t_s")),
        )

    denominator = len(source_rows)
    symbol_speedup = [
        _ratio(full_time(row), arm_time(row, "symbol-level-patch", amortized=True))
        for row in symbol_admitted
    ]
    file_speedup = [
        _ratio(full_time(row), arm_time(row, "file-level-patch", amortized=True))
        for row in file_admitted
    ]
    return {
        "rows": len(rows),
        "source_changing_rows": denominator,
        "no_source_change_rows": len(rows) - denominator,
        "rows_with_error": sum(
            bool(row.get("error"))
            or any(
                (row.get(arm) or {}).get("status") not in {None, "ok"}
                for arm in ("fully-rebuild", "file-level-patch", "symbol-level-patch")
            )
            for row in rows
        ),
        "symbol": {
            "measured": len(symbol_measured),
            "observed_amortized_speedup_vs_full": _distribution(
                _ratio(
                    full_time(row),
                    arm_time(row, "symbol-level-patch", amortized=True),
                )
                for row in symbol_measured
            ),
            "observed_steady_speedup_vs_full": _distribution(
                _ratio(
                    full_time(row),
                    arm_time(row, "symbol-level-patch", amortized=False),
                )
                for row in symbol_measured
            ),
            "admitted": len(symbol_admitted),
            "admission_rate": (
                len(symbol_admitted) / denominator if denominator else None
            ),
            "amortized_speedup_vs_full": _distribution(symbol_speedup),
            "steady_speedup_vs_full": _distribution(
                _ratio(
                    full_time(row), arm_time(row, "symbol-level-patch", amortized=False)
                )
                for row in symbol_admitted
            ),
            "amortized_update_s": _distribution(
                arm_time(row, "symbol-level-patch", amortized=True)
                for row in symbol_admitted
            ),
            "fault_inclusive_amortized_speedup_vs_full": _distribution(
                _ratio(
                    fault_inclusive_time(row, "fully-rebuild"),
                    fault_inclusive_time(row, "symbol-level-patch"),
                )
                for row in symbol_admitted
            ),
            "fault_inclusive_update_s": _distribution(
                fault_inclusive_time(row, "symbol-level-patch")
                for row in symbol_admitted
            ),
            "steady_update_s": _distribution(
                arm_time(row, "symbol-level-patch", amortized=False)
                for row in symbol_admitted
            ),
        },
        "file": {
            "measured": len(file_measured),
            "observed_amortized_speedup_vs_full": _distribution(
                _ratio(
                    full_time(row),
                    arm_time(row, "file-level-patch", amortized=True),
                )
                for row in file_measured
            ),
            "observed_steady_speedup_vs_full": _distribution(
                _ratio(
                    full_time(row),
                    arm_time(row, "file-level-patch", amortized=False),
                )
                for row in file_measured
            ),
            "admitted": len(file_admitted),
            "admission_rate": len(file_admitted) / denominator if denominator else None,
            "amortized_speedup_vs_full": _distribution(file_speedup),
            "steady_speedup_vs_full": _distribution(
                _ratio(
                    full_time(row), arm_time(row, "file-level-patch", amortized=False)
                )
                for row in file_admitted
            ),
        },
        "paired": {
            "measured": len(paired_measured),
            "observed_symbol_speedup_vs_file": _distribution(
                _ratio(
                    arm_time(row, "file-level-patch", amortized=True),
                    arm_time(row, "symbol-level-patch", amortized=True),
                )
                for row in paired_measured
            ),
            "admitted": len(paired_admitted),
            "symbol_speedup_vs_file": _distribution(
                _ratio(
                    arm_time(row, "file-level-patch", amortized=True),
                    arm_time(row, "symbol-level-patch", amortized=True),
                )
                for row in paired_admitted
            ),
        },
        "fully_rebuild_s": _distribution(full_time(row) for row in source_rows),
        "reliability": {
            arm: _graph_arm_reliability(source_rows, arm)
            for arm in (
                "fully-rebuild",
                "file-level-patch",
                "symbol-level-patch",
            )
        },
        "fidelity": {
            arm: _graph_arm_fidelity(source_rows, arm)
            for arm in ("file-level-patch", "symbol-level-patch")
        },
    }


def _graph_provider_stability(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize independent fresh rebuilds without inventing a pass threshold."""

    comparisons = []
    cases = []
    for row in rows:
        if row.get("status") != "ok":
            continue
        for comparison in row.get("comparisons") or ():
            if comparison.get("reference") != "original":
                continue
            metrics = comparison.get("metrics") or {}
            behavior = comparison.get("behavior") or {}
            comparisons.append(comparison)
            cases.append(
                {
                    "instance_id": row.get("instance_id"),
                    "language": row.get("language"),
                    "step": row.get("step"),
                    "repeat": row.get("repeat"),
                    "edge_f1": (metrics.get("edges") or {}).get("f1"),
                    "reference_edge_f1": (metrics.get("reference_edges") or {}).get(
                        "f1"
                    ),
                    "behavior_agreement": behavior.get("equivalent_fraction"),
                    "changed_exact": comparison.get("changed_exact") is True,
                }
            )

    return {
        "rows": len(rows),
        "successful_rows": sum(row.get("status") == "ok" for row in rows),
        "comparisons": len(comparisons),
        "whole_exact": sum(row.get("whole_exact") is True for row in comparisons),
        "changed_exact": sum(row.get("changed_exact") is True for row in comparisons),
        "behavior_exact": sum(row.get("behavior_exact") is True for row in comparisons),
        "fresh_rebuild_s": _distribution(
            (row.get("build") or {}).get("t_s") for row in rows
        ),
        "edge_f1": _distribution(
            ((row.get("metrics") or {}).get("edges") or {}).get("f1")
            for row in comparisons
        ),
        "reference_edge_f1": _distribution(
            ((row.get("metrics") or {}).get("reference_edges") or {}).get("f1")
            for row in comparisons
        ),
        "behavior_agreement": _distribution(
            (row.get("behavior") or {}).get("equivalent_fraction")
            for row in comparisons
        ),
        "cases": cases,
    }


def _graph_setup(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    unique: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows:
        instance_id = str(row.get("instance_id") or "unknown")
        for arm in ("file-level-patch", "symbol-level-patch"):
            payload = row.get(arm) or {}
            if payload.get("status") == "ok":
                unique.setdefault((instance_id, arm), payload)
    return {
        arm: {
            "instances": sum(key[1] == arm for key in unique),
            "server_start_s": _distribution(
                payload.get("server_start_s")
                for key, payload in unique.items()
                if key[1] == arm
            ),
            "workspace_warmup_s": _distribution(
                payload.get("workspace_warmup_s")
                for key, payload in unique.items()
                if key[1] == arm
            ),
            "phase_cold_start_s": _distribution(
                payload.get("phase_cold_start_s")
                for key, payload in unique.items()
                if key[1] == arm
            ),
        }
        for arm in ("symbol-level-patch", "file-level-patch")
    }


def _vector_group(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    source_rows = [
        row for row in rows if int(row.get("delta_files_test_excluded") or 0) > 0
    ]
    admitted = [
        row
        for row in source_rows
        if (row.get("fidelity") or {}).get("equivalence_guardrail_pass") is True
        and (row.get("fully-rebuild") or {}).get("status") == "ok"
        and (row.get("incremental") or {}).get("status") == "ok"
    ]
    denominator = len(source_rows)

    def fidelity_layers(row: Mapping[str, Any]) -> tuple[bool, bool, bool]:
        levels = (row.get("fidelity") or {}).get("levels") or {}
        payloads = list(levels.values())
        artifact_exact = bool(payloads) and all(
            payload.get("document_identities_exact") is True
            and payload.get("vectors_close") is True
            for payload in payloads
        )
        ordered_exact = bool(payloads) and all(
            (payload.get("replay") or {}).get("ordered_exact_fraction") == 1.0
            for payload in payloads
        )
        set_exact = bool(payloads) and all(
            (payload.get("replay") or {}).get("set_exact_fraction") == 1.0
            for payload in payloads
        )
        return artifact_exact, ordered_exact, set_exact

    fidelity = [fidelity_layers(row) for row in source_rows]
    return {
        "rows": len(rows),
        "source_changing_rows": denominator,
        "no_source_change_rows": len(rows) - denominator,
        "rows_with_error": sum(bool(row.get("error")) for row in rows),
        "fidelity": {
            "artifact_exact": sum(layer[0] for layer in fidelity),
            "ordered_replay_exact": sum(layer[1] for layer in fidelity),
            "set_replay_exact": sum(layer[2] for layer in fidelity),
        },
        "incremental": {
            "admitted": len(admitted),
            "admission_rate": len(admitted) / denominator if denominator else None,
            "speedup_vs_full": _distribution(
                _ratio(
                    (row.get("fully-rebuild") or {}).get("t_s"),
                    (row.get("incremental") or {}).get("t_s"),
                )
                for row in admitted
            ),
            "update_s": _distribution(
                (row.get("incremental") or {}).get("t_s") for row in admitted
            ),
            "cache_hit_rate": _distribution(
                ((row.get("incremental") or {}).get("metadata") or {}).get(
                    "cache_hit_rate"
                )
                for row in admitted
            ),
            "chunks_reembedded": _distribution(
                ((row.get("incremental") or {}).get("metadata") or {}).get(
                    "chunks_reembedded"
                )
                for row in admitted
            ),
        },
        "fully_rebuild_s": _distribution(
            (row.get("fully-rebuild") or {}).get("t_s") for row in source_rows
        ),
    }


def _completeness(
    rows: Sequence[Mapping[str, Any]],
    expected: set[tuple[str, int]],
) -> dict[str, Any]:
    observed = [key for row in rows if (key := _transition_key(row)) is not None]
    duplicates = sorted(key for key in set(observed) if observed.count(key) > 1)
    observed_set = set(observed)
    return {
        "expected_rows": len(expected),
        "observed_transition_rows": len(observed),
        "unique_transition_rows": len(observed_set),
        "missing": [list(key) for key in sorted(expected - observed_set)],
        "unexpected": [list(key) for key in sorted(observed_set - expected)],
        "duplicates": [list(key) for key in duplicates],
        "complete": observed_set == expected and not duplicates,
    }


def _validate_result_identity(
    name: str,
    rows: Sequence[Mapping[str, Any]],
    expected: set[tuple[str, int]] | None,
) -> None:
    """Reject inputs that would silently pool incompatible measurements."""

    protocols = {
        row.get("protocol_version")
        for row in rows
        if row.get("protocol_version") is not None
    }
    if len(protocols) != 1:
        ordered_protocols = sorted(protocols, key=repr)
        raise ValueError(f"{name} results mix protocol versions: {ordered_protocols!r}")

    observed = [key for row in rows if (key := _transition_key(row)) is not None]
    duplicate_keys = sorted(key for key in set(observed) if observed.count(key) > 1)
    if duplicate_keys:
        raise ValueError(
            f"{name} results contain duplicate transitions: {duplicate_keys!r}"
        )
    if expected is not None:
        unexpected = sorted(set(observed) - expected)
        if unexpected:
            raise ValueError(
                f"{name} results contain transitions outside the manifest: {unexpected!r}"
            )

    if name in {"graph", "vector"}:
        config_ids = {
            row.get("experiment_config_id")
            for row in rows
            if row.get("experiment_config_id") is not None
        }
        protocol = next(iter(protocols))
        fingerprint_protocol = isinstance(protocol, int) and (
            (name == "vector" and protocol >= 3) or (name == "graph" and protocol >= 18)
        )
        if fingerprint_protocol:
            if len(config_ids) != 1 or any(
                row.get("experiment_config_id") is None for row in rows
            ):
                raise ValueError(
                    f"{name} protocol requires one experiment_config_id on every row"
                )
        elif len(config_ids) > 1:
            raise ValueError(
                f"{name} results mix experiment configurations: {sorted(config_ids)!r}"
            )


def aggregate_incremental_maintenance(
    *,
    graph_rows: Sequence[Mapping[str, Any]] = (),
    graph_stability_rows: Sequence[Mapping[str, Any]] = (),
    vector_rows: Sequence[Mapping[str, Any]] = (),
    expected: set[tuple[str, int]] | None = None,
    expected_manifest_id: str | None = None,
) -> dict[str, Any]:
    all_rows = [*graph_rows, *graph_stability_rows, *vector_rows]
    manifest_ids = sorted(
        {
            str(row["transition_manifest_id"])
            for row in all_rows
            if row.get("transition_manifest_id")
        }
    )
    if expected_manifest_id and any(
        mid != expected_manifest_id for mid in manifest_ids
    ):
        raise ValueError(
            f"result manifest ids {manifest_ids!r} do not match {expected_manifest_id}"
        )

    result: dict[str, Any] = {
        "schema_version": 2,
        "transition_manifest_id": expected_manifest_id,
        "input_manifest_ids": manifest_ids,
    }
    for name, rows in (("graph", graph_rows), ("vector", vector_rows)):
        if not rows:
            continue
        _validate_result_identity(name, rows, expected)
        by_language: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            by_language[str(row.get("language") or "unknown")].append(row)
        group_fn = _graph_group if name == "graph" else _vector_group
        view = {
            "overall": group_fn(rows),
            "by_language": {
                language: group_fn(language_rows)
                for language, language_rows in sorted(by_language.items())
            },
            "protocol_versions": sorted(
                {
                    row.get("protocol_version")
                    for row in rows
                    if row.get("protocol_version") is not None
                }
            ),
        }
        if expected is not None:
            view["completeness"] = _completeness(rows, expected)
        if name == "graph":
            view["backend_setup"] = _graph_setup(rows)
            if graph_stability_rows:
                view["provider_stability"] = _graph_provider_stability(
                    graph_stability_rows
                )
        result[name] = view
    return result


def _fmt(value: Any, *, suffix: str = "") -> str:
    return f"{float(value):.2f}{suffix}" if isinstance(value, (int, float)) else "n/a"


def _fmt_rate(value: Any) -> str:
    return f"{float(value):.2%}" if isinstance(value, (int, float)) else "n/a"


def render_markdown(summary: Mapping[str, Any]) -> str:
    lines = [
        "# Incremental maintenance aggregate",
        "",
        "Only pre-declared source-changing transitions enter admission and "
        "latency populations.",
    ]
    graph = summary.get("graph") or {}
    if graph:
        lines.extend(
            [
                "",
                "## Graph maintenance",
                "",
                "| group | source n | changed exact | edge F1 p50 | behavior agree "
                "| observed speedup p50 (IQR) | strict admitted |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        graph_groups = {"overall": graph["overall"], **graph.get("by_language", {})}
        for name, group in graph_groups.items():
            symbol = group["symbol"]
            fidelity = group["fidelity"]["symbol-level-patch"]
            speedup = symbol["observed_amortized_speedup_vs_full"]
            lines.append(
                "| {name} | {n} | {changed}/{n} | {edge_f1} | {behavior} | "
                "{p50} ({p25}-{p75}) | {sa}/{n} ({sr}) |".format(
                    name=name,
                    n=group["source_changing_rows"],
                    changed=fidelity["raw_changed_exact"],
                    edge_f1=_fmt_rate(fidelity["edge_f1"]["p50"]),
                    behavior=_fmt_rate(fidelity["behavior_agreement"]["p50"]),
                    sa=symbol["admitted"],
                    sr=(
                        _fmt(symbol["admission_rate"], suffix="x").replace("x", "%")
                        if symbol["admission_rate"] is None
                        else f"{symbol['admission_rate']:.0%}"
                    ),
                    p50=_fmt(speedup["p50"], suffix="x"),
                    p25=_fmt(speedup["p25"], suffix="x"),
                    p75=_fmt(speedup["p75"], suffix="x"),
                )
            )
        stability = graph.get("provider_stability")
        if stability:
            lines.extend(
                [
                    "",
                    "Fresh-rebuild stability (independent same-commit rebuilds): "
                    f"n={stability['comparisons']}; edge F1 p50="
                    f"{_fmt_rate(stability['edge_f1']['p50'])}; behavior agreement "
                    f"p50={_fmt_rate(stability['behavior_agreement']['p50'])}; "
                    "changed-scope "
                    f"exact={stability['changed_exact']}/{stability['comparisons']}.",
                ]
            )
        lines.extend(
            [
                "",
                "| arm | attempted | succeeded | recovered | failed attempts "
                "| recovery p50 (s) |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for arm, reliability in graph["overall"]["reliability"].items():
            lines.append(
                "| {arm} | {attempted} | {succeeded} | {recovered} | "
                "{failed_attempts} | {recovery} |".format(
                    arm=arm,
                    attempted=reliability["attempted_rows"],
                    succeeded=reliability["succeeded_rows"],
                    recovered=reliability["recovered_rows"],
                    failed_attempts=reliability["failed_attempts"],
                    recovery=_fmt(reliability["recovery_s"]["p50"]),
                )
            )
    vector = summary.get("vector") or {}
    if vector:
        lines.extend(
            [
                "",
                "## Vector maintenance",
                "",
                "| group | source n | artifact exact | set exact | ordered exact "
                "| admitted | speedup p50 (IQR) | cache hit p50 "
                "| re-embedded p50 |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        vector_groups = {"overall": vector["overall"], **vector.get("by_language", {})}
        for name, group in vector_groups.items():
            arm = group["incremental"]
            speedup = arm["speedup_vs_full"]
            lines.append(
                "| {name} | {n} | {artifact}/{n} | {set_exact}/{n} "
                "| {ordered}/{n} "
                "| {a}/{n} ({rate}) | {p50} ({p25}-{p75}) | {cache} "
                "| {chunks} |".format(
                    name=name,
                    n=group["source_changing_rows"],
                    artifact=group["fidelity"]["artifact_exact"],
                    set_exact=group["fidelity"]["set_replay_exact"],
                    ordered=group["fidelity"]["ordered_replay_exact"],
                    a=arm["admitted"],
                    rate=(
                        "n/a"
                        if arm["admission_rate"] is None
                        else f"{arm['admission_rate']:.0%}"
                    ),
                    p50=_fmt(speedup["p50"], suffix="x"),
                    p25=_fmt(speedup["p25"], suffix="x"),
                    p75=_fmt(speedup["p75"], suffix="x"),
                    cache=(
                        "n/a"
                        if arm["cache_hit_rate"]["p50"] is None
                        else f"{arm['cache_hit_rate']['p50']:.1%}"
                    ),
                    chunks=_fmt(arm["chunks_reembedded"]["p50"]),
                )
            )
    for name in ("graph", "vector"):
        completeness = (summary.get(name) or {}).get("completeness")
        if completeness:
            lines.extend(
                [
                    "",
                    f"{name.capitalize()} completeness: "
                    f"{completeness['unique_transition_rows']}/"
                    f"{completeness['expected_rows']} unique transitions; "
                    f"complete={str(completeness['complete']).lower()}.",
                ]
            )
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--graph-results", type=Path)
    parser.add_argument("--graph-stability-results", type=Path)
    parser.add_argument("--vector-results", type=Path)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    args = parser.parse_args(argv)
    if (
        not args.graph_results
        and not args.graph_stability_results
        and not args.vector_results
    ):
        parser.error("at least one result file is required")

    _instances, chains, expected_manifest_id = load_manifest(args.manifest)
    expected = {
        (instance_id, int(row["step"]))
        for instance_id, transitions in chains.items()
        for row in transitions
    }
    summary = aggregate_incremental_maintenance(
        graph_rows=_load_jsonl(args.graph_results) if args.graph_results else (),
        graph_stability_rows=(
            _load_jsonl(args.graph_stability_results)
            if args.graph_stability_results
            else ()
        ),
        vector_rows=_load_jsonl(args.vector_results) if args.vector_results else (),
        expected=expected,
        expected_manifest_id=expected_manifest_id,
    )
    markdown = render_markdown(summary)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
    if args.output_markdown:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
