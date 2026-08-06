# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for retrieval ops."""

from __future__ import annotations

from dataclasses import dataclass

from codenib.ops.retrieve import (
    dedup_queried_nodes,
    execute_retrieval_stages,
    merge_file_rankings,
    merge_hybrid,
    queried_file_key,
    queried_node_key,
)
from codenib.types import QueriedNode


@dataclass(frozen=True)
class _Stage:
    engine: str
    weight: float = 1.0
    top_k: int | None = None


def test_execute_retrieval_stages_runs_and_fuses_declared_branches():
    stages = [_Stage("dense", 0.6, 3), _Stage("sparse", 0.4, 2)]
    calls = []

    def execute(query, stage):
        calls.append((query, stage.engine, stage.top_k))
        if stage.engine == "dense":
            return [
                QueriedNode(node_name="shared", node_id="shared", score=0.9),
                QueriedNode(node_name="dense", node_id="dense", score=0.8),
            ]
        return [
            QueriedNode(node_name="shared", node_id="shared", score=10.0),
            QueriedNode(node_name="sparse", node_id="sparse", score=9.0),
        ]

    result = execute_retrieval_stages(
        "retry behavior",
        stages,
        execute,
        top_k=3,
        fusion="rrf",
    )

    assert calls == [
        ("retry behavior", "dense", 3),
        ("retry behavior", "sparse", 2),
    ]
    assert [node.node_id for node in result] == ["shared", "dense", "sparse"]


def test_execute_retrieval_stages_rejects_empty_plan():
    try:
        execute_retrieval_stages("query", [], lambda *_: [], top_k=10)
    except ValueError as exc:
        assert "At least one retrieval stage" in str(exc)
    else:
        raise AssertionError("empty retrieval plan should fail")


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


def test_file_rrf_matches_relative_node_ids_to_absolute_artifact_paths():
    semantic = [
        QueriedNode(
            node_name="a1",
            file="/old/cache/repo/src/a.py",
            node_id="src/a.py:a1",
        ),
        QueriedNode(
            node_name="a2",
            file="/old/cache/repo/src/a.py",
            node_id="src/a.py:a2",
        ),
        QueriedNode(
            node_name="b",
            file="/old/cache/repo/src/b.py",
            node_id="src/b.py:b",
        ),
    ]
    graph = [
        QueriedNode(node_name="c", file="src/c.py", node_id="src/c.py:c"),
        QueriedNode(node_name="a", file="src/a.py", node_id="src/a.py:a"),
    ]

    result = merge_file_rankings(
        [semantic, graph], weights=[1.0, 1.0], top_k=3, rrf_k=60
    )

    assert queried_file_key(semantic[0]) == ("file", "src/a.py")
    assert [queried_file_key(node) for node in result] == [
        ("file", "src/a.py"),
        ("file", "src/c.py"),
        ("file", "src/b.py"),
    ]


def test_file_key_does_not_treat_a_symbol_prefix_as_a_file():
    node = QueriedNode(node_name="method", node_id="Type:method")

    assert queried_file_key(node)[0] == "node"
