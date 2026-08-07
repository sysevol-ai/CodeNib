# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import subprocess
import sys
from types import SimpleNamespace

import pytest

from codenib.index.regex_idx import (
    RegexNodeIndex,
    RegexSearchBudgetError,
    RegexSearchTimeoutError,
)
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


def _index_with_nodes(*nodes: NodeInfo) -> RegexNodeIndex:
    index = RegexNodeIndex.__new__(RegexNodeIndex)
    index.nodes = list(nodes)
    return index


def test_regex_index_times_out_pathological_pattern(monkeypatch):
    from codenib.index.regex_idx import regex_idx as regex_module

    monkeypatch.setattr(regex_module, "REGEX_SEARCH_TIMEOUT_SECONDS", 0.01)
    index = _index_with_nodes(
        NodeInfo(
            node_name="worst-case",
            type="file",
            content="a" * 20_000 + "!",
        )
    )

    with pytest.raises(RegexSearchTimeoutError, match="request deadline"):
        index.search(r"(a+)+$")


def test_regex_deadline_includes_file_glob_filtering(monkeypatch):
    from codenib.index.regex_idx import regex_idx as regex_module

    now = [0.0]

    def slow_filter(_path, _glob):
        now[0] = 3.0
        return False

    monkeypatch.setattr(regex_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(regex_module, "fnmatch", slow_filter)
    index = _index_with_nodes(
        NodeInfo(node_name="node", type="file", file="src/a.py", content="x")
    )

    with pytest.raises(RegexSearchTimeoutError, match="request deadline"):
        index.search("needle", file_glob="*.py")


def test_regex_deadline_includes_node_type_filtering(monkeypatch):
    from codenib.index.regex_idx import regex_idx as regex_module

    timestamps = iter([0.0, 0.0, 0.0, 3.0])
    monkeypatch.setattr(regex_module.time, "monotonic", lambda: next(timestamps))
    index = _index_with_nodes(
        NodeInfo(node_name="first", type="class", content="x"),
        NodeInfo(node_name="second", type="class", content="x"),
    )

    with pytest.raises(RegexSearchTimeoutError, match="request deadline"):
        index.search("needle", node_type="function")


def test_regex_index_enforces_node_scan_budget(monkeypatch):
    from codenib.index.regex_idx import regex_idx as regex_module

    monkeypatch.setattr(regex_module, "MAX_REGEX_SCANNED_NODES", 2)
    index = _index_with_nodes(
        *[
            NodeInfo(node_name=f"node-{i}", type="file", file=f"{i}.txt")
            for i in range(3)
        ]
    )

    with pytest.raises(RegexSearchBudgetError, match="2-node scan budget"):
        index.search("needle", file_glob="*.py")


def test_regex_index_enforces_candidate_budget(monkeypatch):
    from codenib.index.regex_idx import regex_idx as regex_module

    monkeypatch.setattr(regex_module, "MAX_REGEX_CANDIDATES", 2)
    index = _index_with_nodes(
        *[
            NodeInfo(node_name=f"node-{i}", type="file", content="haystack")
            for i in range(3)
        ]
    )

    with pytest.raises(RegexSearchBudgetError, match="2-candidate match budget"):
        index.search("needle")


def test_regex_index_stops_when_top_k_is_satisfied(monkeypatch):
    from codenib.index.regex_idx import regex_idx as regex_module

    monkeypatch.setattr(regex_module, "MAX_REGEX_SCANNED_NODES", 1)
    monkeypatch.setattr(regex_module, "MAX_REGEX_CANDIDATES", 1)
    index = _index_with_nodes(
        NodeInfo(node_name="first", type="file", content="needle"),
        NodeInfo(node_name="second", type="file", content="needle"),
    )

    results = index.search("needle", top_k=1)

    assert [node.node_name for node in results] == ["first"]


def test_regex_index_rejects_oversized_pattern():
    index = _index_with_nodes()

    with pytest.raises(ValueError, match="4096-character limit"):
        index.search("x" * 4097)


def test_plain_string_search_is_not_subject_to_regex_limits(monkeypatch):
    from codenib.index.regex_idx import regex_idx as regex_module

    monkeypatch.setattr(regex_module, "MAX_REGEX_SCANNED_NODES", 0)
    monkeypatch.setattr(regex_module, "MAX_REGEX_CANDIDATES", 0)
    index = _index_with_nodes(
        NodeInfo(node_name="plain", type="file", content="needle")
    )

    results = index.search("x" * 4097 + "needle", use_regex=False)

    assert results == []


@pytest.mark.parametrize("top_k", [0, -1, True, 1.5])
def test_plain_string_search_rejects_invalid_top_k(top_k):
    index = _index_with_nodes(
        NodeInfo(node_name="plain", type="file", content="needle")
    )

    with pytest.raises(ValueError, match="top_k must be a positive integer"):
        index.search("needle", use_regex=False, top_k=top_k)
