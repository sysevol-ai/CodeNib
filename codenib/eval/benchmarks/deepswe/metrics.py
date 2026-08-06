# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""DeepSWE metric aggregation and token-cost normalization."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

DEFAULT_PRICES_USD_PER_MTOK = {
    "gpt-5.6-luna": {
        "input": 2.50,
        "cached_input": 0.25,
        "output": 15.00,
    },
    "gpt-5.6-terra": {
        "input": 2.50,
        "cached_input": 0.25,
        "output": 15.00,
    },
}


@dataclass(frozen=True)
class Pricing:
    """Per-million-token rates used to cost one DeepSWE trial."""

    input_usd_per_mtok: float | None
    cached_input_usd_per_mtok: float | None
    output_usd_per_mtok: float | None
    output_includes_reasoning: bool = True


def pricing_for_model(
    model: str,
    *,
    input_usd_per_mtok: float | None = None,
    cached_input_usd_per_mtok: float | None = None,
    output_usd_per_mtok: float | None = None,
    output_includes_reasoning: bool = True,
) -> Pricing:
    """Resolve optional overrides against the known rates for *model*."""

    defaults = DEFAULT_PRICES_USD_PER_MTOK.get(model, {})
    return Pricing(
        input_usd_per_mtok=(
            input_usd_per_mtok
            if input_usd_per_mtok is not None
            else defaults.get("input")
        ),
        cached_input_usd_per_mtok=(
            cached_input_usd_per_mtok
            if cached_input_usd_per_mtok is not None
            else defaults.get("cached_input")
        ),
        output_usd_per_mtok=(
            output_usd_per_mtok
            if output_usd_per_mtok is not None
            else defaults.get("output")
        ),
        output_includes_reasoning=output_includes_reasoning,
    )


def _main_cost(row: Mapping[str, Any], pricing: Pricing) -> float | None:
    if (
        pricing.input_usd_per_mtok is None
        or pricing.cached_input_usd_per_mtok is None
        or pricing.output_usd_per_mtok is None
        or row.get("main_input_tokens") is None
    ):
        return None
    input_tokens = float(row.get("main_input_tokens") or 0)
    cached_tokens = float(row.get("main_cached_input_tokens") or 0)
    output_tokens = float(row.get("main_output_tokens") or 0)
    if not pricing.output_includes_reasoning:
        output_tokens += float(row.get("main_reasoning_output_tokens") or 0)
    uncached_tokens = max(0.0, input_tokens - cached_tokens)
    return (
        uncached_tokens / 1_000_000 * pricing.input_usd_per_mtok
        + cached_tokens / 1_000_000 * pricing.cached_input_usd_per_mtok
        + output_tokens / 1_000_000 * pricing.output_usd_per_mtok
    )


def _guardian_cost(row: Mapping[str, Any], pricing: Pricing) -> float | None:
    if (
        pricing.input_usd_per_mtok is None
        or pricing.output_usd_per_mtok is None
        or row.get("guardian_prompt_tokens") is None
    ):
        return None
    prompt = float(row.get("guardian_prompt_tokens") or 0)
    cached = float(row.get("guardian_cached_input_tokens") or 0)
    completion = float(row.get("guardian_completion_tokens") or 0)
    uncached = max(0.0, prompt - cached)
    cached_rate = (
        pricing.cached_input_usd_per_mtok
        if pricing.cached_input_usd_per_mtok is not None
        else pricing.input_usd_per_mtok
    )
    return (
        uncached / 1_000_000 * pricing.input_usd_per_mtok
        + cached / 1_000_000 * cached_rate
        + completion / 1_000_000 * pricing.output_usd_per_mtok
    )


def add_costs(
    row: Mapping[str, Any],
    *,
    input_usd_per_mtok: float | None = None,
    cached_input_usd_per_mtok: float | None = None,
    output_usd_per_mtok: float | None = None,
    output_includes_reasoning: bool = True,
) -> dict[str, Any]:
    """Return a copy of one trial row with normalized cost fields."""

    result = dict(row)
    pricing = pricing_for_model(
        str(result.get("model") or ""),
        input_usd_per_mtok=input_usd_per_mtok,
        cached_input_usd_per_mtok=cached_input_usd_per_mtok,
        output_usd_per_mtok=output_usd_per_mtok,
        output_includes_reasoning=output_includes_reasoning,
    )
    main_cost = _main_cost(result, pricing)
    guardian_cost = _guardian_cost(result, pricing)
    result["main_cost_usd"] = main_cost
    result["guardian_cost_usd"] = guardian_cost
    result["total_cost_usd"] = (
        (main_cost or 0) + (guardian_cost or 0)
        if main_cost is not None or guardian_cost is not None
        else None
    )
    return result


def metric_mean(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    """Return the arithmetic mean of numeric values for *key*."""

    values = [row.get(key) for row in rows]
    numeric = [value for value in values if isinstance(value, (int, float))]
    return float(statistics.fmean(numeric)) if numeric else None


def metric_sum(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    """Return the sum of numeric values for *key*."""

    values = [row.get(key) for row in rows]
    numeric = [value for value in values if isinstance(value, (int, float))]
    return float(sum(numeric)) if numeric else None
