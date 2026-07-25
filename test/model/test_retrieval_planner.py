# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codenib.model import (
    RetrievalBudget,
    RetrievalCapabilities,
    RetrievalPlanner,
    RetrieveRerankPipeline,
)
from codenib.types import QueriedNode


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


def test_planner_does_not_select_structural_graph_without_sparse_seed():
    planner = RetrievalPlanner()

    plan = planner.select(
        "who calls RetryManager.handle()",
        budget="balanced",
        capabilities=RetrievalCapabilities(
            has_dense=True,
            has_sparse=False,
            has_graph=True,
            has_embedding_rerank=True,
        ),
    )

    assert plan.name == "semantic"
    assert plan.graph is None
    assert [stage.engine for stage in plan.stages] == ["dense"]


def test_planner_respects_budget_before_llm_rerank():
    planner = RetrievalPlanner()

    plan = planner.select(
        "explain retry behavior and failure handling",
        budget="balanced",
        capabilities=RetrievalCapabilities(
            has_dense=True,
            has_sparse=True,
            has_graph=False,
            has_embedding_rerank=False,
            has_llm_rerank=True,
        ),
    )

    assert plan.enable_rerank is False
    assert plan.rerank_strategy is None


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


def test_auto_pipeline_prefers_explicit_rerank_candidate_cap(monkeypatch):
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
    pipeline.rerank_strategy = "embedding"
    pipeline.rerank_candidate_top_k = 7
    rerank_call = {}

    def fake_retrieve(query, stage):
        return [_node(stage.engine)]

    def fake_rerank(query, candidates, top_k, *, strategy=None, candidate_top_k=None):
        rerank_call["candidate_top_k"] = candidate_top_k
        return list(candidates[:top_k])

    monkeypatch.setattr(pipeline, "_run_retrieval_stage", fake_retrieve)
    monkeypatch.setattr(pipeline, "_run_rerank", fake_rerank)

    pipeline.query("how does `RetryManager` handle retries?", top_k=2)

    assert pipeline.last_selected_plan.rerank_candidate_top_k == 50
    assert rerank_call["candidate_top_k"] == 7


class _FakeGraph:
    def __init__(self):
        self.resolve = {"src/seed.py:seed": "seed", "seed": "seed"}
        self.successors = {"seed": [1]}
        self.predecessors = {}
        self.info = {
            1: {
                "name": "neighbor",
                "unified_name": "src/neighbor.py:neighbor",
                "type": "function",
                "file": "src/neighbor.py",
                "start_line": 3,
                "end_line": 5,
            }
        }

    def resolve_symbol(self, symbol):
        return self.resolve.get(symbol), None

    def get_successors(self, node_name, edge_types=None):
        assert edge_types == {"reference"}
        return self.successors.get(node_name, [])

    def get_predecessors(self, node_name, edge_types=None):
        assert edge_types == {"reference"}
        return self.predecessors.get(node_name, [])

    def get_node_info_by_id(self, node_id):
        return self.info.get(node_id)


def test_auto_pipeline_executes_structural_graph_plan(monkeypatch):
    pipeline = object.__new__(RetrieveRerankPipeline)
    pipeline.retrieval_planner = RetrievalPlanner()
    pipeline.planning_budget = RetrievalBudget(
        tier="balanced",
        retrieve_top_k=20,
        rerank_candidate_top_k=None,
        allow_graph=True,
        allow_rerank=False,
    )
    pipeline.planner_capabilities = RetrievalCapabilities(
        has_dense=True,
        has_sparse=True,
        has_graph=True,
        has_embedding_rerank=False,
        has_llm_rerank=False,
    )
    pipeline.retrieve_plan = []
    pipeline.fusion_strategy = "weighted"
    pipeline.rrf_k = 60
    pipeline.enable_rerank = True
    pipeline.rerank_context = object()
    pipeline.rerank_strategy = "llm"
    pipeline.rerank_candidate_top_k = None
    pipeline.repo_path = None
    from codenib.ops.expand import ExpandContext

    pipeline.expand_context = ExpandContext(code_graph=_FakeGraph())

    def fake_retrieve(query, stage):
        assert stage.engine == "sparse"
        return [_node("seed")]

    def fail_rerank(*args, **kwargs):
        pytest.fail("budget disables rerank")

    monkeypatch.setattr(pipeline, "_run_retrieval_stage", fake_retrieve)
    monkeypatch.setattr(pipeline, "_run_rerank", fail_rerank)

    result = pipeline.query("who calls seed?", top_k=5)

    assert pipeline.last_selected_plan.name == "structural_graph"
    assert pipeline.last_planner_trace["signals"]["structural"] is True
    assert pipeline.last_planner_trace["capabilities"]["has_graph"] is True
    assert [node.node_name for node in result] == ["seed", "src/neighbor.py:neighbor"]


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


def test_rrf_merge_dedups_same_node_id_with_different_branch_metadata():
    dense = QueriedNode(
        node_name="foo",
        type="function",
        file="src/a.py",
        node_id="src/a.py:foo()",
        start_line=10,
        end_line=12,
        score=0.9,
        content="def foo(): pass",
    )
    sparse = QueriedNode(
        node_name="src/a.py:foo()",
        type="function",
        file="src/a.py",
        node_id="src/a.py:foo()",
        start_line=None,
        end_line=None,
        score=0.0,
    )

    merged = RetrieveRerankPipeline._merge_hybrid(
        [[dense], [sparse]],
        [1.0, 1.0],
        top_k=5,
        fusion="rrf",
        rrf_k=60,
    )

    assert len(merged) == 1
    assert merged[0].node_id == "src/a.py:foo()"
    assert merged[0].node_name == "foo"
    assert merged[0].score == pytest.approx(2 / 61)
