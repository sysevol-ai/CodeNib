# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

import argparse

import pytest

from codeminer.agent.extract_agent import KeywordExtractor
from codeminer.dataset.swebench import SwebenchDataset
from codeminer.llm.litellm_chat import LiteLLMChat

pytestmark = pytest.mark.slow

args_dict = {
    "model": "Qwen/Qwen2.5-Coder-7B",
    "dataset": "princeton-nlp/SWE-bench_Lite",
    "split": "test",
    "filter_instance": "^(astropy__astropy-12907)$",
}

# start the vLLM server in a separate terminal before running this script
# python scripts/start_vllm_server.py --model Qwen/Qwen2.5-Coder-7B

# Example usage
if __name__ == "__main__":
    # load instance from command line
    args = argparse.Namespace(**args_dict)
    dataset_obj = SwebenchDataset.from_args(args)
    dataset = dataset_obj.load()
    llm = LiteLLMChat(
        model=f"openai/{args_dict['model']}",
        max_tokens=1024,
        temperature=0.8,
        api_base="http://localhost:9000/v1",
        api_key="token-abc123",
    )
    for _, instance in enumerate(dataset):
        print(
            f"Loaded instance: {instance['instance_id']} from repo {instance['repo']}"
        )
        print(f"Base commit: {instance['base_commit']}")
        print(f"Problem statement: {instance['problem_statement']}")

        # use the KeywordExtractor to extract keywords
        extractor = KeywordExtractor(llm=llm)
        result = extractor.extract_keywords(instance["problem_statement"])
        print(f"Extracted keywords: {result.keywords}")
        for keyword in result.keywords:
            print(f"Keyword: {keyword}")
