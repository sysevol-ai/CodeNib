# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for compatibility adapters into ``FactBatch v1``."""

from __future__ import annotations

from codenib.facts import (
    CAPABILITY_DEFINITIONS,
    CAPABILITY_EDGES,
    CAPABILITY_OCCURRENCES,
    PROVENANCE_SCIP,
    fact_batch_from_scip_occurrences,
    fact_batches_from_code_graph,
    merge_fact_batches,
    sha256_digest,
)
from codenib.graph.code_graph import CodeGraph
from codenib.scip_interface.lsp_occurrence_index import (
    SCIPOccurrence,
    SCIPOccurrenceIndex,
)
from codenib.types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
)


def test_scip_occurrence_adapter_keeps_exact_ranges_and_unresolved_monikers() -> None:
    index = SCIPOccurrenceIndex(
        [
            SCIPOccurrence(
                "src/main.py",
                2,
                4,
                2,
                7,
                "scip-python python project 1 pkg/run().",
                1,
            ),
            SCIPOccurrence(
                "src/main.py",
                8,
                10,
                8,
                13,
                "scip-python python project 1 pkg/run().",
                0,
            ),
            SCIPOccurrence(
                "src/other.py",
                0,
                0,
                0,
                3,
                "other",
                1,
            ),
        ],
        position_encoding="UTF8",
    )

    batch = fact_batch_from_scip_occurrences(
        index,
        file_path="src/main.py",
        language="python",
        content_digest=sha256_digest("source"),
        profile_digest=sha256_digest("profile"),
    )

    assert batch.capabilities == (
        CAPABILITY_DEFINITIONS,
        CAPABILITY_OCCURRENCES,
    )
    assert len(batch.symbols) == 1
    assert len(batch.occurrences) == 2
    assert batch.occurrences[0].source_range.sort_key == (2, 4, 2, 7)
    assert batch.occurrences[1].is_definition is False
    assert batch.edges == ()


def _legacy_graph() -> CodeGraph:
    graph = CodeGraph()
    graph._add_vertex("src/caller.py", {"type": NODE_TYPE_FILE})
    graph._add_vertex("src/target.py", {"type": NODE_TYPE_FILE})
    graph._add_vertex(
        "caller-id",
        {
            "type": NODE_TYPE_FUNCTION,
            "file": "src/caller.py",
            "start_line": 1,
            "end_line": 4,
            "unified_name": "src/caller.py:caller()",
            "has_definition": True,
        },
    )
    graph._add_vertex(
        "target-id",
        {
            "type": NODE_TYPE_FUNCTION,
            "file": "src/target.py",
            "start_line": 6,
            "end_line": 8,
            "unified_name": "src/target.py:target()",
            "has_definition": True,
        },
    )
    graph._add_edge("src/caller.py", "caller-id", EDGE_TYPE_CONTAIN)
    graph._add_edge("src/target.py", "target-id", EDGE_TYPE_CONTAIN)
    graph._add_edge(
        "caller-id",
        "target-id",
        EDGE_TYPE_REFERENCE,
        anchor_file="src/caller.py",
        anchor_line=3,
    )
    return graph


def test_legacy_graph_adapter_preserves_resolved_edges_and_line_conversion() -> None:
    profile_digest = sha256_digest("legacy graph profile")
    batches = fact_batches_from_code_graph(
        _legacy_graph(),
        language="python",
        content_digests={
            "src/caller.py": sha256_digest("caller source"),
            "src/target.py": sha256_digest("target source"),
        },
        profile_digest=profile_digest,
        edge_provenance=PROVENANCE_SCIP,
    )

    assert [batch.file_path for batch in batches] == [
        "src/caller.py",
        "src/target.py",
    ]
    caller = batches[0]
    assert caller.capabilities == (
        CAPABILITY_DEFINITIONS,
        CAPABILITY_EDGES,
        CAPABILITY_OCCURRENCES,
    )
    assert caller.symbols[0].definition_range.sort_key == (1, 0, 5, 0)
    reference = next(edge for edge in caller.edges if edge.kind == EDGE_TYPE_REFERENCE)
    assert reference.source_symbol_id == "caller-id"
    assert reference.target_symbol_id == "target-id"
    assert reference.target_moniker is None
    assert reference.anchor_range.sort_key == (3, 0, 4, 0)
    assert reference.provenance == PROVENANCE_SCIP
    assert reference.resolver == "legacy_code_graph"


def test_graph_and_scip_batches_can_form_a_dual_write_projection() -> None:
    content_digest = sha256_digest("caller source")
    graph_batch = fact_batches_from_code_graph(
        _legacy_graph(),
        language="python",
        content_digests={"src/caller.py": content_digest},
        profile_digest=sha256_digest("graph"),
    )[0]
    occurrence_batch = fact_batch_from_scip_occurrences(
        SCIPOccurrenceIndex(
            [
                SCIPOccurrence(
                    "src/caller.py",
                    1,
                    4,
                    1,
                    10,
                    "scip-python python project 1 pkg/caller().",
                    1,
                )
            ]
        ),
        file_path="src/caller.py",
        language="python",
        content_digest=content_digest,
        profile_digest=sha256_digest("occurrences"),
    )

    combined = merge_fact_batches(
        (graph_batch, occurrence_batch),
        provider="legacy+scip",
        profile_digest=sha256_digest("combined"),
    )

    assert len(combined.symbols) == 2
    assert len(combined.occurrences) == 2
    assert any(edge.kind == EDGE_TYPE_REFERENCE for edge in combined.edges)
