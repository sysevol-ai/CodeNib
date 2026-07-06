# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations


def make_fake_graph_builder(calls):
    def fake_build_graph_for_languages(
        project_root,
        output_dir,
        *,
        languages=None,
        project_name=None,
        skip_level=None,
        profiler=None,
        decoder_backend=None,
        graph_route="active",
    ):
        calls.append(
            {
                "project_root": project_root,
                "output_dir": output_dir,
                "languages": languages,
                "project_name": project_name,
                "skip_level": skip_level,
                "profiler": profiler,
                "decoder_backend": decoder_backend,
                "graph_route": graph_route,
            }
        )
        return {
            "project_root": project_root,
            "output_dir": output_dir,
            "languages": list(languages or []),
        }

    return fake_build_graph_for_languages


class FakeBM25CodeIndexer:
    calls = []

    def __init__(self, *, max_k, language):
        self.max_k = max_k
        self.language = language
        self.graph = None
        self.__class__.calls.append(self)

    def build_index_from_graph(self, graph):
        self.graph = graph


def test_bm25_pipeline_routes_requested_language_to_ls_indexer(monkeypatch, tmp_path):
    from codeminer.model import bm25_retrieve_pipeline as pipeline_module

    graph_calls = []
    FakeBM25CodeIndexer.calls = []
    monkeypatch.setattr(
        pipeline_module,
        "build_graph_for_languages",
        make_fake_graph_builder(graph_calls),
    )
    monkeypatch.setattr(pipeline_module, "BM25CodeIndexer", FakeBM25CodeIndexer)

    repo_path = str(tmp_path / "repo")
    index_path = str(tmp_path / "index")
    pipeline = pipeline_module.BM25RetrievePipeline(
        repo_path=repo_path,
        index_path=index_path,
        top_k=17,
        project_name="repo__case",
        language="go",
    )

    assert graph_calls == [
        {
            "project_root": repo_path,
            "output_dir": index_path,
            "languages": ["go"],
            "project_name": "repo__case",
            "skip_level": "graph",
            "profiler": None,
            "decoder_backend": None,
            "graph_route": "active",
        }
    ]

    [bm25] = FakeBM25CodeIndexer.calls
    assert bm25.max_k == 17
    assert bm25.graph == pipeline.code_graph


def test_graph_pipeline_routes_languages_to_graph_builder_and_rerank(
    monkeypatch, tmp_path
):
    from codeminer.model import graph_retrieve_pipeline as pipeline_module

    graph_calls = []
    FakeBM25CodeIndexer.calls = []
    vector_calls = []

    def fake_build_vector_store(**kwargs):
        vector_calls.append(kwargs)
        return object()

    monkeypatch.setattr(
        pipeline_module,
        "build_graph_for_languages",
        make_fake_graph_builder(graph_calls),
    )
    monkeypatch.setattr(pipeline_module, "BM25CodeIndexer", FakeBM25CodeIndexer)
    monkeypatch.setattr(
        pipeline_module,
        "build_hierarchical_vector_store",
        fake_build_vector_store,
    )

    repo_path = str(tmp_path / "repo")
    index_path = str(tmp_path / "index")
    pipeline = pipeline_module.GraphRetrievePipeline(
        repo_path=repo_path,
        index_path=index_path,
        stage1_topk=11,
        use_embedding_rerank=True,
        languages=["rust", "python"],
        graph_route="scip-candidate",
        project_name="repo__case",
    )

    assert graph_calls == [
        {
            "project_root": repo_path,
            "output_dir": index_path,
            "languages": ["rust", "python"],
            "project_name": "repo__case",
            "skip_level": "graph",
            "profiler": None,
            "decoder_backend": None,
            "graph_route": "scip-candidate",
        }
    ]

    [bm25] = FakeBM25CodeIndexer.calls
    assert bm25.max_k == 11
    assert bm25.graph == pipeline.code_graph

    [vector_call] = vector_calls
    assert vector_call["languages"] == ["rust", "python"]


def test_graph_retrieve_pipeline_name_is_sparse_seeded_alias():
    from codeminer.model import graph_retrieve_pipeline as pipeline_module

    assert issubclass(
        pipeline_module.GraphRetrievePipeline,
        pipeline_module.SparseSeededGraphRetrievePipeline,
    )
