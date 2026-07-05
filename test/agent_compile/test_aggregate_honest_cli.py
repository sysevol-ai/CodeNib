# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CLI smoke tests for honest format diagnostics."""

from __future__ import annotations

import json

from scripts.agent_compile import aggregate_honest


def _write_cell(cells_dir, name, arm, files_recall, answer_recall, format_failed):
    cells_dir.joinpath(name).write_text(
        json.dumps(
            {
                "success": True,
                "metrics_meaningful": True,
                "subset_id": arm,
                "format_failed": format_failed,
                "metrics": {
                    "files": {"5": {"recall": files_recall}},
                    "answer_blocks": {"5": {"recall": answer_recall}},
                },
            }
        ),
        encoding="utf-8",
    )


def test_aggregate_honest_cli_reports_format_failures(tmp_path, capsys):
    run_dir = tmp_path / "run"
    cells_dir = run_dir / "cells"
    cells_dir.mkdir(parents=True)
    _write_cell(cells_dir, "ok.json", "preload", 1.0, 1.0, False)
    _write_cell(cells_dir, "fmt.json", "preload", 0.0, 0.0, True)

    rc = aggregate_honest.main([str(run_dir)])

    out = capsys.readouterr().out
    assert rc == 0
    assert "# Honest report - localization accuracy vs format-failure" in out
    assert "preload" in out
    assert "50%" in out
