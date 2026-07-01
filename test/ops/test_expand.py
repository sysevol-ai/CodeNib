# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for graph expansion ops (codeminer.ops.expand)."""

from __future__ import annotations

import pytest

from codeminer.ops.expand import (
    ExpandContext,
    expand_graph_neighbors,
    nodeinfo_to_queried,
)
from codeminer.types import NodeInfo, QueriedNode

# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestNodeInfoToQueried:
    def test_preserves_ppr_score(self):
        infos = [NodeInfo(node_name="a", type="function", score=0.42)]
        result = nodeinfo_to_queried(infos)
        assert len(result) == 1
        assert isinstance(result[0], QueriedNode)
        assert result[0].score == 0.42

    def test_assigns_rank_score_when_missing(self):
        infos = [
            NodeInfo(node_name="a", type="function"),
            NodeInfo(node_name="b", type="function"),
        ]
        result = nodeinfo_to_queried(infos)
        assert result[0].score == 1.0  # 1/(0+1)
        assert result[1].score == 0.5  # 1/(1+1)

    def test_empty_list(self):
        assert nodeinfo_to_queried([]) == []

    def test_preserves_fields(self):
        info = NodeInfo(
            node_name="mod.func",
            type="function",
            file="src/mod.py",
            node_id="src/mod.py:mod.func",
            start_line=10,
            end_line=20,
            content="def func(): pass",
        )
        result = nodeinfo_to_queried([info])
        node = result[0]
        assert node.node_name == "mod.func"
        assert node.node_id == "src/mod.py:mod.func"
        assert node.type == "function"
        assert node.file == "src/mod.py"
        assert node.start_line == 10
        assert node.end_line == 20
        assert node.content == "def func(): pass"


class TestExpandContext:
    def test_defaults(self):
        ctx = ExpandContext()
        assert ctx.code_graph is None
        assert ctx.default_top_k == 50
        assert ctx.default_hops == 2
        assert ctx.default_direction == "both"
        assert ctx.default_method == "bfs"
        assert ctx.default_damping == 0.85
        assert ctx.filter_tests is True


class _FakeGraph:
    def __init__(self, *, resolve=None, successors=None, predecessors=None, info=None):
        self.resolve = resolve or {}
        self.successors = successors or {}
        self.predecessors = predecessors or {}
        self.info = info or {}

    def resolve_symbol(self, symbol):
        return self.resolve.get(symbol), None

    def get_successors(self, node_name):
        return self.successors.get(node_name, [])

    def get_predecessors(self, node_name):
        return self.predecessors.get(node_name, [])

    def get_node_info_by_id(self, node_id):
        return self.info.get(node_id)


class TestExpandGraphNeighbors:
    def test_appends_one_hop_symbol_neighbors(self):
        graph = _FakeGraph(
            resolve={"src/a.py:seed": "seed"},
            successors={"seed": [1]},
            info={
                1: {
                    "name": "neighbor",
                    "unified_name": "src/a.py:neighbor",
                    "type": "function",
                    "file": "src/a.py",
                    "start_line": 2,
                    "end_line": 4,
                }
            },
        )
        seed = QueriedNode(
            node_name="seed",
            type="function",
            file="src/a.py",
            node_id="src/a.py:seed",
            score=0.8,
        )

        result = expand_graph_neighbors(
            ExpandContext(code_graph=graph),
            [seed],
            per_seed=3,
            direction="successors",
        )

        assert len(result) == 1
        assert result[0].node_name == "src/a.py:neighbor"
        assert result[0].node_id == "src/a.py:neighbor"
        assert result[0].score == 0.4

    def test_filters_non_symbol_and_missing_spans_and_caps_per_seed(self):
        graph = _FakeGraph(
            resolve={"seed": "seed"},
            successors={"seed": [1, 2, 3]},
            info={
                1: {
                    "name": "module",
                    "type": "file",
                    "file": "src/a.py",
                    "start_line": 0,
                    "end_line": 1,
                },
                2: {
                    "name": "no_span",
                    "type": "function",
                    "file": "src/a.py",
                    "start_line": None,
                    "end_line": None,
                },
                3: {
                    "name": "kept",
                    "type": "method",
                    "file": "src/a.py",
                    "start_line": 3,
                    "end_line": 5,
                },
            },
        )
        seed = QueriedNode(node_name="seed", type="function", score=1.0)

        result = expand_graph_neighbors(
            ExpandContext(code_graph=graph),
            [seed],
            per_seed=1,
            direction="successors",
        )

        assert [node.node_name for node in result] == ["kept"]

    def test_reads_neighbor_content_from_repo_span(self, tmp_path):
        source = tmp_path / "src" / "a.py"
        source.parent.mkdir()
        source.write_text("l0\nl1\nl2\nl3\n", encoding="utf-8")
        graph = _FakeGraph(
            resolve={"seed": "seed"},
            successors={"seed": [1]},
            info={
                1: {
                    "name": "kept",
                    "type": "function",
                    "file": "src/a.py",
                    "start_line": 1,
                    "end_line": 2,
                }
            },
        )
        seed = QueriedNode(node_name="seed", type="function", score=1.0)

        result = expand_graph_neighbors(
            ExpandContext(code_graph=graph),
            [seed],
            repo_path=str(tmp_path),
            include_content=True,
            direction="successors",
        )

        assert result[0].content == "l1\nl2\n"

    def test_prefers_seed_node_id_over_bare_node_name(self):
        graph = _FakeGraph(
            resolve={"seed": "wrong", "src/a.py:seed": "right"},
            successors={"wrong": [1], "right": [2]},
            info={
                1: {
                    "name": "wrong_neighbor",
                    "type": "function",
                    "file": "src/wrong.py",
                    "start_line": 0,
                    "end_line": 1,
                },
                2: {
                    "name": "right_neighbor",
                    "type": "function",
                    "file": "src/a.py",
                    "start_line": 2,
                    "end_line": 3,
                },
            },
        )
        seed = QueriedNode(
            node_name="seed",
            node_id="src/a.py:seed",
            type="function",
            score=1.0,
        )

        result = expand_graph_neighbors(
            ExpandContext(code_graph=graph),
            [seed],
            direction="successors",
        )

        assert [node.node_name for node in result] == ["right_neighbor"]

    def test_rejects_unknown_direction(self):
        seed = QueriedNode(node_name="seed", type="function")

        with pytest.raises(ValueError, match="Unsupported graph expansion direction"):
            expand_graph_neighbors(
                ExpandContext(code_graph=_FakeGraph()),
                [seed],
                direction="sideways",
            )
