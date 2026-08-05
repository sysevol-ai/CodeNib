# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for quality-constrained localization cost reports."""

from __future__ import annotations

import json

import pytest

from codenib.eval.reports.pricing import parse_pricing_snapshot
from codenib.eval.reports.quality_cost import (
    QualityCostError,
    analyze_quality_cost,
    load_quality_cost_cells,
    main,
    write_quality_cost_report,
)


def _cell(
    query_id: str,
    arm: str,
    *,
    quality: float,
    tokens: int,
    success: bool = True,
    cost_usd: float | None = None,
    model: str = "provider/model",
):
    instance_id = "org__repo-1" if query_id == "q1" else "other__repo-2"
    return {
        "cell_id": f"{query_id}__{arm}__rep1",
        "model": model,
        "instance_id": instance_id,
        "query_id": query_id,
        "query": f"Locate {query_id}",
        "subset_id": arm,
        "rep": 1,
        "success": success,
        "metrics_meaningful": True,
        "metrics": ({"answer_blocks": {"5": {"recall": quality}}} if success else {}),
        "prompt_tokens": tokens - 2,
        "completion_tokens": 2,
        "total_tokens": tokens,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": cost_usd,
    }


def _paired_cells():
    return [
        _cell("q1", "grep_only", quality=1.0, tokens=100, cost_usd=0.10),
        _cell("q2", "grep_only", quality=0.0, tokens=100, cost_usd=0.10),
        _cell("q1", "compact", quality=1.0, tokens=40, cost_usd=0.04),
        _cell("q2", "compact", quality=0.0, tokens=40, cost_usd=0.04),
    ]


def _pricing_snapshot():
    return parse_pricing_snapshot(
        {
            "schema": "codenib.pricing-snapshot.v1",
            "snapshot_id": "fixture",
            "currency": "USD",
            "effective_date": "2026-08-04",
            "retrieved_date": "2026-08-04",
            "models": [
                {
                    "model_id": "provider/model",
                    "aliases": [],
                    "channel": "fixture",
                    "token_semantics": "prompt_excludes_cache",
                    "cache_write_ttl": "5m",
                    "rates_per_million": {
                        "uncached_input": 1.0,
                        "output": 5.0,
                        "cache_read_input": 0.1,
                        "cache_write_input": 1.25,
                    },
                    "source_url": "https://example.com/pricing",
                }
            ],
        }
    )


def test_selects_lowest_cost_quality_qualified_arm():
    report = analyze_quality_cost(_paired_cells(), bootstrap_samples=100)
    model = report["models"]["provider/model"]

    assert model["arms"]["grep_only"]["tokens"]["per_success"] == 200
    assert model["arms"]["compact"]["tokens"]["per_success"] == 80
    optimum = model["constrained_optima"]["tokens_per_success"]
    assert optimum["selected_arm"] == "compact"
    assert optimum["ratio_to_baseline"] == pytest.approx(0.4)
    assert optimum["savings_fraction_vs_baseline"] == pytest.approx(0.6)


def test_infrastructure_failure_scores_zero_and_remains_charged():
    cells = _paired_cells()
    failed = next(
        cell
        for cell in cells
        if cell["query_id"] == "q2" and cell["subset_id"] == "compact"
    )
    failed.update(success=False, metrics={}, total_tokens=60)

    report = analyze_quality_cost(cells, bootstrap_samples=50)
    compact = report["models"]["provider/model"]["arms"]["compact"]

    assert compact["attempted"] == 2
    assert compact["infrastructure_success_rate"] == 0.5
    assert compact["quality_mean"] == 0.5
    assert compact["tokens"]["total"] == 100


def test_strict_denominator_rejects_unmatched_arms():
    cells = [
        cell
        for cell in _paired_cells()
        if not (cell["query_id"] == "q2" and cell["subset_id"] == "compact")
    ]

    with pytest.raises(QualityCostError, match="unmatched paired denominators"):
        analyze_quality_cost(cells, bootstrap_samples=10)

    report = analyze_quality_cost(
        cells, bootstrap_samples=10, strict_denominators=False
    )
    audit = report["models"]["provider/model"]["denominator_audit"]
    assert audit["common_unit_count"] == 1
    assert audit["mismatches"]["compact"]["missing_count"] == 1


