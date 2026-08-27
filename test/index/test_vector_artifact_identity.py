# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ctypes
import inspect
import sys
import threading
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import codenib.index.embedding.artifact_integrity as artifact_integrity_module
from codenib._atomic_directory import capture_directory_ownership
from codenib.index.embedding.artifact_integrity import capture_authenticated_vector_view
from codenib.index.embedding.vector_store import (
    CodeVectorStore,
    _LoadedVectorState,
    _OpenAIEmbeddingWrapper,
    _read_authenticated_faiss,
)
from codenib.native_index_authorization import _mint_trusted_local_admin_authorization


class _Embedding:
    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def embed_query(self, _text: str) -> list[float]:
        return [0.0] * self.dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dimension for _ in texts]


def test_faiss_callback_caps_oversized_native_read_requests(monkeypatch) -> None:
    payload_size = 8 * 1024 * 1024 + 17

    class _Snapshot:
        def __init__(self) -> None:
            self.record = SimpleNamespace(size=payload_size)
            self.offset = 0
            self.requests: list[int] = []

        def read(self, size: int) -> bytes:
            self.requests.append(size)
            remaining = payload_size - self.offset
            consumed = min(size, remaining)
            self.offset += consumed
            return b"x" * consumed

    snapshot = _Snapshot()
    view = SimpleNamespace(
        authenticated_snapshot=lambda _relative: nullcontext(
            (snapshot, snapshot.record)
        )
    )
    observed: list[int] = []
    sentinel = object()

    monkeypatch.setattr(
        "codenib.index.embedding.vector_store.faiss.PyCallbackIOReader",
        lambda callback: callback,
    )

    def read_index(callback):
        while block := callback(1 << 60):
            observed.append(len(block))
        return sentinel

    monkeypatch.setattr(
        "codenib.index.embedding.vector_store.faiss.read_index",
        read_index,
    )

    assert _read_authenticated_faiss(view, "l2/index.faiss") is sentinel
    assert observed == [8 * 1024 * 1024, 17]
    assert max(snapshot.requests) == 8 * 1024 * 1024


def _store(
    path,
    *,
    fingerprint: str,
    provider: str = "huggingface",
    revision: str | None = None,
):
    embedding_kwargs = {"revision": revision} if revision is not None else {}
    return CodeVectorStore(
        embedding_model="vendor/model",
        embedding_provider=provider,
        dimension=4,
        store_path=str(path),
        embedding=_Embedding(4),
        artifact_metadata={"embedding_fingerprint": fingerprint},
        **embedding_kwargs,
    )


def _authorization(path, store):
    with capture_authenticated_vector_view(path) as view:
        return _mint_trusted_local_admin_authorization(
            view.ownership,
            view_type="vector",
            semantic_contract=store.artifact_metadata,
            evidence=(
                "vector-artifact-test-local-admin",
                "captured-vector-tree-subject",
            ),
        )


def test_load_level_releases_native_index_when_validation_fails(
    tmp_path,
    monkeypatch,
) -> None:
    store = _store(tmp_path, fingerprint="sha256:expected")

    class _MismatchedIndex:
        d = store.dimension + 1

        def __init__(self) -> None:
            self.reset_calls = 0

        def reset(self) -> None:
            self.reset_calls += 1

    index = _MismatchedIndex()
    view = SimpleNamespace(
        root=tmp_path,
        has_file=lambda relative: relative.endswith(".faiss"),
    )
    monkeypatch.setattr(
        "codenib.index.embedding.vector_store._read_authenticated_faiss",
        lambda _view, _relative: index,
    )

    with pytest.raises(ValueError, match="FAISS dimension mismatch"):
        store._load_level(view, "l0", "vendor__model")

    assert index.reset_calls == 1


