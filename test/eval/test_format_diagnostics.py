# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for answer-format diagnostics."""

from __future__ import annotations

import json

from codenib.eval.agent_runner.format_diagnostics import (
    load_format_diagnostics,
    load_meaningful_cells,
    summarize_format_cells,
)


def _cell(
    arm: str,
    files_recall: float,
    answer_recall: float,
    *,
    format_failed: bool = False,
    success: bool = True,
    metrics_meaningful: bool = True,
):
    return {
        "success": success,
        "metrics_meaningful": metrics_meaningful,
        "subset_id": arm,
        "format_failed": format_failed,
        "metrics": {
            "files": {"5": {"recall": files_recall}},
            "answer_blocks": {"5": {"recall": answer_recall}},
        },
    }


def test_summarize_format_cells_separates_formatted_subset():
    summaries = summarize_format_cells(
        [
            _cell("preload", 1.0, 1.0),
            _cell("preload", 0.0, 0.0, format_failed=True),
            _cell("grep_only", 0.5, 0.25),
        ]
    )

    by_arm = {summary.arm: summary for summary in summaries}

    preload = by_arm["preload"]
    assert preload.n == 2
    assert preload.format_fail_rate == 0.5
    assert preload.files_all == 0.5
    assert preload.files_formatted == 1.0
    assert preload.answer_blocks_all == 0.5
    assert preload.answer_blocks_formatted == 1.0

    assert by_arm["grep_only"].format_fail_rate == 0.0


def test_load_format_diagnostics_filters_unusable_cells(tmp_path):
    cells_dir = tmp_path / "run" / "cells"
    cells_dir.mkdir(parents=True)
    cells_dir.joinpath("good.json").write_text(
        json.dumps(_cell("grep_only", 0.5, 0.25)),
        encoding="utf-8",
    )
    cells_dir.joinpath("failed.json").write_text(
        json.dumps(_cell("grep_only", 1.0, 1.0, success=False)),
        encoding="utf-8",
    )
    cells_dir.joinpath("bad.json").write_text("{", encoding="utf-8")

    cells = load_meaningful_cells(tmp_path / "run")
    diagnostics = load_format_diagnostics(tmp_path / "run")

    assert len(cells) == 1
    assert diagnostics.result_name == "run"
    assert diagnostics.n_meaningful == 1
    assert diagnostics.arms[0].arm == "grep_only"
