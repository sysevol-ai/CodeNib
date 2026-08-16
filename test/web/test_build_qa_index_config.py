# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest

from codenib.compiler.manifest import IndexEntry, RepoManifest
from scripts.build_qa_index import _vector_builder
from scripts.rebuild_demo_indexes import (
    _builders,
    _parse_repo_ignore_dirs,
    _validated_graph_node_count,
)


def test_demo_index_builder_uses_configured_remote_embedding_route():
    builder = _vector_builder(
        SimpleNamespace(
            embedding_model="Qwen/Qwen3-Embedding-0.6B",
            embedding_provider="openai",
            embedding_dimension=1024,
            embedding_base_url="http://127.0.0.1:8081/v1",
            embedding_api_key="secret-runtime-only",
        ),
        ["rust"],
    )

    identity = builder.artifact_identity()

    assert identity["embedding_provider"] == "openai"
    assert identity["embedding_endpoint"] == "http://127.0.0.1:8081/v1"
    assert identity["embedding_dimension"] == 1024
    assert builder.embedding_runtime_kwargs == {"api_key": "secret-runtime-only"}
    assert "secret-runtime-only" not in str(identity)


def test_demo_rebuild_uses_same_secret_free_embedding_identity():
    config = SimpleNamespace(
        embedding_model="Qwen/Qwen3-Embedding-0.6B",
        embedding_provider="openai",
        embedding_dimension=1024,
        embedding_base_url="http://127.0.0.1:8081/v1",
        embedding_api_key="secret-runtime-only",
    )
    manifest = RepoManifest(languages=["rust"])
    manifest.indexes["vector"] = IndexEntry(
        index_type="vector",
        path="/old/vector",
        built_at="",
        built_at_epoch=0.0,
        status="fresh",
    )
    manifest.indexes["symbol_graph"] = IndexEntry(
        index_type="symbol_graph",
        path="/old/graph",
        built_at="",
        built_at_epoch=0.0,
        status="fresh",
        config={"language": "rust", "languages": ["rust"]},
    )

    builder = _builders(config, manifest).get("vector")
    identity = builder.artifact_identity()

    assert identity["embedding_provider"] == "openai"
    assert identity["embedding_endpoint"] == "http://127.0.0.1:8081/v1"
    assert builder.embedding_runtime_kwargs == {"api_key": "secret-runtime-only"}
    assert "secret-runtime-only" not in str(identity)
    graph_builder = _builders(config, manifest).get("symbol_graph")
    assert graph_builder.languages == ["rust"]
    assert graph_builder.allow_project_preparation is False


def test_demo_rebuild_rejects_successful_but_pathologically_small_graph(
    tmp_path, monkeypatch
):
    loaded = SimpleNamespace(
        get_graph=lambda: SimpleNamespace(vcount=lambda: 12),
    )
    monkeypatch.setattr(
        "codenib.graph.code_graph.CodeGraph.load_graph",
        lambda _path: loaded,
    )

    with pytest.raises(RuntimeError, match="only 12 nodes.*requires 25"):
        _validated_graph_node_count(str(tmp_path), 25)

    assert _validated_graph_node_count(str(tmp_path), 0) == 12


def test_demo_rebuild_records_explicit_repository_dependency_exclusions():
    config = SimpleNamespace(
        embedding_model="Qwen/Qwen3-Embedding-0.6B",
        embedding_provider="openai",
        embedding_dimension=1024,
        embedding_base_url="http://127.0.0.1:8081/v1",
        embedding_api_key="runtime-only",
    )
    manifest = RepoManifest(languages=["cpp"])

    builders = _builders(
        config,
        manifest,
        additional_chunk_ignore_dirs=["lib"],
    )

    assert builders.get("bm25").artifact_identity()["additional_ignore_dirs"] == ["lib"]
    assert builders.get("vector").artifact_identity()["additional_ignore_dirs"] == [
        "lib"
    ]
    assert _parse_repo_ignore_dirs(
        ["micropython__micropython:lib", "micropython__micropython:lib"]
    ) == {"micropython__micropython": ["lib"]}


@pytest.mark.parametrize("value", ["missing-separator", "repo:../lib", "repo:"])
def test_demo_rebuild_rejects_ambiguous_dependency_exclusions(value):
    with pytest.raises(ValueError, match="REPO_ID:DIR_NAME"):
        _parse_repo_ignore_dirs([value])
