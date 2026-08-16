# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from scripts.benchmark_demo_wiki import _summary, evaluate_page


def test_evaluate_page_reports_quality_without_retaining_prose():
    result = evaluate_page(
        {
            "markdown": "Readable explanation.",
            "citations": [{"file": "src/main.py"}],
            "generation": {
                "mode": "generated",
                "fallback": None,
                "metrics": {"model_calls": 1},
            },
            "quality": {"valid": True},
            "grounding": {
                "valid": True,
                "citation_coverage": 1.0,
                "evidence_count": 4,
                "relation_count": 2,
            },
        }
    )

    assert result["quality_valid"] is True
    assert result["generation_mode"] == "generated"
    assert result["citation_count"] == 1
    assert result["markdown_chars"] == 21
    assert "markdown" not in result


def test_wiki_benchmark_summary_counts_degraded_runs():
    summary = _summary(
        [
            {
                "status": "ok",
                "wall_ms": 10.0,
                "quality_valid": True,
                "generation_mode": "generated",
            },
            {
                "status": "ok",
                "wall_ms": 30.0,
                "quality_valid": False,
                "generation_mode": "degraded",
            },
            {"status": "error", "wall_ms": 5.0},
        ]
    )

    assert summary == {
        "successful": 2,
        "failed": 1,
        "quality_valid": 1,
        "degraded": 1,
        "wall_ms": {"p50": 20.0, "p95": 30.0, "max": 30.0},
    }
