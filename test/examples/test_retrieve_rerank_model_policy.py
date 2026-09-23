# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sys

import pytest


@pytest.mark.parametrize(
    ("strategy", "expected_model"),
    [
        ("llm", "openai/Qwen/Qwen2.5-Coder-7B"),
        ("decisions", "~typesafe/jev-latest"),
    ],
)
def test_default_model_is_resolved_before_logging_and_evaluation(
    monkeypatch, caplog, strategy, expected_model
):
    from examples import retrieve_rerank

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "retrieve_rerank.py",
            "--dataset",
            "codenib_base",
            "--rerank-strategy",
            strategy,
        ],
    )
    configs = []
    monkeypatch.setattr(retrieve_rerank, "run_pipeline", configs.append)
    monkeypatch.setattr(retrieve_rerank.logger, "propagate", True)
    with caplog.at_level("INFO", logger=retrieve_rerank.logger.name):
        retrieve_rerank.main()

    assert configs[0].rerank_model == expected_model
    assert f"Rerank model: {expected_model}" in caplog.text


def test_retrieval_and_rerank_embedding_trust_flags_are_independent(
    monkeypatch,
):
    from examples import retrieve_rerank

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "retrieve_rerank.py",
            "--dataset",
            "codenib_base",
            "--embedding-trust-remote-code",
        ],
    )

    args = retrieve_rerank.parse_args()

    assert args.embedding_trust_remote_code is True
    assert args.rerank_embedding_trust_remote_code is False


def test_decisions_strategy_and_versioned_model_are_available(monkeypatch):
    from examples import retrieve_rerank

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "retrieve_rerank.py",
            "--dataset",
            "codenib_base",
            "--rerank-strategy",
            "decisions",
            "--rerank-model",
            "typesafe/jev-1.13",
        ],
    )

    args = retrieve_rerank.parse_args()

    assert args.rerank_strategy == "decisions"
    assert args.rerank_model == "typesafe/jev-1.13"
