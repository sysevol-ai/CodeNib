# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for shared agent-runner metric helpers."""

from __future__ import annotations

import json

from codeminer.eval.agent_runner.metrics import (
    load_cell_jsons,
    metric_at_k,
    safe_mean,
    safe_min,
    usable_cells,
)


def test_load_cell_jsons_skips_invalid_files(tmp_path):
    tmp_path.joinpath("ok.json").write_text(
        json.dumps({"success": True}),
        encoding="utf-8",
    )
    tmp_path.joinpath("bad.json").write_text("{", encoding="utf-8")
    tmp_path.joinpath("list.json").write_text("[]", encoding="utf-8")

    assert load_cell_jsons(tmp_path) == [{"success": True}]


def test_usable_cells_filters_success_and_meaningful_metrics():
    cells = [
        {"success": True, "metrics_meaningful": True, "id": "keep"},
        {"success": True, "metrics_meaningful": False, "id": "drop_metrics"},
        {"success": False, "metrics_meaningful": True, "id": "drop_failure"},
    ]

    usable = usable_cells(cells, require_metrics_meaningful=True)

    assert [cell["id"] for cell in usable] == ["keep"]


def test_metric_at_k_reads_dict_and_scalar_shapes():
    cell = {
        "metrics": {
            "files": {
                "1": {"accuracy": 0.25, "recall": 0.5},
                5: 1.0,
            }
        }
    }

    assert metric_at_k(cell, "files", 1) == 0.25
    assert metric_at_k(cell, "files", 1, field="recall") == 0.5
    assert metric_at_k(cell, "files", 5) == 1.0
    assert metric_at_k(cell, "files", 5, field="recall") is None
    assert metric_at_k(cell, "missing", 1) is None


def test_safe_aggregates_ignore_missing_values():
    values = [None, 1.0, 3.0]

    assert safe_mean(values) == 2.0
    assert safe_min(values) == 1.0
    assert safe_mean([None], default=0.0) == 0.0
    assert safe_min([None]) is None
