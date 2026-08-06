# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import os
import pickle
from pathlib import Path
from types import SimpleNamespace

import pytest

from codenib.artifacts import (
    CONTEXT_ARTIFACT_MANIFEST,
    CONTEXT_ARTIFACT_SCHEMA,
    stage_context_artifact,
)
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.index.embedding.artifact_integrity import (
    VECTOR_PERSISTENCE_SCHEMA,
    vector_config_artifact_record,
    vector_level_artifact_records,
)
from codenib.source_fingerprint import fingerprint_repository


def _fixture_manifest(
    root: Path,
    *,
    view_path: Path | None = None,
    config: dict | None = None,
    status: str = "fresh",
) -> tuple[Path, Path, Path]:
    repo = root / "repo"
    repo.mkdir(parents=True)
    (repo / "sample.py").write_text("VALUE = 1\n")
    index_root = root / "state" / "indexes"
    index_root.mkdir(parents=True)
    if view_path is None:
        view_path = index_root / "bm25"
        view_path.mkdir()
        (view_path / "documents.json").write_text(
            '[{"page_content": "value", "metadata": {"file": "sample.py"}}]\n'
        )
        (view_path / "bm25_metadata.json").write_text(
            json.dumps(
                {
                    "project_root": str(repo),
                    "max_k": 128,
                    "language": "english",
                }
            )
            + "\n"
        )
    manifest_path = index_root / "repo_manifest.json"
    source_fingerprint = fingerprint_repository(repo).value
    RepoManifest(
        repo_path=str(repo),
        commit="a" * 40,
        last_indexed_commit="a" * 40,
        source_fingerprint=source_fingerprint,
        last_indexed_source_fingerprint=source_fingerprint,
        languages=["python"],
        file_count=1,
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(view_path),
                built_at="2026-08-04T00:00:00+00:00",
                built_at_epoch=1.0,
                status=status,
                config=dict(config or {}),
                commit="a" * 40,
                source_fingerprint=source_fingerprint,
            )
        },
        capabilities={"sparse_search": status == "fresh"},
        compiled_at="2026-08-04T00:00:00+00:00",
        compiled_at_epoch=1.0,
    ).save(manifest_path)
    return repo, manifest_path, view_path


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _fixture_vector_manifest(root: Path) -> tuple[Path, Path, Path]:
    repo = root / "repo"
    repo.mkdir(parents=True)
    source = repo / "sample.py"
    source.write_text("VALUE = 1\n")
    index_root = root / "state" / "indexes"
    vector = index_root / "vector"
    level = vector / "l2"
    level.mkdir(parents=True)
    with (level / "documents_test__model.pkl").open("wb") as handle:
        pickle.dump(
            [
                SimpleNamespace(
                    page_content="VALUE = 1",
                    metadata={"file": str(source), "node_id": "sample.py"},
                )
            ],
            handle,
        )
    (level / "index_test__model.faiss").write_bytes(b"serving-index")
    (level / "index_test__model.pkl").write_bytes(
        pickle.dumps({"legacy_path": str(source)})
    )
    (vector / "chunk_store.json").write_text(
        json.dumps({str(source): [{"file": str(source)}]})
    )
    (vector / "embeddings_cache.npz").write_bytes(b"mutable-cache")
    (vector / "incremental_state.json").write_text(
        json.dumps({"index_path": str(vector)})
    )
    (vector / "config_test__model.json").write_text(
        json.dumps(
            {
                "embedding_model": "test/model",
                "embedding_provider": "huggingface",
                "dimension": 4,
                "index_type": "flat",
                "index_metric": "ip",
                "l0_documents": 0,
                "l2_documents": 1,
                "persistence_schema": VECTOR_PERSISTENCE_SCHEMA,
                "level_artifacts": {
                    "l2": vector_level_artifact_records(
                        level,
                        "test__model",
                        documents_file="documents_test__model.pkl",
                    )
                },
            }
        )
    )
    config = {
        "builder_schema": 2,
        "embedding_model": "test/model",
        "embedding_provider": "huggingface",
        "embedding_dimension": 4,
        "dimension": 4,
        "embedding_kwargs": {},
        "index_metric": "ip",
        "persistence_config_fingerprint": vector_config_artifact_record(
            vector,
            "test__model",
        ),
    }
    manifest_path = index_root / "repo_manifest.json"
    source_fingerprint = fingerprint_repository(repo).value
    RepoManifest(
        repo_path=str(repo),
        commit="b" * 40,
        last_indexed_commit="b" * 40,
        source_fingerprint=source_fingerprint,
        last_indexed_source_fingerprint=source_fingerprint,
        languages=["python"],
        file_count=1,
        indexes={
            "vector": IndexEntry(
                index_type="vector",
                path=str(vector),
                built_at="2026-08-04T00:00:00+00:00",
                built_at_epoch=1.0,
                status="fresh",
                config=config,
                metadata=dict(config),
                commit="b" * 40,
                source_fingerprint=source_fingerprint,
            )
        },
        capabilities={"dense_search": True},
        compiled_at="2026-08-04T00:00:00+00:00",
        compiled_at_epoch=1.0,
    ).save(manifest_path)
    return repo, manifest_path, vector


