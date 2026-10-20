"""Submission aggregation preserves all500 outcomes, macro F1 and full usage."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import contextbench_submission_results as submission


@pytest.fixture
def inputs():
    protocol, evaluation, joined = {"queue": []}, {"cases": []}, []
    for i in range(500):
        case = {
            "instance_id": f"case-{i}",
            "original_inst_id": f"repo__task-{i}",
            "base_commit": f"{i:040x}",
        }
        protocol["queue"].append(case)
        p, r = (1.0, 0.1) if i < 250 else (0.1, 1.0)
        metric = {"coverage": r, "precision": p}
        evaluation["cases"].append(
            {
                "instance_id": case["instance_id"],
                "final": {level: dict(metric) for level in ("file", "symbol", "line")},
            }
        )
        sessions = [
            {
                "phase": phase,
                "billing_route": "anthropic-api",
                "session_id_sha256": f"{i * 3 + j:064x}",
                "usage_usd": fee,
            }
            for j, (phase, fee) in enumerate(
                [("search:first", 0.001), ("search:scout", 0.002), ("repair:1", 0.3)]
            )
        ]
        joined.append(
            {
                **case,
                "native_status": "graded",
                "native_resolution": i >= 100,
                "usage": {
                    "instance_id": case["instance_id"],
                    "cost_scope": "search_and_repair_model_usage",
                    "search_included": True,
                    "all_session_fees_and_identities_known": True,
                    "compute_cost_included": False,
                    "sessions": sessions,
                    "total_model_usage_usd": 0.303,
                },
            }
        )
    return protocol, evaluation, joined


def test_complete500_keeps_failed_cases_and_averages_per_case_f1(inputs):
    result, audit = submission.aggregate(*inputs, "CodeNib + models")
    assert result["performance"]["pass_at_1"] == 0.8
    assert result["performance"]["line"] == pytest.approx(
        {"recall": 0.55, "precision": 0.55, "f1": 2 / 11}
    )
    assert result["performance"]["block"] == result["performance"]["line"]
    assert result["patterns"] == {"avg_cost_per_instance": 0.303}
    assert audit["native_unresolved"] == 100
    assert audit["unique_metered_sessions"] == 1500
    assert audit["total_search_plus_repair_usage_decimal"] == "151.500"
    assert not audit["raw_artifact_contents_verified"]


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "duplicate",
        "wrong_native",
        "ungraded",
        "unknown_usage",
        "reused_session",
        "search_only",
        "wrong_total",
        "invalid_metric",
        "metric_error",
        "mean_f1",
    ],
)
def test_incomplete_or_unverifiable_inputs_cannot_produce_scores(inputs, fault):
    protocol, evaluation, joined = copy.deepcopy(inputs)
    if fault == "missing":
        joined.pop()
    elif fault == "duplicate":
        joined[-1] = joined[0]
    elif fault == "wrong_native":
        joined[0]["original_inst_id"] = "different-native-task"
    elif fault == "ungraded":
        joined[0]["native_status"] = "error"
    elif fault == "unknown_usage":
        joined[0]["usage"]["all_session_fees_and_identities_known"] = False
    elif fault == "reused_session":
        joined[1]["usage"]["sessions"][0]["session_id_sha256"] = joined[0]["usage"][
            "sessions"
        ][0]["session_id_sha256"]
    elif fault == "search_only":
        joined[0]["usage"]["sessions"].pop()
    elif fault == "wrong_total":
        joined[0]["usage"]["total_model_usage_usd"] = 0.003
    elif fault == "invalid_metric":
        evaluation["cases"][0]["final"]["line"]["coverage"] = float("nan")
    elif fault == "metric_error":
        evaluation["cases"][0]["error"] = "source unavailable"
    elif fault == "mean_f1":
        evaluation["cases"][0]["final"]["line"]["f1"] = 0.55
    with pytest.raises(ValueError):
        submission.aggregate(protocol, evaluation, joined, "CodeNib + models")


def test_cli_partial_population_creates_no_results_or_audit(tmp_path, inputs):
    protocol, evaluation, joined = inputs
    paths = [
        tmp_path / name for name in ("protocol.json", "evaluation.json", "native.jsonl")
    ]
    paths[0].write_text(json.dumps(protocol))
    paths[1].write_text(json.dumps(evaluation))
    paths[2].write_text("\n".join(json.dumps(row) for row in joined[:26]))
    output, audit = tmp_path / "results.json", tmp_path / "audit.json"
    script = Path(submission.__file__)
    process = subprocess.run(
        [
            sys.executable,
            str(script),
            "--protocol",
            str(paths[0]),
            "--evaluation",
            str(paths[1]),
            "--joined-results",
            str(paths[2]),
            "--model",
            "CodeNib + models",
            "--output",
            str(output),
            "--audit",
            str(audit),
        ],
        capture_output=True,
        text=True,
    )
    assert process.returncode != 0 and "require exactly 500" in process.stderr
    assert not output.exists() and not audit.exists()
