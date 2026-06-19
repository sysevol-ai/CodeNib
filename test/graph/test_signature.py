# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for graph signatures used by backend alignment harnesses."""

from __future__ import annotations

import pytest

from codeminer.graph.code_graph import CodeGraph
from codeminer.graph.signature import (
    GraphAlignmentTolerance,
    assert_graph_alignment_within_tolerance,
    assert_graph_signatures_equal,
    diff_graph_signatures,
    graph_signature,
)
from codeminer.types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE


def _add_symbol(
    graph: CodeGraph,
    name: str,
    *,
    file: str,
    start_line: int,
    end_line: int,
    symbol_type: str = "function",
) -> None:
    graph._add_vertex(
        name,
        {
            "type": symbol_type,
            "file": file,
            "start_line": start_line,
            "end_line": end_line,
            "unified_name": name,
        },
    )


def _make_graph(*, reverse_vertices: bool = False, extra_reference: bool = False):
    graph = CodeGraph(project_root="/repo")
    graph._add_vertex("src/app.py", {"type": "file"})

    symbols = [
        ("src/app.py:main()", "src/app.py", 1, 8),
        ("src/lib.py:helper()", "src/lib.py", 3, 5),
    ]
    if reverse_vertices:
        symbols = list(reversed(symbols))
    for name, file, start_line, end_line in symbols:
        _add_symbol(graph, name, file=file, start_line=start_line, end_line=end_line)

    graph._add_edge(
        "src/app.py",
        "src/app.py:main()",
        EDGE_TYPE_CONTAIN,
    )
    graph._add_edge(
        "src/app.py:main()",
        "src/lib.py:helper()",
        EDGE_TYPE_REFERENCE,
        anchor_file="src/app.py",
        anchor_line=4,
    )
    if extra_reference:
        graph._add_edge(
            "src/app.py:main()",
            "src/lib.py:helper()",
            EDGE_TYPE_REFERENCE,
            anchor_file="src/app.py",
            anchor_line=6,
        )
    return graph


def test_signature_is_order_independent_and_preserves_anchor_edges():
    reference = _make_graph(extra_reference=True)
    candidate = _make_graph(reverse_vertices=True, extra_reference=True)

    assert_graph_signatures_equal(reference, candidate, tag="unit")

    signature = graph_signature(reference)
    reference_edges = [
        edge for edge in signature.edges if edge[2] == EDGE_TYPE_REFERENCE
    ]
    assert reference_edges == [
        (
            "src/app.py:main()",
            "src/lib.py:helper()",
            EDGE_TYPE_REFERENCE,
            "src/app.py",
            4,
        ),
        (
            "src/app.py:main()",
            "src/lib.py:helper()",
            EDGE_TYPE_REFERENCE,
            "src/app.py",
            6,
        ),
    ]


def test_signature_diff_reports_vertex_attribute_mismatches():
    reference = _make_graph()
    candidate = _make_graph()
    vid = candidate.name_to_vertex["src/app.py:main()"]
    candidate.graph.vs[vid]["end_line"] = 9

    diff = diff_graph_signatures(graph_signature(reference), graph_signature(candidate))

    assert diff.vertex_attr_mismatches
    assert diff.vertex_attr_mismatches[0].name == "src/app.py:main()"
    assert diff.vertex_attr_mismatches[0].attr == "end_line"
    with pytest.raises(AssertionError, match="vertex_attr_mismatches=1"):
        assert_graph_signatures_equal(reference, candidate, tag="attrs")


def test_alignment_tolerance_must_explicitly_allow_reference_edge_deltas():
    reference = _make_graph()
    candidate = _make_graph(extra_reference=True)

    with pytest.raises(AssertionError, match="violations=\\['extra_edges'"):
        assert_graph_alignment_within_tolerance(
            reference,
            candidate,
            GraphAlignmentTolerance(),
            tag="strict",
        )

    assert_graph_alignment_within_tolerance(
        reference,
        candidate,
        GraphAlignmentTolerance(max_extra_edges=1, max_edge_count_deltas=1),
        tag="lsp-vs-scip",
    )


def test_alignment_tolerance_keeps_name_differences_strict_by_default():
    reference = _make_graph()
    candidate = _make_graph()
    candidate.graph.delete_vertices([candidate.name_to_vertex["src/lib.py:helper()"]])

    with pytest.raises(AssertionError, match="missing_names=1"):
        assert_graph_alignment_within_tolerance(
            reference,
            candidate,
            GraphAlignmentTolerance(max_extra_edges=10),
            tag="missing",
        )
