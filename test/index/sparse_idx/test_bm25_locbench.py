# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import argparse
from pathlib import Path

import pytest

from codenib.dataset.locbench import LocbenchDataset
from codenib.index import BM25CodeIndexer
from codenib.ls_router import LSIndexer

pytestmark = pytest.mark.integration_serial

args_dict = {
    "model": "gpt-4o",
    "dataset": "czlll/Loc-Bench_V1",
    "split": "test",
    "filter_instance": "^(sympy__sympy-27223)$",
}


def test_bm25_index():
    """Test compatibility between BM25 graph-like output and get_node_data."""
    args = argparse.Namespace(**args_dict)
    dataset_obj = LocbenchDataset.from_args(args)
    dataset = dataset_obj.load()
    exclude_pattern = "test_*"
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
        repo_indexer = LSIndexer(
            repo_path, output_dir=output_path, exclude_patterns=[exclude_pattern]
        )

        # Run the indexing pipeline, allowing skip_index and skip_decode for faster
        # tests.
        graph = repo_indexer.run_pipeline(
            project_name="test_swebench", skip_level="graph"
        )

        # setup bm25 indexer
        bm25_indexer = BM25CodeIndexer()
        bm25_indexer.build_index_from_graph(graph)

        # node_check = "sympy/utilities/lambdify.py::lambdify()"
        # # Check that specific node is in the indexed nodes
        # assert any(
        #     node == node_check for node in bm25_indexer.nodes
        # ), f"Node {node_check} not found in indexed nodes"

        query = "lambdify"
        # query_test = "separability_matrix()."
        # query = query_test
        results_filtered = bm25_indexer.search(
            query, top_k=10, return_code_content=False, filter_test=True
        )
        print(f"BM25 query results for {query!r}:")
        for result in results_filtered:
            print(f"Node: {result.node_name}")
            print(f"  {result}")
            # Verify no test files in results
            assert not any(
                word.startswith("test")
                for word in result.node_name.lower()
                .replace("_", " ")
                .replace("/", " ")
                .split()
            )
