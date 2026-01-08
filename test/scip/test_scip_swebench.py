import argparse
from pathlib import Path

from codeminer.dataset.swebench import SwebenchDataset
from codeminer.scip_interface import SCIPIndexer

args_dict = {
    "model": "gpt-4o",
    "dataset": "princeton-nlp/SWE-bench_Lite",
    "split": "test",
    "filter_instance": "^(sympy__sympy-21847)$",
}


def test_scip_exclude():
    # exclude_file = "sympy/polys/numberfields/resolvent_lookup.py"
    exclude_pattern = "test_*"
    args = argparse.Namespace(**args_dict)
    dataset_obj = SwebenchDataset.from_args(args)
    dataset = dataset_obj.load()

    instance = dataset[0]
    dataset_obj.process_instance(instance)
    repo_path = dataset_obj.get_repo_path(instance)
    # set output path with ~/.codeminer/instance_id
    output_path = str(Path.home()) + "/.codeminer/" + instance["instance_id"]
    # setup codegraph with exclude patterns
    repo_indexer = SCIPIndexer(
        repo_path,
        output_dir=output_path,
        exclude_patterns=[exclude_pattern],
    )

    # Run the indexing pipeline from scratch (skip_level=None)
    repo_indexer.run_pipeline(project_name="test_swebench", skip_level="graph")
