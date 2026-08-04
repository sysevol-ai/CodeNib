# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

from codenib.model.agentless_pipeline import AgentlessPipeline


def test_stage_three_uses_extracted_code_content(monkeypatch) -> None:
    pipeline = AgentlessPipeline.__new__(AgentlessPipeline)
    pipeline.stage3_prompt = "{problem_statement}\n{code_segments}"
    pipeline.structured_llm_stage3 = SimpleNamespace(
        invoke=lambda _messages: SimpleNamespace(refined_nodes=["symbol"])
    )
    pipeline._has_node = lambda _node_id: True

    def get_node_data(_node_ids, return_code_content=False, **_kwargs):
        if return_code_content:
            return [{"code_content": "12 | def target():\n13 |     pass"}]
        return [
            {
                "type": "function",
                "file": "target.py",
                "start_line": 11,
                "end_line": 12,
            }
        ]

    pipeline._get_node_data = get_node_data
    monkeypatch.setattr(
        "codenib.model.agentless_pipeline.human_message",
        lambda value: value,
    )

    result = pipeline._stage_3("fix target", ["symbol"])

    assert result[0].content == "12 | def target():\n13 |     pass"
