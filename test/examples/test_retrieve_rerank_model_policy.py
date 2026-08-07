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
