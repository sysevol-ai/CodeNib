# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``scripts/agent_compile/aggregate.py`` (Phase 0 kill switch)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGG_PATH = _PROJECT_ROOT / "scripts" / "agent_compile" / "aggregate.py"
_SPEC = importlib.util.spec_from_file_location("_aggregate_for_tests", _AGG_PATH)
assert _SPEC is not None and _SPEC.loader is not None
agg_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["_aggregate_for_tests"] = agg_mod
_SPEC.loader.exec_module(agg_mod)


def _cell(
    subset_id: str,
    instance_id: str,
    rep: int,
    *,
    total_tokens: float,
    total_turns: float,
    files_at_5: float,
    cost: float = 0.01,
    success: bool = True,
) -> Dict[str, Any]:
    return {
        "cell_id": f"{instance_id}__{subset_id}__model__rep{rep}",
        "instance_id": instance_id,
        "subset_id": subset_id,
        "rep": rep,
        "success": success,
        "metrics": {"files": {5: {"accuracy": files_at_5}}, "symbols": {}},
        "metrics_meaningful": True,
        "total_turns": total_turns,
        "total_duration_ms": 1000.0,
        "tool_call_count": 2,
        "prompt_tokens": int(total_tokens * 0.6),
        "completion_tokens": int(total_tokens * 0.4),
        "total_tokens": total_tokens,
        "cost_usd": cost,
    }


# ---------------------------------------------------------------------------
# Aggregation invariants
# ---------------------------------------------------------------------------


class TestAggregateRepFolding:
    def test_min_of_3_for_cost_like(self):
        """Tokens / turns / duration are min-of-3 per instance."""
        cells = [
            _cell(
                "A6", "inst-1", 1, total_tokens=10_000, total_turns=10, files_at_5=0.5
            ),
            _cell(
                "A6", "inst-1", 2, total_tokens=20_000, total_turns=15, files_at_5=0.5
            ),
            _cell(
                "A6", "inst-1", 3, total_tokens=30_000, total_turns=20, files_at_5=0.5
            ),
        ]
        metrics = agg_mod.aggregate(cells, max_turns=20)
        a6 = metrics["A6"]
        # Min token rep = 10_000; one instance → instance mean = 10_000.
        assert a6.mean_total_tokens == pytest.approx(10_000)
        # Min turns rep = 10
        assert a6.mean_total_turns == pytest.approx(10.0)
        assert a6.instance_count == 1

    def test_mean_of_3_for_accuracy(self):
        """Accuracy is two-sided noise → mean, not min."""
        cells = [
            _cell("A6", "inst-1", 1, total_tokens=1, total_turns=1, files_at_5=0.0),
            _cell("A6", "inst-1", 2, total_tokens=1, total_turns=1, files_at_5=0.5),
            _cell("A6", "inst-1", 3, total_tokens=1, total_turns=1, files_at_5=1.0),
        ]
        metrics = agg_mod.aggregate(cells, max_turns=20)
        a6 = metrics["A6"]
        # Mean across reps = 0.5; one instance → subset mean = 0.5
        assert a6.files_at_k[5] == pytest.approx(0.5)

    def test_cap_hit_any_rep_counts(self):
        """If ANY rep hit the turn cap, the instance is recorded as cap-hit."""
        cells = [
            _cell("A6", "inst-1", 1, total_tokens=1, total_turns=5, files_at_5=0.5),
            _cell("A6", "inst-1", 2, total_tokens=1, total_turns=20, files_at_5=0.5),
            _cell("A6", "inst-1", 3, total_tokens=1, total_turns=10, files_at_5=0.5),
            _cell("A6", "inst-2", 1, total_tokens=1, total_turns=5, files_at_5=0.5),
            _cell("A6", "inst-2", 2, total_tokens=1, total_turns=5, files_at_5=0.5),
            _cell("A6", "inst-2", 3, total_tokens=1, total_turns=5, files_at_5=0.5),
        ]
        metrics = agg_mod.aggregate(cells, max_turns=20)
        a6 = metrics["A6"]
        # 1 of 2 instances hit cap on any rep → 0.5
        assert a6.cap_hit_rate == pytest.approx(0.5)

    def test_failed_instances_excluded(self):
        cells = [
            _cell("A6", "inst-ok", 1, total_tokens=10, total_turns=5, files_at_5=1.0),
            _cell("A6", "inst-ok", 2, total_tokens=10, total_turns=5, files_at_5=1.0),
            _cell("A6", "inst-ok", 3, total_tokens=10, total_turns=5, files_at_5=1.0),
            _cell(
                "A6",
                "inst-fail",
                1,
                total_tokens=0,
                total_turns=0,
                files_at_5=0.0,
                success=False,
            ),
            _cell(
                "A6",
                "inst-fail",
                2,
                total_tokens=0,
                total_turns=0,
                files_at_5=0.0,
                success=False,
            ),
            _cell(
                "A6",
                "inst-fail",
                3,
                total_tokens=0,
                total_turns=0,
                files_at_5=0.0,
                success=False,
            ),
        ]
        metrics = agg_mod.aggregate(cells, max_turns=20)
        a6 = metrics["A6"]
        assert a6.instance_count == 1
        assert a6.failed_instances == 1
        assert a6.files_at_k[5] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Kill-switch verdict
# ---------------------------------------------------------------------------


class TestKillSwitch:
    def _metrics_with_a6(
        self,
        tokens: float,
        files_at_5: float,
    ) -> Dict[str, agg_mod.SubsetMetrics]:
        cells: List[Dict[str, Any]] = []
        for rep in (1, 2, 3):
            cells.append(
                _cell(
                    "A6",
                    "inst-1",
                    rep,
                    total_tokens=tokens,
                    total_turns=10,
                    files_at_5=files_at_5,
                )
            )
        return agg_mod.aggregate(cells, max_turns=20)

    def test_both_thresholds_clear_closes_rfc(self):
        metrics = self._metrics_with_a6(tokens=20_000, files_at_5=0.7)
        verdict = agg_mod._kill_switch_verdict(metrics)
        assert verdict["verdict"] == "close-rfc"
        assert verdict["tokens_ok"] is True
        assert verdict["files_at_5_ok"] is True

    def test_high_tokens_blocks_close(self):
        metrics = self._metrics_with_a6(tokens=50_000, files_at_5=0.8)
        verdict = agg_mod._kill_switch_verdict(metrics)
        assert verdict["verdict"] == "proceed"
        assert verdict["tokens_ok"] is False

    def test_low_accuracy_blocks_close(self):
        metrics = self._metrics_with_a6(tokens=10_000, files_at_5=0.3)
        verdict = agg_mod._kill_switch_verdict(metrics)
        assert verdict["verdict"] == "proceed"
        assert verdict["files_at_5_ok"] is False

    def test_no_a6_is_inconclusive(self):
        metrics = {"A0": agg_mod.SubsetMetrics(subset_id="A0")}
        verdict = agg_mod._kill_switch_verdict(metrics)
        assert verdict["verdict"] == "inconclusive"
