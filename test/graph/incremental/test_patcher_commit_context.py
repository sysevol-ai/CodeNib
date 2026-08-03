# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Commit-range propagation for incremental symbol patches."""

from __future__ import annotations

import pytest

from codenib.graph.code_graph import CodeGraph
from codenib.graph.incremental import change_mgr
from codenib.graph.incremental.patcher_rust import PatcherRust


@pytest.mark.parametrize(
    ("later_commit", "expected_target", "expected_hunks"),
    [
        (None, "HEAD", [(1, 1, 3, 3)]),
        ("release-candidate", "release-candidate", [(2, 4, 8, 11)]),
    ],
)
def test_patch_files_uses_effective_target_for_hunks_and_stats(
    tmp_path,
    monkeypatch,
    later_commit,
    expected_target,
    expected_hunks,
):
    graph = CodeGraph(str(tmp_path))
    patcher = PatcherRust(
        str(tmp_path),
        graph,
        lsp_client=object(),
    )
    observed = {}

    def fake_changed_line_ranges(
        project_root,
        file_path,
        base_commit,
        target_commit="HEAD",
    ):
        observed["diff"] = (
            project_root,
            file_path,
            base_commit,
            target_commit,
        )
        return list(expected_hunks)

    old_symbols = {
        "src/lib.rs:kept()": {
            "vertex_name": "src/lib.rs:kept():1",
            "start_line": 1,
            "end_line": 5,
        }
    }
    new_symbols = {
        "src/lib.rs:kept()": {
            "start_line": 1,
            "end_line": 5,
            "kind": 12,
        }
    }

    def fake_classify(actual_old, actual_new, hunks):
        observed["classified"] = (actual_old, actual_new, hunks)
        return {
            "deleted": [],
            "added": [],
            "affected": [],
            "shifted": [],
            "unchanged": ["src/lib.rs:kept()"],
            "invisible": [],
        }

    monkeypatch.setattr(
        change_mgr,
        "get_changed_line_ranges",
        fake_changed_line_ranges,
    )
    monkeypatch.setattr(
        patcher,
        "get_old_symbols",
        lambda _file_path: old_symbols,
    )
    monkeypatch.setattr(
        patcher,
        "_read_incremental_symbols",
        lambda **_kwargs: new_symbols,
    )
    monkeypatch.setattr(patcher, "_classify_symbols", fake_classify)

    stats = patcher.patch_files(
        {
            "modified": ["src/lib.rs"],
            "added": [],
            "deleted": [],
            "renamed": [],
        },
        earlier_commit="baseline",
        later_commit=later_commit,
    )

    assert observed["diff"] == (
        str(tmp_path),
        "src/lib.rs",
        "baseline",
        expected_target,
    )
    assert observed["classified"] == (
        old_symbols,
        new_symbols,
        expected_hunks,
    )
    assert stats["commit_earlier"] == "baseline"
    assert stats["commit_later"] == expected_target
