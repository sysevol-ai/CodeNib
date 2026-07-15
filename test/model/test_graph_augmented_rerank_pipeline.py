# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for dense-first graph expand/rerank pipeline."""

from __future__ import annotations

from contextlib import contextmanager

from codeminer.model import (
    DenseGraphExpandRerankPipeline as ExportedDenseGraphExpandRerankPipeline,
)
from codeminer.model.dense_graph_expand_rerank_pipeline import (
    DenseGraphExpandRerankPipeline,
)
from codeminer.model.graph_augmented_rerank_pipeline import GraphAugmentedRerankPipeline
from codeminer.ops.rerank import RerankContext
from codeminer.types import EDGE_TYPE_REFERENCE, NodeInfo, QueriedNode


class _FakeRetriever:
    def __init__(self, results):
        self.results = results
        self.loaded_index = None

    def load_index(self, index_path):
        self.loaded_index = index_path

    def query(self, query, top_k=None):
        return self.results[:top_k]

    def close(self):
        pass


class _FakeGraph:
    def __init__(self, *, resolve=None, successors=None, predecessors=None, info=None):
        self.resolve = resolve or {}
        self.successors = successors or {}
        self.predecessors = predecessors or {}
        self.info = info or {}

    def resolve_symbol(self, symbol):
        return self.resolve.get(symbol), None

    def get_successors(self, node_name, edge_types=None):
        assert edge_types in (None, {EDGE_TYPE_REFERENCE})
        return self.successors.get(node_name, [])

    def get_predecessors(self, node_name, edge_types=None):
        assert edge_types in (None, {EDGE_TYPE_REFERENCE})
        return self.predecessors.get(node_name, [])

    def get_node_info_by_id(self, node_id):
        return self.info.get(node_id)


class _FakeEmbedding:
    def __init__(self, vectors):
        self.vectors = vectors

    def embed_query(self, text):
        return self.vectors[text]

    def embed_documents(self, texts):
        return [self.vectors[text] for text in texts]


class _FakeStore:
    def __init__(self, vectors):
        self.embedding = _FakeEmbedding(vectors)
        self.index_metric = "ip"


def test_pipeline_preserves_dense_topk_and_appends_graph_neighbors():
    dense = [
        QueriedNode(
            node_name="seed",
            type="function",
            file="src/a.py",
            node_id="src/a.py:seed",
            start_line=0,
            end_line=2,
            score=1.0,
        ),
        QueriedNode(
            node_name="dense2",
            type="function",
            file="src/b.py",
            node_id="src/b.py:dense2",
            start_line=4,
            end_line=5,
            score=0.9,
        ),
    ]
    graph = _FakeGraph(
        resolve={"seed": "seed"},
        successors={"seed": [1, 2]},
        info={
            1: {
                "name": "dense2",
                "unified_name": "src/b.py:dense2",
                "type": "function",
                "file": "src/b.py",
                "start_line": 4,
                "end_line": 5,
            },
            2: {
                "name": "neighbor",
                "unified_name": "src/c.py:neighbor",
                "type": "function",
                "file": "src/c.py",
                "start_line": 8,
                "end_line": 10,
            },
        },
    )
    pipeline = DenseGraphExpandRerankPipeline(
        retriever=_FakeRetriever(dense),
        code_graph=graph,
        dense_top_k=2,
        graph_neighbors_per_seed=2,
        final_top_k=5,
        include_graph_content=False,
    )

    result = pipeline.query("query")

    assert [node.node_id for node in result] == [
        "src/a.py:seed",
        "src/b.py:dense2",
        "src/c.py:neighbor",
    ]
    assert len(pipeline.last_dense_results) == 2
    assert len(pipeline.last_graph_results) == 2
    assert len(pipeline.last_candidates) == 3


def test_pipeline_applies_explicit_candidate_filters():
    dense = [
        QueriedNode(
            node_name="keep",
            type="function",
            file="src/a.py",
            node_id="src/a.py:keep",
            start_line=0,
            end_line=2,
            score=1.0,
        ),
        QueriedNode(
            node_name="drop",
            type="function",
            file="tests/test_a.py",
            node_id="tests/test_a.py:drop",
            start_line=4,
            end_line=5,
            score=0.9,
        ),
    ]
    pipeline = DenseGraphExpandRerankPipeline(
        retriever=_FakeRetriever(dense),
        dense_top_k=2,
        graph_neighbors_per_seed=0,
        final_top_k=5,
        candidate_filters=[lambda node: not node.file.startswith("tests/")],
    )

    result = pipeline.query("query")

    assert [node.node_name for node in result] == ["keep"]
    assert [node.node_name for node in pipeline.last_filtered_candidates] == ["keep"]


def test_pipeline_limits_graph_seeds_without_truncating_dense_pool():
    dense = [
        QueriedNode(
            node_name=f"seed{i}",
            type="function",
            file=f"src/{i}.py",
            node_id=f"src/{i}.py:seed{i}",
            start_line=i,
            end_line=i,
            score=1.0 - i / 10,
        )
        for i in range(3)
    ]
    graph = _FakeGraph(
        resolve={f"src/{i}.py:seed{i}": f"seed{i}" for i in range(3)},
        successors={f"seed{i}": [i] for i in range(3)},
        info={
            i: {
                "name": f"neighbor{i}",
                "unified_name": f"src/n{i}.py:neighbor{i}",
                "type": "function",
                "file": f"src/n{i}.py",
                "start_line": i,
                "end_line": i,
            }
            for i in range(3)
        },
    )
    pipeline = DenseGraphExpandRerankPipeline(
        retriever=_FakeRetriever(dense),
        code_graph=graph,
        dense_top_k=3,
        graph_seed_top_k=1,
        graph_neighbors_per_seed=1,
        final_top_k=10,
        include_graph_content=False,
    )

    result = pipeline.query("query")

    assert [node.node_name for node in pipeline.last_dense_results] == [
        "seed0",
        "seed1",
        "seed2",
    ]
    assert [node.node_name for node in pipeline.last_graph_results] == [
        "src/n0.py:neighbor0"
    ]
    assert [node.node_name for node in result] == [
        "seed0",
        "seed1",
        "seed2",
        "src/n0.py:neighbor0",
    ]


