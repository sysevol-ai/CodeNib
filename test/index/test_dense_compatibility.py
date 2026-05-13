#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Test to check if all dense chunk node_ids exist in BM25 graph nodes."""

import warnings

import pytest

from codeminer.code_chunker import CodeChunker
from codeminer.index import BM25CodeIndexer
from codeminer.ls_router import LSIndexer

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

    # target_file = "httpie/cookies.py"
    # target_nodes = []
    # for vertex in graph.graph.vs:
    #     attrs = vertex.attributes()
    #     if vertex["name"] == target_file or attrs.get("file") == target_file:
    #         target_nodes.append(
    #             {
    #                 "name": vertex["name"],
    #                 "type": attrs.get("type"),
    #                 "start_line": attrs.get("start_line"),
    #                 "end_line": attrs.get("end_line"),
    #             }
    #         )
    # print(f"Graph nodes for {target_file}: {target_nodes}")

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

    compatible_ids = dense_node_ids.intersection(bm25_node_ids)
    missing_ids = dense_node_ids - bm25_node_ids
    extra_bm25_ids = bm25_node_ids - dense_node_ids

    compatibility_rate = (
        len(compatible_ids) / len(dense_node_ids) * 100 if dense_node_ids else 0
    )

    assert not missing_ids, (
        "Found dense node IDs missing in BM25: "
        f"{sorted(missing_ids)} (compatibility={compatibility_rate:.1f}%)"
    )

    # Report extra BM25 IDs as context rather than failing the test. Some indices
    # may contain auxiliary nodes that dense chunking intentionally omits.
    if extra_bm25_ids:
        warnings.warn(
            f"BM25 contains node IDs without dense chunks: {sorted(extra_bm25_ids)}",
            UserWarning,
            stacklevel=1,
        )
