# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.rebuild_demo_indexes as rebuild_demo_indexes
from codenib.compiler.manifest import MANIFEST_FILENAME, IndexEntry, RepoManifest
from scripts.build_qa_index import _vector_builder
from scripts.rebuild_demo_indexes import (
    _builders,
    _parse_repo_ignore_dirs,
    _rebuild_one,
    _select_registry_bundles,
    _validate_repo_ignore_dirs,
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


def test_demo_rebuild_rejects_dependency_exclusions_for_unknown_repositories():
    with pytest.raises(
        ValueError,
        match="Unknown repository ids in --ignore-dir: owner__missing",
    ):
        _validate_repo_ignore_dirs(
            {"owner__known": ["vendor"], "owner__missing": ["generated"]},
            {"owner__known"},
        )


def test_demo_rebuild_validates_selectors_before_applying_the_limit():
    bundles = {
        "owner__a": SimpleNamespace(entry=SimpleNamespace(instance_id="owner__a")),
        "owner__b": SimpleNamespace(entry=SimpleNamespace(instance_id="owner__b")),
    }
    registry = SimpleNamespace(
        list_infos=lambda: [SimpleNamespace(id=repo_id) for repo_id in bundles],
        get=bundles.get,
    )

    selected = _select_registry_bundles(
        registry,
        ["owner__a", "owner__b"],
        max_repos=1,
    )

    assert [bundle.entry.instance_id for bundle in selected] == ["owner__a"]
    with pytest.raises(ValueError, match="Unknown repository ids.*owner__missing"):
        _select_registry_bundles(
            registry,
            ["owner__a", "owner__missing"],
            max_repos=1,
        )


def test_demo_rebuild_resets_staging_manifest_from_current_target(
    tmp_path,
    monkeypatch,
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target = tmp_path / "published" / MANIFEST_FILENAME
    source_fingerprint = "sha256-v2:" + ("a" * 64)
    current = RepoManifest(
        repo_path=str(repo_root),
        commit="current-commit",
        source_fingerprint=source_fingerprint,
        languages=["python"],
        capabilities={"sparse_search": True},
    )
    current.indexes["bm25"] = IndexEntry(
        index_type="bm25",
        path="/published/bm25",
        built_at="",
        built_at_epoch=0.0,
        status="fresh",
        commit=current.commit,
        source_fingerprint=source_fingerprint,
    )
    current.save(target)

    output_root = tmp_path / "staging"
    stage_manifest = output_root / "owner__repo" / MANIFEST_FILENAME
    stale = RepoManifest.from_dict(current.to_dict())
    stale.languages.append("rust")
    stale.capabilities["dense_search"] = True
    stale.indexes["vector"] = IndexEntry(
        index_type="vector",
        path="/stale/vector",
        built_at="",
        built_at_epoch=0.0,
        status="fresh",
        commit=stale.commit,
        source_fingerprint=source_fingerprint,
    )
    stale.save(stage_manifest)
    observed = []

    class StopAfterStageCheck(RuntimeError):
        pass

    class FakeCompiler:
        def __init__(self, *_args, **_kwargs):
            pass

        def compile_repo(self, _repo_path, *, cache_dir, **_kwargs):
            observed.append(RepoManifest.load(Path(cache_dir) / MANIFEST_FILENAME))
            raise StopAfterStageCheck

    monkeypatch.setattr(rebuild_demo_indexes, "IndexCompiler", FakeCompiler)
    monkeypatch.setattr(rebuild_demo_indexes, "_builders", lambda *_a, **_k: object())
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            instance_id="owner__repo",
            manifest_path=str(target),
            repo_dir=str(repo_root),
            base_commit=current.commit,
        )
    )

    with pytest.raises(StopAfterStageCheck):
        _rebuild_one(
            config=SimpleNamespace(),
            bundle=bundle,
            output_root=output_root,
            dry_run=False,
            min_graph_nodes=0,
            additional_chunk_ignore_dirs=[],
        )

    assert observed[0].to_dict() == current.to_dict()
