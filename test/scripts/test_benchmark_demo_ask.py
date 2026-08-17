# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

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
