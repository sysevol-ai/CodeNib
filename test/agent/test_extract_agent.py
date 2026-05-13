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
    "model": "vertex_ai/gemini-2.5-flash",
    "dataset": "princeton-nlp/SWE-bench_Lite",
    "split": "test",
    # "filter_instance": "^(astropy__astropy-12907)$",
    "filter_instance": "^(django__django-15202)$",
}

# Example usage
if __name__ == "__main__":
    # load instance from command line
    args = argparse.Namespace(**args_dict)
    dataset_obj = SwebenchDataset.from_args(args)
    dataset = dataset_obj.load()
    llm = LiteLLMChat(
        model=args_dict["model"],
        extra_kwargs={"thinking": {"type": "enabled", "budget_tokens": 2048}},
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
