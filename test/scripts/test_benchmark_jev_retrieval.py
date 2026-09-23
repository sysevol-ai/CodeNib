# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from codenib.types import NodeInfo
from scripts.benchmark_jev_retrieval import MeasuredDecisions, quality, summarize


def test_duplicate_chunks_do_not_inflate_gold_file_group_coverage():
    case = {"required_path_groups": [["a.py", "alternative.py"], ["b.py"]]}
    nodes = [
        NodeInfo(file="unrelated.py"),
        NodeInfo(file="a.py"),
        NodeInfo(file="a.py"),
    ]

    assert quality(case, nodes, 3) == {
        "coverage": 0.5,
        "success": 0.0,
        "mrr": 0.5,
    }
    assert quality(case, nodes, 1) == {"coverage": 0.0, "success": 0.0, "mrr": 0.0}


def test_failed_observations_remain_in_quality_latency_and_failure_totals():
    base = {
        "arm": "bm25+jev",
        "candidate_quality": {"coverage": 1.0},
        "retrieval_ms": 10.0,
        "recorded_cost_usd": 0.01,
    }
    summary = summarize(
        [
            {
                **base,
                "quality_at_5": {"coverage": 1.0, "success": 1.0, "mrr": 1.0},
                "total_ms": 500.0,
                "rerank_ms": 490.0,
                "failed_calls": 0,
            },
            {
                **base,
                "quality_at_5": {"coverage": 0.0, "success": 0.0, "mrr": 0.0},
                "total_ms": 30000.0,
                "rerank_ms": 29990.0,
                "failed_calls": 1,
            },
        ]
    )["bm25+jev"]

    assert summary["observations"] == 2
    assert summary["coverage_at_5"] == 0.5
    assert summary["total_p50_ms"] == 15250.0
    assert summary["failed_calls"] == 1
    assert summary["recorded_cost_usd"] == 0.02


def test_cost_limit_is_recorded_as_a_failure_instead_of_an_unnoticed_fallback():
    client = MeasuredDecisions(model="typesafe/jev-1.13", max_cost_usd=0.01)
    client.cost_usd = 0.01

    with pytest.raises(RuntimeError, match="cost limit"):
        client.decide(state="unused", questions={})

    assert len(client.calls) == 1
    assert "cost limit" in client.calls[0]["error"]
