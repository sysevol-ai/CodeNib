#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Check that dense chunks can be joined to BM25 documents by node ID."""

import pytest

from codenib.code_chunker import CodeChunker
from codenib.index import BM25CodeIndexer
from codenib.ls_router import LSIndexer

pytestmark = pytest.mark.integration


def test_dense_compatibility(httpie_cli_repo, tmp_path_factory):
    """Test that all dense node IDs exist in BM25 graph nodes for httpie CLI."""
    repo_path = httpie_cli_repo
    output_path = tmp_path_factory.mktemp("httpie_cli_nodes_test")

    try:
        repo_indexer = LSIndexer(repo_path, output_dir=output_path)
        graph = repo_indexer.run_pipeline(project_name="httpie_cli_compat")
    except Exception as exc:  # pragma: no cover - defensive
        pytest.fail(f"BM25 nodes creation failed: {exc}")

    if not graph:
        pytest.fail("Failed to create BM25 graph")

    bm25_indexer = BM25CodeIndexer(code_graph=graph)
    bm25_node_ids = {
        doc.metadata.get("node_id")
        for doc in bm25_indexer.documents
        if doc.metadata.get("node_id")
    }

    try:
        code_chunker = CodeChunker(language="python", max_lines_per_chunk=50)
        chunks = code_chunker.chunk_repository(str(repo_path))
    except Exception as exc:  # pragma: no cover - defensive
        pytest.fail(f"Dense chunks creation failed: {exc}")

    dense_node_ids = {chunk.node_id for chunk in chunks if chunk.node_id}

    missing_ids = dense_node_ids - bm25_node_ids
    assert dense_node_ids, "Dense chunking produced no addressable chunks"
    assert bm25_node_ids, "BM25 indexing produced no addressable documents"
    assert not missing_ids, (
        f"{len(missing_ids)}/{len(dense_node_ids)} dense node IDs are missing "
        f"from BM25; sample={sorted(missing_ids)[:10]}"
    )
