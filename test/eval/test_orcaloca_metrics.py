# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the dependency-free OrcaLoca benchmark scorer."""

from __future__ import annotations

import pytest

from codenib.eval.benchmarks.orcaloca import (
    OrcaLocaGroundTruth,
    OrcaLocaLocation,
    parse_orcaloca_locations,
    resolve_orcaloca_locations,
    score_orcaloca_locations,
    summarize_orcaloca_scores,
)


def test_parse_locations_accepts_upstream_and_saved_result_shapes() -> None:
    upstream = parse_orcaloca_locations(
        {
            "bug_locations": [
                {
                    "file_path": "/src/service.py",
                    "class_name": "Service",
                    "method_name": "run",
                }
            ]
        }
    )
    saved = parse_orcaloca_locations(
        [{"path": "./src/service.py", "class_name": "Service", "method_name": "run"}]
    )

    expected = (
        OrcaLocaLocation(
            file_path="src/service.py",
            class_name="Service",
            method_name="run",
        ),
    )
    assert upstream == expected
    assert saved == expected


def test_parse_locations_uses_top_k_fallback_and_rejects_malformed_output() -> None:
    parsed = parse_orcaloca_locations(
        '{"top_k_bug_locations": [{"file_path": "src/module.py", '
        '"class_name": "", "method_name": "helper"}]}'
    )

    assert parsed == (
        OrcaLocaLocation(
            file_path="src/module.py",
            method_name="helper",
        ),
    )
    assert parse_orcaloca_locations("not json") == ()
    assert parse_orcaloca_locations({"bug_locations": "not a list"}) == ()


def test_subset_match_allows_extra_predictions_and_reports_precision() -> None:
    ground_truth = OrcaLocaGroundTruth(
        files=("src/service.py", "src/settings.py"),
        functions=("src/service.py:Service", "src/service.py:Service.run"),
    )
    score = score_orcaloca_locations(
        ground_truth,
        [
            OrcaLocaLocation("src/service.py", "Service", "run"),
            OrcaLocaLocation("src/settings.py"),
            OrcaLocaLocation("src/extra.py", method_name="helper"),
        ],
    )

    assert score.file_match is True
    assert score.function_match is True
    assert score.file_precision == pytest.approx(2 / 3)
    assert score.function_precision == pytest.approx(2 / 3)
    assert score.predicted_functions == (
        "src/extra.py:helper",
        "src/service.py:Service",
        "src/service.py:Service.run",
    )


def test_missing_prediction_fails_match_without_changing_empty_precision_rule() -> None:
    ground_truth = OrcaLocaGroundTruth(
        files=("src/service.py",),
        functions=("src/service.py:Service.run",),
    )
    score = score_orcaloca_locations(ground_truth, [])

    assert score.file_match is False
    assert score.function_match is False
    assert score.file_precision == 1.0
    assert score.function_precision == 1.0


def test_empty_function_ground_truth_matches_like_upstream_artifact() -> None:
    ground_truth = OrcaLocaGroundTruth.from_mapping(
        {
            "ground_truth": {
                "gold_files": ["src/settings.py"],
                "gold_functions": [],
            }
        }
    )
    score = score_orcaloca_locations(
        ground_truth,
        [OrcaLocaLocation("src/settings.py")],
    )

    assert score.file_match is True
    assert score.function_match is True


def test_ground_truth_requires_both_explicit_label_fields() -> None:
    with pytest.raises(ValueError, match="gold_functions"):
        OrcaLocaGroundTruth.from_mapping({"gold_files": ["src/service.py"]})


def test_ground_truth_rejects_string_label_collections() -> None:
    with pytest.raises(ValueError, match="gold_files must be a sequence"):
        OrcaLocaGroundTruth.from_mapping(
            {
                "gold_files": "src/service.py",
                "gold_functions": [],
            }
        )


def test_summary_keeps_every_instance_in_the_denominator() -> None:
    ground_truth = OrcaLocaGroundTruth(
        files=("src/service.py",),
        functions=("src/service.py:Service.run",),
    )
    matched = score_orcaloca_locations(
        ground_truth,
        [OrcaLocaLocation("src/service.py", "Service", "run")],
    )
    missed = score_orcaloca_locations(ground_truth, [])

    summary = summarize_orcaloca_scores([matched, missed])

    assert summary.n == 2
    assert summary.file_match_count == 1
    assert summary.file_match_rate == 0.5
    assert summary.function_match_count == 1
    assert summary.function_match_rate == 0.5


def test_resolve_locations_anchors_python_entities_and_filters_paths(
    tmp_path,
) -> None:
    source = tmp_path / "src/service.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class Service:\n" "    def run(self):\n" "        return 1\n",
        encoding="utf-8",
    )
    payload = {
        "bug_locations": [
            {
                "file_path": str(source),
                "class_name": "Service",
                "method_name": "run",
            },
            {"file_path": "missing.py"},
        ]
    }

    assert resolve_orcaloca_locations(payload, repo_path=tmp_path) == (
        {
            "path": "src/service.py",
            "class_name": "Service",
            "method_name": "run",
            "start": 2,
            "end": 3,
        },
    )
