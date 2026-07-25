# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from codenib.dataset.codenib_synthesis import (
    EXPECTED_CATEGORY_COUNTS_WITH_TRAVERSAL,
    CodeNibSynthesisDataset,
    normalize_synthesis_record,
    validate_synthesis_records,
)
from scripts.rebuild_codenib_synthesis_dataset import build_parser


def _row(**overrides):
    row = {
        "repo": "example/repo",
        "instance_id": "example__repo-1",
        "base_commit": "abc123",
        "query": "Where is the relevant behavior implemented?",
        "category": "behavioral",
        "gt_symbols": ["src/main.py:run()"],
        "gt_symbol_nodes": [
            {
                "content": "def run():\n    pass",
                "end_line": 2,
                "file": "src/main.py",
                "node_name": "src/main.py:run()",
                "start_line": 1,
                "type": "function",
            }
        ],
        "gt_files": ["src/main.py"],
        "query_id": "example__repo-1_behavioral_q1",
        "length_variant": "detailed",
        "judge_verdict": "valid",
        "judge_reason": "ok",
    }
    row.update(overrides)
    return row


def test_normalize_record_injects_language_and_problem_statement():
    raw = _row(gt_files=["src/main.cpp"], gt_symbols=["src/main.cpp:run()"])

    record = normalize_synthesis_record(raw, "C++_C")

    assert record["language_group"] == "C++/C"
    assert record["source_config"] == "C++_C"
    assert record["problem_statement"] == raw["query"]
    assert record["gt_code_blocks_count"] == 1


def test_validate_records_catches_cpp_python_ground_truth_and_category_drift():
    cpp_bad = normalize_synthesis_record(
        _row(
            gt_files=["support/docopt.py"],
            gt_symbols=["support/docopt.py:docopt()"],
            gt_symbol_nodes=[
                {
                    "content": "def docopt():\n    pass",
                    "end_line": 2,
                    "file": "support/docopt.py",
                    "node_name": "support/docopt.py:docopt()",
                    "start_line": 1,
                    "type": "function",
                }
            ],
            query_id="cpp_bad",
        ),
        "C++_C",
    )
    go_ok = normalize_synthesis_record(
        _row(
            gt_files=["cmd/main.go"],
            gt_symbols=["cmd/main.go:main()"],
            gt_symbol_nodes=[],
            query_id="go_ok",
        ),
        "Go",
    )
    python_extra = normalize_synthesis_record(
        _row(category="traversal", query_id="python_extra"),
        "Python",
    )

    report = validate_synthesis_records([cpp_bad, go_ok, python_extra])

    codes = {issue["code"] for issue in report["issues"]}
    assert "gt_file_language_mismatch" in codes
    assert "category_distribution_mismatch" in codes
    assert report["error_count"] == 1


def test_validate_records_accepts_expected_traversal_distribution():
    rows = []
    for category, count in EXPECTED_CATEGORY_COUNTS_WITH_TRAVERSAL.items():
        for idx in range(count):
            rows.append(
                normalize_synthesis_record(
                    _row(
                        category=category,
                        query_id=f"python_{category}_{idx}",
                    ),
                    "Python",
                )
            )

    report = validate_synthesis_records(
        rows,
        expected_category_counts=EXPECTED_CATEGORY_COUNTS_WITH_TRAVERSAL,
        compare_category_distribution=False,
    )

    assert report["error_count"] == 0
    assert report["warning_count"] == 0


def test_rebuild_parser_defaults_to_test_source_split():
    args = build_parser().parse_args(["--output-dir", "/tmp/out"])

    assert args.source_split == "test"


def test_dataset_loader_defaults_to_test_split(monkeypatch, tmp_path):
    calls = []

    def fake_load_dataset(dataset, config_name, *, split, cache_dir):
        calls.append((dataset, config_name, split, cache_dir))
        return [_row(gt_files=["src/main.py"], query_id="python_q1")]

    monkeypatch.setattr(
        "codenib.dataset.codenib_synthesis.datasets.load_dataset",
        fake_load_dataset,
    )

    ds = CodeNibSynthesisDataset(
        configs="Python",
        root=str(tmp_path),
        log=False,
    )
    data = ds.load()

    assert calls[0][1] == "Python"
    assert calls[0][2] == "test"
    assert len(data) == 1
    assert data[0]["language_group"] == "Python"
    assert data[0]["problem_statement"] == _row()["query"]
