# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from codeminer.index.regex_idx import RegexNodeIndex


class _Vertex:
    def __init__(self, index, **attributes):
        self.index = index
        self._attributes = attributes

    def attributes(self):
        return dict(self._attributes)

    def __getitem__(self, key):
        return self._attributes[key]


class _Graph:
    def __init__(self):
        self.vertices = [
            _Vertex(
                0,
                name="external-symbol",
                type="symbol",
                file=None,
                start_line=None,
                end_line=None,
            ),
            _Vertex(
                1,
                name="local-symbol",
                type="function",
                file="src/example.py",
                start_line=2,
                end_line=3,
            ),
        ]
        self.content_calls = []

    def get_graph(self):
        return SimpleNamespace(vs=self.vertices)

    def get_node_content(self, node_id):
        self.content_calls.append(node_id)
        return "needle" if node_id == 1 else None


def test_regex_index_skips_content_lookup_for_nodes_without_source_ranges():
    graph = _Graph()

    index = RegexNodeIndex(graph)

    assert graph.content_calls == [1]
    assert len(index.nodes) == 2
    assert [node.node_name for node in index.search("needle")] == ["local-symbol"]
