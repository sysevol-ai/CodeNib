# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import argparse
from pathlib import Path

import pytest

from codenib.dataset.swebench import SwebenchDataset
from codenib.graph.roi_subgraph import ROISubgraph
from codenib.index import BM25CodeIndexer
from codenib.ls_router import LSIndexer

pytestmark = pytest.mark.integration_serial

args_dict = {
    "model": "gpt-4o",
    "dataset": "princeton-nlp/SWE-bench_Lite",
    "split": "test",
    "filter_instance": "^(astropy__astropy-12907)$",
}


# Example usage
if __name__ == "__main__":
    # load instance from command line
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
        # set output path with ~/.codenib/instance_id
        output_path = str(Path.home()) + "/.codenib/" + instance["instance_id"]

        # setup codegraph
        repo_indexer = LSIndexer(repo_path, output_dir=output_path)

        # Run the indexing pipeline, allowing skip_index and skip_decode for faster tests
        graph = repo_indexer.run_pipeline(
            project_name="test_swebench", skip_level="graph"
        )

        # get node info
        node_name = "astropy.modeling.separable/separability_matrix()."

        # Create BM25 indexer with English stemming for method name matching
        indexer = BM25CodeIndexer(max_k=10, language="english")

        # Build the index from the code graph
        indexer.build_index_from_graph(graph)
        # Search for the node name
        print(f"Searching for node name: {node_name}")
        results = indexer.search(node_name, top_k=5)
        print(f"Search results: {results}")
        # Extract node names from search results
        node_names = [result.node_name for result in results]
        print(f"Node names: {node_names}")
        # Create ROISubgraph object
        roi_subgraph = ROISubgraph(graph)
        # Extract subgraph with k-hop neighbors
        k_hop = 1
        subgraph = roi_subgraph.extract_subgraph(
            [node_names[0]], k_hop, direction="downward"
        )
        # get filtered subgraph nodes
        filtered_nodes = roi_subgraph.get_filtered_subgraph_nodes(
            subgraph, exclude_nodes=[node_names[0]], filter_tests=True
        )
        for node in filtered_nodes:
            print(f"Node ID: {node.node_name}, Type: {node.type}")
