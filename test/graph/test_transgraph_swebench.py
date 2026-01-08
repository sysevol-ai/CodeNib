from pathlib import Path

import pytest

from codeminer.dataset.swebench import SwebenchDataset
from codeminer.graph.transverse_graph import traverse_tree_structure
from codeminer.scip_interface import SCIPIndexer
from codeminer.types import (
    NODE_TYPE_CLASS,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
)


@pytest.fixture
def swebench_args():
    """Fixture providing test arguments for SWE-bench dataset."""
    return {
        "model": "gpt-4o",
        "dataset": "princeton-nlp/SWE-bench_Lite",
        "split": "test",
        "filter_instance": "^(astropy__astropy-12907)$",
    }


@pytest.fixture
def test_instance(swebench_args):
    """Fixture providing a test instance from SWE-bench dataset."""
    import argparse

    args = argparse.Namespace(**swebench_args)
    dataset_obj = SwebenchDataset.from_args(args)
    dataset = dataset_obj.load()
    return next(iter(dataset))


@pytest.fixture
def indexed_repo(test_instance):
    """Fixture providing an indexed repository for testing."""
    dataset_obj = SwebenchDataset()
    dataset_obj.process_instance(test_instance)
    repo_path = dataset_obj.get_repo_path(test_instance)
    output_path = str(Path.home()) + "/.codeminer/" + test_instance["instance_id"]

    repo_indexer = SCIPIndexer(repo_path, output_dir=output_path)
    graph = repo_indexer.run_pipeline(project_name="test_swebench", skip_level="graph")

    return graph


def test_swebench_instance_loading(test_instance):
    """Test that SWE-bench instance can be loaded correctly."""
    assert "instance_id" in test_instance
    assert "repo" in test_instance
    assert "base_commit" in test_instance
    assert "problem_statement" in test_instance
    assert test_instance["instance_id"] == "astropy__astropy-12907"


def test_tree_traversal(indexed_repo):
    """Test traverse_tree_structure functionality."""
    file_nodes = [
        v.attributes()
        for v in indexed_repo.graph.vs
        if "type" in v.attributes() and v["type"] == NODE_TYPE_FILE
    ]

    assert len(file_nodes) > 0

    # Find a suitable root file
    start_file = None
    for node in file_nodes:
        if node["name"].endswith("separable.py"):
            start_file = node["name"]
            break

    if not start_file:
        start_file = file_nodes[0]["name"]

    print(f"\n--- Tree Traversal Test ---")
    print(f"Root file: {start_file}")

    # Test downstream traversal
    tree_result = traverse_tree_structure(
        indexed_repo,
        start_file,
        direction="downstream",
        hops=2,
        node_type_filter=[
            NODE_TYPE_FILE,
            NODE_TYPE_CLASS,
            NODE_TYPE_FUNCTION,
            NODE_TYPE_METHOD,
        ],
    )

    print(f"Tree structure (downstream, 2 hops):")
    print(tree_result)

    assert tree_result is not None
    assert isinstance(tree_result, (dict, list, str))
