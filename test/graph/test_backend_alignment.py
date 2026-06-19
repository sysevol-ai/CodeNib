# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for backend-alignment graph comparisons."""

from __future__ import annotations

from codeminer.graph.backend_alignment import (
    BackendAlignmentTolerances,
    compare_backend_graphs,
)
from codeminer.graph.code_graph import CodeGraph
from codeminer.types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_CLASS,
    NODE_TYPE_FILE,
    NODE_TYPE_METHOD,
)


def _graph(prefix: str = "") -> CodeGraph:
    graph = CodeGraph(project_root="/tmp/repo")
    graph._add_vertex("src/Foo.java", {"type": NODE_TYPE_FILE})
    graph._add_vertex(
        f"{prefix}Foo",
        {
            "type": NODE_TYPE_CLASS,
            "file": "src/Foo.java",
            "start_line": 1,
            "end_line": 20,
            "unified_name": "src/Foo.java:Foo",
        },
    )
    graph._add_vertex(
        f"{prefix}Foo.run",
        {
            "type": NODE_TYPE_METHOD,
            "file": "src/Foo.java",
            "start_line": 4,
            "end_line": 8,
            "unified_name": "src/Foo.java:Foo.run()",
        },
    )
    graph._add_edge("src/Foo.java", f"{prefix}Foo", EDGE_TYPE_CONTAIN)
    graph._add_edge(f"{prefix}Foo", f"{prefix}Foo.run", EDGE_TYPE_CONTAIN)
    graph.build_range_indexes()
    return graph


def test_alignment_matches_on_unified_names_not_vertex_identities():
    reference = _graph(prefix="scip:")
    candidate = _graph(prefix="lsp:")

    report = compare_backend_graphs(reference, candidate)

    assert report.ok
    assert report.reference_symbols == 2
    assert report.candidate_symbols == 2
    assert report.missing_symbols == ()
    assert report.extra_symbols == ()


def test_alignment_reports_missing_symbols_and_containment():
    reference = _graph(prefix="scip:")
    candidate = _graph(prefix="lsp:")
    candidate.graph.delete_vertices([candidate.name_to_vertex["lsp:Foo.run"]])
    candidate.name_to_vertex = {v["name"]: v.index for v in candidate.graph.vs}

    report = compare_backend_graphs(reference, candidate)

    assert not report.ok
    assert report.missing_symbols == ("src/Foo.java:Foo.run()",)
    assert report.missing_containment == (
        ("src/Foo.java:Foo", "src/Foo.java:Foo.run()"),
    )


def test_alignment_tolerances_allow_known_backend_gaps():
    reference = _graph(prefix="scip:")
    candidate = _graph(prefix="lsp:")
    candidate.graph.delete_vertices([candidate.name_to_vertex["lsp:Foo.run"]])
    candidate.name_to_vertex = {v["name"]: v.index for v in candidate.graph.vs}

    report = compare_backend_graphs(
        reference,
        candidate,
        tolerances=BackendAlignmentTolerances(
            max_missing_symbols=1,
            max_missing_containment=1,
        ),
    )

    assert report.ok


def test_reference_edges_are_reported_but_not_required_by_default():
    reference = _graph(prefix="scip:")
    candidate = _graph(prefix="lsp:")
    reference._add_edge(
        "scip:Foo.run",
        "scip:Foo",
        EDGE_TYPE_REFERENCE,
        anchor_file="src/Foo.java",
        anchor_line=6,
    )

    report = compare_backend_graphs(reference, candidate)

    assert report.ok
    assert report.reference_references == 1
    assert report.candidate_references == 0
