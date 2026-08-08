# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import pickle
from pathlib import Path
from types import SimpleNamespace

import pytest

from codenib.artifacts import normalize_owned_query_view
from codenib.compiler.artifact_fingerprints import bm25_artifact_file_fingerprints
from codenib.index.embedding.artifact_integrity import (
    VECTOR_PERSISTENCE_SCHEMA,
    vector_config_artifact_record,
    vector_level_artifact_records,
)

_VECTOR_CONFIG = {
    "builder_schema": 2,
    "embedding_model": "test/model",
    "embedding_provider": "huggingface",
    "embedding_dimension": 4,
    "dimension": 4,
    "embedding_kwargs": {},
    "index_metric": "ip",
}
_MODEL_SUFFIX = "test__model"


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _repository(root: Path) -> tuple[Path, Path]:
    repo = root / "repo"
    repo.mkdir()
    source = repo / "sample.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    return repo, source


def _vector_view(
    root: Path,
    repo: Path,
    *,
    document_format: str,
    document_bytes: bytes | None = None,
) -> Path:
    vector = root / "state" / "vector"
    level = vector / "l2"
    level.mkdir(parents=True)
    documents_path = level / f"documents_{_MODEL_SUFFIX}.{document_format}"
    if document_bytes is not None:
        documents_path.write_bytes(document_bytes)
    elif document_format == "pkl":
        with documents_path.open("wb") as handle:
            pickle.dump(
                [
                    SimpleNamespace(
                        page_content="VALUE = 1",
                        metadata={
                            "file": str(repo / "sample.py"),
                            "node_id": "sample.py",
                        },
                    )
                ],
                handle,
            )
    else:
        documents_path.write_text(
            json.dumps(
                [
                    {
                        "page_content": "VALUE = 1",
                        "metadata": {"file": "sample.py", "node_id": "sample.py"},
                    }
                ]
            ),
            encoding="utf-8",
        )
    (level / f"index_{_MODEL_SUFFIX}.faiss").write_bytes(b"serving-index")
    config = {
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
                _MODEL_SUFFIX,
                documents_file=documents_path.name,
            )
        },
    }
    (vector / f"config_{_MODEL_SUFFIX}.json").write_text(
        json.dumps(config),
        encoding="utf-8",
    )
    return vector


def test_normalize_bm25_rewrites_project_root_and_refreshes_fingerprints(
    tmp_path: Path,
) -> None:
    repo, _source = _repository(tmp_path)
    bm25 = tmp_path / "state" / "bm25"
    bm25.mkdir(parents=True)
    (bm25 / "documents.json").write_text("[]\n", encoding="utf-8")
    (bm25 / "bm25_metadata.json").write_text(
        json.dumps({"project_root": str(repo), "max_k": 128}),
        encoding="utf-8",
    )

    adjustments = normalize_owned_query_view(
        bm25,
        repo_path=repo,
        view_type="bm25",
        view_config={},
    )

    metadata = json.loads((bm25 / "bm25_metadata.json").read_text(encoding="utf-8"))
    assert metadata["project_root"] == "source"
    assert adjustments == {
        "artifact_file_fingerprints": bm25_artifact_file_fingerprints(bm25)
    }


def test_normalize_vector_converts_trusted_pickle_and_strips_build_state(
    tmp_path: Path,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="pkl")
    level = vector / "l2"
    (level / f"index_{_MODEL_SUFFIX}.pkl").write_bytes(b"legacy-docstore")
    for name in (
        "chunk_store.json",
        "chunk_store.pkl",
        "embeddings_cache.json",
        "embeddings_cache.npz",
        "embeddings_cache.pkl",
        "incremental_state.json",
    ):
        (vector / name).write_bytes(b"machine-local")

    adjustments = normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=_VECTOR_CONFIG,
    )

    documents_path = level / f"documents_{_MODEL_SUFFIX}.json"
    documents = json.loads(documents_path.read_text(encoding="utf-8"))
    assert documents[0]["metadata"]["file"] == "sample.py"
    assert not list(vector.rglob("*.pkl"))
    assert not any(
        path.name.startswith(
            ("chunk_store.", "embeddings_cache.", "incremental_state.")
        )
        for path in vector.rglob("*")
    )
    config = json.loads(
        (vector / f"config_{_MODEL_SUFFIX}.json").read_text(encoding="utf-8")
    )
    assert config["level_artifacts"]["l2"]["documents"]["file"] == (documents_path.name)
    assert adjustments["persistence_config_fingerprint"] == (
        vector_config_artifact_record(vector, _MODEL_SUFFIX)
    )


def test_normalize_committed_json_vector_is_idempotent(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")

    first_adjustments = normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=_VECTOR_CONFIG,
    )
    first_tree = _tree(vector)
    second_adjustments = normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=_VECTOR_CONFIG,
    )

    assert _tree(vector) == first_tree
    assert second_adjustments == first_adjustments


def test_normalize_vector_rejects_mixed_document_formats(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="pkl")
    json_path = vector / "l2" / f"documents_{_MODEL_SUFFIX}.json"
    json_path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mixes pickle and JSON"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )

    assert json_path.is_file()
    assert (vector / "l2" / f"documents_{_MODEL_SUFFIX}.pkl").is_file()


def test_normalize_vector_rejects_missing_committed_documents(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    documents = vector / "l2" / f"documents_{_MODEL_SUFFIX}.json"
    documents.unlink()

    with pytest.raises(
        ValueError,
        match="invalid vector artifact file|committed documents|missing documents",
    ):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


def test_normalize_vector_rejects_malformed_committed_json(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(
        tmp_path,
        repo,
        document_format="json",
        document_bytes=b'[{"page_content": "unterminated"',
    )

    with pytest.raises(ValueError, match="invalid JSON"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


def test_normalize_vector_rejects_residual_pickle(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    residual = vector / "unexamined.pkl"
    residual.write_bytes(b"must-not-publish")

    with pytest.raises(ValueError, match="unexpected pickle"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )

    assert residual.is_file()


def test_normalize_vector_rejects_unowned_mutable_state(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    residual = vector / "l2" / "embeddings_cache.json"
    residual.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected mutable state"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )

    assert residual.is_file()


@pytest.mark.parametrize("source_path", ["../outside.py", "/outside/sample.py"])
def test_normalize_vector_rejects_document_paths_outside_repository(
    tmp_path: Path,
    source_path: str,
) -> None:
    repo, _source = _repository(tmp_path)
    payload = json.dumps(
        [
            {
                "page_content": "VALUE = 1",
                "metadata": {"file": source_path},
            }
        ]
    ).encode("utf-8")
    vector = _vector_view(
        tmp_path,
        repo,
        document_format="json",
        document_bytes=payload,
    )

    with pytest.raises(ValueError, match="outside the repository|repository-relative"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


def test_normalize_owned_view_rejects_repository_overlap(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    view = repo / "copied-view"
    view.mkdir()

    with pytest.raises(ValueError, match="must not overlap"):
        normalize_owned_query_view(
            view,
            repo_path=repo,
            view_type="bm25",
            view_config={},
        )
