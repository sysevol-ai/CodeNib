# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Evaluation helpers for multimodal repository knowledge views."""

from __future__ import annotations

import math
import re
from itertools import islice
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

_MAX_INSTANCES = 4096
_MAX_ENTITIES_PER_ARTIFACT = 32
_MAX_GOLD_BINDINGS_PER_ARTIFACT = 32
_MAX_PREDICTED_BINDINGS = 20480
_MAX_K = 20
_MAX_TEXT_BYTES = 4096


def evaluate_visual_fact_extraction(
    visual_facts_manifest: Mapping[str, Any],
    gold: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate extracted visual entities against an MMWiki-style gold file."""

    visual_facts_manifest = _require_mapping(
        visual_facts_manifest,
        label="visual facts manifest",
    )
    predicted_by_artifact: dict[str, Mapping[str, Any]] = {}
    for fact in _mapping_items(
        visual_facts_manifest.get("facts"),
        limit=_MAX_INSTANCES,
    ):
        path = _safe_relative_path(fact.get("artifact_path"))
        if path:
            predicted_by_artifact.setdefault(path, fact)

    true_positive = 0
    predicted_total = 0
    gold_total = 0
    per_artifact = []
    for instance in _gold_instances(gold):
        artifact_path = _gold_artifact_path(instance)
        predicted = {
            key
            for entity in _mapping_items(
                (predicted_by_artifact.get(artifact_path) or {}).get("entities"),
                limit=_MAX_ENTITIES_PER_ARTIFACT,
            )
            if (key := _entity_key(entity))
        }
        expected = {
            _gold_entity_key(entity)
            for entity in _mapping_items(
                instance.get("gold_entities"),
                limit=_MAX_ENTITIES_PER_ARTIFACT,
            )
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

    grounding_manifest = _require_mapping(
        grounding_manifest,
        label="grounding manifest",
    )
    result_limit = _validated_k(k)
    predicted_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for binding in _mapping_items(
        grounding_manifest.get("bindings"),
        limit=_MAX_PREDICTED_BINDINGS,
    ):
        prediction = _prediction(binding)
        if prediction is None:
            continue
        key = (
            prediction["artifact_path"],
            _normalize(prediction["entity_name"]),
        )
        if not key[1]:
            continue
        predicted_by_key.setdefault(key, []).append(prediction)
    for values in predicted_by_key.values():
        values.sort(
            key=lambda item: (
                -item["score"],
                item["source_path"],
                item["symbol"],
            )
        )

    total = 0
    path_hits = 0
    symbol_hits = 0
    per_binding = []
    for instance in _gold_instances(gold):
        artifact_path = _gold_artifact_path(instance)
        for expected in _mapping_items(
            instance.get("gold_bindings"),
            limit=_MAX_GOLD_BINDINGS_PER_ARTIFACT,
        ):
            total += 1
            expected_entity = _required_text(
                expected.get("entity_name"),
                label="gold entity name",
            )
            entity_name = _normalize(expected_entity)
            expected_path = _required_relative_path(
                expected.get("source_path"),
                label="gold source path",
            )
            expected_symbol = _safe_text(expected.get("symbol"))
            predictions = predicted_by_key.get(
                (artifact_path, entity_name),
                [],
            )[:result_limit]
            path_hit = any(
                prediction["source_path"] == expected_path for prediction in predictions
            )
            symbol_hit = any(
                prediction["source_path"] == expected_path
                and (not expected_symbol or prediction["symbol"] == expected_symbol)
                for prediction in predictions
            )
            path_hits += int(path_hit)
            symbol_hits += int(symbol_hit)
            per_binding.append(
                {
                    "artifact_path": artifact_path,
                    "entity_name": expected_entity,
                    "path_hit_at_k": path_hit,
                    "symbol_hit_at_k": symbol_hit,
                    "predicted": predictions,
                }
            )
    return {
        "k": result_limit,
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

    instances = _gold_instances(gold)
    stable_gold = {"instances": instances}
    facts = evaluate_visual_fact_extraction(visual_facts_manifest, stable_gold)
    grounding = evaluate_visual_code_grounding(
        grounding_manifest,
        stable_gold,
        k=k,
    )
    return {
        "task": "mmwiki",
        "artifact_count": len(instances),
        "visual_fact_extraction": facts,
        "visual_code_grounding": grounding,
    }


def _gold_instances(gold: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    gold = _require_mapping(gold, label="gold manifest")
    return list(_mapping_items(gold.get("instances"), limit=_MAX_INSTANCES))


def _gold_artifact_path(instance: Mapping[str, Any]) -> str:
    return _required_relative_path(
        instance.get("artifact_path"),
        label="gold artifact path",
    )


def _prediction(binding: Mapping[str, Any]) -> dict[str, Any] | None:
    artifact_path = _safe_relative_path(binding.get("artifact_path"))
    source_path = _safe_relative_path(binding.get("source_path"))
    entity_name = _safe_text(binding.get("entity_name"))
    if not artifact_path or not source_path or not entity_name:
        return None
    return {
        "artifact_path": artifact_path,
        "entity_name": entity_name,
        "source_path": source_path,
        "symbol": _safe_text(binding.get("symbol")),
        "kind": _safe_text(binding.get("kind") or "source"),
        "line": _positive_int(binding.get("line")),
        "score": _finite_score(binding.get("score")),
        "evidence": _safe_text(binding.get("evidence")),
    }


def _entity_key(entity: Mapping[str, Any]) -> str:
    name = _normalize(_safe_text(entity.get("name")))
    kind = _normalize(_safe_text(entity.get("type") or "unknown"))
    return f"{name}:{kind}" if name else ""


def _gold_entity_key(entity: Mapping[str, Any]) -> str:
    name = _normalize(
        _required_text(
            entity.get("name"),
            label="gold entity name",
        )
    )
    kind = _normalize(_safe_text(entity.get("type") or "unknown"))
    return f"{name}:{kind}"


def _mapping_items(value: Any, *, limit: int) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        return ()
    try:
        values = iter(value or ())
    except TypeError:
        return ()
    return (item for item in islice(values, limit) if isinstance(item, Mapping))


def _require_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _required_relative_path(value: Any, *, label: str) -> str:
    path = _safe_relative_path(value)
    if not path:
        raise ValueError(f"{label} must be a repository-relative path")
    return path


def _required_text(value: Any, *, label: str) -> str:
    text = _safe_text(value)
    if not text:
        raise ValueError(f"{label} is required")
    return text


def _safe_relative_path(value: Any) -> str:
    text = _safe_text(value)
    if not text or "\\" in text:
        return ""
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    return path.as_posix()


def _safe_text(value: Any) -> str:
    text = str(value or "").strip()
    text = "".join(
        character
        for character in text
        if ord(character) >= 0x20 and ord(character) != 0x7F
    )
    raw = text.encode("utf-8")
    if len(raw) <= _MAX_TEXT_BYTES:
        return text
    return raw[:_MAX_TEXT_BYTES].decode("utf-8", errors="ignore").rstrip()


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _finite_score(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return score if math.isfinite(score) and score > 0 else 0.0


def _positive_int(value: Any) -> int:
    if type(value) is not int:
        return 0
    return max(0, value)


def _validated_k(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_K:
        raise ValueError(f"k must be an integer between 1 and {_MAX_K}")
    return value


def _safe_div(numerator: int | float, denominator: int | float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


__all__ = [
    "evaluate_mmwiki_predictions",
    "evaluate_visual_code_grounding",
    "evaluate_visual_fact_extraction",
]
