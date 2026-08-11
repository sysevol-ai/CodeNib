# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the native Python chunk benchmark and promotion gate."""

from __future__ import annotations

import math
import os

import pytest

from codenib.code_chunking.base import CodeChunk
from codenib.code_chunking.python_chunker import _load_native_core
from scripts.profiling import profile_python_native_chunker as profile


def _native_available() -> bool:
    core = _load_native_core()
    return core is not None and hasattr(core, "extract_python_chunk_spans")


def test_promotion_decision_accepts_exact_threshold_with_parity():
    decision = profile.promotion_decision(
        legacy_seconds=1.0,
        candidate_seconds=0.8,
        threshold=0.20,
    )

    assert decision["improvement_fraction"] == pytest.approx(0.20)
    assert decision["promotion_cutoff_seconds"] == 0.8
    assert decision["promoted"] is True
    assert decision["recommendation"] == "promote-native-python-chunker"


def test_promotion_decision_rejects_just_below_threshold():
    decision = profile.promotion_decision(
        legacy_seconds=1.0,
        candidate_seconds=math.nextafter(0.8, math.inf),
        threshold=0.20,
    )

    assert decision["promoted"] is False
    assert decision["reason"] == "end-to-end improvement is below threshold"


def test_promotion_decision_never_promotes_slower_candidate():
    decision = profile.promotion_decision(
        legacy_seconds=1.0,
        candidate_seconds=1.01,
        threshold=0.20,
    )

    assert decision["improvement_fraction"] < 0
    assert decision["promoted"] is False
    assert decision["recommendation"] == "keep-experimental"
    assert decision["reason"] == "candidate is slower than the established chunker"


def test_promotion_decision_requires_parity():
    decision = profile.promotion_decision(
        legacy_seconds=1.0,
        candidate_seconds=0.5,
        parity=False,
    )

    assert decision["promoted"] is False
    assert decision["reason"] == "semantic parity failed"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"legacy_seconds": 0.0, "candidate_seconds": 1.0},
        {"legacy_seconds": math.nan, "candidate_seconds": 1.0},
        {"legacy_seconds": math.inf, "candidate_seconds": 1.0},
        {"legacy_seconds": 1.0, "candidate_seconds": 0.0},
        {"legacy_seconds": 1.0, "candidate_seconds": math.nan},
        {"legacy_seconds": 1.0, "candidate_seconds": math.inf},
        {"legacy_seconds": 1.0, "candidate_seconds": 0.5, "threshold": 0.0},
        {"legacy_seconds": 1.0, "candidate_seconds": 0.5, "threshold": 1.0},
        {
            "legacy_seconds": 1.0,
            "candidate_seconds": 0.5,
            "threshold": math.nan,
        },
        {"legacy_seconds": 1.0, "candidate_seconds": 0.5, "parity": 1},
    ],
)
def test_promotion_decision_rejects_invalid_inputs(kwargs):
    with pytest.raises(ValueError):
        profile.promotion_decision(**kwargs)


def test_summarize_samples_reports_distribution_and_rejects_non_finite():
    assert profile.summarize_samples([0.4, 0.1, 0.2, 0.3]) == {
        "samples": 4,
        "min_seconds": 0.1,
        "median_seconds": 0.25,
        "p95_seconds": 0.4,
        "max_seconds": 0.4,
    }
    with pytest.raises(ValueError, match="finite"):
        profile.summarize_samples([0.1, math.nan])


def test_chunk_parity_reports_first_difference():
    left = CodeChunk("a", 0, 0, "function", "a", "sample.py", "sample.py:a()")
    right = CodeChunk("b", 0, 0, "function", "a", "sample.py", "sample.py:a()")

    with pytest.raises(AssertionError, match="chunk 0 differs"):
        profile._assert_chunk_parity([left], [right])


def test_arm_order_is_balanced_ab_ba():
    orders = [profile._arm_order(iteration) for iteration in range(6)]

    assert orders == [
        ("off", "required"),
        ("required", "off"),
        ("off", "required"),
        ("required", "off"),
        ("off", "required"),
        ("required", "off"),
    ]


def test_profile_rejects_unbalanced_iteration_count_before_loading_core(tmp_path):
    with pytest.raises(ValueError, match="even"):
        profile.profile_repository(repo_root=tmp_path, iterations=5)