def test_quality_regression_cannot_win_on_cost():
    cells = _paired_cells()
    for cell in cells:
        if cell["subset_id"] == "compact":
            cell["metrics"]["answer_blocks"]["5"]["recall"] = 0.0

    report = analyze_quality_cost(cells, bootstrap_samples=100)
    model = report["models"]["provider/model"]

    assert model["arms"]["compact"]["quality_qualified"] is False
    assert (
        model["constrained_optima"]["tokens_per_success"]["selected_arm"] == "grep_only"
    )


def test_arm_allowlist_excludes_unplanned_policy_variants():
    cells = _paired_cells()
    cells.extend(
        [
            _cell(query, "unplanned", quality=1.0, tokens=1, cost_usd=0.001)
            for query in ("q1", "q2")
        ]
    )

    report = analyze_quality_cost(
        cells,
        bootstrap_samples=10,
        included_arms=["grep_only", "compact"],
    )

    model = report["models"]["provider/model"]
    assert set(model["arms"]) == {"grep_only", "compact"}
    assert report["contract"]["included_arms"] == ["grep_only", "compact"]
    assert report["arm_selection_audit"] == {
        "input_cell_count": 6,
        "analyzed_cell_count": 4,
        "excluded_cell_count": 2,
        "excluded_arms": {"provider/model": {"unplanned": 2}},
    }

    with pytest.raises(QualityCostError, match="missing requested arms"):
        analyze_quality_cost(
            cells,
            bootstrap_samples=10,
            included_arms=["grep_only", "missing"],
        )


def test_zero_success_and_zero_only_local_cost_are_unavailable():
    cells = [
        _cell(query, arm, quality=0.0, tokens=10, cost_usd=0.0)
        for query in ("q1", "q2")
        for arm in ("grep_only", "compact")
    ]

    report = analyze_quality_cost(cells, bootstrap_samples=10)
    model = report["models"]["provider/model"]

    assert model["arms"]["compact"]["tokens"]["per_success"] is None
    assert model["arms"]["compact"]["usd"]["available"] is False
    assert (
        model["arms"]["compact"]["usd"]["reason"] == "ambiguous_zero_runtime_estimates"
    )
    assert model["constrained_optima"]["tokens_per_success"]["selected_arm"] is None


def test_mixed_zero_and_positive_runtime_estimates_are_not_undercounted():
    cells = _paired_cells()
    cells[0]["cost_usd"] = 0.0

    report = analyze_quality_cost(cells, bootstrap_samples=10)
    baseline = report["models"]["provider/model"]["arms"]["grep_only"]

    assert baseline["usd"]["available"] is False
    assert baseline["usd"]["reason"] == "ambiguous_zero_runtime_estimates"


def test_projection_requires_complete_raw_classes_and_uses_snapshot():
    report = analyze_quality_cost(
        _paired_cells(),
        bootstrap_samples=10,
        pricing_snapshot=_pricing_snapshot(),
        usd_source="projected",
    )
    compact = report["models"]["provider/model"]["arms"]["compact"]
    assert compact["usd"]["kind"] == "offline_projection"
    assert compact["usd"]["available"] is True

    cells = _paired_cells()
    cells[0]["completion_tokens"] = None
    unavailable = analyze_quality_cost(
        cells,
        bootstrap_samples=10,
        pricing_snapshot=_pricing_snapshot(),
        usd_source="projected",
    )
    baseline = unavailable["models"]["provider/model"]["arms"]["grep_only"]
    assert baseline["usd"]["available"] is False
    assert baseline["usd"]["reason"] == "missing_token_classes"


def test_unmetered_retry_attempt_makes_complete_cost_unavailable():
    cells = _paired_cells()
    compact = next(
        cell
        for cell in cells
        if cell["query_id"] == "q1" and cell["subset_id"] == "compact"
    )
    compact["_quality_cost_retry_attempts"] = [
        {"cell_id": compact["cell_id"], "success": False, "cost_usd": None}
    ]

    report = analyze_quality_cost(cells, bootstrap_samples=10)
    compact_report = report["models"]["provider/model"]["arms"]["compact"]

    assert compact_report["tokens"]["available"] is False
    assert compact_report["tokens"]["reason"] == "unmetered_retry_attempts"
    assert compact_report["tokens"]["execution_attempts"] == 3
    assert compact_report["usd"]["reason"] == "unmetered_retry_attempts"


