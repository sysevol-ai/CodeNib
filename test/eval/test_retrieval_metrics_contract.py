# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codenib.eval.retrieval_eval import compute_metrics


def test_duplicate_prediction_cannot_create_extra_hits():
    metrics = compute_metrics(["target", "target"], ["target"])

    assert metrics == {
        "accuracy": 1.0,
        "precision": 0.5,
        "recall": 1.0,
        "hits": 1,
    }


def test_duplicate_targets_require_only_one_matching_prediction():
    metrics = compute_metrics(["target"], ["target", "target"])

    assert metrics == {
        "accuracy": 1.0,
        "precision": 1.0,
        "recall": 1.0,
        "hits": 1,
    }


@pytest.mark.parametrize(
    ("predictions", "targets"),
    [
        ([], []),
        (["miss", "miss"], ["target"]),
        (["a", "a", "b", "b"], ["a", "b", "b"]),
        (["a", "b", "c"], ["a", "a", "d"]),
    ],
)
def test_metrics_remain_bounded_with_duplicate_inputs(predictions, targets):
    metrics = compute_metrics(predictions, targets)

    assert 0.0 <= metrics["accuracy"] <= 1.0
    assert 0.0 <= metrics["precision"] <= 1.0
    assert 0.0 <= metrics["recall"] <= 1.0
    assert 0 <= metrics["hits"] <= len(set(targets))
