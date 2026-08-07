# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codenib.eval.retrieval_eval import (
    _rel_norm,
    normalize_file_path,
    normalize_symbol_identifier,
    score_agent_localization,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/target.py", "src/target.py"),
        ("./src/target.py", "src/target.py"),
        ("src\\target.py", "src/target.py"),
        ("src/generated/../target.py", "src/target.py"),
        ("src//./target.py", "src/target.py"),
    ],
)
def test_normalize_file_path_canonicalizes_safe_relative_paths(raw, expected):
    assert normalize_file_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "../src/target.py",
        "src/../../target.py",
        "/tmp/repo/src/target.py",
        r"C:\repo\src\target.py",
        r"C:repo\src\target.py",
        r"\\server\share\target.py",
        "\x00target.py",
    ],
)
def test_normalize_file_path_rejects_absolute_and_escaping_paths(raw):
    assert normalize_file_path(raw) is None


def test_repo_aware_normalization_accepts_only_contained_absolute_paths(tmp_path):
    repo = tmp_path / "repo"
    inside = repo / "src" / "target.py"
    outside = tmp_path / "outside.py"

    assert _rel_norm(str(inside), str(repo)) == "src/target.py"
    assert _rel_norm(str(outside), str(repo)) is None


def test_repo_aware_normalization_rejects_symlink_escape(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("outside", encoding="utf-8")
    link = repo / "linked.py"
    link.symlink_to(outside)

    assert _rel_norm(str(link), str(repo)) is None


def test_repo_aware_normalization_resolves_symlink_before_parent_segments(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside_child = tmp_path / "outside" / "child"
    outside_child.mkdir(parents=True)
    (repo / "jump").symlink_to(outside_child, target_is_directory=True)

    escaped = repo / "jump" / ".." / "target.py"

    assert _rel_norm(str(escaped), str(repo)) is None


def test_invalid_file_and_symbol_predictions_cannot_receive_hits(tmp_path):
    metrics = score_agent_localization(
        answer=("Files: ../src/target.py\n" "Symbols: ../src/target.py:target()"),
        file_read_paths=[str(tmp_path / "outside.py")],
        nodes=[],
        target_files=["src/target.py"],
        target_symbols=["src/target.py:target()"],
        ks=[1],
        repo_path=str(tmp_path / "repo"),
    )

    assert metrics["files"][1] == {
        "accuracy": 0.0,
        "precision": 0.0,
        "recall": 0.0,
        "hits": 0,
    }
    assert metrics["symbols"][1] == {
        "accuracy": 0.0,
        "precision": 0.0,
        "recall": 0.0,
        "hits": 0,
    }
    assert normalize_symbol_identifier("../src/target.py:target()") is None


def test_contained_absolute_read_path_can_receive_a_hit(tmp_path):
    repo = tmp_path / "repo"
    target = repo / "src" / "target.py"

    metrics = score_agent_localization(
        answer="",
        file_read_paths=[str(target)],
        nodes=[],
        target_files=["src/target.py"],
        target_symbols=[],
        ks=[1],
        repo_path=str(repo),
    )

    assert metrics["files"][1]["recall"] == 1.0


def test_rejected_prediction_still_consumes_its_rank(tmp_path):
    metrics = score_agent_localization(
        answer="Files: ../outside.py, src/target.py",
        file_read_paths=[],
        nodes=[],
        target_files=["src/target.py"],
        target_symbols=[],
        ks=[1, 2],
        repo_path=str(tmp_path / "repo"),
    )

    assert metrics["files"][1]["recall"] == 0.0
    assert metrics["files"][2]["recall"] == 1.0
    assert metrics["files"][2]["precision"] == 0.5
