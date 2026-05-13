# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess
from pathlib import Path

import pytest

from codeminer.code_chunker import CodeChunker
from codeminer.index import BM25CodeIndexer
from codeminer.ls_router import LSIndexer

pytestmark = pytest.mark.integration_serial


@pytest.fixture(scope="module")
def samplemod_repo():
    """Clone and set up the samplemod repository for testing."""
    test_repo_url = "https://github.com/navdeep-G/samplemod.git"
    test_repo_path = Path.home() / ".codeminer" / "repos" / "samplemod"

    # Clone the repo if it doesn't exist
    if not test_repo_path.exists():
        print(f"Cloning sample module repository from {test_repo_url}...")
        subprocess.run(["git", "clone", test_repo_url, str(test_repo_path)], check=True)
    else:
        print(f"Using existing sample module repository at {test_repo_path}")

    return test_repo_path


@pytest.fixture(scope="module")
def code_graph(samplemod_repo):
    """Create a code graph from the samplemod repository using LSIndexer."""
    current_dir = Path(os.path.dirname(os.path.abspath(__file__)))
    output_file = str(current_dir / "samplemod_index.json")
    # Use a directory under home to avoid permission issues
    output_dir = Path.home() / ".codeminer" / "repos" / "scip_output"

    # Create a new indexer for the samplemod repo
    repo_indexer = LSIndexer(samplemod_repo, output_dir=output_dir)

    # Run the indexing pipeline
    graph = repo_indexer.run_pipeline(
        project_name="SampleModRepo",
        output_file=output_file,
    )

    return graph


@pytest.fixture(scope="module")
def code_chunks(samplemod_repo):
    """Create code chunks from the samplemod repository."""
    chunker = CodeChunker(language="python")
    chunks = chunker.chunk_repository(str(samplemod_repo))
    return chunks


@pytest.fixture
def test_output_dir():
    """Provide a directory for test outputs."""
    return Path(__file__).parent


def test_build_index_from_graph(code_graph, test_output_dir):
    """Test building BM25 index from CodeGraph."""
    # Print basic information about the graph
    print("\nGraph information:")
    code_graph.print_graph_basic_info()

    # Create BM25 indexer with English stemming for method name matching
    indexer = BM25CodeIndexer(max_k=10, language="english")

    # Build index from code graph
    print("Building BM25 index from code graph...")
    indexer.build_index_from_graph(code_graph)

    # Verify index was built
    assert indexer.retriever is not None
    assert len(indexer.documents) > 0
    assert len(indexer.nodes) > 0

    # Test search queries
    search_queries = [
        "hmm()",  # Exact match
        "get_hmm",  # Partial match for "get_hmm_data"
        "gethmm",  # No underscore
        "get hmm",  # Different word segmentation
    ]

    for query in search_queries:
        results = indexer.search(query, top_k=3, return_code_content=False)
        assert isinstance(results, list)

    # Test save and load
    index_dir = test_output_dir / "bm25_index_test"
    index_dir.mkdir(parents=True, exist_ok=True)

    indexer.save_index(str(index_dir))
    assert (index_dir / "documents.json").exists()
    assert (index_dir / "bm25_metadata.json").exists()

    # Load index
    new_indexer = BM25CodeIndexer()
    new_indexer.load_index(str(index_dir))

    # Verify loaded index works
    loaded_results = new_indexer.search(search_queries[0], top_k=3)
    assert len(loaded_results) > 0


