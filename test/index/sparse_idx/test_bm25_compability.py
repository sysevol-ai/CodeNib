# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

import argparse
from pathlib import Path

import pytest

from codeminer.code_chunker import CodeChunker
from codeminer.dataset.swebench import SwebenchDataset
from codeminer.index import BM25CodeIndexer
from codeminer.ls_router import LSIndexer

pytestmark = pytest.mark.integration_serial

args_dict = {
    "model": "gpt-4o",
    "dataset": "princeton-nlp/SWE-bench_Lite",
    "split": "test",
    "filter_instance": "^(astropy__astropy-12907)$",
}


def test_bm25_transgraph_compatibility():
    """Test compatibility between BM25 graph-like output and get_node_data."""
    args = argparse.Namespace(**args_dict)
    dataset_obj = SwebenchDataset.from_args(args)
    dataset = dataset_obj.load()
    for _, instance in enumerate(dataset):
        print(
            f"Loaded instance: {instance['instance_id']} from repo {instance['repo']}"
        )
        print(f"Base commit: {instance['base_commit']}")
        print(f"Problem statement: {instance['problem_statement']}")
        dataset_obj.process_instance(instance)
        repo_path = dataset_obj.get_repo_path(instance)
        # set output path with ~/.codeminer/instance_id
        output_path = str(Path.home()) + "/.codeminer/" + instance["instance_id"]

        # setup codegraph
        repo_indexer = LSIndexer(repo_path, output_dir=output_path)

        # Run the indexing pipeline
        graph = repo_indexer.run_pipeline(
            project_name="test_swebench", skip_level="graph"
        )

        # setup bm25 indexer
        bm25_indexer = BM25CodeIndexer()
        bm25_indexer.build_index_from_graph(graph)

        query = "separability matrix"
        # query_test = "separability_matrix()."
        # query = query_test
        results = bm25_indexer.search(query, top_k=5)
        print(f"BM25 query results for {query!r}:")
        for result in results:
            print(f"Node: {result.node_name}")
            print(f"  {result}")  # Uses the new __repr__ method
            # get original attributes from graph for comparison
            attributes = graph.get_node_info_by_name(node_name=result.node_name)
            print(f"  Graph attributes: {attributes}")


def test_bm25_indexer_from_chunks():
    """Test building BM25 index from CodeChunker output."""
    # Path to repo
    args = argparse.Namespace(**args_dict)
    dataset_obj = SwebenchDataset.from_args(args)
    dataset = dataset_obj.load()
    instance = dataset[0]  # Just take the first instance for testing
    print(f"Loaded instance: {instance['instance_id']} from repo {instance['repo']}")
    dataset_obj.process_instance(instance)
    repo_path = dataset_obj.get_repo_path(instance)
    # chunk repository
    chunker = CodeChunker(language="python")
    chunks = chunker.chunk_repository(str(repo_path))
    bm25_indexer = BM25CodeIndexer()
    bm25_indexer.build_index_from_chunks(chunks)

    # Verify index was built
    assert bm25_indexer.retriever is not None
    assert len(bm25_indexer.documents) > 0
    assert len(bm25_indexer.nodes) > 0

    # Test search queries
    search_queries = [
        "separability matrix",  # Partial match
    ]

    for query in search_queries:
        results = bm25_indexer.search(query, top_k=3, return_code_content=False)
        assert isinstance(results, list)
        print(f"Search results for query {query!r}:")
        for result in results:
            print(f"Node: {result.node_name}")
            print(f"  {result}")  # Uses the __repr__ method

        # Test with filter_test=True
        results_filtered = bm25_indexer.search(
            query, top_k=3, return_code_content=False, filter_test=True
        )
        assert isinstance(results_filtered, list)
        print(f"\nSearch results for query {query!r} (filter_test=True):")
        for result in results_filtered:
            print(f"Node: {result.node_name}")
            print(f"  {result}")
            # Verify no test files in results
            import re

            node_words = re.split(r" |_|\/", result.node_name.lower())
            assert not any(word.startswith("test") for word in node_words)
