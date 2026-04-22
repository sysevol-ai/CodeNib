import argparse
from pathlib import Path

import pytest

from codeminer.dataset.swebench import SwebenchDataset
from codeminer.ls_router import LSIndexer

pytestmark = pytest.mark.integration_serial

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
    repo_indexer = LSIndexer(
        repo_path,
        output_dir=output_path,
        exclude_patterns=[exclude_pattern],
    )

    # skip_level="decode" (not "graph"): reuse cached index.scip / index.decoded
    # but rebuild the graph each run. A stale graph.pkl from an older decoder
    # version would silently mask bugs and break the scip-core parity job.
    repo_indexer.run_pipeline(project_name="test_swebench", skip_level="decode")
