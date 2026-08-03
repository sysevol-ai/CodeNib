# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import argparse
from pathlib import Path

import pytest

from codenib.agent.rerank_agent import RerankAgent
from codenib.dataset.swebench import SwebenchDataset
from codenib.graph.roi_subgraph import ROISubgraph
from codenib.index import BM25CodeIndexer
from codenib.llm.litellm_chat import LiteLLMChat
from codenib.ls_router import LSIndexer

pytestmark = pytest.mark.slow

# args_dict = {
#     "model": "claude-sonnet-4-5-20250929",
#     "dataset": "princeton-nlp/SWE-bench_Lite",
#     "split": "test",
#     "filter_instance": "^(astropy__astropy-12907)$",
# }


args_dict = {
    "model": "claude-sonnet-4@20250514",
    "provider": "vertex",
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

        # Run the indexing pipeline
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
        k_hop = 2
        subgraph = roi_subgraph.extract_subgraph(node_names, k_hop)
        # get filtered subgraph nodes
        filtered_nodes = roi_subgraph.get_filtered_subgraph_nodes(subgraph)

        # Print filtered nodes (sample 3 nodes), return type is NodeInfo
        for i, node in enumerate(filtered_nodes[:3]):
            print(f"Filtered node {i + 1}")
            print(f"Node Name: {node.node_name}")
            print(f"Node Type: {node.type}")
            print(f"Node File: {node.file}")
            print(f"Node Start Line: {node.start_line}")
            print(f"Node End Line: {node.end_line}")

        # Use rerank agent to rank the filtered nodes by relevance to the problem statement
        print(
            f"\nReranking {len(filtered_nodes)} nodes by relevance to problem statement..."
        )
        llm = LiteLLMChat(model=f"vertex_ai/{args.model}")
        rerank_agent = RerankAgent(llm=llm)
        ranked_nodes = rerank_agent.rerank_nodes(
            query=instance["problem_statement"],
            nodes=filtered_nodes,
            top_k=5,
            # window_size=10,
            include_content=True,
        )

        # Print ranked nodes
        for i, node in enumerate(ranked_nodes):
            print(f"Rank {i + 1} (Score: {node.score:.4f})")
            print(f"Node Name: {node.node_name}")
            print(f"Node Type: {node.type}")
            print(f"Node File: {node.file}")
            print(f"Node Start Line: {node.start_line}")
            print(f"Node End Line: {node.end_line}")
            # print content snippet (first 200 chars)
            content_snippet = (
                node.content[:200] + "..."
                if node.content and len(node.content) > 200
                else node.content
            )
            print(f"Node Content Snippet: {content_snippet}\n")