def test_load_level_owns_index_at_first_line_after_native_read(
    tmp_path,
    monkeypatch,
) -> None:
    store = _store(tmp_path, fingerprint="sha256:expected")

    class _Index:
        def __init__(self) -> None:
            self.reset_calls = 0

        def reset(self) -> None:
            self.reset_calls += 1

    index = _Index()
    view = SimpleNamespace(
        root=tmp_path,
        has_file=lambda relative: relative.endswith(".faiss"),
    )
    monkeypatch.setattr(
        "codenib.index.embedding.vector_store._read_authenticated_faiss",
        lambda _view, _relative: index,
    )
    source, first_line = inspect.getsourcelines(CodeVectorStore._load_level)
    finish_line = first_line + next(
        offset
        for offset, line in enumerate(source)
        if "return self._finish_loaded_level(" in line
    )
    interruption = KeyboardInterrupt("injected after native index read")
    injected = False

    def trace(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and frame.f_code is CodeVectorStore._load_level.__code__
            and event == "line"
            and frame.f_lineno == finish_line
        ):
            injected = True
            raise interruption
        return trace

    sys.settrace(trace)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            store._load_level(view, "l0", "vendor__model")
    finally:
        sys.settrace(None)

    assert caught.value is interruption
    assert injected
    assert index.reset_calls == 1


def test_load_denies_missing_authorization_before_tree_capture(
    tmp_path,
    monkeypatch,
) -> None:
    store = _store(tmp_path, fingerprint="sha256:expected")
    monkeypatch.setattr(
        "codenib.index.embedding.vector_store.capture_authenticated_vector_view",
        lambda _path: pytest.fail("denied native load must not capture the tree"),
    )

    with pytest.raises(ValueError, match="requires external authorization"):
        store.load()


def test_load_denies_foreign_pid_authorization_before_tree_capture(
    tmp_path,
    monkeypatch,
) -> None:
    import codenib.native_index_authorization as authorization_module

    store = _store(tmp_path, fingerprint="sha256:expected")
    authorization = _authorization(tmp_path, store)
    monkeypatch.setattr(
        authorization_module.os,
        "getpid",
        lambda: authorization.process_id + 1,
    )
    monkeypatch.setattr(
        "codenib.index.embedding.vector_store.capture_authenticated_vector_view",
        lambda _path: pytest.fail("foreign-PID load must not capture the tree"),
    )

    with pytest.raises(ValueError, match="another process"):
        store.load(native_index_authorization=authorization)


def test_load_rejects_manifest_and_saved_artifact_fingerprint_mismatch(
    tmp_path,
) -> None:
    _store(tmp_path, fingerprint="sha256:first").save()
    reopened = _store(tmp_path, fingerprint="sha256:second")

    with pytest.raises(ValueError, match="does not match manifest"):
        reopened.load(native_index_authorization=_authorization(tmp_path, reopened))


def test_load_rejects_provider_substitution(tmp_path) -> None:
    _store(tmp_path, fingerprint="sha256:same").save()
    reopened = _store(
        tmp_path,
        fingerprint="sha256:same",
        provider="openai",
    )

    with pytest.raises(ValueError, match="provider mismatch"):
        reopened.load(native_index_authorization=_authorization(tmp_path, reopened))


def test_load_rejects_embedding_revision_substitution(tmp_path) -> None:
    _store(tmp_path, fingerprint="sha256:same", revision="a" * 40).save()
    reopened = _store(
        tmp_path,
        fingerprint="sha256:same",
        revision="b" * 40,
    )

    with pytest.raises(ValueError, match="embedding revision mismatch"):
        reopened.load(native_index_authorization=_authorization(tmp_path, reopened))


@pytest.mark.parametrize("option", ["revision", "trust_remote_code"])
def test_remote_provider_rejects_huggingface_model_options(tmp_path, option) -> None:
    value = "a" * 40 if option == "revision" else False

    with pytest.raises(ValueError, match="require provider='huggingface'"):
        CodeVectorStore(
            embedding_model="text-embedding-3-small",
            embedding_provider="openai",
            dimension=4,
            store_path=str(tmp_path),
            embedding=_Embedding(4),
            **{option: value},
        )


