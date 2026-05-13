# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

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
    graph = repo_indexer.run_pipeline(project_name="test_swebench", skip_level="decode")
    # Asserting non-None is load-bearing: scip-core parity job depends on this
    # test producing graph.pkl + index.decoded under
    # ~/.codeminer/<instance_id>/. If run_pipeline silently returns None
    # (scip-python crash, generate_index failure, etc.) and we don't assert
    # here, the test passes but the cache is empty, and scip-core then fails
    # in a confusing way several minutes later.
    assert graph is not None, (
        f"run_pipeline returned None for {instance['instance_id']}; "
        "scip-python or downstream decode likely failed — check captured "
        "stderr above for the real error."
    )
