# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for retrieval ops."""

from __future__ import annotations

from codeminer.ops.retrieve import dedup_queried_nodes, merge_hybrid, queried_node_key
from codeminer.types import QueriedNode


def test_queried_node_key_prefers_node_id():
    node = QueriedNode(
        node_name="symbol",
        file="src/a.py",
        node_id="src/a.py:symbol",
        start_line=1,
        end_line=2,
    )

    assert queried_node_key(node) == ("node_id", "src/a.py:symbol")


def test_dedup_preserves_first_rank_and_fills_missing_content():
    dense = QueriedNode(
        node_name="symbol",
        file="src/a.py",
        node_id="src/a.py:symbol",
        start_line=1,
        end_line=2,
        score=10.0,
    )
    graph_duplicate = dense.model_copy(
        update={"score": 0.5, "content": "def f(): pass"}
    )
    later = QueriedNode(
        node_name="other",
        file="src/b.py",
        node_id="src/b.py:other",
        start_line=3,
        end_line=4,
        score=5.0,
        content="def g(): pass",
    )

    result = dedup_queried_nodes([dense, graph_duplicate, later])

    assert [node.node_id for node in result] == ["src/a.py:symbol", "src/b.py:other"]
    assert result[0].score == 10.0
    assert result[0].content == "def f(): pass"


def test_merge_hybrid_supports_rrf_with_node_id_identity():
    first = [
        QueriedNode(node_name="a", type="function", node_id="a", score=10.0),
        QueriedNode(node_name="c", type="function", node_id="c", score=9.0),
    ]
    second = [
        QueriedNode(node_name="b", type="function", node_id="b", score=1.0),
        QueriedNode(node_name="a", type="function", node_id="a", score=1.0),
    ]

    result = merge_hybrid(
        [first, second],
        weights=[1.0, 1.0],
        top_k=3,
        fusion="rrf",
        rrf_k=60,
    )

    assert [node.node_id for node in result] == ["a", "b", "c"]
    assert result[0].score > result[1].score