def test_load_requires_saved_identity_when_manifest_has_a_fingerprint(tmp_path) -> None:
    reopened = _store(tmp_path, fingerprint="sha256:expected")

    with pytest.raises(ValueError, match="top-level configuration"):
        reopened.load(native_index_authorization=_authorization(tmp_path, reopened))


def test_load_keeps_previous_state_when_final_tree_verification_fails(
    tmp_path,
    monkeypatch,
) -> None:
    store = _store(tmp_path, fingerprint="sha256:expected")
    old_l0 = store.l0_index
    old_l2 = store.l2_index
    old_metadata = store.artifact_metadata
    old_path = store.store_path
    ownership = capture_directory_ownership(tmp_path)
    authorization = _mint_trusted_local_admin_authorization(
        ownership,
        view_type="vector",
        semantic_contract=store.artifact_metadata,
        evidence=("vector-final-verification-test",),
    )

    class ReplacementIndex:
        def __init__(self) -> None:
            self.reset_calls = 0

        def reset(self) -> None:
            self.reset_calls += 1

    replacement_l0 = ReplacementIndex()
    replacement_l2 = ReplacementIndex()
    view = SimpleNamespace(
        ownership=ownership,
        verify_final=lambda: (_ for _ in ()).throw(
            ValueError("injected final vector verification failure")
        ),
        close=lambda: None,
    )
    monkeypatch.setattr(
        "codenib.index.embedding.vector_store.capture_authenticated_vector_view",
        lambda _path: view,
    )
    monkeypatch.setattr(
        store,
        "_load_captured",
        lambda _view: _LoadedVectorState(
            l0_index=replacement_l0,
            l0_documents=[object()],
            l2_index=replacement_l2,
            l2_documents=[object()],
            artifact_metadata={"embedding_fingerprint": "sha256:replacement"},
            store_path=tmp_path / "replacement",
        ),
    )

    with pytest.raises(ValueError, match="final vector verification"):
        store.load(native_index_authorization=authorization)

    assert store.l0_index is old_l0
    assert store.l2_index is old_l2
    assert store.artifact_metadata is old_metadata
    assert store.store_path is old_path
    assert replacement_l0.reset_calls == 1
    assert replacement_l2.reset_calls == 1


@pytest.mark.parametrize(
    "cleanup_failure",
    [RuntimeError("index reset failed"), KeyboardInterrupt("index reset cancelled")],
)
@pytest.mark.parametrize("caller_ambient", [False, True])
def test_load_preserves_primary_and_visits_all_temporary_cleanup(
    tmp_path,
    monkeypatch,
    cleanup_failure: BaseException,
    caller_ambient: bool,
) -> None:
    store = _store(tmp_path, fingerprint="sha256:expected")
    ownership = capture_directory_ownership(tmp_path)
    authorization = _mint_trusted_local_admin_authorization(
        ownership,
        view_type="vector",
        semantic_contract=store.artifact_metadata,
        evidence=("vector-failed-load-cleanup-test",),
    )

    class _Index:
        def __init__(self, failure=None) -> None:
            self.failure = failure
            self.reset_calls = 0

        def reset(self) -> None:
            self.reset_calls += 1
            if self.failure is not None:
                raise self.failure

    primary = ValueError("injected final vector verification failure")
    replacement_l0 = _Index(cleanup_failure)
    replacement_l2 = _Index()
    close_calls = []
    view = SimpleNamespace(
        ownership=ownership,
        verify_final=lambda: (_ for _ in ()).throw(primary),
        close=lambda: close_calls.append(True),
    )
    monkeypatch.setattr(
        "codenib.index.embedding.vector_store.capture_authenticated_vector_view",
        lambda _path: view,
    )
    monkeypatch.setattr(
        store,
        "_load_captured",
        lambda _view: _LoadedVectorState(
            l0_index=replacement_l0,
            l0_documents=[object()],
            l2_index=replacement_l2,
            l2_documents=[object()],
            artifact_metadata={"embedding_fingerprint": "sha256:replacement"},
            store_path=tmp_path / "replacement",
        ),
    )

    def load_with_expected_failure():
        with pytest.raises(ValueError) as caught:
            store.load(native_index_authorization=authorization)
        return caught

    if caller_ambient:
        try:
            raise primary
        except ValueError as active_error:
            assert active_error is primary
            caught = load_with_expected_failure()
    else:
        caught = load_with_expected_failure()

    assert caught.value is primary
    assert close_calls == [True]
    assert replacement_l0.reset_calls == replacement_l2.reset_calls == 1
    owner = caught.value.vector_index_cleanup_owner
    assert owner.indices == [replacement_l0]
    replacement_l0.failure = None
    owner.close()
    assert owner.closed
    assert replacement_l0.reset_calls == 2
    assert replacement_l2.reset_calls == 1


