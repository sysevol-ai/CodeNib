#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Check canonical chunk coverage and graph-backed BM25 identity."""

import pytest

from codenib.code_chunker import CodeChunker
from codenib.index import BM25CodeIndexer
from codenib.ls_router import LSIndexer

pytestmark = pytest.mark.integration


def test_dense_compatibility(httpie_cli_repo, tmp_path_factory):
    """Preserve chunk IDs/spans and separately retain physical graph IDs."""
    repo_path = httpie_cli_repo
    output_path = tmp_path_factory.mktemp("httpie_cli_nodes_test")

    try:
        repo_indexer = LSIndexer(repo_path, output_dir=output_path)
        graph = repo_indexer.run_pipeline(project_name="httpie_cli_compat")
    except Exception as exc:  # pragma: no cover - defensive
        pytest.fail(f"BM25 nodes creation failed: {exc}")

    if not graph:
        pytest.fail("Failed to create BM25 graph")

    graph_indexer = BM25CodeIndexer(code_graph=graph)
    graph_document_ids = {
        doc.metadata.get("node_id")
        for doc in graph_indexer.documents
        if doc.metadata.get("node_id")
    }
    # Graph expansion consumes physical identities, including distinct property
    # accessors. Source chunk aliases are not interchangeable with these IDs.
    assert graph_document_ids == set(graph.name_to_vertex)

    try:
        code_chunker = CodeChunker(language="python", max_lines_per_chunk=50)
        chunks = code_chunker.chunk_repository(str(repo_path))
    except Exception as exc:  # pragma: no cover - defensive
        pytest.fail(f"Dense chunks creation failed: {exc}")

    dense_node_ids = {chunk.node_id for chunk in chunks if chunk.node_id}
    # The canonical BM25/vector path shares source chunks, as in the compiler
    # and RetrieveRerankPipeline; it does not reconstruct IDs from a SCIP graph.
    bm25_indexer = BM25CodeIndexer(chunks=chunks, project_root=str(repo_path))
    bm25_node_ids = {
        doc.metadata.get("node_id")
        for doc in bm25_indexer.documents
        if doc.metadata.get("node_id")
    }

    missing_ids = dense_node_ids - bm25_node_ids
    assert dense_node_ids, "Dense chunking produced no addressable chunks"
    assert bm25_node_ids, "BM25 indexing produced no addressable documents"
    assert not missing_ids, (
        f"{len(missing_ids)}/{len(dense_node_ids)} dense node IDs are missing "
        f"from BM25; sample={sorted(missing_ids)[:10]}"
    )

    chunk_spans = {
        (chunk.node_id, chunk.start_line, chunk.end_line)
        for chunk in chunks
        if chunk.node_id
    }
    document_spans = {
        (
            doc.metadata.get("node_id"),
            doc.metadata.get("start_line"),
            doc.metadata.get("end_line"),
        )
        for doc in bm25_indexer.documents
    }
    assert chunk_spans <= document_spans, "BM25 lost source chunk spans"
