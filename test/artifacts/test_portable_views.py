# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib
import json
import os
import pickle
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codenib.artifacts import normalize_owned_query_view, validate_portable_query_view
from codenib.compiler.artifact_fingerprints import bm25_artifact_file_fingerprints
from codenib.index.embedding.artifact_integrity import (
    VECTOR_PERSISTENCE_SCHEMA,
    vector_config_artifact_record,
    vector_level_artifact_records,
)
from codenib.provider_routes import resolve_inference_route

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
_REVISION = "a" * 40


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def _repository(root: Path) -> tuple[Path, Path]:
    repo = root / "repo"
    repo.mkdir(parents=True)
    source = repo / "sample.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    return repo, source


def _bm25_view(
    root: Path,
    *,
    documents: list[dict[str, object]] | None = None,
    metadata: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    repo, _source = _repository(root)
    bm25 = root / "state" / "bm25"
    bm25.mkdir(parents=True)
    (bm25 / "documents.json").write_text(
        json.dumps(documents or []),
        encoding="utf-8",
    )
    (bm25 / "bm25_metadata.json").write_text(
        json.dumps(metadata or {}),
        encoding="utf-8",
    )
    return repo, bm25


def _write_faiss(
    path: Path,
    *,
    count: int = 1,
    dimension: int = 4,
    metric: str = "ip",
    index_type: str = "flat",
    trained: bool = True,
) -> None:
    faiss = importlib.import_module("faiss")
    numpy = importlib.import_module("numpy")
    if metric == "ip":
        quantizer = faiss.IndexFlatIP(dimension)
        metric_type = faiss.METRIC_INNER_PRODUCT
    else:
        quantizer = faiss.IndexFlatL2(dimension)
        metric_type = faiss.METRIC_L2
    if index_type == "ivf":
        index = faiss.IndexIVFFlat(quantizer, dimension, max(1, count), metric_type)
    else:
        index = quantizer
    if count:
        vectors = numpy.arange(count * dimension, dtype="float32").reshape(
            count, dimension
        )
        if index_type == "ivf":
            if trained:
                index.train(vectors)
            else:
                index.ntotal = count
        if index_type != "ivf" or trained:
            index.add(vectors)
    faiss.write_index(index, str(path))


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
    _write_faiss(level / f"index_{_MODEL_SUFFIX}.faiss")
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


def _refresh_level_record(vector: Path, level: str = "l2") -> None:
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    documents = next(
        path
        for path in (
            vector / level / f"documents_{_MODEL_SUFFIX}.json",
            vector / level / f"documents_{_MODEL_SUFFIX}.pkl",
        )
        if path.is_file()
    )
    config["level_artifacts"][level] = vector_level_artifact_records(
        vector / level,
        _MODEL_SUFFIX,
        documents_file=documents.name,
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")


def _schema6_identity(
    *,
    metric: str = "ip",
    revision: str = _REVISION,
) -> dict[str, object]:
    options = {"revision": revision, "trust_remote_code": False}
    route = resolve_inference_route(
        operation="embeddings",
        provider="huggingface",
        model="test/model",
        dimension=4,
        compatibility_options=options,
    )
    return {
        "builder_schema": 6,
        "embedding_model": route.model,
        "embedding_provider": route.provider,
        "embedding_dimension": 4,
        "dimension": 4,
        "embedding_endpoint": route.endpoint,
        "embedding_kwargs": route.compatibility_options,
        "embedding_route": route.public_identity(),
        "embedding_fingerprint": route.compatibility_fingerprint,
        "index_metric": metric,
        "levels": ["l2"],
    }


def _production_vector_view(
    root: Path,
    *,
    index_type: str = "flat",
    metric: str = "ip",
) -> tuple[Path, Path, dict[str, object]]:
    repo, _source = _repository(root)
    vector = _vector_view(root, repo, document_format="json")
    level = vector / "l2"
    _write_faiss(
        level / f"index_{_MODEL_SUFFIX}.faiss",
        metric=metric,
        index_type=index_type,
    )
    identity = _schema6_identity(metric=metric)
    (level / f"config_{_MODEL_SUFFIX}.json").write_text(
        json.dumps(
            {
                "embedding_model": "test/model",
                "embedding_provider": "huggingface",
                "embedding_revision": _REVISION,
                "dimension": 4,
                "index_type": index_type,
                "index_metric": metric,
                "level": "l2",
                "num_documents": 1,
            }
        ),
        encoding="utf-8",
    )
    documents = level / f"documents_{_MODEL_SUFFIX}.json"
    config = {
        "embedding_model": "test/model",
        "embedding_provider": "huggingface",
        "embedding_revision": _REVISION,
        "dimension": 4,
        "index_type": index_type,
        "index_metric": metric,
        "l0_documents": 0,
        "l2_documents": 1,
        "persistence_schema": VECTOR_PERSISTENCE_SCHEMA,
        "level_artifacts": {
            "l2": vector_level_artifact_records(
                level,
                _MODEL_SUFFIX,
                documents_file=documents.name,
            )
        },
        "artifact": identity,
    }
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    view_config = {
        **identity,
        "document_count": {"l2": 1},
        "persistence_config_fingerprint": vector_config_artifact_record(
            vector,
            _MODEL_SUFFIX,
        ),
    }
    if index_type != "flat":
        view_config["index_type"] = index_type
    return repo, vector, view_config


def _refresh_view_fingerprint(
    vector: Path,
    view_config: dict[str, object],
) -> None:
    view_config["persistence_config_fingerprint"] = vector_config_artifact_record(
        vector,
        _MODEL_SUFFIX,
    )


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


def test_normalize_bm25_rejects_unicode_escaped_secret_metadata(
    tmp_path: Path,
) -> None:
    repo, bm25 = _bm25_view(tmp_path)
    (bm25 / "documents.json").write_text(
        '[{"page_content":"safe","metadata":{"x\\u002dapi\\u002dkey":"value"}}]',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="secret field"):
        normalize_owned_query_view(
            bm25,
            repo_path=repo,
            view_type="bm25",
            view_config={},
        )


def test_normalize_bm25_does_not_scan_semantic_page_content(tmp_path: Path) -> None:
    repo, bm25 = _bm25_view(
        tmp_path,
        documents=[
            {
                "page_content": "example Authorization: Bearer documentation-token",
                "metadata": {"file": "sample.py"},
            }
        ],
    )

    normalize_owned_query_view(
        bm25,
        repo_path=repo,
        view_type="bm25",
        view_config={},
    )


@pytest.mark.parametrize("extra", ["extra.json", "nested/extra.txt"])
def test_normalize_bm25_rejects_every_extra_entry(
    tmp_path: Path,
    extra: str,
) -> None:
    repo, bm25 = _bm25_view(tmp_path)
    path = bm25 / extra
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected (file|directory)"):
        normalize_owned_query_view(
            bm25,
            repo_path=repo,
            view_type="bm25",
            view_config={},
        )


def test_normalize_bm25_rejects_source_symlink_component(tmp_path: Path) -> None:
    repo, bm25 = _bm25_view(
        tmp_path,
        documents=[
            {
                "page_content": "outside",
                "metadata": {"file": "linked/outside.py"},
            }
        ],
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.py").write_text("OUTSIDE = 1\n", encoding="utf-8")
    (repo / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="stable contained source"):
        normalize_owned_query_view(
            bm25,
            repo_path=repo,
            view_type="bm25",
            view_config={},
        )


def test_portable_view_validator_detects_source_symlink_swap(tmp_path: Path) -> None:
    repo, bm25 = _bm25_view(
        tmp_path,
        documents=[
            {
                "page_content": "VALUE = 1",
                "metadata": {"file": "sample.py"},
            }
        ],
    )
    adjustments = normalize_owned_query_view(
        bm25,
        repo_path=repo,
        view_type="bm25",
        view_config={},
    )
    outside = tmp_path / "outside.py"
    outside.write_text("OUTSIDE = 1\n", encoding="utf-8")
    (repo / "sample.py").unlink()
    (repo / "sample.py").symlink_to(outside)

    with pytest.raises(ValueError, match="stable contained source"):
        validate_portable_query_view(
            bm25,
            repo_path=repo,
            view_type="bm25",
            view_config=adjustments,
        )


@pytest.mark.parametrize("leak", ["path", "secret"])
def test_portable_bm25_validator_rejects_decoded_metadata_without_mutation(
    tmp_path: Path,
    leak: str,
) -> None:
    repo, bm25 = _bm25_view(
        tmp_path,
        documents=[
            {
                "page_content": "VALUE = 1",
                "metadata": {"file": "sample.py"},
            }
        ],
    )
    normalize_owned_query_view(
        bm25,
        repo_path=repo,
        view_type="bm25",
        view_config={},
    )
    documents_path = bm25 / "documents.json"
    documents = json.loads(documents_path.read_text(encoding="utf-8"))
    secret = "decoded-runtime-secret"
    documents[0]["metadata"]["innocent_note"] = str(repo) if leak == "path" else secret
    documents_path.write_bytes(_canonical_json_bytes(documents))
    view_config = {"artifact_file_fingerprints": bm25_artifact_file_fingerprints(bm25)}
    before = _tree(bm25)

    with pytest.raises(ValueError, match="build-machine path|configured credential"):
        validate_portable_query_view(
            bm25,
            repo_path=repo,
            view_type="bm25",
            view_config=view_config,
            environ={"MODELS_TOKEN": secret},
        )

    assert _tree(bm25) == before


def test_portable_bm25_accepts_contained_relative_source_symlink(
    tmp_path: Path,
) -> None:
    repo, bm25 = _bm25_view(
        tmp_path,
        documents=[
            {
                "page_content": "VALUE = 1",
                "metadata": {"file": "alias.py"},
            }
        ],
    )
    (repo / "alias.py").symlink_to("sample.py")

    adjustments = normalize_owned_query_view(
        bm25,
        repo_path=repo,
        view_type="bm25",
        view_config={},
    )
    validate_portable_query_view(
        bm25,
        repo_path=repo,
        view_type="bm25",
        view_config=adjustments,
    )


def test_portable_bm25_rejects_contained_source_symlink_swap_and_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, bm25 = _bm25_view(
        tmp_path,
        documents=[
            {
                "page_content": "VALUE = 1",
                "metadata": {"file": "alias.py"},
            }
        ],
    )
    alias = repo / "alias.py"
    alias.symlink_to("sample.py")
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 1\n", encoding="utf-8")
    adjustments = normalize_owned_query_view(
        bm25,
        repo_path=repo,
        view_type="bm25",
        view_config={},
    )
    import codenib._contained_source as contained_source_module

    real_verify = contained_source_module._BoundRepositoryFile.verify
    calls = 0

    def swap_then_restore(binding) -> None:
        nonlocal calls
        calls += 1
        if calls != 2:
            return real_verify(binding)
        alias.unlink()
        alias.symlink_to("../outside.py")
        try:
            return real_verify(binding)
        finally:
            alias.unlink()
            alias.symlink_to("sample.py")

    monkeypatch.setattr(
        contained_source_module._BoundRepositoryFile,
        "verify",
        swap_then_restore,
    )

    with pytest.raises(ValueError, match="stable contained source"):
        validate_portable_query_view(
            bm25,
            repo_path=repo,
            view_type="bm25",
            view_config=adjustments,
        )

    assert calls == 2


def test_portable_bm25_requires_complete_file_fingerprints(tmp_path: Path) -> None:
    repo, bm25 = _bm25_view(tmp_path)
    normalize_owned_query_view(
        bm25,
        repo_path=repo,
        view_type="bm25",
        view_config={},
    )

    for view_config in (None, {}, {"artifact_file_fingerprints": {}}):
        with pytest.raises(ValueError, match="view config|complete artifact"):
            validate_portable_query_view(
                bm25,
                repo_path=repo,
                view_type="bm25",
                view_config=view_config,
            )


def test_portable_bm25_rejects_documents_replaced_after_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, bm25 = _bm25_view(
        tmp_path,
        documents=[
            {
                "page_content": "VALUE = 1",
                "metadata": {"file": "sample.py"},
            }
        ],
    )
    adjustments = normalize_owned_query_view(
        bm25,
        repo_path=repo,
        view_type="bm25",
        view_config={},
    )
    import codenib.artifacts.portable_views as portable_views_module

    real_authenticate = portable_views_module._OwnedViewReader.authenticate
    replaced = False

    def replace_after_authentication(reader, path, expected, **kwargs):
        nonlocal replaced
        result = real_authenticate(reader, path, expected, **kwargs)
        if Path(path).name == "documents.json" and not replaced:
            replaced = True
            Path(path).write_bytes(_canonical_json_bytes([]))
        return result

    monkeypatch.setattr(
        portable_views_module._OwnedViewReader,
        "authenticate",
        replace_after_authentication,
    )

    with pytest.raises(RuntimeError, match="changed during semantic validation"):
        validate_portable_query_view(
            bm25,
            repo_path=repo,
            view_type="bm25",
            view_config=adjustments,
        )

    assert replaced


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
@pytest.mark.timeout(2)
def test_portable_bm25_rejects_fifo_swapped_after_capture_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, bm25 = _bm25_view(tmp_path)
    adjustments = normalize_owned_query_view(
        bm25,
        repo_path=repo,
        view_type="bm25",
        view_config={},
    )
    import codenib.artifacts.portable_views as portable_views_module

    real_capture = portable_views_module.capture_directory_ownership
    documents = bm25 / "documents.json"
    original = documents.read_bytes()
    swapped = False

    def capture_then_swap(path, **kwargs):
        nonlocal swapped
        token = real_capture(path, **kwargs)
        if Path(path) == bm25 and not swapped:
            swapped = True
            documents.unlink()
            os.mkfifo(documents)
        return token

    monkeypatch.setattr(
        portable_views_module,
        "capture_directory_ownership",
        capture_then_swap,
    )
    try:
        with pytest.raises(ValueError, match="private file|safely readable"):
            validate_portable_query_view(
                bm25,
                repo_path=repo,
                view_type="bm25",
                view_config=adjustments,
            )
    finally:
        if documents.exists():
            documents.unlink()
        documents.write_bytes(original)


def test_portable_validator_does_not_use_path_rglob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, bm25 = _bm25_view(tmp_path)
    adjustments = normalize_owned_query_view(
        bm25,
        repo_path=repo,
        view_type="bm25",
        view_config={},
    )

    def forbidden_rglob(*_args, **_kwargs):
        raise AssertionError("portable validation must use its bounded fd inventory")

    monkeypatch.setattr(Path, "rglob", forbidden_rglob)
    validate_portable_query_view(
        bm25,
        repo_path=repo,
        view_type="bm25",
        view_config=adjustments,
    )


def test_portable_documents_contract_has_a_fixed_nonconfigurable_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, bm25 = _bm25_view(tmp_path)
    adjustments = normalize_owned_query_view(
        bm25,
        repo_path=repo,
        view_type="bm25",
        view_config={},
    )
    monkeypatch.setattr(
        "codenib.artifacts.portable_views._MAX_DOCUMENTS_JSON_BYTES",
        1,
    )

    with pytest.raises(ValueError, match="size mismatch"):
        validate_portable_query_view(
            bm25,
            repo_path=repo,
            view_type="bm25",
            view_config={**adjustments, "max_documents_bytes": 10**12},
        )


def test_normalize_json_disappearance_is_reported_as_value_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, bm25 = _bm25_view(tmp_path)
    documents = bm25 / "documents.json"
    import codenib.artifacts.portable_views as portable_views_module

    real_capture = portable_views_module.capture_directory_ownership
    disappeared = False

    def capture_then_disappear(path, **kwargs):
        nonlocal disappeared
        token = real_capture(path, **kwargs)
        if Path(path) == bm25 and not disappeared:
            disappeared = True
            documents.unlink()
        return token

    monkeypatch.setattr(
        portable_views_module,
        "capture_directory_ownership",
        capture_then_disappear,
    )

    with pytest.raises(ValueError, match="safely readable"):
        normalize_owned_query_view(
            bm25,
            repo_path=repo,
            view_type="bm25",
            view_config={},
        )

    assert disappeared


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
        source_trust="trusted-local",
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
        view_config={**_VECTOR_CONFIG, **first_adjustments},
    )

    assert _tree(vector) == first_tree
    assert second_adjustments == first_adjustments


def test_portable_vector_validator_rejects_noncanonical_json_without_mutation(
    tmp_path: Path,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    adjustments = normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=_VECTOR_CONFIG,
    )
    documents_path = vector / "l2" / f"documents_{_MODEL_SUFFIX}.json"
    documents = json.loads(documents_path.read_text(encoding="utf-8"))
    documents_path.write_text(json.dumps(documents), encoding="utf-8")
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["level_artifacts"]["l2"] = vector_level_artifact_records(
        vector / "l2",
        _MODEL_SUFFIX,
        documents_file=documents_path.name,
    )
    config_path.write_bytes(_canonical_json_bytes(config))
    view_config = {
        **_VECTOR_CONFIG,
        **adjustments,
        "persistence_config_fingerprint": vector_config_artifact_record(
            vector,
            _MODEL_SUFFIX,
        ),
    }
    before = _tree(vector)
    before_modes = {
        path.relative_to(vector).as_posix(): path.stat().st_mode
        for path in vector.rglob("*")
    }

    with pytest.raises(ValueError, match="not canonical JSON"):
        validate_portable_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=view_config,
        )

    assert _tree(vector) == before
    assert {
        path.relative_to(vector).as_posix(): path.stat().st_mode
        for path in vector.rglob("*")
    } == before_modes


def test_portable_vector_validator_rechecks_faiss_semantics_after_records(
    tmp_path: Path,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    adjustments = normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=_VECTOR_CONFIG,
    )
    level = vector / "l2"
    _write_faiss(level / f"index_{_MODEL_SUFFIX}.faiss", dimension=8)
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["level_artifacts"]["l2"] = vector_level_artifact_records(
        level,
        _MODEL_SUFFIX,
        documents_file=f"documents_{_MODEL_SUFFIX}.json",
    )
    config_path.write_bytes(_canonical_json_bytes(config))
    view_config = {
        **_VECTOR_CONFIG,
        **adjustments,
        "persistence_config_fingerprint": vector_config_artifact_record(
            vector,
            _MODEL_SUFFIX,
        ),
    }

    with pytest.raises(ValueError, match="FAISS dimension mismatch"):
        validate_portable_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=view_config,
        )


