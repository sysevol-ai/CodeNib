# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
import pytest

from codenib.eval.benchmarks.swe_milestone_runner import (
    evaluator_identity,
    main,
    source_packet,
    summarize_result,
    validate_comparison,
)


def result(missing=0):
    return {
        "milestone_id": "milestone_003_sub-01",
        "resolved": False,
        "test_summary": {
            "fail_to_pass_required": 4,
            "fail_to_pass_achieved": 4,
            "none_to_pass_required": 603,
            "none_to_pass_achieved": 233,
            "none_to_pass_missing": missing,
            "pass_to_pass_required": 910,
            "pass_to_pass_achieved": 910,
            "pass_to_pass_missing": 0,
        },
        "build_failure_policy": {"partial_test_universe": True},
    }


def test_mixed_evaluator_settings_cannot_share_comparison():
    first = evaluator_identity("a" * 40, {"prune_start_guard_exempt": []})
    second = evaluator_identity(
        "a" * 40, {"prune_start_guard_exempt": ["db/migrations"]}
    )
    with pytest.raises(ValueError, match="different evaluator"):
        validate_comparison(
            [
                summarize_result(result(), identity=identity)
                for identity in [first, second]
            ]
        )


def test_missing_classified_tests_remain_visible():
    row = summarize_result(result(missing=367), identity="pinned")
    assert row["classified_tests_missing"] == 367
    assert not row["resolved"]


def test_global_partial_flag_does_not_imply_missing_classified_tests():
    row = summarize_result(result(), identity="pinned")
    assert row["partial_test_universe"]
    assert row["classified_tests_missing"] == 0


def test_infrastructure_failure_cannot_be_resolved():
    raw = result()
    raw.update(resolved=True, infra_invalid=True)
    assert not summarize_result(raw, identity="pinned")["resolved"]


def test_inconsistent_resolved_flag_cannot_override_failed_tests():
    raw = result()
    raw["resolved"] = True
    assert not summarize_result(raw, identity="pinned")["resolved"]


@pytest.mark.parametrize("budget", [0, 1, 2, 3, 7, 5000])
def test_source_bytes_count_the_actual_utf8_payload(budget):
    packet = source_packet("é漢abc", budget)
    assert packet["source_bytes"] == len(packet["source"].encode("utf-8"))
    assert packet["source_bytes"] <= budget
    if budget == 0:
        assert packet["source"] == ""


def test_cli_summarizes_saved_result(tmp_path, capsys):
    import json

    report = tmp_path / "result.json"
    settings = tmp_path / "settings.json"
    report.write_text(json.dumps(result(missing=367)))
    settings.write_text(json.dumps({"prune_start_guard_exempt": ["db/migrations"]}))
    main(
        [
            "summarize",
            str(report),
            "--evaluator-commit",
            "a" * 40,
            "--settings",
            str(settings),
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert output["classified_tests_missing"] == 367
