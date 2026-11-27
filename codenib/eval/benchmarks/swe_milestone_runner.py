# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
"""Inspect SWE-Milestone tasks and validate explicitly pinned result cohorts.

Snapshot grading delegates to a pinned upstream Docker evaluator. This adapter
does not yet launch agents. Missing classified tests remain visible in results.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

from codenib.dataset.swe_milestone import load_swe_milestone_cases
from codenib.eval.benchmarks.swe_milestone_harness import score_snapshot


def source_packet(source: str, budget_bytes: int) -> dict:
    """Return the exact source payload and its UTF-8 count, without an LLM call.

    The caller must record this packet only after including it in an actual
    agent input. Constructing a packet alone does not prove delivery.
    """
    if type(budget_bytes) is not int or budget_bytes < 0:
        raise ValueError("source byte budget must be a nonnegative integer")
    payload = source.encode("utf-8")[:budget_bytes].decode("utf-8", errors="ignore")
    return {
        "source": payload,
        "source_bytes": len(payload.encode("utf-8")),
        "budget_bytes": budget_bytes,
        "source_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }


def evaluator_identity(commit: str, settings: Mapping) -> str:
    """Bind evaluation policy separately from agent model and condition."""
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ValueError("evaluator commit must be a full lowercase Git SHA")
    encoded = json.dumps(
        {"commit": commit, "settings": dict(settings)}, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def summarize_result(result: Mapping, *, identity: str) -> dict:
    """Retain coverage and infrastructure failures alongside pass counts."""
    summary = result["test_summary"]
    fields = (
        "fail_to_pass_required",
        "fail_to_pass_achieved",
        "none_to_pass_required",
        "none_to_pass_achieved",
        "none_to_pass_missing",
        "pass_to_pass_required",
        "pass_to_pass_achieved",
        "pass_to_pass_missing",
    )
    counts = {field: summary[field] for field in fields}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("test counts must be nonnegative integers")
    for group in ("fail_to_pass", "none_to_pass", "pass_to_pass"):
        if (
            counts[f"{group}_achieved"] + counts.get(f"{group}_missing", 0)
            > counts[f"{group}_required"]
        ):
            raise ValueError(f"inconsistent {group} counts")
    invalid = bool(result.get("infra_invalid") or result.get("error_message"))
    missing = counts["none_to_pass_missing"] + counts["pass_to_pass_missing"]
    all_required_passed = all(
        counts[f"{group}_achieved"] == counts[f"{group}_required"]
        for group in ("fail_to_pass", "none_to_pass", "pass_to_pass")
    )
    return {
        "evaluator_identity": identity,
        "milestone_id": result["milestone_id"],
        "resolved": bool(result.get("resolved"))
        and not invalid
        and all_required_passed,
        "infra_invalid": invalid,
        "classified_tests_missing": missing,
        "partial_test_universe": bool(
            result.get("build_failure_policy", {}).get("partial_test_universe")
        ),
        "counts": counts,
    }


def validate_comparison(rows: Sequence[Mapping]) -> None:
    """Reject mixed evaluator protocols instead of silently pooling scores."""
    if not rows or any(not row.get("evaluator_identity") for row in rows):
        raise ValueError("every result needs an evaluator identity")
    if len({row["evaluator_identity"] for row in rows}) != 1:
        raise ValueError("cannot compare different evaluator versions or settings")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Read specifications only")
    inspect.add_argument("workspace", type=Path)
    inspect.add_argument("--milestone", action="append", default=[])
    summarize = commands.add_parser("summarize", help="Import one evaluator result")
    summarize.add_argument("result", type=Path)
    summarize.add_argument("--evaluator-commit", required=True)
    summarize.add_argument("--settings", type=Path, required=True)
    compare = commands.add_parser("compare", help="Check a cohort before tabulation")
    compare.add_argument("rows", type=Path, help="JSON array of summarized results")
    score = commands.add_parser(
        "score", help="Grade a saved snapshot with a pinned harness"
    )
    score.add_argument("workspace", type=Path)
    score.add_argument("--checkout", type=Path, required=True)
    score.add_argument("--evaluator-commit", required=True)
    score.add_argument("--milestone", required=True)
    score.add_argument("--snapshot", type=Path, required=True)
    score.add_argument("--classification", type=Path, required=True)
    score.add_argument("--repo-config", type=Path, required=True)
    score.add_argument("--output-dir", type=Path, required=True)
    score.add_argument("--allow-partial-build-reports", action="store_true")
    score.add_argument("--allow-legacy-snapshot", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "inspect":
        cases = load_swe_milestone_cases(args.workspace, instance_ids=args.milestone)
        output = [case.prompt_record() for case in cases]
    elif args.command == "summarize":
        identity = evaluator_identity(
            args.evaluator_commit, json.loads(args.settings.read_text(encoding="utf-8"))
        )
        output = summarize_result(
            json.loads(args.result.read_text(encoding="utf-8")), identity=identity
        )
    elif args.command == "score":
        receipt = score_snapshot(
            checkout=args.checkout,
            commit=args.evaluator_commit,
            workspace=args.workspace,
            milestone=args.milestone,
            snapshot=args.snapshot,
            classification=args.classification,
            repo_config=args.repo_config,
            output_dir=args.output_dir,
            allow_partial=args.allow_partial_build_reports,
            allow_legacy=args.allow_legacy_snapshot,
        )
        identity = evaluator_identity(receipt["evaluator_commit"], receipt["settings"])
        output = summarize_result(
            json.loads(
                (args.output_dir / "evaluation_result.json").read_text(encoding="utf-8")
            ),
            identity=identity,
        )
        (args.output_dir / "summary.json").write_text(
            json.dumps(output, indent=2) + "\n", encoding="utf-8"
        )
    else:
        output = json.loads(args.rows.read_text(encoding="utf-8"))
        validate_comparison(output)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