def test_pipeline_rejects_non_positive_graph_seed_limit():
    try:
        DenseGraphExpandRerankPipeline(
            retriever=_FakeRetriever([]),
            graph_seed_top_k=0,
        )
    except ValueError as exc:
        assert "graph_seed_top_k" in str(exc)
    else:
        raise AssertionError("expected graph_seed_top_k validation to fail")


def test_pipeline_reranks_dense_and_graph_candidates(tmp_path):
    source = tmp_path / "src" / "a.py"
    source.parent.mkdir()
    source.write_text("graph best\n", encoding="utf-8")
    dense = [
        QueriedNode(
            node_name="seed",
            type="function",
            file="src/a.py",
            node_id="src/a.py:seed",
            start_line=0,
            end_line=0,
            score=1.0,
            content="dense low",
        )
    ]
    graph = _FakeGraph(
        resolve={"seed": "seed"},
        successors={"seed": [1]},
        info={
            1: {
                "name": "neighbor",
                "unified_name": "src/a.py:neighbor",
                "type": "function",
                "file": "src/a.py",
                "start_line": 0,
                "end_line": 0,
            }
        },
    )
    store = _FakeStore(
        vectors={
            "query": [1.0, 0.0],
            "dense low": [0.1, 0.0],
            "graph best\n": [0.9, 0.0],
        }
    )
    pipeline = DenseGraphExpandRerankPipeline(
        retriever=_FakeRetriever(dense),
        code_graph=graph,
        rerank_context=RerankContext(embedding_store=store),
        repo_path=str(tmp_path),
        dense_top_k=1,
        graph_neighbors_per_seed=1,
        final_top_k=2,
    )

    result = pipeline.query("query")

    assert [node.node_id for node in result] == [
        "src/a.py:neighbor",
        "src/a.py:seed",
    ]


def test_pipeline_fuses_semantic_and_graph_rankings_by_file():
    dense = [
        QueriedNode(
            node_name="seed",
            type="function",
            file="/cache/repo/src/a.py",
            node_id="src/a.py:seed",
            score=1.0,
        ),
        QueriedNode(
            node_name="other",
            type="function",
            file="/cache/repo/src/b.py",
            node_id="src/b.py:other",
            score=0.9,
        ),
    ]
    graph = _FakeGraph(
        resolve={"src/a.py:seed": "seed"},
        successors={"seed": [1]},
        info={
            1: {
                "name": "neighbor",
                "unified_name": "src/c.py:neighbor",
                "type": "function",
                "file": "src/c.py",
                "start_line": 1,
                "end_line": 2,
            }
        },
    )

    class FakeIndexedStore:
        def search_within_ids(self, query, mask_node_ids, top_k, level):
            return [
                NodeInfo(**dense[0].model_dump()),
                NodeInfo(**dense[1].model_dump()),
                NodeInfo(
                    node_name="neighbor",
                    node_id="src/c.py:neighbor",
                    file="src/c.py",
                    score=0.1,
                ),
            ][:top_k]

    pipeline = DenseGraphExpandRerankPipeline(
        retriever=_FakeRetriever(dense),
        code_graph=graph,
        rerank_context=RerankContext(
            embedding_store=FakeIndexedStore(), embedding_source="index"
        ),
        dense_top_k=2,
        graph_seed_top_k=1,
        graph_neighbors_per_seed=1,
        graph_fusion_weight=1.0,
        final_top_k=3,
        include_graph_content=False,
    )

    result = pipeline.query("query")

    assert [node.file for node in result] == [
        "src/c.py",
        "/cache/repo/src/a.py",
        "/cache/repo/src/b.py",
    ]


def test_pipeline_scopes_query_embedding_reuse_to_one_composed_request():
    dense = QueriedNode(
        node_name="seed",
        node_id="src/a.py:seed",
        file="src/a.py",
        score=1.0,
    )

    class ScopedStore:
        active = False
        entries = 0

        @contextmanager
        def reuse_query_embedding(self):
            assert not self.active
            self.active = True
            self.entries += 1
            try:
                yield
            finally:
                self.active = False

        def search_within_ids(self, query, mask_node_ids, top_k, level):
            assert self.active
            return [NodeInfo(**dense.model_dump())]

    store = ScopedStore()

    class ScopedRetriever(_FakeRetriever):
        vector_store = store

        def query(self, query, top_k=None):
            assert store.active
            return super().query(query, top_k=top_k)

    pipeline = DenseGraphExpandRerankPipeline(
        retriever=ScopedRetriever([dense]),
        rerank_context=RerankContext(
            embedding_store=store,
            embedding_source="index",
        ),
        graph_neighbors_per_seed=0,
        final_top_k=1,
    )

    result = pipeline.query("query")

    assert [node.node_id for node in result] == ["src/a.py:seed"]
    assert store.entries == 1
    assert store.active is False


def test_old_graph_augmented_name_remains_compatible():
    assert issubclass(GraphAugmentedRerankPipeline, DenseGraphExpandRerankPipeline)


def test_model_exports_new_dense_graph_name():
    assert ExportedDenseGraphExpandRerankPipeline is DenseGraphExpandRerankPipeline