def test_context_artifact_rewrites_paths_and_hashes_current_views(
    tmp_path: Path,
) -> None:
    repo, manifest_path, _view_path = _fixture_manifest(tmp_path)
    output = tmp_path / "publish" / "context"

    result = stage_context_artifact(
        repo,
        manifest_path,
        output,
        repository="Example/Project",
        views=["bm25"],
        environ={"GITHUB_TOKEN": "configured-secret-value"},
    )

    assert result.repository == "example/project"
    assert result.commit == "a" * 40
    assert result.views == ("bm25",)
    metadata = json.loads((output / CONTEXT_ARTIFACT_MANIFEST).read_text())
    assert metadata["schema"] == CONTEXT_ARTIFACT_SCHEMA
    assert metadata["repository"]["slug"] == "example/project"
    portable = json.loads((output / "repo_manifest.json").read_text())
    assert portable["repo"]["path"] == "source"
    assert portable["indexes"]["bm25"]["path"] == "views/bm25"
    assert portable["capabilities"]["sparse_search"] is True
    bm25_metadata = json.loads(
        (output / "views" / "bm25" / "bm25_metadata.json").read_text()
    )
    assert bm25_metadata["project_root"] == "source"

    files = {item["path"]: item for item in metadata["files"]}
    assert set(files) == {
        "repo_manifest.json",
        "views/bm25/bm25_metadata.json",
        "views/bm25/documents.json",
    }
    for relative, record in files.items():
        payload = (output / relative).read_bytes()
        assert record["bytes"] == len(payload)
        assert record["sha256"] == hashlib.sha256(payload).hexdigest()
    serialized = b"".join(_tree(output).values())
    assert str(repo).encode() not in serialized
    assert str(manifest_path.parent).encode() not in serialized
    assert b"configured-secret-value" not in serialized
    fingerprints = portable["indexes"]["bm25"]["config"]["artifact_file_fingerprints"]
    for relative, record in fingerprints.items():
        payload = (output / "views" / "bm25" / relative).read_bytes()
        assert record["size"] == len(payload)
        assert record["sha256"] == hashlib.sha256(payload).hexdigest()


def test_context_artifact_is_deterministic_for_one_manifest(tmp_path: Path) -> None:
    repo, manifest_path, _view_path = _fixture_manifest(tmp_path)

    stage_context_artifact(
        repo,
        manifest_path,
        tmp_path / "first" / "context",
        repository="example/project",
    )
    stage_context_artifact(
        repo,
        manifest_path,
        tmp_path / "second" / "context",
        repository="example/project",
    )

    assert _tree(tmp_path / "first" / "context") == _tree(
        tmp_path / "second" / "context"
    )