@pytest.mark.parametrize("artifact", ["config", "documents", "faiss"])
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
@pytest.mark.timeout(2)
def test_portable_vector_rejects_fifo_swapped_after_capture_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    adjustments = normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=_VECTOR_CONFIG,
    )
    view_config = {**_VECTOR_CONFIG, **adjustments}
    targets = {
        "config": vector / f"config_{_MODEL_SUFFIX}.json",
        "documents": vector / "l2" / f"documents_{_MODEL_SUFFIX}.json",
        "faiss": vector / "l2" / f"index_{_MODEL_SUFFIX}.faiss",
    }
    target = targets[artifact]
    original = target.read_bytes()
    import codenib.artifacts.portable_views as portable_views_module

    real_capture = portable_views_module.capture_directory_ownership
    swapped = False

    def capture_then_swap(path, **kwargs):
        nonlocal swapped
        token = real_capture(path, **kwargs)
        if Path(path) == vector and not swapped:
            swapped = True
            target.unlink()
            os.mkfifo(target)
        return token

    monkeypatch.setattr(
        portable_views_module,
        "capture_directory_ownership",
        capture_then_swap,
    )
    try:
        with pytest.raises(
            ValueError,
            match=(
                "private file|safely readable|manifest fingerprint|"
                "committed documents"
            ),
        ):
            validate_portable_query_view(
                vector,
                repo_path=repo,
                view_type="vector",
                view_config=view_config,
            )
    finally:
        if target.exists():
            target.unlink()
        target.write_bytes(original)


