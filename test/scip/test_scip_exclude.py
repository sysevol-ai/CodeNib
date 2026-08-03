# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import argparse
from pathlib import Path

import pytest

from codenib.dataset.locbench import LocbenchDataset
from codenib.graph.traverse_graph import traverse_tree_structure
from codenib.ls_router import LSIndexer

pytestmark = pytest.mark.integration_serial

args_dict = {
    "model": "gpt-4o",
    "dataset": "czlll/Loc-Bench_V1",
    "split": "test",
    "filter_instance": "^(sympy__sympy-27223)$",
}


def test_scip_exclude():
    # exclude_file = "sympy/polys/numberfields/resolvent_lookup.py"
    exclude_pattern = "test_*"
    args = argparse.Namespace(**args_dict)
    dataset_obj = LocbenchDataset.from_args(args)
    dataset = dataset_obj.load()

    instance = dataset[0]
    dataset_obj.process_instance(instance)
    repo_path = dataset_obj.get_repo_path(instance)
    # set output path with ~/.codenib/instance_id
    output_path = str(Path.home()) + "/.codenib/" + instance["instance_id"]
    # setup codegraph with exclude patterns
    repo_indexer = LSIndexer(
        repo_path,
        output_dir=output_path,
        exclude_patterns=[exclude_pattern],
    )

    # Run the indexing pipeline from scratch (skip_level=None)
    graph = repo_indexer.run_pipeline(project_name="test_swebench", skip_level="graph")

    # list the neighbors of the sympy/utilities/lamdify.py file node
    start_file = "sympy/utilities/lambdify.py"

    # Test downstream traversal
    tree_result = traverse_tree_structure(
        graph,
        start_file,
        direction="downstream",
        hops=1,
    )

    print(f"Tree structure (downstream, 1 hop):")
    print(tree_result)

    assert tree_result is not None
    assert isinstance(tree_result, (dict, list, str))
