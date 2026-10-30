#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Attribute frozen workflow costs without changing any score or observation."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

_SETUP_LABELS = frozenset(
    {
        "product-venv",
        "install-product",
        "prepare-language-providers",
        "prepare-python-provider",
        "initialize-product",
    }
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("workflow duration must be a finite nonnegative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("workflow duration must be a finite nonnegative number")
    return number


def attribute_costs(execution_path: Path, receipts_root: Path) -> dict:
    """Bind command durations to the exact receipts sealed by the execution."""

    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    rows = []
    for workflow in execution["workflows"]:
        identity = workflow["workflow"]
        directory = (
            f"{identity['workflow_index']:02d}-"
            f"{identity['instance_id']}-{identity['arm']}"
        )
        path = receipts_root / directory / "receipt.json"
        if _sha256(path) != workflow["receipt_sha256"]:
            raise ValueError(f"captured workflow receipt changed: {directory}")
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if receipt["workflow"] != identity:
            raise ValueError(f"captured workflow identity differs: {directory}")
        setup = _seconds(workflow["setup_seconds"])
        total = _seconds(workflow["observed_workflow_seconds"])
        phases = {
            phase["kind"]: _seconds(phase["seconds"]) for phase in workflow["phases"]
        }
        remainder = total - setup - sum(phases.values())
        if remainder < -0.001:
            raise ValueError(f"recorded workflow components exceed total: {directory}")
        commands = defaultdict(float)
        for command in receipt["commands"]:
            if command["label"] in _SETUP_LABELS:
                commands[command["label"]] += _seconds(command["seconds"])
        if sum(commands.values()) > setup + 0.001:
            raise ValueError(f"recorded setup commands exceed setup: {directory}")
        rows.append(
            {
                "workflow": identity,
                "receipt_sha256": workflow["receipt_sha256"],
                "observed_workflow_seconds": total,
                "setup_seconds": setup,
                "setup_commands_seconds": dict(commands),
                "setup_other_seconds": setup - sum(commands.values()),
                "task_phase_seconds": phases,
                "session_and_orchestration_remainder_seconds": remainder,
                "normalized_recorded_tokens": workflow["normalized_recorded_tokens"],
                "optional_mcp_attempts": workflow["optional_mcp_attempts"],
                "tool_attempts_by_name": workflow["tool_attempts_by_name"],
            }
        )
    arms = {}
    for arm in sorted({row["workflow"]["arm"] for row in rows}):
        selected = [row for row in rows if row["workflow"]["arm"] == arm]
        commands = Counter()
        phases = Counter()
        calls = Counter()
        for row in selected:
            commands.update(row["setup_commands_seconds"])
            phases.update(row["task_phase_seconds"])
            calls.update(row["tool_attempts_by_name"])
        arms[arm] = {
            "workflow_count": len(selected),
            "observed_workflow_seconds": sum(
                row["observed_workflow_seconds"] for row in selected
            ),
            "setup_seconds": sum(row["setup_seconds"] for row in selected),
            "setup_commands_seconds": dict(commands),
            "task_phase_seconds": dict(phases),
            "session_and_orchestration_remainder_seconds": sum(
                row["session_and_orchestration_remainder_seconds"] for row in selected
            ),
            "normalized_recorded_tokens": sum(
                row["normalized_recorded_tokens"] for row in selected
            ),
            "optional_mcp_attempts": sum(
                row["optional_mcp_attempts"] for row in selected
            ),
            "tool_attempts_by_name": dict(calls),
        }
    comparisons = {}
    candidate = arms["codenib"]
    for baseline in ("serena", "native"):
        reference = arms[baseline]
        gap = (
            candidate["observed_workflow_seconds"]
            - reference["observed_workflow_seconds"]
        )
        setup_gap = candidate["setup_seconds"] - reference["setup_seconds"]
        comparisons[baseline] = {
            "observed_workflow_gap_seconds": gap,
            "setup_gap_seconds": setup_gap,
            "setup_share_of_observed_gap": setup_gap / gap if gap else None,
            "task_phase_gap_seconds": sum(candidate["task_phase_seconds"].values())
            - sum(reference["task_phase_seconds"].values()),
            "session_and_orchestration_gap_seconds": (
                candidate["session_and_orchestration_remainder_seconds"]
                - reference["session_and_orchestration_remainder_seconds"]
            ),
        }
    zero_setup = candidate["observed_workflow_seconds"] - candidate["setup_seconds"]
    return {
        "schema": 1,
        "scope": (
            "retrospective model-free cost attribution on twelve frozen v2 workflows; "
            "not a rescore, new cohort or causal efficacy estimate"
        ),
        "inputs": {
            "execution_sha256": _sha256(execution_path),
            "script_sha256": _sha256(Path(__file__)),
            "captured_receipts_verified": len(rows),
        },
        "workflows": rows,
        "arms": arms,
        "descriptive_comparisons": comparisons,
        "zero_candidate_setup_estimate": {
            "assumption": (
                "remove all candidate setup and hold every recorded model, tool and "
                "session duration fixed; an optimistic arithmetic scenario, not a run"
            ),
            "candidate_seconds": zero_setup,
            "serena_observed_seconds": arms["serena"]["observed_workflow_seconds"],
            "serena_twenty_percent_target_seconds": 0.8
            * arms["serena"]["observed_workflow_seconds"],
        },
        "limitations": [
            "Three exposed repository clusters; original quality/eligibility failure remains.",
            "CodeNib and Serena receive no optional MCP or product CLI calls.",
            "Task-phase wall time combines inference, native tools and client scheduling.",
            "Initialization includes indexing, source checks and registration; "
            "it is not pure graph compiler time.",
            "Advertised tools do not establish which full schemas reached the model.",
            "No latency difference is attributed to an unused graph query or tool schema.",
            "No frozen outcomes, source-read credit, costs or inputs are changed.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("execution", type=Path)
    parser.add_argument("receipts_root", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = attribute_costs(args.execution, args.receipts_root)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, allow_nan=False)
        handle.write("\n")


if __name__ == "__main__":
    main()