def test_portable_vector_parses_faiss_from_authenticated_callback_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    adjustments = normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=_VECTOR_CONFIG,
    )
    faiss = importlib.import_module("faiss")
    real_read_index = faiss.read_index
    sources: list[object] = []

    def tracking_read_index(source, *args, **kwargs):
        sources.append(source)
        return real_read_index(source, *args, **kwargs)

    monkeypatch.setattr(faiss, "read_index", tracking_read_index)
    validate_portable_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config={**_VECTOR_CONFIG, **adjustments},
    )

    assert len(sources) == 1
    assert not isinstance(sources[0], (str, Path))


def test_portable_vector_rejects_external_symlink_swap_and_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    adjustments = normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=_VECTOR_CONFIG,
    )
    target = vector / "l2" / f"documents_{_MODEL_SUFFIX}.json"
    saved = target.with_name("saved-documents.json")
    outside = tmp_path / "outside.json"
    outside.write_text('[{"secret": "outside"}]', encoding="utf-8")
    import codenib.artifacts.portable_views as portable_views_module

    real_open = portable_views_module._OwnedViewReader._open_file
    swapped = False

    def swap_then_restore(reader, relative):
        nonlocal swapped
        if relative.name != target.name or swapped:
            return real_open(reader, relative)
        swapped = True
        target.rename(saved)
        target.symlink_to(outside)
        try:
            return real_open(reader, relative)
        finally:
            target.unlink()
            saved.rename(target)

    monkeypatch.setattr(
        portable_views_module._OwnedViewReader,
        "_open_file",
        swap_then_restore,
    )

    with pytest.raises(
        ValueError,
        match="private file|safely readable|committed documents",
    ):
        validate_portable_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config={**_VECTOR_CONFIG, **adjustments},
        )

    assert swapped


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="requires procfs")
def test_portable_validator_closes_anchored_descriptors(tmp_path: Path) -> None:
    repo, bm25 = _bm25_view(tmp_path)
    adjustments = normalize_owned_query_view(
        bm25,
        repo_path=repo,
        view_type="bm25",
        view_config={},
    )
    before = len(list(Path("/proc/self/fd").iterdir()))

    for _ in range(20):
        validate_portable_query_view(
            bm25,
            repo_path=repo,
            view_type="bm25",
            view_config=adjustments,
        )

    assert len(list(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.parametrize("mutation", ["faiss-dimension", "unicode-secret"])
def test_portable_vector_validator_rejects_late_synchronized_tree_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    adjustments = normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=_VECTOR_CONFIG,
    )
    view_config = {**_VECTOR_CONFIG, **adjustments}
    import codenib.artifacts.portable_views as portable_views_module

    real_validate = portable_views_module._validate_normalized_document_sources
    validation_calls = 0
    secret = "晚到的凭据秘密值-12345678"

    def mutate_after_document_validation(path, repo_path, **kwargs):
        nonlocal validation_calls
        result = real_validate(path, repo_path, **kwargs)
        validation_calls += 1
        if validation_calls == 1:
            level = vector / "l2"
            documents_path = level / f"documents_{_MODEL_SUFFIX}.json"
            if mutation == "faiss-dimension":
                _write_faiss(
                    level / f"index_{_MODEL_SUFFIX}.faiss",
                    dimension=8,
                )
            else:
                documents = json.loads(documents_path.read_text(encoding="utf-8"))
                documents[0]["metadata"]["innocent_note"] = secret
                documents_path.write_bytes(_canonical_json_bytes(documents))
            config_path = vector / f"config_{_MODEL_SUFFIX}.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["level_artifacts"]["l2"] = vector_level_artifact_records(
                level,
                _MODEL_SUFFIX,
                documents_file=documents_path.name,
            )
            config_path.write_bytes(_canonical_json_bytes(config))
        return result

    monkeypatch.setattr(
        portable_views_module,
        "_validate_normalized_document_sources",
        mutate_after_document_validation,
    )
    with pytest.raises(RuntimeError, match="changed during semantic validation"):
        validate_portable_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=view_config,
            environ={"MODELS_TOKEN": secret},
        )

    assert validation_calls == 1


