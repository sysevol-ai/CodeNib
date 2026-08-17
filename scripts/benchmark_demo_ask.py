#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Benchmark the configured demo Ask model on fixed source-grounded cases."""

from __future__ import annotations

import argparse
import json
import statistics
import urllib.error
import urllib.request
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

_DEFAULT_CASES = (
    {
        "id": "requests_proxy",
        "repo": "psf__requests",
        "question": "Where is proxy bypass logic implemented and how does it work?",
        "expected_files": ["src/requests/utils.py"],
        "expected_terms": [
            "should_bypass_proxies",
            "get_environ_proxies",
            "resolve_proxies",
        ],
    },
    {
        "id": "gin_routes",
        "repo": "gin-gonic__gin",
        "question": "How is a GET route registered and inserted into the method tree?",
        "expected_files": ["routergroup.go", "gin.go", "tree.go"],
        "expected_terms": ["RouterGroup.GET", "addRoute", "methodTrees"],
    },
    {
        "id": "vue_scheduler",
        "repo": "vuejs__core",
        "question": "How are component update jobs queued and flushed?",
        "expected_files": [
            "packages/runtime-core/src/scheduler.ts",
            "packages/runtime-core/src/renderer.ts",
        ],
        "expected_terms": ["queueJob", "queueFlush", "flushJobs"],
    },
)

_DEFAULT_MIN_FILE_RECALL = 0.3
_DEFAULT_MIN_TERM_COVERAGE = 0.3


def _unit_fraction(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number from 0 to 1") from exc
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be a number from 0 to 1")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000")
    parser.add_argument("--candidate", required=True)
    parser.add_argument(
        "--case-file",
        type=Path,
        help="Optional JSON list replacing the built-in three-repository gate",
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument(
        "--min-file-recall",
        type=_unit_fraction,
        default=_DEFAULT_MIN_FILE_RECALL,
        help="Minimum expected-file recall required for every case (default: 0.3)",
    )
    parser.add_argument(
        "--min-term-coverage",
        type=_unit_fraction,
        default=_DEFAULT_MIN_TERM_COVERAGE,
        help="Minimum expected-term coverage required for every case (default: 0.3)",
    )
    parser.add_argument("--compact", action="store_true")
    return parser


def evaluate_response(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic grounding/coverage signals for one Ask response."""

    answer = str(response.get("answer") or "")
    answer_folded = answer.casefold()
    citations = response.get("citations") or []
    cited_files = {
        str(citation.get("file") or "")
        for citation in citations
        if isinstance(citation, dict)
    }
    expected_files = [str(value) for value in case.get("expected_files") or []]
    expected_terms = [str(value) for value in case.get("expected_terms") or []]
    found_files = [value for value in expected_files if value in cited_files]
    found_terms = [
        value for value in expected_terms if value.casefold() in answer_folded
    ]
    citation_locations_valid = bool(citations) and all(
        isinstance(citation, dict)
        and bool(citation.get("file"))
        and isinstance(citation.get("start_line"), int)
        and citation["start_line"] >= 1
        and isinstance(citation.get("end_line"), int)
        and citation["end_line"] >= citation["start_line"]
        for citation in citations
    )

    def fraction(found: Iterable[str], expected: list[str]) -> float:
        return round(len(tuple(found)) / len(expected), 3) if expected else 1.0

    return {
        "answer_chars": len(answer),
        "citation_count": len(citations),
        "tool_call_count": len(response.get("tool_calls") or []),
        "total_turns": response.get("total_turns"),
        "reported_duration_ms": response.get("total_duration_ms"),
        "citation_locations_valid": citation_locations_valid,
        "expected_file_recall": fraction(found_files, expected_files),
        "expected_term_coverage": fraction(found_terms, expected_terms),
        "found_files": found_files,
        "found_terms": found_terms,
    }


def _request(
    endpoint: str,
    case: dict[str, Any],
    *,
    timeout: float,
) -> tuple[dict[str, Any], float, str | None]:
    payload = json.dumps(
        {
            "repo_id": case["repo"],
            "messages": [{"role": "user", "content": case["question"]}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as raw:
            response = json.load(raw)
            server_timing = raw.headers.get("Server-Timing")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ask returned HTTP {exc.code}: {detail}") from exc
    return response, round((perf_counter() - started) * 1000, 1), server_timing


def _summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "median": None, "min": None, "max": None}
    return {
        "count": len(values),
        "median": round(statistics.median(values), 1),
        "min": round(min(values), 1),
        "max": round(max(values), 1),
    }


def _grounding_failures(
    result: dict[str, Any],
    *,
    min_file_recall: float,
    min_term_coverage: float,
) -> list[str]:
    failures = []
    if not result["citation_locations_valid"]:
        failures.append("citation_locations_invalid")
    if float(result["expected_file_recall"]) < min_file_recall:
        failures.append("expected_file_recall_below_threshold")
    if float(result["expected_term_coverage"]) < min_term_coverage:
        failures.append("expected_term_coverage_below_threshold")
    return failures


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.case_file:
        cases = json.loads(args.case_file.read_text(encoding="utf-8"))
    else:
        cases = list(_DEFAULT_CASES)
    if not isinstance(cases, list) or not all(isinstance(case, dict) for case in cases):
        raise ValueError("benchmark cases must be a JSON list of objects")

    runs: list[dict[str, Any]] = []
    failed = False
    for repetition in range(max(1, args.repeat)):
        for case in cases:
            record = {
                "case": str(case.get("id") or ""),
                "repo": str(case.get("repo") or ""),
                "repetition": repetition + 1,
            }
            try:
                response, wall_ms, server_timing = _request(
                    args.endpoint,
                    case,
                    timeout=max(1.0, args.timeout),
                )
                record.update(evaluate_response(case, response))
                grounding_failures = _grounding_failures(
                    record,
                    min_file_recall=args.min_file_recall,
                    min_term_coverage=args.min_term_coverage,
                )
                grounding_passed = not grounding_failures
                if not grounding_passed:
                    failed = True
                record.update(
                    {
                        "status": "ok" if grounding_passed else "grounding_failed",
                        "grounding_passed": grounding_passed,
                        "grounding_failures": grounding_failures,
                        "wall_ms": wall_ms,
                        "server_timing": server_timing,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - retain the other cases
                failed = True
                record.update({"status": "error", "error": str(exc)})
            runs.append(record)

    completed = [run for run in runs if run["status"] != "error"]
    successful = [run for run in completed if run["status"] == "ok"]
    report = {
        "schema_version": 1,
        "candidate": args.candidate,
        "endpoint": args.endpoint,
        "case_count": len(cases),
        "repeat": max(1, args.repeat),
        "thresholds": {
            "min_file_recall": args.min_file_recall,
            "min_term_coverage": args.min_term_coverage,
        },
        "summary": {
            "successful": len(successful),
            "failed": len(runs) - len(successful),
            "grounding_failed": sum(
                run["status"] == "grounding_failed" for run in completed
            ),
            "wall_ms": _summary([float(run["wall_ms"]) for run in completed]),
            "expected_file_recall_mean": (
                round(
                    statistics.mean(
                        float(run["expected_file_recall"]) for run in completed
                    ),
                    3,
                )
                if completed
                else None
            ),
            "expected_term_coverage_mean": (
                round(
                    statistics.mean(
                        float(run["expected_term_coverage"]) for run in completed
                    ),
                    3,
                )
                if completed
                else None
            ),
        },
        "runs": runs,
    }
    print(
        json.dumps(
            report,
            indent=None if args.compact else 2,
            separators=(",", ":") if args.compact else None,
            sort_keys=True,
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
