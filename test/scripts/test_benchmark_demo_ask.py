# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import json

from scripts import benchmark_demo_ask
from scripts.benchmark_demo_ask import _parser, evaluate_response


def test_default_endpoint_matches_the_demo_backend():
    args = _parser().parse_args(["--candidate", "test-model"])

    assert args.endpoint == "http://127.0.0.1:8000"


def test_evaluate_response_reports_source_and_term_coverage():
    case = {
        "expected_files": ["src/core.py", "src/runtime.py"],
        "expected_terms": ["queueJob", "flushJobs"],
    }
    response = {
        "answer": "queueJob schedules work before flushJobs drains it.",
        "citations": [
            {
                "file": "src/core.py",
                "start_line": 10,
                "end_line": 20,
            }
        ],
        "tool_calls": [{"skill_id": "repository_search"}],
        "total_turns": 2,
        "total_duration_ms": 42.0,
    }

    result = evaluate_response(case, response)

    assert result["citation_locations_valid"] is True
    assert result["expected_file_recall"] == 0.5
    assert result["expected_term_coverage"] == 1.0
    assert result["tool_call_count"] == 1


def test_evaluate_response_rejects_missing_or_reversed_citation_ranges():
    result = evaluate_response(
        {"expected_files": [], "expected_terms": []},
        {
            "answer": "answer",
            "citations": [{"file": "src/core.py", "start_line": 20, "end_line": 10}],
        },
    )

    assert result["citation_locations_valid"] is False


def test_main_fails_when_a_successful_response_is_not_grounded(
    tmp_path, monkeypatch, capsys
):
    cases = [
        {
            "id": "runtime",
            "repo": "owner__repo",
            "question": "How does it run?",
            "expected_files": ["src/runtime.py"],
            "expected_terms": ["dispatch"],
        }
    ]
    case_file = tmp_path / "cases.json"
    case_file.write_text(json.dumps(cases), encoding="utf-8")
    monkeypatch.setattr(
        benchmark_demo_ask,
        "_request",
        lambda *_args, **_kwargs: (
            {"answer": "No source evidence.", "citations": []},
            12.0,
            None,
        ),
    )

    result = benchmark_demo_ask.main(
        ["--candidate", "test", "--case-file", str(case_file), "--compact"]
    )
    report = json.loads(capsys.readouterr().out)

    assert result == 1
    assert report["summary"]["successful"] == 0
    assert report["summary"]["grounding_failed"] == 1
    assert report["runs"][0]["status"] == "grounding_failed"
    assert report["runs"][0]["grounding_failures"] == [
        "citation_locations_invalid",
        "expected_file_recall_below_threshold",
        "expected_term_coverage_below_threshold",
    ]


def test_main_accepts_a_response_that_meets_explicit_grounding_thresholds(
    tmp_path, monkeypatch, capsys
):
    case_file = tmp_path / "cases.json"
    case_file.write_text(
        json.dumps(
            [
                {
                    "id": "runtime",
                    "repo": "owner__repo",
                    "question": "How does it run?",
                    "expected_files": ["src/runtime.py", "src/router.py"],
                    "expected_terms": ["dispatch", "queue"],
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        benchmark_demo_ask,
        "_request",
        lambda *_args, **_kwargs: (
            {
                "answer": "dispatch handles the request",
                "citations": [
                    {"file": "src/runtime.py", "start_line": 1, "end_line": 4}
                ],
            },
            12.0,
            None,
        ),
    )

    result = benchmark_demo_ask.main(
        [
            "--candidate",
            "test",
            "--case-file",
            str(case_file),
            "--min-file-recall",
            "0.5",
            "--min-term-coverage",
            "0.5",
            "--compact",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert result == 0
    assert report["runs"][0]["grounding_passed"] is True
    assert report["summary"]["successful"] == 1