def test_load_preserves_pending_captured_view_owner_on_cleanup_failure(
    tmp_path,
    monkeypatch,
) -> None:
    store = _store(tmp_path, fingerprint="sha256:expected")
    ownership = capture_directory_ownership(tmp_path)
    authorization = _mint_trusted_local_admin_authorization(
        ownership,
        view_type="vector",
        semantic_contract=store.artifact_metadata,
        evidence=("vector-view-cleanup-owner-test",),
    )
    primary = ValueError("injected final vector verification failure")
    cleanup_failure = KeyboardInterrupt("injected captured view cleanup failure")

    class _Owner:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    owner = _Owner()

    def fail_close() -> None:
        cleanup_failure.captured_directory_cleanup_owner = owner
        raise cleanup_failure

    view = SimpleNamespace(
        ownership=ownership,
        verify_final=lambda: (_ for _ in ()).throw(primary),
        close=fail_close,
        reader=object(),
    )
    monkeypatch.setattr(
        "codenib.index.embedding.vector_store.capture_authenticated_vector_view",
        lambda _path: view,
    )
    monkeypatch.setattr(
        store,
        "_load_captured",
        lambda _view: (_ for _ in ()).throw(primary),
    )

    with pytest.raises(ValueError) as caught:
        store.load(native_index_authorization=authorization)

    assert caught.value is primary
    assert caught.value.captured_directory_cleanup_owner is owner
    owner.close()
    assert owner.close_calls == 1


