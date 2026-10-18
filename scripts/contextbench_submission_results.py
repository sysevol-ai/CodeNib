#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Aggregate canonical500 final-context scores, native outcomes and system usage.

This makes no model, Docker or network calls. Inputs must contain all500 unique
canonical instances, including unsuccessful repairs. This verifies aggregation
coverage and metering, not raw artifact contents or leaderboard comparability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from decimal import Decimal
from pathlib import Path

POPULATION = 500


def number(value, label, *, fraction=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label}: require a finite number")
    if value < 0 or (fraction and value > 1):
        raise ValueError(f"{label}: value outside the allowed range")
    return Decimal(str(value))


def index(rows, expected, label):
    result = {}
    for row in rows:
        identity = row.get("instance_id")
        if not isinstance(identity, str) or identity in result:
            raise ValueError(f"{label}: missing or duplicate instance identity")
        result[identity] = row
    if set(result) != expected:
        raise ValueError(
            f"{label}: require exactly {POPULATION} canonical instances; "
            f"missing={len(expected - set(result))}, extra={len(set(result) - expected)}"
        )
    return result


def context_performance(metrics):
    """Average evaluated per-instance P/R/F1 without a ratio of macro averages."""
    if len(metrics) != POPULATION:
        raise ValueError("Context metrics must cover all500 canonical instances")
    levels = {name: [] for name in ("file", "symbol", "line")}
    for identity, metric in metrics.items():
        if "error" in metric:
            raise ValueError(f"{identity}: context evaluation has an error")
        for level in levels:
            values = metric["final"][level]
            recall = number(values["coverage"], identity + " recall", fraction=True)
            precision = number(
                values["precision"], identity + " precision", fraction=True
            )
            # The pinned bridge reports evaluated P/R without an F1 field.
            # Compute F1 for THIS instance before averaging across instances.
            f1 = (
                2 * recall * precision / (recall + precision)
                if recall + precision
                else Decimal(0)
            )
            if "f1" in values and abs(
                number(values["f1"], identity + " F1", fraction=True) - f1
            ) > Decimal("1e-12"):
                raise ValueError(f"{identity}: reported F1 differs from per-case P/R")
            levels[level].append((recall, precision, f1))
    return {
        target: {
            label: float(
                sum((row[column] for row in levels[source]), Decimal(0)) / POPULATION
            )
            for column, label in enumerate(("recall", "precision", "f1"))
        }
        for source, target in [("file", "file"), ("symbol", "block"), ("line", "line")]
    }


def aggregate(protocol, evaluation, joined, model):
    """Use per-case F1 and all500 native outcomes, with no successful-case filter."""
    if not isinstance(model, str) or not model.strip():
        raise ValueError("A system/model display name is required")
    queue = protocol["queue"]
    expected = {row["instance_id"] for row in queue}
    if len(queue) != POPULATION or len(expected) != POPULATION:
        raise ValueError("Protocol must identify exactly500 unique canonical instances")
    metrics = index(evaluation["cases"], expected, "Context metrics")
    native = index(joined, expected, "Native outcomes")
    performance = context_performance(metrics)
    seen_sessions = set()
    total_cost = Decimal(0)
    resolved = 0
    for case in queue:
        identity = case["instance_id"]
        result = native[identity]
        if (
            result.get("native_status") != "graded"
            or type(result.get("native_resolution")) is not bool
            or result.get("original_inst_id") != case["original_inst_id"]
            or result.get("base_commit") != case["base_commit"]
        ):
            raise ValueError(
                f"{identity}: missing or mismatched terminal native outcome"
            )
        resolved += result["native_resolution"]
        usage = result["usage"]
        if (
            usage.get("instance_id") != identity
            or usage.get("cost_scope") != "search_and_repair_model_usage"
            or usage.get("search_included") is not True
            or usage.get("all_session_fees_and_identities_known") is not True
            or usage.get("compute_cost_included") is not False
        ):
            raise ValueError(
                f"{identity}: complete search-plus-repair usage is required"
            )
        phases = []
        cost = Decimal(0)
        for session in usage["sessions"]:
            session_id = session.get("session_id_sha256")
            if (
                not isinstance(session_id, str)
                or not re.fullmatch(r"[0-9a-f]{64}", session_id)
                or session_id in seen_sessions
            ):
                raise ValueError(
                    f"{identity}: missing or repeated metered session identity"
                )
            if session.get("billing_route") != "anthropic-api":
                raise ValueError(f"{identity}: session is outside the frozen API route")
            phase = session.get("phase")
            if not isinstance(phase, str) or not (
                phase.startswith("search:") or phase.startswith("repair:")
            ):
                raise ValueError(f"{identity}: unknown metering phase")
            seen_sessions.add(session_id)
            phases.append(phase)
            cost += number(session.get("usage_usd"), identity + " session cost")
        if (
            phases.count("search:first") != 1
            or phases.count("search:scout") != 1
            or sum(p.startswith("search:") for p in phases) != 2
            or not any(p.startswith("repair:") for p in phases)
            or len(set(phases)) != len(phases)
        ):
            raise ValueError(
                f"{identity}: require both frozen search workers and repair usage"
            )
        if abs(
            cost - number(usage.get("total_model_usage_usd"), identity + " total cost")
        ) > Decimal("1e-10"):
            raise ValueError(f"{identity}: session sum differs from system cost")
        total_cost += cost
    performance["pass_at_1"] = resolved / POPULATION
    results = {
        "model": model,
        "performance": performance,
        "patterns": {"avg_cost_per_instance": float(total_cost / POPULATION)},
    }
    audit = {
        "instances": POPULATION,
        "context_valid": POPULATION,
        "native_resolved": resolved,
        "native_unresolved": POPULATION - resolved,
        "unique_metered_sessions": len(seen_sessions),
        "total_search_plus_repair_usage_decimal": str(total_cost),
        "f1_aggregation": "Mean of per-instance F1 from evaluated per-instance P/R",
        "cost_scope": "SDK search-plus-repair usage; compute excluded, invoice unverified",
        "raw_artifact_contents_verified": False,
        "final_selection_input_provenance_verified": False,
        "official_evaluator_or_native_tests_rerun": False,
        "population_comparability_or_publication_approved": False,
    }
    return results, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--joined-results", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == args.audit.resolve() or any(
        path.exists() for path in (args.output, args.audit)
    ):
        raise ValueError("Results and audit must be separate new files")
    paths = (args.protocol, args.evaluation, args.joined_results)
    contents = [path.read_bytes() for path in paths]
    protocol, evaluation = [json.loads(content) for content in contents[:2]]
    joined = [json.loads(line) for line in contents[2].splitlines() if line.strip()]
    results, audit = aggregate(protocol, evaluation, joined, args.model)
    audit["input_sha256"] = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in zip(
            ("protocol", "evaluation", "joined_results"), contents, strict=True
        )
    }
    for path, value in [(args.output, results), (args.audit, audit)]:
        with path.open("x") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
    print(
        json.dumps(
            {"instances": POPULATION, "native_resolved": audit["native_resolved"]}
        )
    )


if __name__ == "__main__":
    main()
