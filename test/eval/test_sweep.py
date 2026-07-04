# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for reusable agent-runner sweep helpers."""

from __future__ import annotations

import pytest

from codeminer.eval.agent_runner.sweep import validate_sweep_harness
from codeminer.eval.agent_runner.sweep_config import SweepConfig


def test_validate_sweep_harness_accepts_registered_tools_and_skills():
    cfg = SweepConfig(
        sweep_id="ok",
        default_tool_ids=["read", "grep"],
        subsets={"retrieval": ["read", "bm25_search"]},
    )

    validate_sweep_harness(cfg)


def test_validate_sweep_harness_rejects_unknown_default_tool():
    cfg = SweepConfig(
        sweep_id="bad_defaults",
        default_tool_ids=["file_read"],
        subsets={"baseline": ["read"]},
    )

    with pytest.raises(ValueError, match="default_tool_ids"):
        validate_sweep_harness(cfg)


def test_validate_sweep_harness_rejects_unknown_skill():
    cfg = SweepConfig(
        sweep_id="bad_skill",
        subsets={"retrieval": ["definitely_not_a_skill"]},
    )

    with pytest.raises(ValueError, match="unknown skills"):
        validate_sweep_harness(cfg)
