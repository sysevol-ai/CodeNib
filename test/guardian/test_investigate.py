# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for codeminer.guardian.investigate (fake retriever, no embeddings)."""

from codeminer.guardian.investigate import (
    Evidence,
    hotspot_query,
    investigate_hotspot,
)
from codeminer.guardian.signals import Hotspot


class _Node:
    def __init__(self, file, node_name, type, start_line, end_line, score):
        self.file = file
        self.node_name = node_name
        self.type = type
        self.start_line = start_line
        self.end_line = end_line
        self.score = score


class _FakeRetriever:
    def __init__(self, nodes):
        self._nodes = nodes
        self.calls = []

    def query(self, query, top_k=None):
        self.calls.append((query, top_k))
        return self._nodes[:top_k] if top_k else self._nodes


def test_hotspot_query_uses_stem_and_parent():
    q = hotspot_query(Hotspot(path="codeminer/agent/runner.py", commit_count=9))
    assert "runner" in q and "agent" in q and "runner.py" in q


def test_investigate_maps_nodes_to_evidence():
    nodes = [
        _Node("a.py", "foo", "function", 1, 10, 0.91),
        _Node("b.py", "Bar", "class", 20, 40, 0.80),
    ]
    retriever = _FakeRetriever(nodes)
    hotspot = Hotspot(path="a.py", commit_count=5)

    evidence = investigate_hotspot(hotspot, retriever, top_k=2)

    assert retriever.calls[0][1] == 2  # top_k threaded through
    assert evidence == [
        Evidence("a.py", "foo", "function", 1, 10, 0.91),
        Evidence("b.py", "Bar", "class", 20, 40, 0.80),
    ]


def test_investigate_degrades_on_retriever_error():
    class _Boom:
        def query(self, query, top_k=None):
            raise RuntimeError("index missing")

    evidence = investigate_hotspot(Hotspot("a.py", 1), _Boom(), top_k=5)
    assert evidence == []
