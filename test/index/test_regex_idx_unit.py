# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import subprocess
import sys
from types import SimpleNamespace

import pytest

from codenib.index.regex_idx import RegexNodeIndex, RegexSearchTimeoutError
from codenib.types import NodeInfo


def test_regex_index_import_does_not_load_graph_runtime():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import codenib.index.regex_idx; "
                "assert 'codenib.graph.code_graph' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


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


def test_regex_index_times_out_pathological_pattern(monkeypatch):
    from codenib.index.regex_idx import regex_idx as regex_module

    monkeypatch.setattr(regex_module, "REGEX_SEARCH_TIMEOUT_SECONDS", 0.01)
    index = RegexNodeIndex.__new__(RegexNodeIndex)
    index.nodes = [
        NodeInfo(
            node_name="worst-case",
            type="file",
            content="a" * 20_000 + "!",
        )
    ]

    with pytest.raises(RegexSearchTimeoutError, match="execution limit"):
        index.search(r"(a+)+$")


def test_regex_index_rejects_oversized_pattern():
    index = RegexNodeIndex.__new__(RegexNodeIndex)
    index.nodes = []

    with pytest.raises(ValueError, match="4096-character limit"):
        index.search("x" * 4097)


def test_plain_string_search_is_not_subject_to_regex_limits():
    index = RegexNodeIndex.__new__(RegexNodeIndex)
    index.nodes = [NodeInfo(node_name="plain", type="file", content="needle")]

    results = index.search("x" * 4097 + "needle", use_regex=False)

    assert results == []