def test_context_artifact_restores_previous_output_on_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, manifest_path, view_path = _fixture_manifest(tmp_path)
    output = tmp_path / "publish" / "context"
    stage_context_artifact(
        repo,
        manifest_path,
        output,
        repository="example/project",
    )
    previous = _tree(output)
    (view_path / "documents.json").write_text("[]\n", encoding="utf-8")
    real_replace = os.replace

    def fail_final_publish(source, target):
        source_path = Path(source)
        if source_path.name.startswith(".context.tmp-") and Path(target) == output:
            raise OSError("injected context publish failure")
        return real_replace(source, target)

    monkeypatch.setattr("codenib._atomic_directory.os.replace", fail_final_publish)

    with pytest.raises(OSError, match="injected context publish failure"):
        stage_context_artifact(
            repo,
            manifest_path,
            output,
            repository="example/project",
        )

    assert _tree(output) == previous
    assert not list(output.parent.glob(".context.tmp-*"))
    assert not list(output.parent.glob(".context.previous-*"))


def test_context_artifact_default_ignores_nonportable_current_views(
    tmp_path: Path,
) -> None:
    repo, manifest_path, _view_path = _fixture_manifest(tmp_path)
    graph = manifest_path.parent / "symbol_graph"
    graph.mkdir()
    (graph / "graph.bin").write_bytes(b"nonportable graph")
    manifest = RepoManifest.load(manifest_path)
    manifest.indexes["symbol_graph"] = IndexEntry(
        index_type="symbol_graph",
        path=str(graph),
        built_at="2026-08-04T00:00:00+00:00",
        built_at_epoch=1.0,
        status="fresh",
        commit=manifest.commit,
        source_fingerprint=manifest.source_fingerprint,
    )
    manifest.save(manifest_path)

    result = stage_context_artifact(
        repo,
        manifest_path,
        tmp_path / "publish" / "context",
        repository="example/project",
    )

    assert result.views == ("bm25",)
    portable = RepoManifest.load(result.manifest_path)
    assert set(portable.indexes) == {"bm25"}


def test_context_artifact_keeps_only_portable_vector_serving_state(
    tmp_path: Path,
) -> None:
    repo, manifest_path, _vector = _fixture_vector_manifest(tmp_path)
    output = tmp_path / "publish" / "context"

    stage_context_artifact(
        repo,
        manifest_path,
        output,
        repository="example/vector-project",
    )

    vector = output / "views" / "vector"
    assert not (vector / "chunk_store.json").exists()
    assert not (vector / "embeddings_cache.npz").exists()
    assert not (vector / "incremental_state.json").exists()
    assert not (vector / "l2" / "index_test__model.pkl").exists()
    assert (vector / "l2" / "index_test__model.faiss").is_file()
    assert not (vector / "l2" / "documents_test__model.pkl").exists()
    documents = json.loads((vector / "l2" / "documents_test__model.json").read_text())
    assert documents[0]["metadata"]["file"] == "sample.py"
    vector_config = json.loads((vector / "config_test__model.json").read_text())
    committed_documents = vector_config["level_artifacts"]["l2"]["documents"]
    assert committed_documents["file"] == "documents_test__model.json"
    payload = (vector / "l2" / committed_documents["file"]).read_bytes()
    assert committed_documents["size"] == len(payload)
    assert committed_documents["sha256"] == hashlib.sha256(payload).hexdigest()
    portable = json.loads((output / "repo_manifest.json").read_text())
    assert portable["indexes"]["vector"]["config"]["artifact_scope"] == (
        "query-serving"
    )
    assert (
        portable["indexes"]["vector"]["config"]["portable_document_format"]
        == "codenib.vector-documents.v1"
    )
    vector_config = portable["indexes"]["vector"]["config"]
    assert vector_config["persistence_config_fingerprint"] == (
        vector_config_artifact_record(output / "views" / "vector", "test__model")
    )
    assert not list(output.rglob("*.pkl"))
    assert str(repo).encode() not in b"".join(_tree(output).values())


def test_context_artifact_rejects_interrupted_vector_save(tmp_path: Path) -> None:
    repo, manifest_path, vector = _fixture_vector_manifest(tmp_path)
    (vector / ".config_test__model.json.save-in-progress").write_text("{}")

    with pytest.raises(ValueError, match="interrupted save marker"):
        stage_context_artifact(
            repo,
            manifest_path,
            tmp_path / "publish" / "context",
            repository="example/vector-project",
        )


def test_context_artifact_rejects_unpublished_vector_generation(tmp_path: Path) -> None:
    repo, manifest_path, vector = _fixture_vector_manifest(tmp_path)
    config_path = vector / "config_test__model.json"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest fingerprint"):
        stage_context_artifact(
            repo,
            manifest_path,
            tmp_path / "publish" / "context",
            repository="example/vector-project",
        )


