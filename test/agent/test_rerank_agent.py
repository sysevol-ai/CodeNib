# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codenib.agent.rerank_agent import RerankAgent, RerankResult
from codenib.types import NodeInfo


def _nodes(count=3):
    return [
        NodeInfo(
            node_name=f"node_{index}",
            type="function",
            file=f"file_{index}.py",
            node_id=str(index),
            content=f"def node_{index}(): pass",
        )
        for index in range(count)
    ]


class _StructuredLLM:
    model = "structured-test"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def with_structured_output(self, _schema):
        return self

    def invoke(self, _messages):
        if self.error is not None:
            raise self.error
        return self.result


class _RankGPTLLM:
    model = "rankgpt-test"

    def __init__(self, response="", error=None):
        self.response = response
        self.error = error

    def invoke(self, _messages):
        if self.error is not None:
            raise self.error
        return self.response


def test_partial_structured_output_preserves_unranked_candidates():
    llm = _StructuredLLM(RerankResult(ranked_indices=[1], scores=[0.9]))

    ranked = RerankAgent(llm).rerank_nodes("find bug", _nodes())

    assert [node.node_name for node in ranked] == ["node_1", "node_0", "node_2"]
    assert [node.score for node in ranked] == [0.9, 0.0, 0.0]


def test_structured_output_ignores_duplicate_invalid_and_non_finite_entries():
    llm = _StructuredLLM(
        SimpleNamespace(
            ranked_indices=[2, 2, 99, 1, 0],
            scores=[5.0, 0.7, 0.6, float("nan")],
        )
    )

    ranked = RerankAgent(llm).rerank_nodes("find bug", _nodes())

    assert [node.node_name for node in ranked] == ["node_2", "node_0", "node_1"]
    assert [node.score for node in ranked] == [1.0, 0.0, 0.0]


def test_structured_invocation_failure_preserves_first_stage_top_k():
    llm = _StructuredLLM(error=RuntimeError("provider unavailable"))

    ranked = RerankAgent(llm).rerank_nodes("find bug", _nodes(), top_k=2)

    assert [node.node_name for node in ranked] == ["node_0", "node_1"]


def test_malformed_structured_output_preserves_first_stage_order():
    llm = _StructuredLLM(SimpleNamespace(ranked_indices=None, scores=None))

    ranked = RerankAgent(llm).rerank_nodes("find bug", _nodes())

    assert [node.node_name for node in ranked] == ["node_0", "node_1", "node_2"]


def test_partial_output_preserves_candidates_without_content_and_scores():
    nodes = _nodes()
    nodes[0].score = 0.7
    nodes[1].content = None
    nodes[1].score = 0.6
    nodes[2].score = 0.5
    llm = _StructuredLLM(RerankResult(ranked_indices=[1], scores=[0.9]))

    ranked = RerankAgent(llm).rerank_nodes("find bug", nodes)

    assert [node.node_name for node in ranked] == ["node_2", "node_0", "node_1"]
    assert [node.score for node in ranked] == [0.9, 0.7, 0.6]


def test_unexpected_rerank_error_preserves_first_stage_order(monkeypatch):
    nodes = _nodes()
    nodes[0].score = 0.8
    agent = RerankAgent(_StructuredLLM())

    def fail_rerank(*_args, **_kwargs):
        raise RuntimeError("malformed provider result")

    monkeypatch.setattr(agent, "_rerank_window", fail_rerank)

    ranked = agent.rerank_nodes("find bug", nodes, top_k=2, include_content=True)

    assert [node.node_name for node in ranked] == ["node_0", "node_1"]
    assert [node.score for node in ranked] == [0.8, 0.0]
    assert ranked[0].content == nodes[0].content


def test_rankgpt_invocation_failure_preserves_first_stage_top_k():
    llm = _RankGPTLLM(error=RuntimeError("provider unavailable"))

    ranked = RerankAgent(llm, listwise_format="rankgpt").rerank_nodes(
        "find bug", _nodes(), top_k=2
    )

    assert [node.node_name for node in ranked] == ["node_0", "node_1"]


def test_malformed_rankgpt_output_preserves_first_stage_scores():
    nodes = _nodes()
    nodes[0].score = 0.8
    nodes[1].score = 0.6
    nodes[2].score = 0.4
    llm = _RankGPTLLM(response="Unable to rank these candidates")

    ranked = RerankAgent(llm, listwise_format="rankgpt").rerank_nodes("find bug", nodes)

    assert [node.node_name for node in ranked] == ["node_0", "node_1", "node_2"]
    assert [node.score for node in ranked] == [0.8, 0.6, 0.4]


def test_constructor_rejects_unknown_listwise_format():
    with pytest.raises(ValueError, match="listwise_format"):
        RerankAgent(_StructuredLLM(), listwise_format="unknown")


def test_zero_top_k_skips_reranking():
    llm = _StructuredLLM(error=AssertionError("must not invoke"))

    assert RerankAgent(llm).rerank_nodes("find bug", _nodes(), top_k=0) == []


@pytest.mark.parametrize("top_k", [-1, True, 1.5])
def test_invalid_top_k_fails_locally(top_k):
    with pytest.raises(ValueError, match="top_k"):
        RerankAgent(_StructuredLLM()).rerank_nodes("find bug", _nodes(), top_k=top_k)
