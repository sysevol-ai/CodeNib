import argparse

from codeminer.agent.extract_agent import KeywordExtractor
from codeminer.dataset.swebench import SwebenchDataset
from codeminer.llm.llm_config import LLMConfig, LLMProvider

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
    llm_config = LLMConfig(
        model_name=args_dict["model"],
        provider=LLMProvider.VLLM_OPENAI,  # Use vLLM with OpenAI-compatible API
        max_tokens=1024,
        top_k=10,
        top_p=0.95,
        temperature=0.8,
        config_data={
            "VLLM_API_BASE_URL": "http://localhost:9000/v1",
            "VLLM_API_KEY": "token-abc123",
        },
    )
    for _, instance in enumerate(dataset):
        print(
            f"Loaded instance: {instance['instance_id']} from repo {instance['repo']}"
        )
        print(f"Base commit: {instance['base_commit']}")
        print(f"Problem statement: {instance['problem_statement']}")

        # use the KeywordExtractor to extract keywords
        extractor = KeywordExtractor(
            llm_config=llm_config,
        )
        result = extractor.extract_keywords(instance["problem_statement"])
        print(f"Extracted keywords: {result.keywords}")
        for keyword in result.keywords:
            print(f"Keyword: {keyword}")
