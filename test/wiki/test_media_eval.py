# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

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
