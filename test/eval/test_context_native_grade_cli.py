# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Unified native CLI options reach only the admitted Multi grader."""

import json
import sys

import pytest

from scripts import benchmark_context_solver as solver
from scripts import contextbench_multiswe as multi

CASES = [
    ("--native-memory-gib", "8", "native_memory_gib", 8),
    ("--native-network", "bridge", "native_network", "bridge"),
    ("--native-test-workers", "2", "native_test_workers", 2),
    ("--native-cpu-count", "2", "native_cpu_count", 2),
    # Additional native options are covered by their owning stack PR.
]


def cli(monkeypatch, tmp_path, source, options=()):
    run = tmp_path / "run"
    run.mkdir()
    (run / "config.json").write_text(json.dumps({"source": source}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_context_solver",
            "grade",
            "--solver-run",
            str(run),
            "--dataset",
            str(tmp_path / "dataset.json"),
            "--output",
            str(tmp_path / "output"),
            *options,
        ],
    )


@pytest.mark.parametrize("flag,value,field,expected", CASES)
def test_unified_grade_forwards_multi_options(
    monkeypatch, tmp_path, flag, value, field, expected
):
    cli(monkeypatch, tmp_path, "Multi", [flag, value])
    seen = []
    monkeypatch.setattr(multi, "grade_multi", lambda args: seen.append(args))
    solver.main()
    assert len(seen) == 1
    assert getattr(seen[0], field) == expected


@pytest.mark.parametrize("source", ["Verified", "Pro", "Poly"])
@pytest.mark.parametrize("flag,value,field,expected", CASES)
def test_multi_only_options_reject_other_graders(
    monkeypatch, tmp_path, source, flag, value, field, expected
):
    cli(monkeypatch, tmp_path, source, [flag, value])
    with pytest.raises(ValueError, match="require a Multi run"):
        solver.main()
    assert not (tmp_path / "output").exists()


def test_unified_multi_defaults_preserve_original_profile(monkeypatch, tmp_path):
    cli(monkeypatch, tmp_path, "Multi")
    seen = []
    monkeypatch.setattr(multi, "grade_multi", lambda args: seen.append(args))
    solver.main()
    assert seen[0].native_memory_gib == 3
    assert seen[0].native_network == "none"
    assert seen[0].native_test_workers is None


@pytest.mark.parametrize("flag,value,field,expected", CASES)
def test_unified_grade_rejects_invalid_resource_values(
    monkeypatch, tmp_path, flag, value, field, expected
):
    cli(monkeypatch, tmp_path, "Multi", [flag, "invalid"])
    with pytest.raises(SystemExit) as error:
        solver.main()
    assert error.value.code == 2
    assert not (tmp_path / "output").exists()
