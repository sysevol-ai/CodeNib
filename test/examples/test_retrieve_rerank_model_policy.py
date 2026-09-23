# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sys


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
