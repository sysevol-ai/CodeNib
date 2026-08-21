# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Evaluation helpers for multimodal repository knowledge views."""

from __future__ import annotations

import re
from typing import Any, Mapping


def evaluate_visual_fact_extraction(
    visual_facts_manifest: Mapping[str, Any],
    gold: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate extracted visual entities against an MMWiki-style gold file."""

    predicted_by_artifact = {
        str(fact.get("artifact_path") or ""): fact
        for fact in visual_facts_manifest.get("facts") or ()
        if isinstance(fact, Mapping)
    }
    true_positive = 0
    predicted_total = 0
    gold_total = 0
    per_artifact = []
    for instance in _gold_instances(gold):
        artifact_path = str(instance.get("artifact_path") or "")
        predicted = {
            _entity_key(entity)
            for entity in (predicted_by_artifact.get(artifact_path) or {}).get(
                "entities"
            )
            or ()
            if isinstance(entity, Mapping) and _entity_key(entity)
        }
        expected = {
            _entity_key(entity)
            for entity in instance.get("gold_entities") or ()
            if isinstance(entity, Mapping) and _entity_key(entity)
        }
        hits = predicted & expected
        true_positive += len(hits)
        predicted_total += len(predicted)
        gold_total += len(expected)
        per_artifact.append(
            {
                "artifact_path": artifact_path,
                "entity_precision": _safe_div(len(hits), len(predicted)),
                "entity_recall": _safe_div(len(hits), len(expected)),
                "matched_entities": sorted(hits),
            }
        )
    precision = _safe_div(true_positive, predicted_total)
    recall = _safe_div(true_positive, gold_total)
    return {
        "entity_precision": precision,
        "entity_recall": recall,
        "entity_f1": _f1(precision, recall),
        "entity_true_positive": true_positive,
        "entity_predicted": predicted_total,
        "entity_gold": gold_total,
        "per_artifact": per_artifact,
    }


def evaluate_visual_code_grounding(
    grounding_manifest: Mapping[str, Any],
    gold: Mapping[str, Any],
    *,
    k: int = 5,
) -> dict[str, Any]:
    """Evaluate visual entity to source binding accuracy."""

    predicted_by_key: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for binding in grounding_manifest.get("bindings") or ():
        if not isinstance(binding, Mapping):
            continue
        key = (
            str(binding.get("artifact_path") or ""),
            _normalize(str(binding.get("entity_name") or "")),
        )
        predicted_by_key.setdefault(key, []).append(binding)
    for values in predicted_by_key.values():
        values.sort(
            key=lambda item: (
                -float(item.get("score") or 0.0),
                str(item.get("source_path") or ""),
                str(item.get("symbol") or ""),
            )
        )

    total = 0
    path_hits = 0
    symbol_hits = 0
    per_binding = []
    for instance in _gold_instances(gold):
        artifact_path = str(instance.get("artifact_path") or "")
        for expected in instance.get("gold_bindings") or ():
            if not isinstance(expected, Mapping):
                continue
            total += 1
            entity_name = _normalize(str(expected.get("entity_name") or ""))
            predictions = predicted_by_key.get((artifact_path, entity_name), [])[
                : max(0, k)
            ]
            expected_path = str(expected.get("source_path") or "")
            expected_symbol = str(expected.get("symbol") or "")
            path_hit = any(
                prediction.get("source_path") == expected_path
                for prediction in predictions
            )
            symbol_hit = any(
                prediction.get("source_path") == expected_path
                and (not expected_symbol or prediction.get("symbol") == expected_symbol)
                for prediction in predictions
            )
            path_hits += int(path_hit)
            symbol_hits += int(symbol_hit)
            per_binding.append(
                {
                    "artifact_path": artifact_path,
                    "entity_name": expected.get("entity_name") or "",
                    "path_hit_at_k": path_hit,
                    "symbol_hit_at_k": symbol_hit,
                    "predicted": [dict(prediction) for prediction in predictions],
                }
            )
    return {
        "k": k,
        "binding_count": total,
        "path_hit_at_k": _safe_div(path_hits, total),
        "symbol_hit_at_k": _safe_div(symbol_hits, total),
        "path_hits": path_hits,
        "symbol_hits": symbol_hits,
        "per_binding": per_binding,
    }


def evaluate_mmwiki_predictions(
    visual_facts_manifest: Mapping[str, Any],
    grounding_manifest: Mapping[str, Any],
    gold: Mapping[str, Any],
    *,
    k: int = 5,
) -> dict[str, Any]:
    """Evaluate visual fact extraction and visual-code grounding together."""

    facts = evaluate_visual_fact_extraction(visual_facts_manifest, gold)
    grounding = evaluate_visual_code_grounding(grounding_manifest, gold, k=k)
    return {
        "task": "mmwiki",
        "artifact_count": len(list(_gold_instances(gold))),
        "visual_fact_extraction": facts,
        "visual_code_grounding": grounding,
    }


def _gold_instances(gold: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    instances = gold.get("instances") or ()
    return [instance for instance in instances if isinstance(instance, Mapping)]


def _entity_key(entity: Mapping[str, Any]) -> str:
    name = _normalize(str(entity.get("name") or ""))
    kind = _normalize(str(entity.get("type") or "unknown"))
    return f"{name}:{kind}" if name else ""


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _safe_div(numerator: int | float, denominator: int | float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


__all__ = [
    "evaluate_mmwiki_predictions",
    "evaluate_visual_code_grounding",
    "evaluate_visual_fact_extraction",
]
