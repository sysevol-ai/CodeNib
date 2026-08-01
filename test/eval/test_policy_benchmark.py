# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the paired external-policy benchmark entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codenib.eval.benchmarks.policy_benchmark import main
from codenib.eval.benchmarks.policy_compat import policy_result_path, write_json_atomic


def _write_cases(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "dataset": "fixture",
                "cases": [
                    {
                        "instance_id": "demo__repo-1",
                        "repo": "demo/repo",
                        "base_commit": "a" * 40,
                        "problem_statement": "Locate the parser",
                        "repo_path": "repo",
                        "manifest_path": "manifest.json",
                        "ground_truth": {
                            "gold_files": ["src/parser.py"],
                            "gold_functions": ["src/parser.py::parse"],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_result(results: Path, *, provider: str, model: str = "test-model") -> None:
    write_json_atomic(
        policy_result_path(
            results,
            instance_id="demo__repo-1",
            agent="orcaloca",
            provider=provider,
        ),
        {
            "agent": "orcaloca",
            "provider": provider,
            "instance_id": "demo__repo-1",
            "model": model,
            "locations": [
                {
                    "file_path": "src/parser.py",
                    "method_name": "parse",
                }
            ],
        },
    )


def test_score_command_enforces_the_requested_cell_denominator(tmp_path: Path) -> None:
    cases = tmp_path / "cases.json"
    results = tmp_path / "results"
    output = tmp_path / "summary.json"
    _write_cases(cases)
    _write_result(results, provider="native")
    base_args = [
        "score-orcaloca",
        "--cases",
        str(cases),
        "--results-dir",
        str(results),
        "--output",
        str(output),
    ]

    assert main(base_args) == 1
    assert main([*base_args, "--allow-incomplete"]) == 0
    partial = json.loads(output.read_text(encoding="utf-8"))
    assert partial["coverage"]["expected_cell_count"] == 2
    assert partial["coverage"]["observed_cell_count"] == 1
    assert partial["coverage"]["paired_successful_count"] == 0

    _write_result(results, provider="codenib")
    assert main(base_args) == 0
    complete = json.loads(output.read_text(encoding="utf-8"))
    assert complete["coverage"]["observed_cell_count"] == 2
    assert complete["coverage"]["paired_successful_count"] == 1
    assert complete["paired_summary"]["n"] == 1


def test_score_command_rejects_mixed_model_aggregation(tmp_path: Path) -> None:
    cases = tmp_path / "cases.json"
    results = tmp_path / "results"
    _write_cases(cases)
    _write_result(results, provider="native", model="model-a")
    _write_result(results, provider="codenib", model="model-b")

    with pytest.raises(ValueError, match="mixed models"):
        main(
            [
                "score-orcaloca",
                "--cases",
                str(cases),
                "--results-dir",
                str(results),
            ]
        )
