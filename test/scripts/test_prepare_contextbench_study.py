# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

import json

import pytest

from scripts.agent_compile.prepare_contextbench_study import _write_summary


def test_build_summary_keeps_fixed_protocol_denominator(tmp_path):
    output = tmp_path / "summary.json"
    _write_summary(
        output,
        {
            "one": {"instance_id": "one", "status": "ready"},
            "two": {"instance_id": "two", "status": "failed"},
        },
        planned_instance_ids={"one", "two", "three"},
    )

    payload = json.loads(output.read_text())
    assert payload["schema_version"] == 2
    assert payload["planned"] == 3
    assert payload["recorded"] == 2
    assert payload["pending"] == 1
    assert payload["ready"] == 1
    assert payload["failed"] == 1


def test_build_summary_rejects_rows_outside_frozen_plan(tmp_path):
    with pytest.raises(ValueError, match="unplanned instances"):
        _write_summary(
            tmp_path / "summary.json",
            {"other": {"instance_id": "other", "status": "ready"}},
            planned_instance_ids={"one"},
        )
