# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Quality-constrained token and USD cost per successful localization."""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .pricing import PricingSnapshot, load_pricing_snapshot, project_cell_cost

QUALITY_COST_REPORT_SCHEMA = "codenib.quality-cost-report.v1"
DEFAULT_BASELINE = "grep_only"
DEFAULT_QUALITY_SCOPE = "answer_blocks"
DEFAULT_QUALITY_FIELD = "recall"
DEFAULT_QUALITY_K = 5
DEFAULT_SUCCESS_THRESHOLD = 1.0
DEFAULT_NONINFERIORITY_MARGIN = 0.02
DEFAULT_BOOTSTRAP_SAMPLES = 5000

_TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


class QualityCostError(ValueError):
    """Raised when cell identity or report denominators are not auditable."""


def load_quality_cost_cells(
    paths: Sequence[Path],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Recursively load cell JSON while ignoring non-cell result metadata."""

    if not paths:
        raise QualityCostError("at least one cell file or directory is required")
    candidates: dict[Path, set[str]] = defaultdict(set)
    source_roots = []
    root_names = {}
    for root_index, raw_path in enumerate(paths, start=1):
        path = Path(raw_path).expanduser().resolve()
        root_id = f"input-{root_index:02d}"
        root_names[root_id] = path.name
        source_roots.append({"id": root_id, "name": path.name})
        if path.is_file():
            candidates[path].add(root_id)
        elif path.is_dir():
            for candidate in sorted(path.rglob("*.json")):
                if candidate.is_file():
                    candidates[candidate.resolve()].add(root_id)
        else:
            raise QualityCostError(f"cell input does not exist: {path}")

    cells = []
    retry_failures = []
    skipped_non_cells = 0
    for path in sorted(candidates):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise QualityCostError(f"cannot read {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise QualityCostError(f"invalid cell JSON {path}: {exc}") from exc
        if _looks_like_retry_failure(path, value):
            retry = dict(value)
            retry["_quality_cost_source"] = str(path)
            retry["_quality_cost_root"] = _primary_source_root(candidates[path])
            retry_failures.append(retry)
            continue
        if not _looks_like_cell(value):
            skipped_non_cells += 1
            continue
        cell = dict(value)
        cell["_quality_cost_source"] = str(path)
        cell["_quality_cost_root"] = _primary_source_root(candidates[path])
        cells.append(cell)

    normalized = _validate_cells(cells)
    if not normalized:
        raise QualityCostError("no agent-runner cells found under the supplied inputs")
    cells_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cell in normalized:
        if cell.get("cell_id"):
            cells_by_id[str(cell["cell_id"])].append(cell)
    linked_retries = 0
    for retry in retry_failures:
        cell_id = str(retry.get("cell_id") or "")
        candidates_for_retry = cells_by_id.get(cell_id, [])
        same_root = [
            cell
            for cell in candidates_for_retry
            if cell.get("_quality_cost_root") == retry.get("_quality_cost_root")
        ]
        if len(same_root) == 1:
            target = same_root[0]
        elif len(candidates_for_retry) == 1:
            target = candidates_for_retry[0]
        elif not candidates_for_retry:
            raise QualityCostError(
                f"retry failure {retry['_quality_cost_source']} has no final cell "
                f"with id {cell_id!r}"
            )
        else:
            raise QualityCostError(
                f"retry failure {retry['_quality_cost_source']} ambiguously matches "
                f"{len(candidates_for_retry)} final cells with id {cell_id!r}"
            )
        retry["model"] = target["model"]
        target.setdefault("_quality_cost_retry_attempts", []).append(retry)
        linked_retries += 1
    roots = {}
    for root_id, root_name in root_names.items():
        root_cells = [
            cell for cell in normalized if cell.get("_quality_cost_root") == root_id
        ]
        root_retries = [
            retry
            for retry in retry_failures
            if retry.get("_quality_cost_root") == root_id
        ]
        roots[root_id] = {
            "name": root_name,
            "cell_files": len(root_cells),
            "models": {
                model: sum(cell["model"] == model for cell in root_cells)
                for model in sorted({cell["model"] for cell in root_cells})
            },
            "retry_failure_records": len(root_retries),
        }
    return normalized, {
        "source_roots": source_roots,
        "roots": roots,
        "candidate_json_files": len(candidates),
        "cell_files": len(normalized),
        "skipped_non_cell_json": skipped_non_cells,
        "retry_failure_records": len(retry_failures),
        "linked_retry_failures": linked_retries,
        "retry_failures_with_total_tokens": sum(
            retry.get("total_tokens") is not None for retry in retry_failures
        ),
        "retry_failures_with_cost_usd": sum(
            retry.get("cost_usd") is not None for retry in retry_failures
        ),
        "model_count": len({cell["model"] for cell in normalized}),
    }


def analyze_quality_cost(
    cells: Sequence[Mapping[str, Any]],
    *,
    baseline: str = DEFAULT_BASELINE,
    quality_scope: str = DEFAULT_QUALITY_SCOPE,
    quality_field: str = DEFAULT_QUALITY_FIELD,
    quality_k: int = DEFAULT_QUALITY_K,
    success_threshold: float = DEFAULT_SUCCESS_THRESHOLD,
    noninferiority_margin: float = DEFAULT_NONINFERIORITY_MARGIN,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    bootstrap_seed: int = 0,
    strict_denominators: bool = True,
    included_arms: Optional[Sequence[str]] = None,
    pricing_snapshot: Optional[PricingSnapshot] = None,
    usd_source: str = "auto",
    shared_build_cost_usd: Optional[float] = None,
    query_horizons: Sequence[int] = (),
) -> dict[str, Any]:
    """Analyze paired arms without dropping failures or unpriced attempts."""

    _validate_contract(
        baseline=baseline,
        quality_scope=quality_scope,
        quality_field=quality_field,
        quality_k=quality_k,
        success_threshold=success_threshold,
        noninferiority_margin=noninferiority_margin,
        bootstrap_samples=bootstrap_samples,
        usd_source=usd_source,
        shared_build_cost_usd=shared_build_cost_usd,
        query_horizons=query_horizons,
        included_arms=included_arms,
    )
    normalized = _validate_cells(cells)
    input_cells = list(normalized)
    models_in_input = sorted({cell["model"] for cell in input_cells})
    selected_arms = tuple(str(arm).strip() for arm in (included_arms or ()))
    if selected_arms:
        normalized = [cell for cell in normalized if cell["subset_id"] in selected_arms]
        excluded_cells = [
            cell for cell in input_cells if cell["subset_id"] not in selected_arms
        ]
    else:
        excluded_cells = []
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cell in normalized:
        by_model[cell["model"]].append(cell)

    model_reports = {}
    for model in models_in_input:
        model_cells = by_model.get(model, [])
        actual_arms = {cell["subset_id"] for cell in model_cells}
        missing_arms = sorted(set(selected_arms) - actual_arms)
        if missing_arms:
            raise QualityCostError(
                f"model {model!r} is missing requested arms {missing_arms}"
            )
        model_reports[model] = _analyze_model(
            model,
            model_cells,
            baseline=baseline,
            quality_scope=quality_scope,
            quality_field=quality_field,
            quality_k=quality_k,
            success_threshold=success_threshold,
            noninferiority_margin=noninferiority_margin,
            bootstrap_samples=bootstrap_samples,
            bootstrap_seed=bootstrap_seed,
            strict_denominators=strict_denominators,
            pricing_snapshot=pricing_snapshot,
            usd_source=usd_source,
        )

    return {
        "schema": QUALITY_COST_REPORT_SCHEMA,
        "contract": {
            "unit": "query repetition",
            "baseline": baseline,
            "quality_metric": f"{quality_scope}.{quality_field}@{quality_k}",
            "localization_success_threshold": success_threshold,
            "noninferiority_margin": noninferiority_margin,
            "misses_are_charged": True,
            "infrastructure_failures_score_zero_quality": True,
            "strict_denominators": strict_denominators,
            "included_arms": list(selected_arms) if selected_arms else None,
            "usd_source": usd_source,
            "claim_boundary": "successful localization, not issue resolution",
        },
        "bootstrap": {
            "method": "repository-clustered paired percentile bootstrap",
            "confidence": 0.95,
            "samples": bootstrap_samples,
            "seed": bootstrap_seed,
        },
        "pricing_snapshot": (
            pricing_snapshot.public_identity() if pricing_snapshot else None
        ),
        "arm_selection_audit": {
            "input_cell_count": len(input_cells),
            "analyzed_cell_count": len(normalized),
            "excluded_cell_count": len(excluded_cells),
            "excluded_arms": {
                model: {
                    arm: sum(
                        cell["model"] == model and cell["subset_id"] == arm
                        for cell in excluded_cells
                    )
                    for arm in sorted(
                        {
                            cell["subset_id"]
                            for cell in excluded_cells
                            if cell["model"] == model
                        }
                    )
                }
                for model in models_in_input
                if any(cell["model"] == model for cell in excluded_cells)
            },
        },
        "shared_build_cost": _build_cost_report(shared_build_cost_usd, query_horizons),
        "models": model_reports,
    }


def render_quality_cost_markdown(report: Mapping[str, Any]) -> str:
    """Render a concise artifact- and paper-facing report."""

    contract = report["contract"]
    lines = ["# Quality-constrained cost per localization", ""]
    lines.append(
        "The unit is one attempted query repetition. A success reaches "
        f"`{contract['quality_metric']} >= "
        f"{contract['localization_success_threshold']:.3g}`. All paired attempts, "
        "including misses and recorded infrastructure failures, remain in the "
        "token and cost denominator. This is localization, not issue resolution."
    )
    lines.append("")
    lines.append(
        "An arm qualifies when the lower bound of its repository-clustered paired "
        f"quality-delta CI is greater than `-{contract['noninferiority_margin']:.3g}` "
        f"relative to `{contract['baseline']}`."
    )
    lines.append("")

    for model, model_report in report["models"].items():
        audit = model_report["denominator_audit"]
        lines.extend(
            [
                f"## `{model}`",
                "",
                f"Paired units: {audit['common_unit_count']}; queries: "
                f"{audit['query_count']}; repositories: {audit['repository_count']}.",
                "",
                _render_optimum(
                    "Token optimum",
                    model_report["constrained_optima"]["tokens_per_success"],
                    formatter=lambda value: f"{value:,.0f} tokens/success",
                ),
                _render_optimum(
                    "USD optimum",
                    model_report["constrained_optima"]["usd_per_success"],
                    formatter=lambda value: f"${value:.6f}/success",
                ),
                "",
                "| arm | attempts | infra | loc success | quality | delta [95% CI] "
                "| qualified | tokens/attempt | tokens/success | USD source "
                "| USD/attempt | USD/success |",
                "| --- | ---: | ---: | ---: | ---: | ---: | :---: | ---: "
                "| ---: | --- | ---: | ---: |",
            ]
        )
        for arm, arm_report in model_report["arms"].items():
            ci = arm_report["quality_delta"]["ci95"]
            delta = arm_report["quality_delta"]["point"]
            delta_text = (
                f"{delta:+.3f} [{ci['lower']:+.3f}, {ci['upper']:+.3f}]"
                if delta is not None and ci["lower"] is not None
                else "n/a"
            )
            tokens = arm_report["tokens"]
            usd = arm_report["usd"]
            lines.append(
                "| "
                + " | ".join(
                    [
                        arm,
                        str(arm_report["attempted"]),
                        f"{arm_report['infrastructure_success_rate']:.1%}",
                        f"{arm_report['localization_success_rate']:.1%}",
                        f"{arm_report['quality_mean']:.3f}",
                        delta_text,
                        "yes" if arm_report["quality_qualified"] else "no",
                        _format_number(tokens["per_attempt"], 0),
                        _format_number(tokens["per_success"], 0),
                        usd.get("kind") if usd.get("available") else "unavailable",
                        _format_money(usd.get("per_attempt")),
                        _format_money(usd.get("per_success")),
                    ]
                )
                + " |"
            )
        lines.append("")
        unavailable = [
            f"`{arm}`: {values['usd'].get('reason')}"
            for arm, values in model_report["arms"].items()
            if not values["usd"].get("available")
        ]
        if unavailable:
            lines.append("USD unavailable for " + "; ".join(unavailable) + ".")
            lines.append("")

    shared = report.get("shared_build_cost")
    if shared:
        lines.extend(
            [
                "## Shared build-cost amortization",
                "",
                "This cost is reported separately and is not added to model-call cost.",
                "",
                "| query horizon | shared build USD/query |",
                "| ---: | ---: |",
            ]
        )
        for row in shared["horizons"]:
            lines.append(
                f"| {row['queries']} | {_format_money(row['usd_per_query'])} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_optimum(
    label: str,
    optimum: Mapping[str, Any],
    *,
    formatter: Callable[[float], str],
) -> str:
    selected = optimum.get("selected_arm")
    value = optimum.get("value")
    if selected is None or value is None:
        return f"{label}: unavailable ({optimum.get('reason', 'no eligible arm')})."
    comparison = ""
    savings = optimum.get("savings_fraction_vs_baseline")
    if selected == optimum["baseline"]:
        comparison = "; baseline remains the constrained optimum"
    elif savings is not None and savings >= 0:
        comparison = f"; {savings:.1%} less than `{optimum['baseline']}`"
    elif savings is not None:
        comparison = f"; {-savings:.1%} more than `{optimum['baseline']}`"
    return f"{label}: `{selected}` at {formatter(value)}{comparison}."


def write_quality_cost_report(
    *,
    cells: Sequence[Mapping[str, Any]],
    output_dir: Path,
    load_audit: Optional[Mapping[str, Any]] = None,
    **analysis_options: Any,
) -> dict[str, Any]:
    """Write ``quality_cost.json`` and ``quality_cost.md``."""

    report = analyze_quality_cost(cells, **analysis_options)
    report["load_audit"] = dict(load_audit or {})
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "quality_cost.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    (output / "quality_cost.md").write_text(
        render_quality_cost_markdown(report), encoding="utf-8"
    )
    return report


def _analyze_model(
    model: str,
    cells: Sequence[dict[str, Any]],
    *,
    baseline: str,
    quality_scope: str,
    quality_field: str,
    quality_k: int,
    success_threshold: float,
    noninferiority_margin: float,
    bootstrap_samples: int,
    bootstrap_seed: int,
    strict_denominators: bool,
    pricing_snapshot: Optional[PricingSnapshot],
    usd_source: str,
) -> dict[str, Any]:
    by_arm: dict[str, dict[tuple[str, int], dict[str, Any]]] = defaultdict(dict)
    for cell in cells:
        key = _unit_key(cell)
        by_arm[cell["subset_id"]][key] = cell
    if baseline not in by_arm:
        raise QualityCostError(f"model {model!r} has no baseline arm {baseline!r}")
    if len(by_arm) < 2:
        raise QualityCostError(f"model {model!r} needs at least two arms")

    unit_sets = {arm: set(rows) for arm, rows in by_arm.items()}
    union = set().union(*unit_sets.values())
    common = set.intersection(*unit_sets.values())
    mismatched = {
        arm: {
            "missing_count": len(union - keys),
            "extra_vs_baseline_count": len(keys - unit_sets[baseline]),
            "missing_examples": [
                _format_unit_key(key) for key in sorted(union - keys)[:10]
            ],
        }
        for arm, keys in sorted(unit_sets.items())
    }
    if strict_denominators and any(
        keys != unit_sets[baseline] for keys in unit_sets.values()
    ):
        detail = ", ".join(
            f"{arm}={len(keys)}" for arm, keys in sorted(unit_sets.items())
        )
        raise QualityCostError(
            f"model {model!r} has unmatched paired denominators: {detail}; "
            f"baseline={len(unit_sets[baseline])}"
        )
    if not common:
        raise QualityCostError(f"model {model!r} has no complete paired units")

    ordered_keys = sorted(common)
    baseline_cells = [by_arm[baseline][key] for key in ordered_keys]
    baseline_quality = [
        _quality_value(
            cell,
            scope=quality_scope,
            field=quality_field,
            k=quality_k,
        )
        for cell in baseline_cells
    ]
    arm_reports = {}
    for arm in sorted(by_arm, key=lambda name: (name != baseline, name)):
        selected = [by_arm[arm][key] for key in ordered_keys]
        quality = [
            _quality_value(
                cell,
                scope=quality_scope,
                field=quality_field,
                k=quality_k,
            )
            for cell in selected
        ]
        delta_rows = [
            {
                "repository": _repository_key(cell),
                "delta": arm_value - baseline_value,
            }
            for cell, arm_value, baseline_value in zip(
                selected, quality, baseline_quality, strict=True
            )
        ]
        point = statistics.fmean(row["delta"] for row in delta_rows)
        samples = _repository_clustered_bootstrap(
            delta_rows,
            samples=bootstrap_samples,
            seed=bootstrap_seed,
        )
        lower = _percentile(samples, 0.025)
        upper = _percentile(samples, 0.975)
        localization_successes = sum(
            bool(cell["success"] and value >= success_threshold)
            for cell, value in zip(selected, quality, strict=True)
        )
        arm_reports[arm] = {
            "attempted": len(selected),
            "infrastructure_successes": sum(bool(cell["success"]) for cell in selected),
            "infrastructure_success_rate": sum(
                bool(cell["success"]) for cell in selected
            )
            / len(selected),
            "localization_successes": localization_successes,
            "localization_success_rate": localization_successes / len(selected),
            "quality_mean": statistics.fmean(quality),
            "quality_delta": {
                "estimand": f"{arm} - {baseline}",
                "point": point,
                "ci95": {"lower": lower, "upper": upper},
            },
            "quality_qualified": bool(
                arm == baseline
                or (lower is not None and lower > -noninferiority_margin)
            ),
            "tokens": _token_report(selected, localization_successes),
            "usd": _usd_report(
                selected,
                localization_successes,
                pricing_snapshot=pricing_snapshot,
                usd_source=usd_source,
            ),
        }

    constrained_optima = {
        "tokens_per_success": _constrained_optimum(
            arm_reports,
            baseline=baseline,
            report_key="tokens",
            value_key="per_success",
        ),
        "usd_per_success": _constrained_optimum(
            arm_reports,
            baseline=baseline,
            report_key="usd",
            value_key="per_success",
        ),
    }
    return {
        "model": model,
        "baseline": baseline,
        "denominator_audit": {
            "strict": strict_denominators,
            "arm_unit_counts": {
                arm: len(keys) for arm, keys in sorted(unit_sets.items())
            },
            "common_unit_count": len(common),
            "union_unit_count": len(union),
            "query_count": len({key[0] for key in common}),
            "repository_count": len(
                {_repository_key(by_arm[baseline][key]) for key in ordered_keys}
            ),
            "mismatches": mismatched,
        },
        "arms": arm_reports,
        "constrained_optima": constrained_optima,
    }


def _constrained_optimum(
    arms: Mapping[str, Mapping[str, Any]],
    *,
    baseline: str,
    report_key: str,
    value_key: str,
) -> dict[str, Any]:
    """Select the least-cost arm only after the quality guard passes."""

    candidates = []
    for arm, arm_report in arms.items():
        cost_report = arm_report[report_key]
        value = cost_report.get(value_key)
        if arm_report["quality_qualified"] and value is not None:
            candidates.append((float(value), arm))
    candidates.sort(key=lambda row: (row[0], row[1]))

    baseline_value = arms[baseline][report_key].get(value_key)
    if not candidates:
        return {
            "objective": f"minimize {report_key}.{value_key}",
            "baseline": baseline,
            "baseline_value": baseline_value,
            "eligible_arms": [],
            "selected_arm": None,
            "value": None,
            "ratio_to_baseline": None,
            "savings_fraction_vs_baseline": None,
            "reason": "no quality-qualified arm has a complete cost denominator",
        }

    value, selected = candidates[0]
    ratio = None
    if baseline_value is not None and float(baseline_value) > 0:
        ratio = value / float(baseline_value)
    return {
        "objective": f"minimize {report_key}.{value_key}",
        "baseline": baseline,
        "baseline_value": baseline_value,
        "eligible_arms": [arm for _, arm in candidates],
        "selected_arm": selected,
        "value": value,
        "ratio_to_baseline": ratio,
        "savings_fraction_vs_baseline": 1.0 - ratio if ratio is not None else None,
        "reason": None,
    }


def _token_report(
    cells: Sequence[Mapping[str, Any]], localization_successes: int
) -> dict[str, Any]:
    attempts = list(_execution_attempts(cells))
    sums = {}
    coverage = {}
    for field in _TOKEN_FIELDS:
        values = [_optional_nonnegative(cell.get(field), field) for cell in attempts]
        known = [value for value in values if value is not None]
        sums[field] = sum(known)
        coverage[field] = len(known) / len(attempts)
    complete = coverage["total_tokens"] == 1.0
    total = sums["total_tokens"] if complete else None
    missing_retry_tokens = any(
        cell.get("_quality_cost_is_retry") and cell.get("total_tokens") is None
        for cell in attempts
    )
    return {
        "available": complete,
        "reason": (
            None
            if complete
            else (
                "unmetered_retry_attempts"
                if missing_retry_tokens
                else "missing_total_tokens"
            )
        ),
        "field_coverage": coverage,
        "known_sums": sums,
        "query_repetitions": len(cells),
        "execution_attempts": len(attempts),
        "total": total,
        "per_attempt": total / len(cells) if total is not None else None,
        "per_success": (
            total / localization_successes
            if total is not None and localization_successes
            else None
        ),
    }


def _usd_report(
    cells: Sequence[Mapping[str, Any]],
    localization_successes: int,
    *,
    pricing_snapshot: Optional[PricingSnapshot],
    usd_source: str,
) -> dict[str, Any]:
    projected = _projected_usd(cells, pricing_snapshot)
    recorded = _recorded_usd(cells)
    if usd_source == "projected":
        selected = projected
    elif usd_source == "recorded":
        selected = recorded
    else:
        selected = projected if projected.get("available") else recorded
    result = dict(selected)
    result["alternatives"] = {
        "projected": _cost_availability(projected),
        "recorded": _cost_availability(recorded),
    }
    if result.get("available"):
        total = float(result["total"])
        result["per_attempt"] = total / len(cells)
        result["per_success"] = (
            total / localization_successes if localization_successes else None
        )
    else:
        result["per_attempt"] = None
        result["per_success"] = None
    return result


def _projected_usd(
    cells: Sequence[Mapping[str, Any]], snapshot: Optional[PricingSnapshot]
) -> dict[str, Any]:
    if snapshot is None:
        return {
            "available": False,
            "kind": "offline_projection",
            "reason": "no_pricing_snapshot",
        }
    attempts = list(_execution_attempts(cells))
    rows = [project_cell_cost(cell, snapshot) for cell in attempts]
    unavailable = [row for row in rows if not row.get("available")]
    if unavailable:
        reasons = sorted({str(row.get("reason")) for row in unavailable})
        return {
            "available": False,
            "kind": "offline_projection",
            "reason": "+".join(reasons),
            "priced_attempts": len(rows) - len(unavailable),
            "attempted": len(rows),
            "snapshot": snapshot.public_identity(),
        }
    return {
        "available": True,
        "kind": "offline_projection",
        "currency": snapshot.currency,
        "total": sum(float(row["total"]) for row in rows),
        "priced_attempts": len(rows),
        "attempted": len(rows),
        "snapshot": snapshot.public_identity(),
        "pricing_model_id": rows[0]["pricing_model_id"],
        "provider_channel": rows[0]["provider_channel"],
        "source_url": rows[0]["source_url"],
    }


def _recorded_usd(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    attempts = list(_execution_attempts(cells))
    values = [
        _optional_nonnegative(cell.get("cost_usd"), "cost_usd") for cell in attempts
    ]
    known = [value for value in values if value is not None]
    if len(known) != len(attempts):
        missing_retry_cost = any(
            cell.get("_quality_cost_is_retry") and cell.get("cost_usd") is None
            for cell in attempts
        )
        return {
            "available": False,
            "kind": "recorded_runtime_estimate",
            "reason": (
                "unmetered_retry_attempts"
                if missing_retry_cost
                else "missing_recorded_cost"
            ),
            "priced_attempts": len(known),
            "attempted": len(attempts),
        }
    total = sum(known)
    ambiguous_zero = any(
        value == 0 and (cell.get("cost_provenance") or {}).get("kind") != "metered_zero"
        for cell, value in zip(attempts, values, strict=True)
    )
    if ambiguous_zero:
        return {
            "available": False,
            "kind": "recorded_runtime_estimate",
            "reason": "ambiguous_zero_runtime_estimates",
            "priced_attempts": len(known),
            "attempted": len(attempts),
        }
    provenance = []
    for cell in attempts:
        raw = cell.get("cost_provenance")
        if isinstance(raw, Mapping):
            identity = {
                key: raw.get(key)
                for key in ("kind", "calculator", "calculator_version", "model")
                if raw.get(key) is not None
            }
            if identity and identity not in provenance:
                provenance.append(identity)
    return {
        "available": True,
        "kind": "recorded_runtime_estimate",
        "currency": "USD",
        "total": total,
        "priced_attempts": len(known),
        "attempted": len(attempts),
        "provenance": provenance or [{"kind": "legacy_cell_cost_usd"}],
        "reproducibility": "unpinned calculator estimate",
    }


def _cost_availability(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in ("available", "kind", "reason", "priced_attempts", "attempted")
        if key in value
    }


def _repository_clustered_bootstrap(
    rows: Sequence[Mapping[str, Any]], *, samples: int, seed: int
) -> list[float]:
    by_repository: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_repository[str(row["repository"])].append(float(row["delta"]))
    repositories = sorted(by_repository)
    rng = random.Random(seed)
    result = []
    for _ in range(samples):
        values = []
        for repository in rng.choices(repositories, k=len(repositories)):
            values.extend(by_repository[repository])
        result.append(statistics.fmean(values))
    return result


def _percentile(values: Sequence[float], quantile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _quality_value(cell: Mapping[str, Any], *, scope: str, field: str, k: int) -> float:
    if not cell["success"]:
        return 0.0
    metrics = cell.get("metrics")
    if not isinstance(metrics, Mapping):
        raise QualityCostError(f"successful cell {_cell_label(cell)} has no metrics")
    scoped = metrics.get(scope)
    if not isinstance(scoped, Mapping):
        raise QualityCostError(
            f"successful cell {_cell_label(cell)} lacks metric scope {scope!r}"
        )
    cutoff = scoped.get(k) or scoped.get(str(k))
    if not isinstance(cutoff, Mapping):
        raise QualityCostError(f"successful cell {_cell_label(cell)} lacks {scope}@{k}")
    value = cutoff.get(field)
    numeric = _optional_nonnegative(value, f"{scope}.{field}@{k}")
    if numeric is None or numeric > 1:
        raise QualityCostError(
            f"successful cell {_cell_label(cell)} has invalid {scope}.{field}@{k}"
        )
    return numeric


def _validate_cells(
    cells: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalized = []
    identities: dict[tuple[str, str, str, int], str] = {}
    query_identity: dict[tuple[str, str, int], tuple[str, Optional[str]]] = {}
    for index, raw in enumerate(cells):
        if not isinstance(raw, Mapping):
            raise QualityCostError(f"cell {index} must be an object")
        cell = dict(raw)
        model = _required_cell_string(cell, "model", index)
        query_id = _required_cell_string(cell, "query_id", index)
        instance_id = _required_cell_string(cell, "instance_id", index)
        arm = _required_cell_string(cell, "subset_id", index)
        rep = cell.get("rep")
        if isinstance(rep, bool) or not isinstance(rep, int) or rep < 1:
            raise QualityCostError(f"cell {index} rep must be a positive integer")
        if not isinstance(cell.get("success"), bool):
            raise QualityCostError(f"cell {index} success must be boolean")
        if cell.get("metrics_meaningful") is False:
            raise QualityCostError(
                f"cell {index} has no meaningful ground truth for quality reporting"
            )
        cell.update(
            model=model,
            query_id=query_id,
            instance_id=instance_id,
            subset_id=arm,
            rep=rep,
        )
        identity = (model, query_id, arm, rep)
        source = str(cell.get("_quality_cost_source") or cell.get("cell_id") or index)
        if identity in identities:
            raise QualityCostError(
                f"duplicate cell {identity!r}: {identities[identity]} and {source}"
            )
        identities[identity] = source

        query_key = (model, query_id, rep)
        query_text = str(cell.get("query")) if cell.get("query") is not None else None
        expected = query_identity.setdefault(query_key, (instance_id, query_text))
        if expected[0] != instance_id or (
            expected[1] is not None
            and query_text is not None
            and expected[1] != query_text
        ):
            raise QualityCostError(
                f"query identity mismatch for {query_key!r}: "
                f"expected {expected!r}, found {(instance_id, query_text)!r}"
            )
        normalized.append(cell)
    return normalized


def _validate_contract(
    *,
    baseline: str,
    quality_scope: str,
    quality_field: str,
    quality_k: int,
    success_threshold: float,
    noninferiority_margin: float,
    bootstrap_samples: int,
    usd_source: str,
    shared_build_cost_usd: Optional[float],
    query_horizons: Sequence[int],
    included_arms: Optional[Sequence[str]],
) -> None:
    if not baseline.strip() or not quality_scope.strip() or not quality_field.strip():
        raise QualityCostError("baseline and quality metric names must be non-empty")
    if quality_k < 1:
        raise QualityCostError("quality_k must be positive")
    if not 0 <= success_threshold <= 1:
        raise QualityCostError("success_threshold must be in [0, 1]")
    if not 0 <= noninferiority_margin < 1:
        raise QualityCostError("noninferiority_margin must be in [0, 1)")
    if bootstrap_samples < 1:
        raise QualityCostError("bootstrap_samples must be positive")
    if usd_source not in {"auto", "recorded", "projected"}:
        raise QualityCostError("usd_source must be auto, recorded, or projected")
    if shared_build_cost_usd is not None:
        _nonnegative(shared_build_cost_usd, "shared_build_cost_usd")
    for horizon in query_horizons:
        if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
            raise QualityCostError("query horizons must be positive integers")
    if query_horizons and shared_build_cost_usd is None:
        raise QualityCostError("query horizons require shared_build_cost_usd")
    if included_arms is not None:
        normalized_arms = [str(arm).strip() for arm in included_arms]
        if any(not arm for arm in normalized_arms):
            raise QualityCostError("included arms must be non-empty strings")
        if len(set(normalized_arms)) != len(normalized_arms):
            raise QualityCostError("included arms must be unique")
        if baseline not in normalized_arms:
            raise QualityCostError("included arms must contain the baseline")
        if len(normalized_arms) < 2:
            raise QualityCostError("at least two included arms are required")


def _build_cost_report(
    cost: Optional[float], horizons: Sequence[int]
) -> Optional[dict[str, Any]]:
    if cost is None:
        return None
    return {
        "currency": "USD",
        "total": float(cost),
        "included_in_model_call_cost": False,
        "horizons": [
            {"queries": horizon, "usd_per_query": float(cost) / horizon}
            for horizon in sorted(set(horizons))
        ],
    }


def _execution_attempts(
    cells: Sequence[Mapping[str, Any]],
) -> Iterable[Mapping[str, Any]]:
    for cell in cells:
        yield cell
        retries = cell.get("_quality_cost_retry_attempts") or []
        if not isinstance(retries, Sequence) or isinstance(retries, (str, bytes)):
            raise QualityCostError(
                f"cell {_cell_label(cell)} retry attempts must be an array"
            )
        for retry in retries:
            if not isinstance(retry, Mapping):
                raise QualityCostError(
                    f"cell {_cell_label(cell)} contains a malformed retry attempt"
                )
            normalized = dict(retry)
            normalized["model"] = cell["model"]
            normalized["_quality_cost_is_retry"] = True
            yield normalized


def _looks_like_cell(value: Any) -> bool:
    required = {
        "model",
        "instance_id",
        "query_id",
        "subset_id",
        "rep",
        "success",
    }
    return isinstance(value, Mapping) and required <= set(value)


def _looks_like_retry_failure(path: Path, value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and path.name == "failed_cell.json"
        and "failure_retries" in path.parts
        and isinstance(value.get("cell_id"), str)
        and bool(value.get("cell_id"))
    )


def _primary_source_root(roots: Iterable[str]) -> str:
    selected = sorted(set(roots), key=lambda root: (len(Path(root).parts), root))
    if not selected:
        raise QualityCostError("internal error: candidate JSON has no source root")
    return selected[-1]


def _required_cell_string(cell: Mapping[str, Any], key: str, index: int) -> str:
    value = cell.get(key)
    if not isinstance(value, str) or not value.strip():
        source = cell.get("_quality_cost_source") or index
        raise QualityCostError(f"cell {source} {key} must be a non-empty string")
    return value.strip()


def _unit_key(cell: Mapping[str, Any]) -> tuple[str, int]:
    return str(cell["query_id"]), int(cell["rep"])


def _format_unit_key(key: tuple[str, int]) -> str:
    return f"{key[0]}#rep{key[1]}"


def _repository_key(cell: Mapping[str, Any]) -> str:
    explicit = cell.get("repository") or cell.get("repo")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    instance_id = str(cell["instance_id"])
    head, separator, tail = instance_id.rpartition("-")
    return head if separator and tail.isdigit() else instance_id


def _cell_label(cell: Mapping[str, Any]) -> str:
    return str(cell.get("cell_id") or _format_unit_key(_unit_key(cell)))


def _optional_nonnegative(value: Any, field: str) -> Optional[float]:
    if value is None:
        return None
    return _nonnegative(value, field)


def _nonnegative(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QualityCostError(f"{field} must be a non-negative number")
    number = float(value)
    if number < 0 or number != number or number in {float("inf"), float("-inf")}:
        raise QualityCostError(f"{field} must be a finite non-negative number")
    return number


def _format_number(value: Optional[float], digits: int) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{digits}f}"


def _format_money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"${value:.6f}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codenib-quality-cost-report",
        description="Report quality-constrained cost per successful localization.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--baseline", default=DEFAULT_BASELINE)
    parser.add_argument("--quality-scope", default=DEFAULT_QUALITY_SCOPE)
    parser.add_argument("--quality-field", default=DEFAULT_QUALITY_FIELD)
    parser.add_argument("--quality-k", type=int, default=DEFAULT_QUALITY_K)
    parser.add_argument(
        "--success-threshold", type=float, default=DEFAULT_SUCCESS_THRESHOLD
    )
    parser.add_argument(
        "--noninferiority-margin",
        type=float,
        default=DEFAULT_NONINFERIORITY_MARGIN,
    )
    parser.add_argument(
        "--bootstrap-samples", type=int, default=DEFAULT_BOOTSTRAP_SAMPLES
    )
    parser.add_argument("--bootstrap-seed", type=int, default=0)
    parser.add_argument("--allow-unmatched", action="store_true")
    parser.add_argument(
        "--arm",
        dest="included_arms",
        action="append",
        help="Include only this arm; repeat for each compared arm.",
    )
    parser.add_argument("--pricing-snapshot", type=Path)
    parser.add_argument(
        "--usd-source", choices=("auto", "recorded", "projected"), default="auto"
    )
    parser.add_argument("--shared-build-cost-usd", type=float)
    parser.add_argument("--query-horizon", type=int, action="append", default=[])
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""

    args = _build_parser().parse_args(argv)
    try:
        cells, load_audit = load_quality_cost_cells(args.inputs)
        snapshot = (
            load_pricing_snapshot(args.pricing_snapshot)
            if args.pricing_snapshot
            else None
        )
        report = write_quality_cost_report(
            cells=cells,
            output_dir=args.output_dir,
            load_audit=load_audit,
            baseline=args.baseline,
            quality_scope=args.quality_scope,
            quality_field=args.quality_field,
            quality_k=args.quality_k,
            success_threshold=args.success_threshold,
            noninferiority_margin=args.noninferiority_margin,
            bootstrap_samples=args.bootstrap_samples,
            bootstrap_seed=args.bootstrap_seed,
            strict_denominators=not args.allow_unmatched,
            included_arms=args.included_arms,
            pricing_snapshot=snapshot,
            usd_source=args.usd_source,
            shared_build_cost_usd=args.shared_build_cost_usd,
            query_horizons=args.query_horizon,
        )
    except (QualityCostError, ValueError) as exc:
        print(f"codenib-quality-cost-report: {exc}", file=sys.stderr)
        return 2
    print(render_quality_cost_markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_BASELINE",
    "DEFAULT_BOOTSTRAP_SAMPLES",
    "DEFAULT_NONINFERIORITY_MARGIN",
    "DEFAULT_QUALITY_FIELD",
    "DEFAULT_QUALITY_K",
    "DEFAULT_QUALITY_SCOPE",
    "DEFAULT_SUCCESS_THRESHOLD",
    "QUALITY_COST_REPORT_SCHEMA",
    "QualityCostError",
    "analyze_quality_cost",
    "load_quality_cost_cells",
    "main",
    "render_quality_cost_markdown",
    "write_quality_cost_report",
]
