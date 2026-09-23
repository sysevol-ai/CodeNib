# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Exercise Jev reranking through the real client, mocking only the HTTP boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests

from codenib.agent.rerank_agent import RerankAgent
from codenib.agent.skills.llm_rerank.executor import create_executor
from codenib.llm import OpenRouterDecisions
from codenib.ops.rerank import RerankContext
from codenib.types import NODE_TYPE_FUNCTION, NodeInfo


@pytest.fixture
def api(monkeypatch):
    stub = SimpleNamespace(calls=[], scores={}, status=200, incomplete=False)

    def send(_session, request, **kwargs):
        assert request.url == "https://openrouter.ai/api/alpha/decisions"
        payload = json.loads(request.body)
        stub.calls.append(payload)
        answers = {}
        for name, question in payload["questions"].items():
            candidate = payload["state"]["candidates"][name]
            score = stub.scores.get(candidate["name"], 0.0)
            answers[name] = {
                "type": "score",
                "score": score,
                "confidence": 0.01,
                "probabilities": {"0": 1 - score / 3, "1": 0, "2": 0, "3": score / 3},
                "legend": {str(i): text for i, text in enumerate(question["criteria"])},
            }
        if stub.incomplete:
            answers.pop(next(iter(answers)))
        response = requests.Response()
        response.status_code = stub.status
        response.url = request.url
        response._content = json.dumps(
            {"model": "typesafe/jev-1.13", "answers": answers}
        ).encode()
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return stub


def _nodes(count=3):
    return [
        NodeInfo(
            node_name=f"node_{index}",
            node_id=f"code.py:node_{index}",
            type=NODE_TYPE_FUNCTION,
            file="code.py",
            start_line=index * 2,
            end_line=index * 2 + 1,
            score=1.0 / (index + 1),
            content=f"def node_{index}(): pass",
        )
        for index in range(count)
    ]


def test_skill_sorts_by_normalized_score_and_preserves_node_metadata(api):
    api.scores.update(node_0=0.3, node_1=2.4, node_2=1.5)
    nodes = _nodes()
    nodes[1].content += "#" * 5000
    context = RerankContext(decisions=OpenRouterDecisions())

    ranked = create_executor(context)(
        "find implementation", nodes, top_k=2, return_content=True
    )

    assert [node.node_id for node in ranked] == [nodes[1].node_id, nodes[2].node_id]
    assert [node.score for node in ranked] == pytest.approx([0.8, 0.5])
    assert ranked[0].content == nodes[1].content
    assert ranked[0].start_line == nodes[1].start_line
    assert ranked[0].end_line == nodes[1].end_line
    assert len(api.calls) == 1
    request = api.calls[0]
    assert request["state"]["query"] == "find implementation"
    assert len(request["state"]["candidates"]["node_1"]["content"]) == 3000
    assert all(q["type"] == "score" for q in request["questions"].values())


def test_bounded_batches_cover_every_candidate_and_preserve_ties(api):
    nodes = _nodes(23)
    api.scores.update({node.node_name: 1.5 for node in nodes})
    ranked = RerankAgent(decisions=OpenRouterDecisions()).rerank_nodes(
        "find implementation", nodes, window_size=100, window_step=100
    )

    assert [node.node_id for node in ranked] == [node.node_id for node in nodes]
    assert all(node.score == 0.5 for node in ranked)
    assert len(api.calls) == 3
    assert sum(len(call["questions"]) for call in api.calls) == len(nodes)
    assert all(len(call["questions"]) <= 10 for call in api.calls)
    assert {name for call in api.calls for name in call["questions"]} == {
        node.node_name for node in nodes
    }


@pytest.mark.parametrize("failure", ["http", "incomplete"])
def test_failed_decisions_preserve_first_stage_order_scores_and_top_k(api, failure):
    if failure == "http":
        api.status = 503
    else:
        api.incomplete = True
    nodes = _nodes()
    agent = RerankAgent(decisions=OpenRouterDecisions(max_retries=0))

    ranked = agent.rerank_nodes("find implementation", nodes, top_k=2)

    assert [node.node_id for node in ranked] == [node.node_id for node in nodes[:2]]
    assert [node.score for node in ranked] == [node.score for node in nodes[:2]]
    assert all(node.content is None for node in ranked)


def test_candidates_without_content_keep_original_identity_and_score(api):
    nodes = _nodes()
    nodes[0].content = None
    api.scores.update(node_1=1.5, node_2=3.0)

    ranked = RerankAgent(decisions=OpenRouterDecisions()).rerank_nodes("query", nodes)

    assert [node.node_id for node in ranked] == [
        nodes[2].node_id,
        nodes[1].node_id,
        nodes[0].node_id,
    ]
    assert ranked[-1].score == nodes[0].score
    assert set(api.calls[0]["questions"]) == {"node_1", "node_2"}


def test_empty_work_does_not_call_decisions(api):
    agent = RerankAgent(decisions=OpenRouterDecisions())
    assert agent.rerank_nodes("query", _nodes(), top_k=0) == []
    assert agent.rerank_nodes("query", []) == []
    assert agent.rerank_nodes("   ", _nodes()) == []
    nodes = _nodes()
    for node in nodes:
        node.content = None
    assert len(agent.rerank_nodes("query", nodes)) == len(nodes)
    assert api.calls == []


def test_decisions_cannot_be_combined_with_chat_or_rankgpt():
    with pytest.raises(ValueError, match="exactly one"):
        RerankAgent(llm=object(), decisions=OpenRouterDecisions())
    with pytest.raises(ValueError, match="listwise_format"):
        RerankAgent(decisions=OpenRouterDecisions(), listwise_format="rankgpt")


def test_sparse_retrieval_to_decisions_pipeline_uses_no_chat(
    api, monkeypatch, tmp_path
):
    from codenib.model import retrieve_rerank_pipeline as pipeline_module

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "code.py").write_text(
        "def payment_target():\n    return 'payment'\n\n"
        "def payment_other():\n    return None\n",
        encoding="utf-8",
    )

    def no_chat(**_kwargs):
        pytest.fail("The decisions strategy must not construct a chat model")

    monkeypatch.setattr(pipeline_module, "LiteLLMChat", no_chat)
    pipeline = pipeline_module.RetrieveRerankPipeline(
        repo_path=str(repo),
        index_path=str(tmp_path / "index"),
        retrieval_mode="sparse",
        rerank_strategy="decisions",
    )
    assert pipeline.rerank_context.decisions.model == "~typesafe/jev-latest"
    assert pipeline.planner_capabilities.has_llm_rerank
    candidates = pipeline.bm25_index.search("payment", top_k=10)
    assert len(candidates) >= 2
    target = next(node for node in candidates if "payment_target" in node.node_name)
    api.scores[target.node_name] = 3.0

    ranked = pipeline.query("payment", top_k=1)

    assert len(api.calls) >= 1
    assert [node.node_id for node in ranked] == [target.node_id]
    assert ranked[0].score == 1.0