@pytest.mark.parametrize("operation", ["mint", "require"])
def test_vector_authorization_gate_preserves_pending_view_cleanup_owner(
    tmp_path,
    monkeypatch,
    operation: str,
) -> None:
    primary = ValueError("injected vector gate verification failure")
    cleanup_failure = KeyboardInterrupt("injected vector gate cleanup failure")
    owner = MagicMock()

    def fail_close() -> None:
        cleanup_failure.captured_directory_cleanup_owner = owner
        raise cleanup_failure

    view = SimpleNamespace(
        ownership=object(),
        verify_final=lambda: (_ for _ in ()).throw(primary),
        close=fail_close,
        reader=object(),
    )
    monkeypatch.setattr(
        artifact_integrity_module,
        "capture_authenticated_vector_view",
        lambda _path: view,
    )
    monkeypatch.setattr(
        artifact_integrity_module,
        "_mint_trusted_local_admin_authorization",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        artifact_integrity_module,
        "require_native_index_authorization_preflight",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        artifact_integrity_module,
        "require_native_index_authorization",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(ValueError) as caught:
        if operation == "mint":
            artifact_integrity_module._mint_trusted_local_vector_authorization(
                tmp_path,
                {},
                evidence=("test",),
            )
        else:
            artifact_integrity_module.require_authorized_vector_view(
                tmp_path,
                object(),
                {},
            )

    assert caught.value is primary
    assert caught.value.captured_directory_cleanup_owner is owner


def test_load_keeps_published_state_when_cleanup_is_cancelled_under_ambient_error(
    tmp_path,
    monkeypatch,
) -> None:
    store = _store(tmp_path, fingerprint="sha256:expected")
    ownership = capture_directory_ownership(tmp_path)
    authorization = _mint_trusted_local_admin_authorization(
        ownership,
        view_type="vector",
        semantic_contract=store.artifact_metadata,
        evidence=("vector-old-index-cleanup-test",),
    )

    class Index:
        def __init__(self, failure=None) -> None:
            self.failure = failure
            self.reset_calls = 0

        def reset(self) -> None:
            self.reset_calls += 1
            if self.failure is not None:
                raise self.failure

    interruption = KeyboardInterrupt("injected old-index cleanup cancellation")
    old_l0 = Index(interruption)
    old_l2 = Index()
    store.l0_index = old_l0
    store.l2_index = old_l2
    replacement_l0 = Index()
    replacement_l2 = Index()
    view = SimpleNamespace(
        ownership=ownership,
        verify_final=lambda: None,
        close=lambda: None,
    )
    monkeypatch.setattr(
        "codenib.index.embedding.vector_store.capture_authenticated_vector_view",
        lambda _path: view,
    )
    monkeypatch.setattr(
        store,
        "_load_captured",
        lambda _view: _LoadedVectorState(
            l0_index=replacement_l0,
            l0_documents=[object()],
            l2_index=replacement_l2,
            l2_documents=[object()],
            artifact_metadata={"embedding_fingerprint": "sha256:replacement"},
            store_path=tmp_path / "replacement",
        ),
    )

    ambient_error = OSError("ambient caller failure")
    try:
        raise ambient_error
    except OSError as active_error:
        assert active_error is ambient_error
        with pytest.raises(KeyboardInterrupt) as caught:
            store.load(native_index_authorization=authorization)

    assert caught.value is interruption
    assert store.l0_index is replacement_l0
    assert store.l2_index is replacement_l2
    assert replacement_l0.reset_calls == replacement_l2.reset_calls == 0
    owner = caught.value.vector_index_cleanup_owner
    assert owner.indices == [old_l0]
    old_l0.failure = None
    owner.close()
    assert owner.closed
    assert old_l0.reset_calls == 2
    assert old_l2.reset_calls == 1


def test_load_preserves_same_ambient_object_raised_after_publication(
    tmp_path,
    monkeypatch,
) -> None:
    store = _store(tmp_path, fingerprint="sha256:expected")
    ownership = capture_directory_ownership(tmp_path)
    authorization = _mint_trusted_local_admin_authorization(
        ownership,
        view_type="vector",
        semantic_contract=store.artifact_metadata,
        evidence=("vector-post-publication-primary-test",),
    )

    class Index:
        def __init__(self, failure=None) -> None:
            self.failure = failure
            self.reset_calls = 0

        def reset(self) -> None:
            self.reset_calls += 1
            if self.failure is not None:
                raise self.failure

    cleanup_interruption = KeyboardInterrupt("injected old-index cleanup cancellation")
    old_l0 = Index(cleanup_interruption)
    old_l2 = Index()
    store.l0_index = old_l0
    store.l2_index = old_l2
    replacement_l0 = Index()
    replacement_l2 = Index()
    view = SimpleNamespace(
        ownership=ownership,
        verify_final=lambda: None,
        close=lambda: None,
    )
    monkeypatch.setattr(
        "codenib.index.embedding.vector_store.capture_authenticated_vector_view",
        lambda _path: view,
    )
    monkeypatch.setattr(
        store,
        "_load_captured",
        lambda _view: _LoadedVectorState(
            l0_index=replacement_l0,
            l0_documents=[object()],
            l2_index=replacement_l2,
            l2_documents=[object()],
            artifact_metadata={"embedding_fingerprint": "sha256:replacement"},
            store_path=tmp_path / "replacement",
        ),
    )

    source_lines, first_line = inspect.getsourcelines(CodeVectorStore.load)
    target_lines = [
        first_line + index
        for index, source_line in enumerate(source_lines)
        if source_line.strip() == "body_completed = True"
    ]
    assert len(target_lines) == 1
    target_line = target_lines[0]
    # Keep the injected exception inside a disposable thread: trace-raised
    # exceptions can otherwise remain as ambient state for later tests.
    locals_to_fast = ctypes.pythonapi.PyFrame_LocalsToFast
    locals_to_fast.argtypes = [ctypes.py_object, ctypes.c_int]
    locals_to_fast.restype = None

    class HostileAmbientError(OSError):
        @property
        def __traceback__(self):
            raise AssertionError("raw traceback access used the subclass property")

    ambient_error = HostileAmbientError("ambient caller and post-publication failure")
    worker_failure: list[str] = []

    def run_load() -> None:
        try:
            try:
                raise ambient_error
            except OSError as active_error:
                assert active_error is ambient_error
                entry_traceback = BaseException.__traceback__.__get__(
                    ambient_error,
                    type(ambient_error),
                )
                injected = False

                def inject_after_publication(frame, event, _arg):
                    nonlocal injected
                    if (
                        frame.f_code is CodeVectorStore.load.__code__
                        and event == "line"
                        and frame.f_lineno == target_line
                        and not injected
                    ):
                        assert frame.f_locals["body_completed"] is False
                        frame.f_locals["body_completed"] = True
                        locals_to_fast(frame, 1)
                        assert frame.f_locals["body_completed"] is True
                        injected = True
                        sys.settrace(None)
                        raise ambient_error
                    return inject_after_publication

                sys.settrace(inject_after_publication)
                try:
                    store.load(native_index_authorization=authorization)
                except (Exception, KeyboardInterrupt) as observed:
                    assert observed is ambient_error
                else:
                    raise AssertionError(
                        "post-publication failure was suppressed"
                    ) from None
                finally:
                    sys.settrace(None)
                assert injected
                assert (
                    BaseException.__traceback__.__get__(
                        ambient_error,
                        type(ambient_error),
                    )
                    is not entry_traceback
                )

            assert store.l0_index is replacement_l0
            assert store.l2_index is replacement_l2
            assert replacement_l0.reset_calls == replacement_l2.reset_calls == 0
            owner = ambient_error.vector_index_cleanup_owner
            assert owner.indices == [old_l0]
            old_l0.failure = None
            owner.close()
            assert owner.closed
            assert old_l0.reset_calls == 2
            assert old_l2.reset_calls == 1
        except (Exception, KeyboardInterrupt) as failure:
            worker_failure.append(type(failure).__name__)

    worker = threading.Thread(target=run_load)
    worker.start()
    worker.join()
    assert not worker.is_alive()
    assert worker_failure == []


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("index reset failed"), KeyboardInterrupt("index reset cancelled")],
)
def test_close_visits_all_indices_and_retains_failed_index_for_retry(
    tmp_path,
    failure: BaseException,
) -> None:
    store = _store(tmp_path, fingerprint="sha256:expected")

    class _Index:
        def __init__(self, reset_failure=None) -> None:
            self.reset_failure = reset_failure
            self.reset_calls = 0

        def reset(self) -> None:
            self.reset_calls += 1
            if self.reset_failure is not None:
                raise self.reset_failure

    first = _Index(failure)
    second = _Index()
    store.l0_index = first
    store.l0_documents = [object()]
    store.l2_index = second
    store.l2_documents = [object()]

    with pytest.raises(type(failure)) as caught:
        store.close()

    assert caught.value is failure
    assert first.reset_calls == second.reset_calls == 1
    assert store.l0_index is first
    assert len(store.l0_documents) == 1
    assert store.l2_index is None
    assert store.l2_documents == []
    assert store.embedding is not None

    first.reset_failure = None
    store.close()

    assert first.reset_calls == 2
    assert second.reset_calls == 1
    assert store.l0_index is store.l2_index is None
    assert store.embedding is None


