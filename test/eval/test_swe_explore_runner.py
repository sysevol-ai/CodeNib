# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from types import SimpleNamespace

from codenib.eval.benchmarks import swe_explore_runner
from codenib.eval.benchmarks.swe_explore import SWE_EXPLORE_METRICS


def test_runner_keeps_case_failures_in_denominator(monkeypatch, tmp_path) -> None:
    case_set = tmp_path / "cases.json"
    case_set.write_text(
        json.dumps(
            {
                "cases": [
                    {"instance_id": "ok", "language": "python"},
                    {"instance_id": "failed", "language": "go"},
                ]
            }
        ),
        encoding="utf-8",
    )
    cases = (
        SimpleNamespace(instance_id="ok", dataset="verified"),
        SimpleNamespace(instance_id="failed", dataset="multilingual"),
    )
    monkeypatch.setattr(swe_explore_runner, "_load_pinned_source_rows", lambda: {})
    monkeypatch.setattr(swe_explore_runner, "load_swe_explore_sources", lambda _x: {})
    monkeypatch.setattr(
        swe_explore_runner, "load_swe_explore_cases", lambda *_a, **_kw: cases
    )

    def run_case(case, **kwargs):
        if case.instance_id == "failed":
            raise RuntimeError("index unavailable")
        metrics = {metric: 1.0 for metric in SWE_EXPLORE_METRICS}
        return {
            "instance_id": case.instance_id,
            "dataset": case.dataset,
            "language": kwargs["language_label"],
            "success": True,
            "timing_seconds": {"build": 2.0, "load": 0.2, "query": 0.01},
            "evaluations": {"5": {"metrics": metrics}},
        }

    monkeypatch.setattr(swe_explore_runner, "_run_case", run_case)

    report = swe_explore_runner.run_swe_explore_benchmark(
        bench_path=tmp_path / "bench.jsonl",
        case_set_path=case_set,
        repos_root=tmp_path / "repos",
        top_ks=(5,),
    )

    assert report["summary"]["requested_cases"] == 2
    assert report["summary"]["successful_cases"] == 1
    assert report["summary"]["failed_cases"] == 1
    assert report["summary"]["failure_ids"] == ["failed"]
    assert report["summary"]["metrics_by_top_k"]["5"]["recall"] == 0.5
    assert report["cases"][1]["error_type"] == "RuntimeError"


def test_runner_rejects_duplicate_case_ids(tmp_path) -> None:
    case_set = tmp_path / "cases.json"
    case_set.write_text(
        json.dumps(
            {
                "cases": [
                    {"instance_id": "same", "language": "python"},
                    {"instance_id": "same", "language": "python"},
                ]
            }
        ),
        encoding="utf-8",
    )

    try:
        swe_explore_runner._load_case_set(case_set)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate case IDs must fail")