@pytest.mark.parametrize("extra", ["extra.json", "l2/nested/extra.json"])
def test_normalize_vector_rejects_every_extra_entry(
    tmp_path: Path,
    extra: str,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    path = vector / extra
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected (file|directory)"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


def test_normalize_vector_rejects_root_config_url_credentials(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["endpoint"] = "//user:password@example.test/api"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="URL credentials"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


def test_normalize_vector_rejects_level_config_authorization_header(
    tmp_path: Path,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    level_config = vector / "l2" / f"config_{_MODEL_SUFFIX}.json"
    level_config.write_text(
        json.dumps(
            {
                "embedding_model": "test/model",
                "embedding_provider": "huggingface",
                "embedding_revision": None,
                "dimension": 4,
                "index_type": "flat",
                "index_metric": "ip",
                "level": "l2",
                "num_documents": 1,
                "Proxy-Authorization": "Basic dXNlcjpwYXNz",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="secret field"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


def test_normalize_vector_rejects_document_metadata_secret(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    documents_path = vector / "l2" / f"documents_{_MODEL_SUFFIX}.json"
    documents = json.loads(documents_path.read_text(encoding="utf-8"))
    documents[0]["metadata"]["github_token"] = "value"
    documents_path.write_text(json.dumps(documents), encoding="utf-8")
    _refresh_level_record(vector)

    with pytest.raises(ValueError, match="secret field"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


def test_normalize_portable_json_is_idempotent_in_a_different_checkout(
    tmp_path: Path,
) -> None:
    first_repo, _source = _repository(tmp_path / "first")
    vector = _vector_view(tmp_path, first_repo, document_format="json")
    first_adjustments = normalize_owned_query_view(
        vector,
        repo_path=first_repo,
        view_type="vector",
        view_config=_VECTOR_CONFIG,
    )
    first_tree = _tree(vector)
    second_repo, _second_source = _repository(tmp_path / "second")

    normalize_owned_query_view(
        vector,
        repo_path=second_repo,
        view_type="vector",
        view_config={**_VECTOR_CONFIG, **first_adjustments},
    )

    assert _tree(vector) == first_tree


def test_normalize_upgrades_fully_legacy_vector_config(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="pkl")
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for field in (
        "l0_documents",
        "l2_documents",
        "persistence_schema",
        "level_artifacts",
    ):
        config.pop(field)
    config_path.write_text(json.dumps(config), encoding="utf-8")

    normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=_VECTOR_CONFIG,
        source_trust="trusted-local",
    )

    upgraded = json.loads(config_path.read_text(encoding="utf-8"))
    assert upgraded["l0_documents"] == 0
    assert upgraded["l2_documents"] == 1
    assert upgraded["persistence_schema"] == VECTOR_PERSISTENCE_SCHEMA
    assert set(upgraded["level_artifacts"]) == {"l2"}
    assert not list(vector.rglob("*.pkl"))


@pytest.mark.parametrize(
    ("stale_index_type", "stale_metric"),
    [("ivf", "ip"), ("flat", "l2")],
)
def test_normalize_prunes_semantically_mismatched_derived_empty_legacy_level(
    tmp_path: Path,
    stale_index_type: str,
    stale_metric: str,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for field in (
        "l0_documents",
        "l2_documents",
        "persistence_schema",
        "level_artifacts",
    ):
        config.pop(field)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    empty = vector / "l0"
    empty.mkdir()
    (empty / f"documents_{_MODEL_SUFFIX}.json").write_text("[]\n", encoding="utf-8")
    _write_faiss(
        empty / f"index_{_MODEL_SUFFIX}.faiss",
        count=0,
        index_type=stale_index_type,
        metric=stale_metric,
    )
    (empty / f"config_{_MODEL_SUFFIX}.json").write_text("{}\n", encoding="utf-8")

    normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=_VECTOR_CONFIG,
    )

    upgraded = json.loads(config_path.read_text(encoding="utf-8"))
    assert upgraded["l0_documents"] == 0
    assert not empty.exists()


def test_normalize_preserves_format_when_later_legacy_level_is_empty(
    tmp_path: Path,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    active = vector / "l0"
    (vector / "l2").rename(active)
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for field in (
        "l0_documents",
        "l2_documents",
        "persistence_schema",
        "level_artifacts",
    ):
        config.pop(field)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    empty = vector / "l2"
    empty.mkdir()
    (empty / f"documents_{_MODEL_SUFFIX}.json").write_text("[]\n", encoding="utf-8")
    _write_faiss(empty / f"index_{_MODEL_SUFFIX}.faiss", count=0)
    (empty / f"config_{_MODEL_SUFFIX}.json").write_text("{}\n", encoding="utf-8")

    normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=_VECTOR_CONFIG,
    )

    upgraded = json.loads(config_path.read_text(encoding="utf-8"))
    assert upgraded["l0_documents"] == 1
    assert upgraded["l2_documents"] == 0
    assert set(upgraded["level_artifacts"]) == {"l0"}
    assert active.is_dir()
    assert not empty.exists()


def test_normalize_rejects_partial_legacy_level_counts(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.pop("l0_documents")
    config.pop("persistence_schema")
    config.pop("level_artifacts")
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="partial level counts"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


def test_normalize_prunes_current_model_files_for_zero_count(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    stale = vector / "l0"
    stale.mkdir()
    (stale / f"documents_{_MODEL_SUFFIX}.json").write_text("[]\n", encoding="utf-8")
    _write_faiss(stale / f"index_{_MODEL_SUFFIX}.faiss", count=0)
    (stale / f"index_{_MODEL_SUFFIX}.pkl").write_bytes(b"stale")
    (stale / f"config_{_MODEL_SUFFIX}.json").write_text("{}\n", encoding="utf-8")

    normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=_VECTOR_CONFIG,
        source_trust="trusted-local",
    )

    assert not stale.exists()


def test_normalize_rejects_other_model_artifacts(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    other = vector / "l0" / "documents_other__model.json"
    other.parent.mkdir()
    other.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unknown or other-model"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


def test_normalize_inert_pickle_rejects_before_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="pkl")
    called = False

    def fail_if_called(_handle):
        nonlocal called
        called = True
        raise AssertionError("pickle loader must not run")

    monkeypatch.setattr(
        "codenib.artifacts.portable_views.compat_pickle.load", fail_if_called
    )
    with pytest.raises(ValueError, match="portable-inert.*pickle"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )
    assert called is False


def test_portable_views_do_not_import_faiss_eagerly() -> None:
    script = (
        "import sys; import codenib.artifacts.portable_views; "
        "assert 'faiss' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", script], check=True)


@pytest.mark.parametrize("index_type", ["flat", "ivf"])
def test_normalize_schema6_view_loads_with_runtime_contract(
    tmp_path: Path,
    index_type: str,
) -> None:
    from codenib.index.embedding.vector_store import CodeVectorStore

    class _Embedding:
        def embed_query(self, _text: str) -> list[float]:
            return [0.0] * 4

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 4 for _text in texts]

    repo, vector, view_config = _production_vector_view(
        tmp_path,
        index_type=index_type,
    )
    adjustments = normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=view_config,
    )
    portable_config = {**view_config, **adjustments}
    store = CodeVectorStore(
        embedding_model="test/model",
        embedding_provider="huggingface",
        dimension=4,
        index_type=index_type,
        index_metric="ip",
        store_path=str(vector),
        embedding=_Embedding(),
        artifact_metadata=portable_config,
        revision=_REVISION,
        trust_remote_code=False,
    )

    store.load()

    assert len(store.l2_documents) == 1
    assert int(store.l2_index.ntotal) == 1
    assert store.artifact_metadata["embedding_fingerprint"] == (
        view_config["embedding_fingerprint"]
    )


def test_normalize_schema6_rejects_top_revision_drift(tmp_path: Path) -> None:
    repo, vector, view_config = _production_vector_view(tmp_path)
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["embedding_revision"] = "b" * 40
    config_path.write_text(json.dumps(config), encoding="utf-8")
    _refresh_view_fingerprint(vector, view_config)

    with pytest.raises(ValueError, match="embedding_revision"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=view_config,
        )


def test_normalize_schema6_rejects_level_revision_drift(tmp_path: Path) -> None:
    repo, vector, view_config = _production_vector_view(tmp_path)
    config_path = vector / "l2" / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["embedding_revision"] = "b" * 40
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="l2 persistence embedding_revision"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=view_config,
        )


def test_normalize_schema6_requires_persisted_artifact_identity(
    tmp_path: Path,
) -> None:
    repo, vector, view_config = _production_vector_view(tmp_path)
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.pop("artifact")
    config["embedding_kwargs"] = view_config["embedding_kwargs"]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    _refresh_view_fingerprint(vector, view_config)

    with pytest.raises(ValueError, match="missing.*artifact identity"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=view_config,
        )


def test_normalize_schema6_rejects_persisted_artifact_revision_drift(
    tmp_path: Path,
) -> None:
    repo, vector, view_config = _production_vector_view(tmp_path)
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["artifact"] = _schema6_identity(revision="b" * 40)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    _refresh_view_fingerprint(vector, view_config)

    with pytest.raises(ValueError, match="route identity|artifact revision"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=view_config,
        )


def test_normalize_openai_compatible_bundled_model_has_no_hf_revision(
    tmp_path: Path,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    model = "nomic-ai/CodeRankEmbed"
    model_suffix = model.replace("/", "__")
    level = vector / "l2"
    documents = level / f"documents_{_MODEL_SUFFIX}.json"
    index = level / f"index_{_MODEL_SUFFIX}.faiss"
    documents = documents.rename(level / f"documents_{model_suffix}.json")
    index.rename(level / f"index_{model_suffix}.faiss")

    route = resolve_inference_route(
        operation="embeddings",
        provider="openai",
        model=model,
        endpoint="https://embeddings.example.test/v1",
        dimension=4,
        compatibility_options={"dimensions": 4},
        environ={},
    )
    identity = {
        "builder_schema": 6,
        "embedding_model": route.model,
        "embedding_provider": route.provider,
        "embedding_dimension": route.dimension,
        "dimension": route.dimension,
        "embedding_endpoint": route.endpoint,
        "embedding_kwargs": route.compatibility_options,
        "embedding_route": route.public_identity(),
        "embedding_fingerprint": route.compatibility_fingerprint,
        "index_metric": "ip",
        "levels": ["l2"],
    }
    old_config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config_path = vector / f"config_{model_suffix}.json"
    config = json.loads(old_config_path.read_text(encoding="utf-8"))
    config.update(
        {
            "embedding_model": model,
            "embedding_provider": "openai",
            "embedding_revision": None,
            "level_artifacts": {
                "l2": vector_level_artifact_records(
                    level,
                    model_suffix,
                    documents_file=documents.name,
                )
            },
            "artifact": identity,
        }
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")
    old_config_path.unlink()
    view_config = {
        **identity,
        "document_count": {"l2": 1},
        "persistence_config_fingerprint": vector_config_artifact_record(
            vector,
            model_suffix,
        ),
    }

    adjustments = normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=view_config,
    )

    normalized = json.loads(config_path.read_text(encoding="utf-8"))
    assert normalized["embedding_revision"] is None
    assert adjustments["persistence_config_fingerprint"] == (
        vector_config_artifact_record(vector, model_suffix)
    )


def test_normalize_schema6_rejects_faiss_metric_drift(tmp_path: Path) -> None:
    repo, vector, view_config = _production_vector_view(tmp_path)
    _write_faiss(
        vector / "l2" / f"index_{_MODEL_SUFFIX}.faiss",
        metric="l2",
    )
    _refresh_level_record(vector)
    _refresh_view_fingerprint(vector, view_config)

    with pytest.raises(ValueError, match="FAISS metric mismatch"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=view_config,
        )


def test_normalize_schema6_rejects_faiss_index_type_drift(tmp_path: Path) -> None:
    repo, vector, view_config = _production_vector_view(tmp_path)
    _write_faiss(
        vector / "l2" / f"index_{_MODEL_SUFFIX}.faiss",
        index_type="ivf",
    )
    _refresh_level_record(vector)
    _refresh_view_fingerprint(vector, view_config)

    with pytest.raises(ValueError, match="FAISS index type mismatch"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=view_config,
        )


def test_normalize_rejects_untrained_active_ivf_before_runtime_first_search(
    tmp_path: Path,
) -> None:
    from codenib.index.embedding.vector_store import CodeVectorStore

    class _Embedding:
        def embed_query(self, _text: str) -> list[float]:
            return [0.0] * 4

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 4 for _text in texts]

    repo, vector, view_config = _production_vector_view(
        tmp_path,
        index_type="ivf",
    )
    _write_faiss(
        vector / "l2" / f"index_{_MODEL_SUFFIX}.faiss",
        count=1,
        index_type="ivf",
        trained=False,
    )
    _refresh_level_record(vector)
    _refresh_view_fingerprint(vector, view_config)
    store = CodeVectorStore(
        embedding_model="test/model",
        embedding_provider="huggingface",
        dimension=4,
        index_type="ivf",
        index_metric="ip",
        store_path=str(vector),
        embedding=_Embedding(),
        artifact_metadata=view_config,
        revision=_REVISION,
        trust_remote_code=False,
    )
    store.load()
    assert store.l2_index.is_trained is False
    with pytest.raises(RuntimeError, match="is_trained"):
        store.search("VALUE", top_k=1)

    with pytest.raises(ValueError, match="active IVF index is untrained"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=view_config,
        )


@pytest.mark.parametrize(
    "document_count",
    [
        {"l2": 2},
        {"l0": 0, "l2": 1},
        {"other": 1, "l2": 1},
        [1],
    ],
)
def test_normalize_rejects_noncanonical_view_document_count(
    tmp_path: Path,
    document_count: object,
) -> None:
    repo, vector, view_config = _production_vector_view(tmp_path)
    view_config["document_count"] = document_count

    with pytest.raises(ValueError, match="document_count"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=view_config,
        )


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
            source_trust="trusted-local",
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
            source_trust="trusted-local",
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
            source_trust="trusted-local",
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


@pytest.mark.parametrize("absolute", [False, True])
def test_normalize_vector_accepts_native_document_paths(
    tmp_path: Path,
    absolute: bool,
) -> None:
    repo, source = _repository(tmp_path)
    if absolute:
        source_path = str(source)
        expected = "sample.py"
    else:
        nested = repo / "nested" / "sample.py"
        nested.parent.mkdir()
        nested.write_text("VALUE = 2\n", encoding="utf-8")
        source_path = str(Path("nested") / "sample.py")
        expected = "nested/sample.py"
    payload = json.dumps(
        [{"page_content": "VALUE = 1", "metadata": {"file": source_path}}]
    ).encode("utf-8")
    vector = _vector_view(
        tmp_path,
        repo,
        document_format="json",
        document_bytes=payload,
    )

    normalize_owned_query_view(
        vector,
        repo_path=repo,
        view_type="vector",
        view_config=_VECTOR_CONFIG,
    )

    documents = json.loads(
        (vector / "l2" / f"documents_{_MODEL_SUFFIX}.json").read_text(encoding="utf-8")
    )
    assert documents[0]["metadata"]["file"] == expected


@pytest.mark.skipif(os.name == "nt", reason="Windows syntax is native on Windows")
@pytest.mark.parametrize(
    "source_path",
    ["C:\\repo\\sample.py", "\\\\server\\share\\sample.py", "dir\\sample.py"],
)
def test_normalize_vector_rejects_foreign_windows_document_paths(
    tmp_path: Path,
    source_path: str,
) -> None:
    repo, _source = _repository(tmp_path)
    payload = json.dumps(
        [{"page_content": "VALUE = 1", "metadata": {"file": source_path}}]
    ).encode("utf-8")
    vector = _vector_view(
        tmp_path,
        repo,
        document_format="json",
        document_bytes=payload,
    )

    with pytest.raises(ValueError, match="portable"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


@pytest.mark.parametrize(
    "source_path",
    [
        "C:sample.py",
        "//server/share/sample.py",
        "sample.py\x00tail",
        "sample.py\x1ftail",
        ".",
        "./sample.py",
        "dir/../sample.py",
        "/sample.py",
        "dir//sample.py",
    ],
)
def test_normalize_vector_rejects_nonportable_document_paths(
    tmp_path: Path,
    source_path: str,
) -> None:
    repo, _source = _repository(tmp_path)
    payload = json.dumps(
        [{"page_content": "VALUE = 1", "metadata": {"file": source_path}}]
    ).encode("utf-8")
    vector = _vector_view(
        tmp_path,
        repo,
        document_format="json",
        document_bytes=payload,
    )

    with pytest.raises(ValueError, match="portable|repository-relative|outside"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("embedding_model", "other/model", "embedding_model"),
        ("embedding_provider", "openai", "embedding provider"),
        ("dimension", 8, "dimension"),
        ("index_metric", "l2", "index metric"),
        ("index_type", "ivf", "index type"),
    ],
)
def test_normalize_rejects_persistence_semantic_drift(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config[field] = value
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


def test_normalize_rejects_level_config_semantic_drift(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    level_config = vector / "l2" / f"config_{_MODEL_SUFFIX}.json"
    level_config.write_text(
        json.dumps(
            {
                "embedding_model": "test/model",
                "embedding_provider": "huggingface",
                "embedding_revision": None,
                "dimension": 4,
                "index_type": "flat",
                "index_metric": "l2",
                "level": "l2",
                "num_documents": 1,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="l2 persistence index_metric"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


def test_normalize_rejects_persisted_route_identity_drift(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["artifact"] = {**_VECTOR_CONFIG, "embedding_kwargs": {}}
    config["artifact"]["embedding_dimension"] = 8
    config["artifact"]["dimension"] = 8
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="route identity"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


def test_normalize_rejects_persisted_embedding_options_drift(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["artifact"] = {
        **_VECTOR_CONFIG,
        "embedding_kwargs": {"encode_kwargs": {"normalize_embeddings": False}},
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    view_config = {
        **_VECTOR_CONFIG,
        "embedding_kwargs": {"encode_kwargs": {"normalize_embeddings": True}},
    }

    with pytest.raises(ValueError, match="route identity"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=view_config,
        )


def test_normalize_rejects_persisted_embedding_fingerprint_drift(
    tmp_path: Path,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["artifact"] = {
        **_VECTOR_CONFIG,
        "embedding_fingerprint": "sha256:" + "0" * 64,
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="embedding fingerprint mismatch"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


def test_normalize_rejects_persistence_commit_fingerprint_drift(
    tmp_path: Path,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    expected = vector_config_artifact_record(vector, _MODEL_SUFFIX)
    config_path = vector / f"config_{_MODEL_SUFFIX}.json"
    config_path.write_bytes(config_path.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="manifest fingerprint"):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config={
                **_VECTOR_CONFIG,
                "persistence_config_fingerprint": expected,
            },
        )


@pytest.mark.parametrize(
    ("dimension", "count", "message"),
    [(8, 1, "FAISS dimension mismatch"), (4, 2, "FAISS count mismatch")],
)
def test_normalize_rejects_faiss_shape_drift(
    tmp_path: Path,
    dimension: int,
    count: int,
    message: str,
) -> None:
    repo, _source = _repository(tmp_path)
    vector = _vector_view(tmp_path, repo, document_format="json")
    _write_faiss(
        vector / "l2" / f"index_{_MODEL_SUFFIX}.faiss",
        dimension=dimension,
        count=count,
    )
    _refresh_level_record(vector)

    with pytest.raises(ValueError, match=message):
        normalize_owned_query_view(
            vector,
            repo_path=repo,
            view_type="vector",
            view_config=_VECTOR_CONFIG,
        )


@pytest.mark.parametrize("name", ["cache.PKL", "cache.pickle", "cache.PICKLE"])
def test_normalize_bm25_rejects_pickle_case_insensitively(
    tmp_path: Path,
    name: str,
) -> None:
    repo, _source = _repository(tmp_path)
    bm25 = tmp_path / "state" / "bm25"
    bm25.mkdir(parents=True)
    (bm25 / "documents.json").write_text("[]\n", encoding="utf-8")
    (bm25 / "bm25_metadata.json").write_text("{}\n", encoding="utf-8")
    (bm25 / name).write_bytes(b"unsafe")

    with pytest.raises(ValueError, match="must not contain pickle"):
        normalize_owned_query_view(
            bm25,
            repo_path=repo,
            view_type="bm25",
            view_config={},
        )


def test_normalize_rejects_invalid_source_trust(tmp_path: Path) -> None:
    repo, _source = _repository(tmp_path)
    bm25 = tmp_path / "state" / "bm25"
    bm25.mkdir(parents=True)

    with pytest.raises(ValueError, match="invalid.*source trust"):
        normalize_owned_query_view(
            bm25,
            repo_path=repo,
            view_type="bm25",
            view_config={},
            source_trust="implicit-local",  # type: ignore[arg-type]
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
