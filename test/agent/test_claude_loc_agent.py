import json
import asyncio

from codeminer.agent.claude_agent import ClaudeLocAgent
from codeminer.dataset import SwebenchMultilingualDataset

INSTANCE_ID = "fmtlib__fmt-1683"


async def test_loc_agent():
    # Load instance from SWE-bench Multilingual dataset
    ds = SwebenchMultilingualDataset(filter_instance=INSTANCE_ID)
    data = ds.load()
    assert len(data) > 0, f"Instance {INSTANCE_ID} not found in dataset"
    instance = data[0]

    # Prepare repo (clone + checkout base_commit)
    ds.process_instance(instance)
    repo = ds.get_repo_path(instance)

    issue_query = instance["problem_statement"]

    context = {
        'issue_title': f"[{instance['instance_id']}] {instance['repo']}",
        'issue_body': instance["problem_statement"],
    }
    hints = instance.get("hints_text", "")
    if hints:
        context['hints'] = hints

    agent = ClaudeLocAgent()
    result = await agent.locate_code(issue_query, repo, context=context)

    if result.success:
        print(f"\nLocalization succeeded in: {result.repo_path}")
        print(f"Found {len(result.locations)} relevant symbol(s):\n")
        output = [
            {
                "name": sym.name,
                "type": sym.type,
                "file_path": sym.file_path,
                "line_start": sym.line_start,
                "line_end": sym.line_end,
                "action": sym.action,
                "description": sym.description,
            }
            for sym in result.locations
        ]
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print(f"Localization failed: {result.error_message}")

    if result.usage:
        print(f"\nUsage: {json.dumps(result.usage, indent=2)}")


if __name__ == "__main__":
    asyncio.run(test_loc_agent())
