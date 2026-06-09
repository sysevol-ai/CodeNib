# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the web codemap payload builder."""

from codeminer.graph.code_graph import CodeGraph
from codeminer.web.codemap import build_codemap


def _symbol_graph_with_many_call_sites() -> CodeGraph:
    graph = CodeGraph()
    graph._add_vertex(
        "src/main.py:caller()",
        {
            "type": "function",
            "file": "src/main.py",
            "start_line": 0,
            "end_line": 4,
            "unified_name": "src/main.py:caller()",
        },
    )
    graph._add_vertex(
        "src/helper.py:callee()",
        {
            "type": "function",
            "file": "src/helper.py",
            "start_line": 10,
            "end_line": 14,
            "unified_name": "src/helper.py:callee()",
        },
    )
    for line in range(12):
        graph._add_edge(
            "src/main.py:caller()",
            "src/helper.py:callee()",
            "reference",
            anchor_file="src/main.py",
            anchor_line=line,
        )
    return graph


def test_codemap_preserves_all_call_site_anchors():
    graph = _symbol_graph_with_many_call_sites()

    result = build_codemap(
        graph, symbol="caller", direction="callees", depth=1, max_nodes=5
    )

    assert result["available"] is True
    assert len(result["edges"]) == 1
    edge = result["edges"][0]
    assert edge["weight"] == 12
    assert len(edge["anchors"]) == 12
    assert {anchor["file"] for anchor in edge["anchors"]} == {"src/main.py"}
    assert sorted(anchor["line"] for anchor in edge["anchors"]) == list(range(1, 13))
