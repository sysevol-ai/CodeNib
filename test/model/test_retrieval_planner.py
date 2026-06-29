# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codeminer.model import (
    RetrievalCapabilities,
    RetrievalPlanner,
    RetrieveRerankPipeline,
)
from codeminer.types import QueriedNode


def _node(name: str, score: float = 1.0) -> QueriedNode:
    return QueriedNode(
        node_name=name,
        type="function",
        file=f"src/{name}.py",
        node_id=f"src/{name}.py:{name}",
        start_line=0,
        end_line=1,
        score=score,
        content=f"def {name}(): pass",
    )


def test_planner_selects_fast_lexical_for_exact_symbol_budget():
    planner = RetrievalPlanner()

    plan = planner.select("find `RetryManager`", budget="fast")

    assert plan.name == "fast_lexical"
    assert [stage.engine for stage in plan.stages] == ["sparse"]
    assert plan.retrieval_top_k == 32
    assert plan.enable_rerank is False


def test_planner_selects_structural_graph_when_graph_is_available():
    planner = RetrievalPlanner()

    plan = planner.select(
        "who calls RetryManager.handle()",
        budget="thorough",
        capabilities=RetrievalCapabilities(
            has_dense=True,
            has_sparse=True,
            has_graph=True,
            has_embedding_rerank=True,
            has_llm_rerank=True,
        ),
    )

    assert plan.name == "structural_graph"
    assert plan.graph is not None
    assert plan.graph.seed_engine == "sparse"
    assert plan.graph.use_ppr is True
    assert plan.rerank_strategy == "llm"


def test_planner_falls_back_to_hybrid_for_structural_query_without_graph():
    planner = RetrievalPlanner()

    plan = planner.select(
        "who calls RetryManager.handle()",
        budget="balanced",
        capabilities=RetrievalCapabilities(has_graph=False),
    )

    assert plan.name == "hybrid_fusion"
    assert [stage.engine for stage in plan.stages] == ["dense", "sparse"]
    assert plan.fusion == "rrf"


def test_auto_pipeline_executes_only_the_selected_fast_branch(monkeypatch):
    pipeline = object.__new__(RetrieveRerankPipeline)
    pipeline.retrieval_planner = RetrievalPlanner()
    pipeline.planning_budget = "fast"
    pipeline.planner_capabilities = RetrievalCapabilities()
    pipeline.retrieve_plan = []
    pipeline.fusion_strategy = "weighted"
    pipeline.rrf_k = 60
    pipeline.enable_rerank = True
    pipeline.rerank_context = object()
    pipeline.rerank_strategy = "llm"
    pipeline.rerank_candidate_top_k = None
    calls = []

    def fake_retrieve(query, stage):
        calls.append((query, stage.engine, stage.top_k))
        return [_node(stage.engine)]

    def fail_rerank(*args, **kwargs):
        pytest.fail("fast budget should not rerank")

    monkeypatch.setattr(pipeline, "_run_retrieval_stage", fake_retrieve)
    monkeypatch.setattr(pipeline, "_run_rerank", fail_rerank)

    result = pipeline.query("find `RetryManager`", top_k=5)

    assert [node.node_name for node in result] == ["sparse"]
    assert calls == [("find `RetryManager`", "sparse", 32)]
    assert pipeline.last_selected_plan.name == "fast_lexical"


def test_auto_pipeline_passes_hybrid_plan_to_rerank(monkeypatch):
    pipeline = object.__new__(RetrieveRerankPipeline)
    pipeline.retrieval_planner = RetrievalPlanner()
    pipeline.planning_budget = "balanced"
    pipeline.planner_capabilities = RetrievalCapabilities(
        has_dense=True,
        has_sparse=True,
        has_graph=False,
        has_embedding_rerank=True,
        has_llm_rerank=False,
    )
    pipeline.retrieve_plan = []
    pipeline.fusion_strategy = "weighted"
    pipeline.rrf_k = 60
    pipeline.enable_rerank = True
    pipeline.rerank_context = object()
    pipeline.rerank_strategy = "llm"
    pipeline.rerank_candidate_top_k = None
    retrieved = {
        "dense": [_node("dense_a"), _node("shared")],
        "sparse": [_node("shared"), _node("sparse_b")],
    }
    rerank_call = {}

    def fake_retrieve(query, stage):
        return retrieved[stage.engine]

    def fake_rerank(query, candidates, top_k, *, strategy=None, candidate_top_k=None):
        rerank_call.update(
            {
                "query": query,
                "names": [node.node_name for node in candidates],
                "top_k": top_k,
                "strategy": strategy,
                "candidate_top_k": candidate_top_k,
            }
        )
        return list(candidates[:top_k])

    monkeypatch.setattr(pipeline, "_run_retrieval_stage", fake_retrieve)
    monkeypatch.setattr(pipeline, "_run_rerank", fake_rerank)

    result = pipeline.query("how does `RetryManager` handle retries?", top_k=2)

    assert pipeline.last_selected_plan.name == "hybrid_fusion"
    assert pipeline.last_selected_plan.fusion == "rrf"
    assert [node.node_name for node in result] == ["shared", "dense_a"]
    assert rerank_call == {
        "query": "how does `RetryManager` handle retries?",
        "names": ["shared", "dense_a", "sparse_b"],
        "top_k": 2,
        "strategy": "embedding",
        "candidate_top_k": 50,
    }


def test_rrf_merge_combines_duplicate_ranks():
    first = [_node("a"), _node("c")]
    second = [_node("b"), _node("a")]

    merged = RetrieveRerankPipeline._merge_hybrid(
        [first, second],
        [1.0, 1.0],
        top_k=3,
        fusion="rrf",
        rrf_k=60,
    )

    assert [node.node_name for node in merged] == ["a", "b", "c"]
    assert merged[0].score > merged[1].score
