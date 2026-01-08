import argparse
from pathlib import Path

from codeminer.dataset.swebench import SwebenchDataset
from codeminer.scip_interface import SCIPIndexer

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
        # set output path with ~/.codeminer/instance_id
        output_path = str(Path.home()) + "/.codeminer/" + instance["instance_id"]

        # setup codegraph
        repo_indexer = SCIPIndexer(repo_path, output_dir=output_path)

        # Run the indexing pipeline, allowing skip_index and skip_decode for faster tests
        graph = repo_indexer.run_pipeline(
            project_name="test_swebench",
        )

        # get node info
        # node name = astropy/modeling/separable.py:separability_matrix()
        attributes = graph.get_node_info_by_name(
            node_name="astropy/modeling/separable.py:separability_matrix()"
        )
        print(f"Node attributes: {attributes}")