def test_closed_attestation_includes_query_cache_state(tmp_path) -> None:
    store = _store(tmp_path, fingerprint="sha256:expected")
    store._loaded_state = _LoadedVectorState(None, [], None, [], {}, tmp_path)
    store.embedding = None
    store._query_cache_depth = 1
    store._cached_query_text = "query"
    store._cached_query_vector = object()

    assert store.closed is False

    store._query_cache_depth = 0
    store._cached_query_text = None
    store._cached_query_vector = None
    assert store.closed is True


def test_openai_wrapper_sends_vector_options_on_embedding_requests() -> None:
    client = MagicMock()
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[1.0, 2.0])]
    )
    with patch("openai.OpenAI", return_value=client) as factory:
        embedding = _OpenAIEmbeddingWrapper(
            "text-embedding-3-small",
            request_options={"dimensions": 2},
            api_key="runtime-secret",
        )
        assert embedding.embed_query("query") == [1.0, 2.0]

    factory.assert_called_once_with(api_key="runtime-secret")
    client.embeddings.create.assert_called_once_with(
        input=["query"],
        model="text-embedding-3-small",
        dimensions=2,
    )


def test_openai_wrapper_batches_document_embeddings_in_order() -> None:
    client = MagicMock()
    client.embeddings.create.side_effect = [
        SimpleNamespace(
            data=[
                SimpleNamespace(index=1, embedding=[2.0]),
                SimpleNamespace(index=0, embedding=[1.0]),
            ]
        ),
        SimpleNamespace(
            data=[
                SimpleNamespace(index=1, embedding=[4.0]),
                SimpleNamespace(index=0, embedding=[3.0]),
            ]
        ),
        SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[5.0])]),
    ]
    with patch("openai.OpenAI", return_value=client):
        embedding = _OpenAIEmbeddingWrapper("served", batch_size=2)

    assert embedding.embed_documents(["a", "b", "c", "d", "e"]) == [
        [1.0],
        [2.0],
        [3.0],
        [4.0],
        [5.0],
    ]
    assert [
        call.kwargs["input"] for call in client.embeddings.create.call_args_list
    ] == [
        ["a", "b"],
        ["c", "d"],
        ["e"],
    ]


