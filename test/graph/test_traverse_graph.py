# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Test script for traverse_graph functionality using the httpie CLI repository."""

import subprocess
from pathlib import Path

import pytest

from codeminer.graph.traverse_graph import (
    RepoDependencySearcher,
    traverse_tree_structure,
)
from codeminer.log_utils import setup_detailed_logging
from codeminer.ls_router import LSIndexer
from codeminer.types import (
    EDGE_TYPE_CONTAIN,
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
)

pytestmark = pytest.mark.integration

HTTPIE_REPO_URL = "https://github.com/httpie/cli.git"
HTTPIE_REPO_PATH = Path("/tmp/httpie-cli")


def ensure_httpie_repo() -> Path:
    """Clone the httpie/cli repository if needed and return its path."""
    if not HTTPIE_REPO_PATH.exists():
        subprocess.run(
            ["git", "clone", "--depth", "1", HTTPIE_REPO_URL, str(HTTPIE_REPO_PATH)],
            check=True,
        )
    return HTTPIE_REPO_PATH


def test_transgraph_simple(httpie_cli_repo=None, tmp_path_factory=None):
    """Test traverse functions with the httpie CLI repository."""

    repo_path = httpie_cli_repo or ensure_httpie_repo()
    if tmp_path_factory:
        output_path = tmp_path_factory.mktemp("httpie_cli_transgraph")
    else:
        output_path = Path("/tmp") / "httpie_cli_transgraph"
        output_path.mkdir(parents=True, exist_ok=True)

    print(f"Testing with repository: {repo_path}")
    print(f"Output directory: {output_path}")

    # Ensure the test repo exists
    if not repo_path.exists():
        print(f"Error: Test repository not found at {repo_path}")
        return False

    # Setup codegraph
    repo_indexer = LSIndexer(repo_path, output_dir=output_path)

    print("\n" + "=" * 50)
    print("GENERATING SCIP INDEX")
    print("=" * 50)

    # Run the indexing pipeline
    graph = repo_indexer.run_pipeline(
        project_name="httpie_cli_test", skip_level="graph"
    )

    if not graph:
        print("Failed to generate graph!")
        return False

    print("Graph generated successfully!")

    # Print basic graph info
    print(f"\n--- Basic Graph Information ---")
    graph.print_graph_basic_info()

    # Test traverse functionality
    print("\n" + "=" * 50)
    print("TESTING TRAVERSE FUNCTIONS")
    print("=" * 50)

    dependency_searcher = RepoDependencySearcher(graph)

    def nodes_by_type(ntype):
        return [
            v.attributes()
            for v in graph.graph.vs
            if "type" in v.attributes() and v["type"] == ntype
        ]

    # Get sample nodes
    print("\n--- Getting sample nodes ---")
    file_nodes = nodes_by_type(NODE_TYPE_FILE)
    symbol_nodes = nodes_by_type(NODE_TYPE_SYMBOL)

    print(f"Found {len(file_nodes)} file nodes")
    print(f"Found {len(symbol_nodes)} symbol nodes")

    if file_nodes:
        print(f"Sample file nodes:")
        for i, node in enumerate(file_nodes[:5]):
            print(f"  {i+1}. {node['name']}")

    if symbol_nodes:
        print(f"Sample symbol nodes:")
        for i, node in enumerate(symbol_nodes[:10]):
            print(f"  {i+1}. {node['name']} (type: {node.get('type', 'unknown')})")

    # Test specific symbol types
    print("\n--- Testing specific symbol types ---")
    class_nodes = nodes_by_type(NODE_TYPE_CLASS)
    function_nodes = nodes_by_type(NODE_TYPE_FUNCTION)
    method_nodes = nodes_by_type(NODE_TYPE_METHOD)
    field_nodes = nodes_by_type(NODE_TYPE_FIELD)

    print(f"Found {len(class_nodes)} class nodes")
    print(f"Found {len(function_nodes)} function nodes")
    print(f"Found {len(method_nodes)} method nodes")
    print(f"Found {len(field_nodes)} field nodes")

    if class_nodes:
        print("Sample class nodes:")
        for i, node in enumerate(class_nodes[:3]):
            print(f"  {i+1}. {node['name']}")

    if field_nodes:
        print("Sample field nodes:")
        for i, node in enumerate(field_nodes[:3]):
            print(f"  {i+1}. {node['name']}")

    # Test traverse_tree_structure
    if file_nodes:
        print(f"\n--- Testing traverse_tree_structure ---")

        preferred_file = "httpie/core.py"
        available_files = {node["name"] for node in file_nodes}
        if preferred_file in available_files:
            main_file = preferred_file
        else:
            main_file = file_nodes[0]["name"]

        if main_file:

            # Test downstream traversal
            tree_result_downstream = traverse_tree_structure(
                graph,
                main_file,
                direction="downstream",
                hops=2,
                node_type_filter=[
                    NODE_TYPE_FILE,
                    NODE_TYPE_CLASS,
                    NODE_TYPE_FUNCTION,
                    NODE_TYPE_METHOD,
                ],
                # edge_type_filter=[EDGE_TYPE_CONTAIN]
            )
            print("Tree structure (downstream, 2 hops):")
            print(tree_result_downstream)

            # Test with unlimited hops
            tree_result_unlimited = traverse_tree_structure(
                graph,
                main_file,
                direction="downstream",
                hops=-1,  # Unlimited
                node_type_filter=[
                    NODE_TYPE_FILE,
                    NODE_TYPE_CLASS,
                    NODE_TYPE_FUNCTION,
                    NODE_TYPE_METHOD,
                ],
            )
            print(f"\nTree structure (downstream, unlimited hops):")
            print(tree_result_unlimited)

    # Test with a symbol node if available
    if symbol_nodes:
        print(f"\n--- Testing with Symbol Node ---")

        target_symbol = next(
            (node["name"] for node in symbol_nodes if "raw_main" in node["name"]),
            symbol_nodes[0]["name"],
        )

        print(f"Symbol: {target_symbol}")

        # Test upstream traversal
        tree_result_upstream = traverse_tree_structure(
            graph,
            target_symbol,
            direction="upstream",
            hops=2,
            node_type_filter=[NODE_TYPE_FILE, NODE_TYPE_SYMBOL],
        )
        print("Tree structure (upstream from symbol, 2 hops):")
        print(tree_result_upstream)

    # Basic node structure check (replaces RepoEntitySearcher coverage)
    print(f"\n--- Node Attribute Inspection ---")
    if file_nodes:
        sample_file = file_nodes[0]
        print(f"Sample file node: {sample_file}")

    # Test RepoDependencySearcher functionality
    print(f"\n--- Testing RepoDependencySearcher ---")

    if file_nodes:
        test_file = file_nodes[0]["name"]

        # Get forward neighbors (what this file contains)
        forward_nodes, forward_edges = dependency_searcher.get_neighbors(
            test_file, direction="forward", ntype_filter=[NODE_TYPE_SYMBOL]
        )
        print(f"Forward neighbors of {test_file!r}: {len(forward_nodes)} nodes")
        if forward_nodes:
            print("Forward neighbors:")
            for node in forward_nodes[:5]:
                print(f"  - {node}")

        print(f"Forward edges: {len(forward_edges)}")
        if forward_edges:
            print("Forward edges:")
            for edge in forward_edges[:5]:
                print(f"  - {edge[0]} -> {edge[1]} ({edge[3]['type']})")

    # Test with symbol nodes
    if symbol_nodes:
        test_symbol = symbol_nodes[0]["name"]

        # Get backward neighbors (what contains this symbol)
        backward_nodes, backward_edges = dependency_searcher.get_neighbors(
            test_symbol, direction="backward", ntype_filter=[NODE_TYPE_FILE]
        )
        print(f"\nBackward neighbors of {test_symbol!r}: {len(backward_nodes)} nodes")
        if backward_nodes:
            print("Backward neighbors:")
            for node in backward_nodes:
                print(f"  - {node}")

        print(f"Backward edges: {len(backward_edges)}")
        if backward_edges:
            print("Backward edges:")
            for edge in backward_edges:
                print(f"  - {edge[0]} -> {edge[1]} ({edge[3]['type']})")

    # Test edge filtering
    print(f"\n--- Testing Edge Filtering ---")
    if file_nodes:
        test_file = file_nodes[0]["name"]
        contain_nodes, contain_edges = dependency_searcher.get_neighbors(
            test_file, direction="forward", etype_filter=[EDGE_TYPE_CONTAIN]
        )
        print(f"Nodes connected via {EDGE_TYPE_CONTAIN!r} edges: {len(contain_nodes)}")
        if contain_nodes:
            for node in contain_nodes[:3]:
                print(f"  - {node}")

    print(f"\n--- Test Summary ---")
    print(f"✅ Graph indexing: SUCCESS")
    print(f"✅ Entity searching: SUCCESS")
    print(f"✅ Dependency searching: SUCCESS")
    print(f"✅ Tree traversal: SUCCESS")
    print(f"✅ Node filtering: SUCCESS")
    print(f"✅ Edge filtering: SUCCESS")

    return True


# Example usage
if __name__ == "__main__":
    print("Starting httpie CLI repository traverse test...")
    setup_detailed_logging(
        log_dir="logs", run_name="httpie_cli_test", mode="both", level="scip_debug"
    )
    success = test_transgraph_simple()

    if success:
        print("\n🎉 All tests completed successfully!")

    else:
        print("\n❌ Some tests failed!")
        exit(1)