def test_run_arm_keeps_candidate_validation_outside_stopwatch(tmp_path, monkeypatch):
    source = tmp_path / "sample.py"
    source.write_text("def sample():\n    return 1\n", encoding="utf-8")
    clock = {"value": 0.0}
    events = []

    class FakeChunker:
        def chunk_file(self, *args, **kwargs):
            events.append("chunk")
            clock["value"] += 2.0
            return []

        @property
        def native_chunk_backend(self):
            events.append("backend-check")
            clock["value"] += 10.0
            return "native-tree-sitter-poc"

        @property
        def native_chunk_timings(self):
            events.append("stage-aggregation")
            clock["value"] += 10.0
            return {"native_parse": 0.25}

    monkeypatch.setattr(profile.time, "perf_counter", lambda: clock["value"])
    monkeypatch.setattr(profile.gc, "isenabled", lambda: True)
    monkeypatch.setattr(profile.gc, "collect", lambda: events.append("gc-collect"))
    monkeypatch.setattr(profile.gc, "disable", lambda: events.append("gc-disable"))
    monkeypatch.setattr(profile.gc, "enable", lambda: events.append("gc-enable"))
    monkeypatch.setenv("CODENIB_NATIVE_PYTHON_CHUNKER", "auto")

    chunks, elapsed, stages = profile._run_arm(
        chunker=FakeChunker(),
        files=[source],
        repo_root=tmp_path,
        mode="required",
    )

    assert chunks == []
    assert elapsed == 2.0
    assert stages == {"native_parse": 0.25}
    assert events == [
        "gc-collect",
        "gc-disable",
        "chunk",
        "backend-check",
        "stage-aggregation",
        "gc-enable",
    ]
    assert os.environ["CODENIB_NATIVE_PYTHON_CHUNKER"] == "auto"


def test_run_arm_restores_disabled_gc_and_environment_on_failure(tmp_path, monkeypatch):
    source = tmp_path / "sample.py"
    source.write_text("def sample():\n    return 1\n", encoding="utf-8")
    events = []

    class BrokenChunker:
        def chunk_file(self, *args, **kwargs):
            raise RuntimeError("synthetic failure")

    monkeypatch.setattr(profile.gc, "isenabled", lambda: False)
    monkeypatch.setattr(profile.gc, "collect", lambda: events.append("gc-collect"))
    monkeypatch.setattr(profile.gc, "disable", lambda: events.append("gc-disable"))
    monkeypatch.setattr(profile.gc, "enable", lambda: events.append("gc-enable"))
    monkeypatch.delenv("CODENIB_NATIVE_PYTHON_CHUNKER", raising=False)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        profile._run_arm(
            chunker=BrokenChunker(),
            files=[source],
            repo_root=tmp_path,
            mode="off",
        )

    assert events == ["gc-collect", "gc-disable", "gc-disable"]
    assert "CODENIB_NATIVE_PYTHON_CHUNKER" not in os.environ


def test_git_subject_identity_is_explicit_when_directory_is_not_a_repository(tmp_path):
    assert profile._git_subject_identity(tmp_path) == {
        "git_commit": None,
        "git_dirty": None,
    }


def test_subject_identity_must_remain_stable_during_benchmark():
    clean = {"git_commit": "abc123", "git_dirty": False}

    assert profile._require_stable_subject_identity(clean, clean.copy()) == clean
    with pytest.raises(RuntimeError, match="changed during the benchmark"):
        profile._require_stable_subject_identity(
            clean,
            {"git_commit": "def456", "git_dirty": False},
        )


@pytest.mark.skipif(not _native_available(), reason="native chunk POC not built")
def test_profile_repository_reports_parity_raw_samples_and_arm_order(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Example:\n"
        "    def method(self):\n"
        "        return 1\n\n"
        "def top():\n"
        "    return Example().method()\n",
        encoding="utf-8",
    )

    report = profile.profile_repository(
        repo_root=tmp_path,
        iterations=2,
        warmups=1,
    )

    assert report["schema_version"] == 1
    assert report["parity"] == {"passed": True, "error": None}
    assert report["subject"]["file_count"] == 1
    assert report["subject"]["chunk_count"] == 2
    assert report["subject"]["git_commit"] is None
    assert report["subject"]["git_dirty"] is None
    assert report["legacy"]["total"]["samples"] == 2
    assert len(report["legacy"]["samples_seconds"]) == 2
    assert len(report["candidate"]["samples_seconds"]) == 2
    assert [round_["arm_order"] for round_ in report["measurements"]] == [
        ["legacy", "candidate"],
        ["candidate", "legacy"],
    ]
    native_parse = report["candidate"]["native_stages"]["native_parse"]
    assert native_parse["samples"] == 2
    assert len(native_parse["samples_seconds"]) == 2
