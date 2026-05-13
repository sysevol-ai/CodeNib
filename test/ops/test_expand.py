# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for graph expansion ops (codeminer.ops.expand)."""

from __future__ import annotations

from codeminer.ops.expand import ExpandContext, nodeinfo_to_queried
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
            start_line=10,
            end_line=20,
            content="def func(): pass",
        )
        result = nodeinfo_to_queried([info])
        node = result[0]
        assert node.node_name == "mod.func"
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
