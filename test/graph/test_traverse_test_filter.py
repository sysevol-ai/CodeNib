# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""``ignore_test_file`` must key off the node's file, not just its identity."""

from codenib.graph.code_graph import CodeGraph
from codenib.graph.traverse_graph import RepoDependencySearcher


def _graph_with_a_test_neighbour() -> CodeGraph:
    """TS/JS graph identities carry no directory.

    ``test/ts/custom-elements.tsx:JSX/IntrinsicElements`` is stored under the
    identity ``custom-elements.tsx:JSX/IntrinsicElements``, so a name-only test
    check sees a bare filename and lets the symbol through — that is how a test
    symbol reached the preact codemap in issue #390.
    """
    graph = CodeGraph()
    graph._add_vertex(
        "src/index.js:createElement()",
        {
            "type": "function",
            "file": "src/index.js",
            "start_line": 0,
            "end_line": 4,
            "unified_name": "src/index.js:createElement()",
        },
    )
    graph._add_vertex(
        "custom-elements.tsx:JSX/IntrinsicElements",
        {
            "type": "class",
            "file": "test/ts/custom-elements.tsx",
            "start_line": 10,
            "end_line": 27,
            "unified_name": "test/ts/custom-elements.tsx:JSX/IntrinsicElements",
        },
    )
    graph._add_edge(
        "src/index.js:createElement()",
        "custom-elements.tsx:JSX/IntrinsicElements",
        "reference",
        anchor_file="test/ts/custom-elements.tsx",
        anchor_line=11,
    )
    return graph


def test_ignore_test_file_uses_the_file_attribute():
    searcher = RepoDependencySearcher(_graph_with_a_test_neighbour())

    neighbors, edges = searcher.get_neighbors(
        "src/index.js:createElement()",
        direction="forward",
        etype_filter={"reference"},
        ignore_test_file=True,
    )

    assert neighbors == []
    assert edges == []


def test_ignore_test_file_off_still_returns_the_neighbour():
    searcher = RepoDependencySearcher(_graph_with_a_test_neighbour())

    neighbors, _ = searcher.get_neighbors(
        "src/index.js:createElement()",
        direction="forward",
        etype_filter={"reference"},
        ignore_test_file=False,
    )

    assert neighbors == ["custom-elements.tsx:JSX/IntrinsicElements"]


def test_test_call_site_does_not_make_a_reference_only_symbol_a_test_definition():
    graph = CodeGraph()
    graph._add_vertex(
        "src/runtime.py:run()",
        {
            "type": "function",
            "file": "src/runtime.py",
            "start_line": 0,
            "unified_name": "src/runtime.py:run()",
        },
    )
    # SCIP can first encounter an external symbol at a reference in a test file.
    # Its ``file`` is provenance for that reference, not a definition location.
    graph._add_vertex(
        "stdlib:ExternalApi",
        {
            "type": "class",
            "file": "tests/test_runtime.py",
            "unified_name": "stdlib:ExternalApi",
        },
    )
    graph._add_edge(
        "src/runtime.py:run()",
        "stdlib:ExternalApi",
        "reference",
        anchor_file="tests/test_runtime.py",
        anchor_line=4,
    )

    neighbors, _ = RepoDependencySearcher(graph).get_neighbors(
        "src/runtime.py:run()",
        direction="forward",
        etype_filter={"reference"},
        ignore_test_file=True,
    )

    assert neighbors == ["stdlib:ExternalApi"]
