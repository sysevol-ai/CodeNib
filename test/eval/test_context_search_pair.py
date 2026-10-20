# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Version-bound multi-worker source assembly; no model/provider calls."""

import copy
import json

import pytest
import tiktoken

from scripts.contextbench_search_pair import assemble_workers, scout_query


def worker(label, spans, fee=0.01):
    return {
        "label": label,
        "commit": "a" * 40,
        "source_fingerprint": "b" * 64,
        "accepted": True,
        "all_model_usage_usd": fee,
        "prediction": {
            "instance_id": "task",
            "traj_data": {
                "pred_files": list(spans),
                "pred_spans": spans,
                "pred_steps": [{"files": list(spans), "spans": spans}],
            },
            "model_patch": "",
        },
    }


def test_pair_unions_actual_intervals_preserves_steps_and_does_not_mutate_workers():
    a = worker("first", {"a.py": [{"start": 4, "end": 8}]})
    b = worker(
        "second", {"a.py": [{"start": 6, "end": 11}], "b.py": [{"start": 1, "end": 2}]}
    )
    before = copy.deepcopy([a, b])
    pred, receipt = assemble_workers("task", [a, b])
    assert pred["traj_data"]["pred_spans"] == {
        "a.py": [{"start": 4, "end": 11}],
        "b.py": [{"start": 1, "end": 2}],
    }
    assert receipt["unique_observed_source_lines"] == 10
    assert receipt["repeated_source_lines_across_workers"] == 3
    assert receipt["all_model_usage_usd"] == pytest.approx(0.02)
    assert receipt["source_bodies_added_by_assembler"] == 0
    pred["traj_data"]["pred_steps"][0]["files"].append("other")
    assert [a, b] == before


@pytest.mark.parametrize(
    "fault",
    [
        "snapshot",
        "fingerprint",
        "task",
        "unaccepted",
        "duplicate",
        "patch",
        "path",
        "zero_line",
        "bool_line",
        "fee",
        "missing_file",
    ],
)
def test_pair_rejects_mixed_or_unaccepted_source_receipts(fault):
    a = worker("first", {"a.py": [{"start": 1, "end": 2}]})
    b = copy.deepcopy(a)
    b["label"] = "second"
    if fault == "snapshot":
        b["commit"] = "c" * 40
    if fault == "fingerprint":
        b["source_fingerprint"] = "c" * 64
    if fault == "task":
        b["prediction"]["instance_id"] = "other"
    if fault == "unaccepted":
        b["accepted"] = False
    if fault == "duplicate":
        b["label"] = "first"
    if fault == "patch":
        b["prediction"]["model_patch"] = "diff"
    if fault == "path":
        b["prediction"]["traj_data"]["pred_files"] = ["../a.py"]
    if fault in {"zero_line", "bool_line"}:
        b["prediction"]["traj_data"]["pred_spans"]["a.py"][0]["start"] = (
            0 if fault == "zero_line" else True
        )
    if fault == "fee":
        b["all_model_usage_usd"] = float("nan")
    if fault == "missing_file":
        b["prediction"]["traj_data"]["pred_files"] = []
    with pytest.raises(ValueError):
        assemble_workers("task", [a, b])


def test_unknown_worker_fee_survives_assembly_without_becoming_zero():
    a = worker("first", {"a.py": [{"start": 1, "end": 2}]})
    b = worker("second", {"b.py": [{"start": 1, "end": 2}]}, fee=None)
    _, receipt = assemble_workers("task", [a, b])
    assert receipt["all_model_usage_usd"] is None
    assert receipt["unknown_fee_workers"] == 1
    assert receipt["known_only_usage_usd"] == 0.01


def test_scout_sees_only_version_bound_locations_not_worker_bodies_or_answer():
    a = worker("first", {"a.py": [{"start": 4, "end": 8}]})
    a["prediction"]["traj_data"]["pred_files"].append("filename-only.py")
    a["prediction"]["answer"] = "UNTRUSTED FIRST WORKER BODY"
    issue = 'An issue containing "quotes" and\nnewlines'
    query = scout_query(
        issue,
        a["prediction"],
        commit=a["commit"],
        source_fingerprint=a["source_fingerprint"],
    )
    payload = json.loads(query[query.index('{"original_issue"') :])
    assert payload["original_issue"] == issue
    nav = payload["navigation_only"]
    assert nav["source_bodies_included"] is False
    assert nav["entries"][0]["file"] == "a.py"
    assert nav["entries"][0]["lines"] == [4, 8]
    assert "UNTRUSTED" not in query and "filename-only.py" not in query
    assert nav["omitted_intervals"] == 0
    other = scout_query(
        issue,
        a["prediction"],
        commit="c" * 40,
        source_fingerprint=a["source_fingerprint"],
    )
    assert nav["entries"][0]["handle"] not in other


def test_navigation_cap_is_explicit_without_shortening_original_issue():
    a = worker(
        "first", {f"path/{i:03}.py": [{"start": 1, "end": 20}] for i in range(100)}
    )
    issue = "Keep all issue details. " * 500
    query = scout_query(
        issue,
        a["prediction"],
        commit=a["commit"],
        source_fingerprint=a["source_fingerprint"],
        token_budget=256,
    )
    payload = json.loads(query[query.index('{"original_issue"') :])
    nav = payload["navigation_only"]
    assert payload["original_issue"] == issue
    assert 0 < len(nav["entries"]) < 100
    assert nav["omitted_intervals"] == 100 - len(nav["entries"])
    assert (
        len(
            tiktoken.get_encoding("cl100k_base").encode(
                json.dumps(nav), disallowed_special=()
            )
        )
        <= 256
    )
