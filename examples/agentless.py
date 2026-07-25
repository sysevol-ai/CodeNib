#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
This script demonstrates the usage of AgentlessPipeline on SWE-bench or LocBench
datasets.
Before running the pipeline, start a vLLM server for the llm model:

```bash
python scripts/start_vllm_server.py --model Qwen/Qwen2.5-Coder-7B
```

Usage example:
    python examples/agentless.py --dataset swebench_lite \
        --filter-instance "^(psf__requests-1963)$" \
        --llm-model "Qwen/Qwen3-32B" --llm-provider "vllm_openai"
"""

import argparse
import sys
from pathlib import Path

from codenib.dataset.locbench import LocbenchDataset
from codenib.dataset.swebench import SwebenchDataset
from codenib.log_utils import get_logger
from codenib.model import AgentlessPipeline
from codenib.paths import prebuilt_data_dir

logger = get_logger(__name__)

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


DATASET_CONFIGS = {
    "swebench_lite": {
        "dataset": "princeton-nlp/SWE-bench_Lite",
        "split": "test",
        "class": SwebenchDataset,
    },
    "locbench_v1": {
        "dataset": "czlll/Loc-Bench_V1",
        "split": "test",
        "class": LocbenchDataset,
    },
}


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Run Agentless Pipeline on benchmark datasets",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Dataset configuration
    parser.add_argument(
        "--dataset",
        type=str,
        required=True,
        choices=["swebench_lite", "locbench_v1"],
        help="Type of dataset to run on",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="test",
        help="Dataset split to use",
    )
    parser.add_argument(
        "--filter-instance",
        type=str,
        default=None,
        help="Regex pattern to filter instances (None processes all instances)",
    )

    # LLM configuration
    parser.add_argument(
        "--llm-model",
        type=str,
        default="gpt-4o",
        help="LLM model name for localization",
    )
    # --llm-model now accepts a full litellm identifier
    # (e.g. "gpt-4o", "vertex_ai/gemini-2.5-flash")
    parser.add_argument(
        "--llm-temperature",
        type=float,
        default=0.0,
        help="Temperature for LLM sampling",
    )
    parser.add_argument(
        "--llm-max-tokens",
        type=int,
        default=2048,
        help="Max tokens for LLM response",
    )

    # Localization configuration
    parser.add_argument(
        "--top-n-files",
        type=int,
        default=3,
        help="Number of files to localize in Stage 1",
    )

    # Repository processing configuration
    parser.add_argument(
        "--languages",
        type=str,
        nargs="+",
        default=["python"],
        help="Programming languages to process",
    )

    # Cache configuration
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=str(prebuilt_data_dir()),
        help="Directory for reusable prebuilt indexes",
    )

    return parser.parse_args()


def run_pipeline(args):
    """Run the Agentless pipeline on the specified dataset."""

    # Get dataset configuration
    dataset = args.dataset
    dataset_config = DATASET_CONFIGS[dataset]

    # Load dataset
    dataset_obj = dataset_config["class"](
        dataset=dataset_config["dataset"],
        split=args.split,
        filter_instance=args.filter_instance or ".*",
    )
    dataset_instances = dataset_obj.load()

    if len(dataset_instances) == 0:
        raise ValueError(f"No instances found in {dataset} dataset")

    logger.info(f"Loaded {len(dataset_instances)} instance(s)")

    # Process each instance
    for _, instance in enumerate(dataset_instances):
        dataset_obj.process_instance(instance)
        repo_path = dataset_obj.get_repo_path(instance)
        repo_commit = instance.get("base_commit", "HEAD")

        # Get instance_id and convert to directory name
        instance_id = instance["instance_id"]
        instance_dir_name = instance_id.replace("/", "__")

        # Initialize pipeline
        pipeline = AgentlessPipeline(
            repo_path=repo_path,
            repo_commit=repo_commit,
            llm_model=args.llm_model,
            llm_temperature=args.llm_temperature,
            llm_max_tokens=args.llm_max_tokens,
            top_n_files=args.top_n_files,
            languages=args.languages,
            cache_dir=args.cache_dir,
            instance_id=instance_dir_name,
        )

        # Query the pipeline
        query = instance["problem_statement"]
        results = pipeline.query(problem_statement=query)

        for i, node in enumerate(results):
            # TODO: save the results to a file
            logger.info("--------------------------------")
            logger.info(f"Rank {i + 1} (Score: {node.score:.4f})")
            logger.info(f"  Node Name: {node.node_name}")
            logger.info(f"  Node Type: {node.type}")
            logger.info(f"  File: {node.file}")
            logger.info(f"  Lines: {node.start_line}-{node.end_line}")
            logger.info(f"  Content: {node.content}")


def main():
    """Main entry point."""
    args = parse_args()

    logger.info(f"Dataset type: {args.dataset}")
    logger.info(f"LLM model: {args.llm_model}")
    logger.info(f"Top-N files: {args.top_n_files}")

    run_pipeline(args)


if __name__ == "__main__":
    main()