def test_openai_wrapper_splits_only_context_limit_failures() -> None:
    class ContextLimitError(Exception):
        status_code = 400

    client = MagicMock()

    def create(*, input, **_kwargs):
        if len(input) > 1:
            raise ContextLimitError("maximum context length; input_tokens")
        return SimpleNamespace(
            data=[SimpleNamespace(index=0, embedding=[float(ord(input[0]))])]
        )

    client.embeddings.create.side_effect = create
    with patch("openai.OpenAI", return_value=client):
        embedding = _OpenAIEmbeddingWrapper("served", batch_size=4)

    assert embedding.embed_documents(["a", "b", "c"]) == [
        [97.0],
        [98.0],
        [99.0],
    ]


def test_openai_wrapper_bounds_one_oversized_document_deterministically() -> None:
    client = MagicMock()

    def create(*, input, **_kwargs):
        assert len(input) == 1
        assert len(input[0]) == 80
        assert input[0].startswith("a" * 20)
        assert input[0].endswith("z" * 5)
        return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0])])

    client.embeddings.create.side_effect = create
    with patch("openai.OpenAI", return_value=client):
        embedding = _OpenAIEmbeddingWrapper("served", max_input_chars=80)

    assert embedding.embed_documents(["a" * 100 + "z" * 100]) == [[1.0]]