def test_context_artifact_rejects_tampered_vector_level(tmp_path: Path) -> None:
    repo, manifest_path, vector = _fixture_vector_manifest(tmp_path)
    documents = vector / "l2" / "documents_test__model.pkl"
    documents.write_bytes(documents.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="committed documents vector artifact"):
        stage_context_artifact(
            repo,
            manifest_path,
            tmp_path / "publish" / "context",
            repository="example/vector-project",
        )


def test_context_artifact_rejects_credential_shaped_config(tmp_path: Path) -> None:
    repo, manifest_path, _view_path = _fixture_manifest(
        tmp_path,
        config={"api_key": "must-not-persist"},
    )

    with pytest.raises(ValueError, match="credential field"):
        stage_context_artifact(
            repo,
            manifest_path,
            tmp_path / "publish" / "context",
        )


def test_context_artifact_rejects_credential_shaped_metadata(tmp_path: Path) -> None:
    repo, manifest_path, _view_path = _fixture_manifest(tmp_path)
    manifest = RepoManifest.load(manifest_path)
    manifest.indexes["bm25"].metadata["authorization"] = "must-not-persist"
    manifest.save(manifest_path)

    with pytest.raises(ValueError, match="credential field"):
        stage_context_artifact(
            repo,
            manifest_path,
            tmp_path / "publish" / "context",
        )


def test_context_artifact_rejects_source_drift(tmp_path: Path) -> None:
    repo, manifest_path, _view_path = _fixture_manifest(tmp_path)
    (repo / "sample.py").write_text("VALUE = 2\n")

    with pytest.raises(ValueError, match="source files do not match"):
        stage_context_artifact(
            repo,
            manifest_path,
            tmp_path / "publish" / "context",
        )


def test_context_artifact_rejects_configured_secret_in_view(tmp_path: Path) -> None:
    repo, manifest_path, view_path = _fixture_manifest(tmp_path)
    (view_path / "leak.bin").write_bytes(b"runtime-secret-value")

    with pytest.raises(ValueError, match="configured credential"):
        stage_context_artifact(
            repo,
            manifest_path,
            tmp_path / "publish" / "context",
            environ={"MODELS_TOKEN": "runtime-secret-value"},
        )


def test_context_artifact_stream_scan_finds_boundary_spanning_secret(
    tmp_path: Path,
) -> None:
    repo, manifest_path, view_path = _fixture_manifest(tmp_path)
    secret = b"boundary-spanning-secret"
    (view_path / "large.bin").write_bytes(b"x" * (1024 * 1024 - 5) + secret)

    with pytest.raises(ValueError, match="configured credential"):
        stage_context_artifact(
            repo,
            manifest_path,
            tmp_path / "publish" / "context",
            environ={"CODENIB_ACTION_EMBEDDING_KEY": secret.decode()},
        )


def test_context_artifact_rejects_view_outside_manifest_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "documents.json").write_text("[]\n")
    (outside / "bm25_metadata.json").write_text("{}\n")
    repo, manifest_path, _view_path = _fixture_manifest(
        tmp_path,
        view_path=outside,
    )

    with pytest.raises(ValueError, match="outside the manifest index root"):
        stage_context_artifact(
            repo,
            manifest_path,
            tmp_path / "publish" / "context",
        )


def test_context_artifact_rejects_stale_or_linked_views(tmp_path: Path) -> None:
    stale_root = tmp_path / "stale"
    repo, manifest_path, _view_path = _fixture_manifest(stale_root, status="stale")
    with pytest.raises(ValueError, match="requires at least one current view"):
        stage_context_artifact(
            repo,
            manifest_path,
            stale_root / "publish" / "context",
        )

    linked_root = tmp_path / "linked"
    repo, manifest_path, view_path = _fixture_manifest(linked_root)
    (view_path / "alias").symlink_to(view_path / "index.json")
    with pytest.raises(ValueError, match="symbolic link"):
        stage_context_artifact(
            repo,
            manifest_path,
            linked_root / "publish" / "context",
        )