def test_recursive_loader_audits_non_cells_and_duplicates(tmp_path):
    nested = tmp_path / "shard" / "cells"
    nested.mkdir(parents=True)
    cell = _cell("q1", "grep_only", quality=1.0, tokens=10)
    (nested / "cell.json").write_text(json.dumps(cell), encoding="utf-8")
    (tmp_path / "summary.json").write_text(
        json.dumps({"schema": "summary"}), encoding="utf-8"
    )

    cells, audit = load_quality_cost_cells([tmp_path])
    assert len(cells) == 1
    assert audit["skipped_non_cell_json"] == 1

    retry_dir = tmp_path / "protocol" / "failure_retries" / cell["cell_id"]
    retry_dir.mkdir(parents=True)
    (retry_dir / "failed_cell.json").write_text(
        json.dumps(
            {
                "cell_id": cell["cell_id"],
                "instance_id": cell["instance_id"],
                "query_id": cell["query_id"],
                "subset_id": cell["subset_id"],
                "success": False,
                "error": "provider failure",
            }
        ),
        encoding="utf-8",
    )
    cells, audit = load_quality_cost_cells([tmp_path])
    assert audit["retry_failure_records"] == 1
    assert audit["retry_failures_with_total_tokens"] == 0
    assert len(cells[0]["_quality_cost_retry_attempts"]) == 1

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(json.dumps(cell), encoding="utf-8")
    with pytest.raises(QualityCostError, match="duplicate cell"):
        load_quality_cost_cells([tmp_path])


def test_retry_links_within_input_root_when_models_share_cell_ids(tmp_path):
    roots = [tmp_path / "model-a", tmp_path / "model-b"]
    for index, root in enumerate(roots):
        cells_dir = root / "cells"
        cells_dir.mkdir(parents=True)
        cell = _cell(
            "q1",
            "grep_only",
            quality=1.0,
            tokens=10,
            model=f"provider/model-{index}",
        )
        (cells_dir / "shared.json").write_text(json.dumps(cell), encoding="utf-8")

    retry_dir = roots[1] / "protocol" / "failure_retries" / "shared"
    retry_dir.mkdir(parents=True)
    (retry_dir / "failed_cell.json").write_text(
        json.dumps({"cell_id": "q1__grep_only__rep1", "success": False}),
        encoding="utf-8",
    )

    cells, audit = load_quality_cost_cells(roots)
    by_model = {cell["model"]: cell for cell in cells}

    assert "_quality_cost_retry_attempts" not in by_model["provider/model-0"]
    assert len(by_model["provider/model-1"]["_quality_cost_retry_attempts"]) == 1
    assert audit["roots"]["input-02"]["name"] == "model-b"
    assert audit["roots"]["input-02"]["retry_failure_records"] == 1


def test_report_writer_and_cli_emit_json_and_markdown(tmp_path, capsys):
    output = tmp_path / "direct"
    report = write_quality_cost_report(
        cells=_paired_cells(), output_dir=output, bootstrap_samples=10
    )
    assert (
        json.loads((output / "quality_cost.json").read_text())["schema"]
        == report["schema"]
    )
    assert "Token optimum" in (output / "quality_cost.md").read_text()

    cells_dir = tmp_path / "cells"
    cells_dir.mkdir()
    for cell in _paired_cells():
        (cells_dir / f"{cell['cell_id']}.json").write_text(
            json.dumps(cell), encoding="utf-8"
        )
    cli_output = tmp_path / "cli"
    assert (
        main(
            [
                str(cells_dir),
                "--output-dir",
                str(cli_output),
                "--bootstrap-samples",
                "10",
            ]
        )
        == 0
    )
    assert "Quality-constrained cost" in capsys.readouterr().out
    assert (cli_output / "quality_cost.json").is_file()


def test_rejects_queries_without_quality_ground_truth():
    cells = _paired_cells()
    cells[0]["metrics_meaningful"] = False

    with pytest.raises(QualityCostError, match="meaningful ground truth"):
        analyze_quality_cost(cells, bootstrap_samples=10)
