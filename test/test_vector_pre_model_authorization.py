# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.index.embedding.artifact_integrity import (
    capture_authenticated_vector_view,
    require_authorized_vector_view,
)
from codenib.native_index_authorization import (
    InvalidNativeIndexAuthorizationError,
    MissingNativeIndexAuthorizationError,
    _mint_trusted_local_admin_authorization,
)
from codenib.repository_source_selection import RepositorySourceSelection

_VECTOR_CONTRACT = {
    "builder_schema": 2,
    "embedding_model": "vendor/model",
    "embedding_provider": "huggingface",
    "embedding_dimension": 4,
    "dimension": 4,
    "embedding_kwargs": {},
}
_SOURCE_COMMIT = "a" * 40
_SOURCE_FINGERPRINT = "sha256-v2:" + ("b" * 64)
_SOURCE_SELECTION = RepositorySourceSelection()


def _mint_vector_authorization(root: Path, semantic_contract):
    root.mkdir(parents=True, exist_ok=True)
    with capture_authenticated_vector_view(root) as view:
        return _mint_trusted_local_admin_authorization(
            view.ownership,
            view_type="vector",
            semantic_contract=semantic_contract,
            evidence=(
                "pre-model-gate-test-local-admin",
                "captured-vector-tree-subject",
            ),
        )


def _authorization_case(
    case: str,
    *,
    target: Path,
    semantic_contract,
    tmp_path: Path,
):
    if case == "missing":
        return None
    if case == "malformed":
        return object()
    if case == "wrong-tree":
        wrong_root = tmp_path / f"wrong-tree-{target.name}"
        return _mint_vector_authorization(wrong_root, semantic_contract)
    if case == "wrong-semantic":
        return _mint_vector_authorization(
            target,
            {"different": "semantic-contract"},
        )
    raise AssertionError(f"unsupported authorization case: {case}")


