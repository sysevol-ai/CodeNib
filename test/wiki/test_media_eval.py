# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codenib.wiki.media_eval import (
    evaluate_mmwiki_predictions,
    evaluate_visual_code_grounding,
    evaluate_visual_fact_extraction,
)


def _facts():
    return {
        "facts": [
            {
                "artifact_path": "docs/architecture.svg",
                "entities": [
                    {"name": "IndexCompiler", "type": "component"},
                    {"name": "VectorStore", "type": "component"},
                    {"name": "Noise", "type": "component"},
                ],
            }
        ]
    }


def _grounding():
    return {
        "bindings": [
            {
                "artifact_path": "docs/architecture.svg",
                "entity_name": "IndexCompiler",
                "source_path": "codenib/compiler/index_compiler.py",
                "symbol": "IndexCompiler",
                "score": 1.0,
            },
            {
                "artifact_path": "docs/architecture.svg",
                "entity_name": "VectorStore",
                "source_path": "codenib/index/embedding/vector_store.py",
                "symbol": "VectorStore",
                "score": 0.9,
            },
        ]
    }


def _gold():
    return {
        "instances": [
            {
                "artifact_path": "docs/architecture.svg",
                "gold_entities": [
                    {"name": "IndexCompiler", "type": "component"},
                    {"name": "VectorStore", "type": "component"},
                ],
                "gold_bindings": [
                    {
                        "entity_name": "IndexCompiler",
                        "source_path": "codenib/compiler/index_compiler.py",
                        "symbol": "IndexCompiler",
                    },
                    {
                        "entity_name": "VectorStore",
                        "source_path": "codenib/index/embedding/vector_store.py",
                        "symbol": "VectorStore",
                    },
                ],
            }
        ]
    }


def test_evaluate_visual_fact_extraction_reports_precision_recall_f1():
    metrics = evaluate_visual_fact_extraction(_facts(), _gold())

    assert metrics["entity_true_positive"] == 2
    assert metrics["entity_predicted"] == 3
    assert metrics["entity_gold"] == 2
    assert round(metrics["entity_precision"], 3) == 0.667
    assert metrics["entity_recall"] == 1.0
    assert round(metrics["entity_f1"], 3) == 0.8


def test_evaluate_visual_code_grounding_reports_hits_at_k():
    metrics = evaluate_visual_code_grounding(_grounding(), _gold(), k=1)

    assert metrics["binding_count"] == 2
    assert metrics["path_hit_at_k"] == 1.0
    assert metrics["symbol_hit_at_k"] == 1.0
    assert metrics["path_hits"] == 2
    assert metrics["symbol_hits"] == 2


def test_evaluate_mmwiki_predictions_combines_tasks():
    metrics = evaluate_mmwiki_predictions(_facts(), _grounding(), _gold(), k=1)

    assert metrics["task"] == "mmwiki"
    assert metrics["artifact_count"] == 1
    assert metrics["visual_fact_extraction"]["entity_true_positive"] == 2
    assert metrics["visual_code_grounding"]["symbol_hit_at_k"] == 1.0


def test_evaluate_visual_code_grounding_handles_missing_predictions():
    metrics = evaluate_visual_code_grounding({"bindings": []}, _gold(), k=5)

    assert metrics["path_hit_at_k"] == 0.0
    assert metrics["symbol_hit_at_k"] == 0.0
    assert metrics["binding_count"] == 2


@pytest.mark.parametrize("k", [0, -1, 21, True, 1.5, "1"])
def test_evaluate_visual_code_grounding_rejects_invalid_k(k):
    with pytest.raises(ValueError, match="k must be"):
        evaluate_visual_code_grounding(_grounding(), _gold(), k=k)


def test_grounding_evaluation_handles_nonfinite_scores_and_bounds_output():
    grounding = _grounding()
    grounding["bindings"][0]["score"] = float("nan")
    grounding["bindings"][0]["private_prompt"] = "must not leak"

    metrics = evaluate_visual_code_grounding(grounding, _gold(), k=1)

    predicted = metrics["per_binding"][0]["predicted"][0]
    assert predicted["score"] == 0.0
    assert "private_prompt" not in predicted


def test_combined_evaluation_materializes_generator_gold_once():
    gold = {"instances": (instance for instance in _gold()["instances"])}

    metrics = evaluate_mmwiki_predictions(_facts(), _grounding(), gold, k=1)

    assert metrics["artifact_count"] == 1
    assert metrics["visual_fact_extraction"]["entity_gold"] == 2
    assert metrics["visual_code_grounding"]["binding_count"] == 2


def test_evaluation_rejects_unsafe_gold_paths():
    gold = _gold()
    gold["instances"][0]["artifact_path"] = "../secret.svg"

    with pytest.raises(ValueError, match="repository-relative"):
        evaluate_visual_fact_extraction(_facts(), gold)


def test_evaluation_rejects_non_object_inputs():
    with pytest.raises(ValueError, match="visual facts manifest"):
        evaluate_visual_fact_extraction([], _gold())
    with pytest.raises(ValueError, match="gold manifest"):
        evaluate_visual_fact_extraction(_facts(), [])


def test_evaluation_rejects_incomplete_gold_entities_and_bindings():
    entity_gold = _gold()
    entity_gold["instances"][0]["gold_entities"][0]["name"] = ""
    with pytest.raises(ValueError, match="gold entity name"):
        evaluate_visual_fact_extraction(_facts(), entity_gold)

    binding_gold = _gold()
    binding_gold["instances"][0]["gold_bindings"][0]["entity_name"] = ""
    with pytest.raises(ValueError, match="gold entity name"):
        evaluate_visual_code_grounding(_grounding(), binding_gold)
