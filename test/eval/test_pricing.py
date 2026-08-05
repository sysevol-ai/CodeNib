# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for immutable offline pricing snapshots."""

from __future__ import annotations

import json

import pytest

from codenib.eval.reports.pricing import (
    PricingSnapshotError,
    bundled_pricing_snapshot_path,
    load_pricing_snapshot,
    parse_pricing_snapshot,
    project_cell_cost,
)


def _snapshot(*, token_semantics: str = "prompt_excludes_cache", input_rate=1.0):
    return {
        "schema": "codenib.pricing-snapshot.v1",
        "snapshot_id": "fixture-2026-08-04",
        "currency": "USD",
        "effective_date": "2026-08-04",
        "retrieved_date": "2026-08-04",
        "models": [
            {
                "model_id": "provider/model-v1",
                "aliases": ["provider/model"],
                "channel": "direct-api",
                "token_semantics": token_semantics,
                "cache_write_ttl": "5m",
                "rates_per_million": {
                    "uncached_input": input_rate,
                    "output": 5.0,
                    "cache_read_input": 0.1,
                    "cache_write_input": 1.25,
                },
                "source_url": "https://example.com/pricing",
            }
        ],
    }


def _cell(*, prompt_tokens=1_000_000):
    return {
        "model": "provider/model",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
    }


def test_projects_each_token_class_under_declared_semantics():
    snapshot = parse_pricing_snapshot(_snapshot())

    projected = project_cell_cost(_cell(), snapshot)

    assert projected["available"] is True
    assert projected["billable_tokens"]["uncached_input"] == 1_000_000
    assert projected["total"] == pytest.approx(7.35)


def test_prompt_including_cache_subtracts_cached_classes_once():
    snapshot = parse_pricing_snapshot(
        _snapshot(token_semantics="prompt_includes_cache")
    )

    projected = project_cell_cost(_cell(prompt_tokens=3_000_000), snapshot)

    assert projected["billable_tokens"]["uncached_input"] == 1_000_000
    assert projected["total"] == pytest.approx(7.35)


def test_projection_refuses_missing_or_inconsistent_token_classes():
    snapshot = parse_pricing_snapshot(_snapshot())
    missing = _cell()
    missing["completion_tokens"] = None

    assert project_cell_cost(missing, snapshot)["reason"] == "missing_token_classes"

    inclusive = parse_pricing_snapshot(
        _snapshot(token_semantics="prompt_includes_cache")
    )
    inconsistent = project_cell_cost(_cell(prompt_tokens=1), inclusive)
    assert inconsistent["reason"] == "inconsistent_token_classes"


def test_snapshot_rejects_invalid_dates_and_ambiguous_aliases():
    invalid_date = _snapshot()
    invalid_date["retrieved_date"] = "2026-02-30"
    with pytest.raises(PricingSnapshotError, match="valid calendar date"):
        parse_pricing_snapshot(invalid_date)

    ambiguous = _snapshot()
    duplicate = dict(ambiguous["models"][0])
    duplicate.update(model_id="provider/other", aliases=["provider/model"])
    ambiguous["models"].append(duplicate)
    with pytest.raises(PricingSnapshotError, match="ambiguous"):
        parse_pricing_snapshot(ambiguous)


def test_bundled_snapshot_has_hashed_portable_identity():
    path = bundled_pricing_snapshot_path()

    snapshot = load_pricing_snapshot(path)
    identity = snapshot.public_identity()

    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == identity["schema"]
    assert identity["source_file"] == path.name
    assert str(path.parent) not in json.dumps(identity)
    assert identity["sha256"].startswith("sha256:")


def test_price_changes_affect_projection_without_changing_tokens():
    low = parse_pricing_snapshot(_snapshot(input_rate=1.0))
    high = parse_pricing_snapshot(_snapshot(input_rate=2.0))

    low_cost = project_cell_cost(_cell(), low)
    high_cost = project_cell_cost(_cell(), high)

    assert low_cost["billable_tokens"] == high_cost["billable_tokens"]
    assert high_cost["total"] - low_cost["total"] == pytest.approx(1.0)