def test_build_index_from_chunks(code_chunks, test_output_dir):
    """Test building BM25 index from a list of CodeChunk objects."""
    print(f"\nReceived {len(code_chunks)} chunks")

    # Create BM25 indexer from chunks
    print("Building BM25 index from chunks...")
    indexer = BM25CodeIndexer(chunks=code_chunks, max_k=10, language="english")

    # Verify index was built
    assert indexer.retriever is not None, "Index should have a retriever"
    assert len(indexer.documents) > 0, "Index should contain documents"
    assert len(indexer.nodes) > 0, "Index should contain nodes"

    print(
        f"Index built with {len(indexer.documents)} documents from {len(code_chunks)} chunks"
    )
    print(f"Index contains {len(indexer.nodes)} unique nodes")

    # Test that we have unique nodes (chunks can be split but share same node_id)
    unique_node_ids = set(
        chunk.node_id
        for chunk in code_chunks
        if hasattr(chunk, "node_id") and chunk.node_id
    )
    assert len(indexer.nodes) == len(
        unique_node_ids
    ), "Should have one node per unique node_id"

    # Test search functionality
    search_queries = [
        "hmm",  # Should match get_hmm
        "get_hmm",  # Partial match
        "sample",  # Should match sample module
    ]

    for query in search_queries:
        print(f"\nSearching for {query!r}:")
        results = indexer.search(query, top_k=3, return_code_content=False)

        assert isinstance(results, list), "Search should return a list"
        for i, result in enumerate(results, 1):
            print(f"  {i}. {result.node_name} (type: {result.type})")
            assert hasattr(result, "node_name")
            assert hasattr(result, "type")

    # Test save and load functionality
    index_dir = test_output_dir / "bm25_chunks_index_test"
    index_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nSaving index to {index_dir}...")
    indexer.save_index(str(index_dir))

    assert (index_dir / "documents.json").exists(), "documents.json should be created"
    assert (
        index_dir / "bm25_metadata.json"
    ).exists(), "bm25_metadata.json should be created"

    # Load the index
    print("Loading index from disk...")
    new_indexer = BM25CodeIndexer()
    new_indexer.load_index(str(index_dir))

    assert new_indexer.retriever is not None, "Loaded index should have a retriever"
    assert len(new_indexer.documents) == len(
        indexer.documents
    ), "Loaded index should have same number of documents"

    # Verify loaded index works
    print("\nSearching with loaded index:")
    loaded_results = new_indexer.search(search_queries[0], top_k=3)

    assert len(loaded_results) > 0, "Loaded index should return search results"
    for i, result in enumerate(loaded_results, 1):
        print(f"  {i}. {result.node_name} (type: {result.type})")


def test_search_with_code_content(code_graph, samplemod_repo):
    """Test searching with code content retrieval."""
    # Create indexer from graph (which has project_root)
    indexer = BM25CodeIndexer(code_graph=code_graph, max_k=10, language="english")

    # Search with code content
    query = "hmm"
    results = indexer.search(
        query, top_k=3, return_code_content=True, wrap_with_ln=True
    )

    assert len(results) > 0, "Should return results"

    for result in results:
        print(f"\nResult: {result.node_name}")
        if result.content:
            print(f"Content preview:\n{result.content[:200]}...")
            assert isinstance(result.content, str), "Content should be a string"


def test_search_with_filter_test(code_graph):
    """Test searching with test file filtering."""
    # Create indexer from graph
    indexer = BM25CodeIndexer(code_graph=code_graph, max_k=20, language="english")

    # Search without filter
    query = "test"
    results_unfiltered = indexer.search(query, top_k=20, filter_test=False)

    # Search with filter_test=True
    results_filtered = indexer.search(query, top_k=20, filter_test=True)

    print(f"\nUnfiltered results for {query!r}: {len(results_unfiltered)}")
    for result in results_unfiltered:
        print(f"  {result.node_name}")

    print(
        f"\nFiltered results for {query!r} (filter_test=True): {len(results_filtered)}"
    )
    for result in results_filtered:
        print(f"  {result.node_name}")

    # Verify that filtered results don't contain test files
    import re

    for result in results_filtered:
        node_words = re.split(r" |_|\/", result.node_name.lower())
        assert not any(
            word.startswith("test") for word in node_words
        ), f"Test file found in filtered results: {result.node_name}"

    # Verify that filtering actually removed some results if test files exist
    has_test_files = any(
        any(
            word.startswith("test") for word in re.split(r" |_|\/", r.node_name.lower())
        )
        for r in results_unfiltered
    )
    if has_test_files:
        assert len(results_filtered) < len(
            results_unfiltered
        ), "filter_test should have removed some results"


# For backward compatibility with direct script execution
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