def test_authorized_vector_gate_preserves_primary_base_exception_on_cleanup(
    monkeypatch,
) -> None:
    from codenib.index.embedding import artifact_integrity

    events = []
    primary_error = KeyboardInterrupt("primary")

    class FailingView:
        ownership = object()

        def verify_final(self):
            events.append("verify")

        def close(self):
            events.append("close")
            raise SystemExit("cleanup")

    monkeypatch.setattr(
        artifact_integrity,
        "require_native_index_authorization_preflight",
        lambda *_args, **_kwargs: events.append("preflight"),
    )
    monkeypatch.setattr(
        artifact_integrity,
        "capture_authenticated_vector_view",
        lambda _root: events.append("capture") or FailingView(),
    )

    def fail_exact(*_args, **_kwargs):
        events.append("exact")
        raise primary_error

    monkeypatch.setattr(
        artifact_integrity,
        "require_native_index_authorization",
        fail_exact,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        require_authorized_vector_view("/vector", object(), {})

    assert caught.value is primary_error
    assert events == ["preflight", "capture", "exact", "close"]
    cleanup_notes = getattr(primary_error, "__notes__", ()) or getattr(
        primary_error,
        "_codenib_cleanup_notes",
        (),
    )
    assert any("cleanup" in note for note in cleanup_notes)


@pytest.mark.parametrize(
    ("case", "error_type"),
    [
        ("missing", MissingNativeIndexAuthorizationError),
        ("malformed", InvalidNativeIndexAuthorizationError),
        ("wrong-tree", InvalidNativeIndexAuthorizationError),
        ("wrong-semantic", InvalidNativeIndexAuthorizationError),
    ],
)
def test_skill_vector_rejects_unbound_authority_before_store_constructor(
    monkeypatch,
    tmp_path,
    case,
    error_type,
) -> None:
    from codenib.compiler import skill_context
    from codenib.index.embedding import vector_store as vector_store_module

    target = tmp_path / "skill-vector"
    target.mkdir()
    authorization = _authorization_case(
        case,
        target=target,
        semantic_contract=_VECTOR_CONTRACT,
        tmp_path=tmp_path,
    )
    constructors = []

    monkeypatch.setattr(
        vector_store_module,
        "CodeVectorStore",
        lambda **kwargs: constructors.append(kwargs),
    )

    with pytest.raises(error_type):
        skill_context._load_vector(
            str(target),
            embedding_model="vendor/model",
            embedding_provider="huggingface",
            embedding_dimension=4,
            artifact_metadata=dict(_VECTOR_CONTRACT),
            native_index_authorization=authorization,
        )

    assert constructors == []


def test_skill_vector_valid_authority_continues_to_store_load(
    monkeypatch,
    tmp_path,
) -> None:
    from codenib.compiler import skill_context
    from codenib.index.embedding import vector_store as vector_store_module

    target = tmp_path / "skill-vector"
    authorization = _mint_vector_authorization(target, _VECTOR_CONTRACT)
    constructors = []
    loads = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            constructors.append(kwargs)

        def load(self, path, *, native_index_authorization):
            loads.append((path, native_index_authorization))

    monkeypatch.setattr(vector_store_module, "CodeVectorStore", FakeVectorStore)

    result = skill_context._load_vector(
        str(target),
        embedding_model="vendor/model",
        embedding_provider="huggingface",
        embedding_dimension=4,
        artifact_metadata=dict(_VECTOR_CONTRACT),
        native_index_authorization=authorization,
    )

    assert isinstance(result, FakeVectorStore)
    assert len(constructors) == 1
    assert loads == [(str(target), authorization)]


def _server_context(target: Path, authorization):
    from codenib.mcp.context import ServerContext

    entry = IndexEntry(
        index_type="vector",
        path=str(target),
        built_at="2026-08-11T00:00:00Z",
        built_at_epoch=0.0,
        status="fresh",
        config=dict(_VECTOR_CONTRACT),
        commit=_SOURCE_COMMIT,
        source_fingerprint=_SOURCE_FINGERPRINT,
        source_selection_digest=_SOURCE_SELECTION.digest,
    )
    return ServerContext(
        manifest=RepoManifest(
            repo_path="/repo",
            commit=_SOURCE_COMMIT,
            last_indexed_commit=_SOURCE_COMMIT,
            source_fingerprint=_SOURCE_FINGERPRINT,
            last_indexed_source_fingerprint=_SOURCE_FINGERPRINT,
            source_selection=_SOURCE_SELECTION,
            last_indexed_source_selection_digest=_SOURCE_SELECTION.digest,
            indexes={"vector": entry},
        ),
        _native_index_authorization=authorization,
    )


@pytest.mark.parametrize(
    "case",
    ["missing", "malformed", "wrong-tree", "wrong-semantic"],
)
def test_mcp_vector_rejects_unbound_authority_before_store_constructor(
    monkeypatch,
    tmp_path,
    case,
) -> None:
    from codenib.index.embedding import vector_store as vector_store_module

    target = tmp_path / "mcp-vector"
    target.mkdir()
    authorization = _authorization_case(
        case,
        target=target,
        semantic_contract=_VECTOR_CONTRACT,
        tmp_path=tmp_path,
    )
    constructors = []
    monkeypatch.setattr(
        vector_store_module,
        "CodeVectorStore",
        lambda **kwargs: constructors.append(kwargs),
    )
    context = _server_context(target, authorization)

    context._load_vector(probe=True)

    assert constructors == []
    assert context.vector is None
    assert "vector" in context.errors


def test_mcp_vector_valid_authority_continues_to_store_load(
    monkeypatch,
    tmp_path,
) -> None:
    from codenib.index.embedding import vector_store as vector_store_module

    target = tmp_path / "mcp-vector"
    authorization = _mint_vector_authorization(target, _VECTOR_CONTRACT)
    constructors = []
    loads = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            constructors.append(kwargs)
            self.embedding_model = kwargs["embedding_model"]

        def load(self, *, native_index_authorization):
            loads.append(native_index_authorization)

        def get_stats(self):
            return {"total_documents": 1}

    monkeypatch.setattr(vector_store_module, "CodeVectorStore", FakeVectorStore)
    context = _server_context(target, authorization)

    context._load_vector(probe=True)

    assert isinstance(context.vector, FakeVectorStore)
    assert len(constructors) == 1
    assert loads == [authorization]
    assert "vector" not in context.errors


def _retrieve_pipeline(pipeline_module, tmp_path, authorization):
    pipeline = object.__new__(pipeline_module.RetrieveRerankPipeline)
    pipeline.repo_path = str(tmp_path)
    pipeline.index_path = tmp_path / "retrieve-vector"
    pipeline.retrieval_level = "l2"
    pipeline.languages = ["python"]
    pipeline.max_lines_per_chunk = 300
    pipeline.index_metric = "ip"
    pipeline.profiler = None
    pipeline._native_index_authorization = authorization
    return pipeline


@pytest.mark.parametrize("case", ["malformed", "wrong-tree", "wrong-semantic"])
def test_retrieve_cache_rejects_unbound_authority_before_store_constructor(
    monkeypatch,
    tmp_path,
    case,
) -> None:
    from codenib.model import retrieve_rerank_pipeline as pipeline_module

    target = tmp_path / "retrieve-vector"
    target.mkdir()
    (target / "config_model.json").write_text("{}", encoding="utf-8")
    authorization = _authorization_case(
        case,
        target=target,
        semantic_contract={},
        tmp_path=tmp_path,
    )
    constructors = []
    monkeypatch.setattr(
        pipeline_module,
        "CodeVectorStore",
        lambda **kwargs: constructors.append(kwargs),
    )
    pipeline = _retrieve_pipeline(pipeline_module, tmp_path, authorization)

    with pytest.raises(InvalidNativeIndexAuthorizationError):
        pipeline._initialize_vector_store(
            embedding_model="model",
            embedding_provider="huggingface",
            embedding_dimension=4,
            embedding_kwargs={},
        )

    assert constructors == []


def test_retrieve_cache_valid_authority_continues_to_store_load(
    monkeypatch,
    tmp_path,
) -> None:
    from codenib.model import retrieve_rerank_pipeline as pipeline_module

    target = tmp_path / "retrieve-vector"
    target.mkdir()
    (target / "config_model.json").write_text("{}", encoding="utf-8")
    authorization = _mint_vector_authorization(target, {})
    constructors = []
    loads = []

    class FakeVectorStore:
        def __init__(self, **kwargs):
            constructors.append(kwargs)
            self.embedding = object()
            self.l0_documents = [object()]
            self.l2_documents = [object()]

        def load(self, path, *, native_index_authorization):
            loads.append((path, native_index_authorization))

    monkeypatch.setattr(pipeline_module, "CodeVectorStore", FakeVectorStore)
    monkeypatch.setattr(
        pipeline_module,
        "build_hierarchical_vector_store",
        lambda **_kwargs: pytest.fail("authorized cache must be reused"),
    )
    pipeline = _retrieve_pipeline(pipeline_module, tmp_path, authorization)

    result = pipeline._initialize_vector_store(
        embedding_model="model",
        embedding_provider="huggingface",
        embedding_dimension=4,
        embedding_kwargs={},
    )

    assert isinstance(result, FakeVectorStore)
    assert len(constructors) == 1
    assert loads == [(str(target), authorization)]
