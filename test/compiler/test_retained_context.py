# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import select
import signal
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

import codenib.mcp.retained_context as retained_context_module
from codenib.compiler.manifest import RepoManifest
from codenib.mcp.context import ServerContext
from codenib.mcp.retained_context import (
    RetainedServerContextOwner,
    RetainedServerContextResult,
    load_retained_server_context_ref,
    load_retained_server_context_snapshot,
)
from codenib.storage import PublishConflict
from codenib.storage.models import NamespaceIdentity, RepositoryIdentity

from .test_manifest_export import _retained_fixture
from .test_manifest_import import _TestWorkspaceProvider


def test_loads_retained_ref_with_bm25_inside_exact_reader(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        owner = RetainedServerContextOwner()
        destination = tmp_path / "context"
        try:
            with (
                patch(
                    "codenib.artifacts.runtime.reopen_authenticated_directory",
                    side_effect=AssertionError(
                        "retained query binding reopened a path"
                    ),
                ),
                patch(
                    "codenib.mcp.context.reopen_authenticated_directory",
                    side_effect=AssertionError("retained BM25 reopened a path"),
                ),
            ):
                result = load_retained_server_context_ref(
                    "owner/repo",
                    destination,
                    catalog=catalog,
                    object_store=object_store,
                    workspace_provider=_TestWorkspaceProvider(),
                    runtime_owner=owner,
                    expected_generation=imported.generation,
                )

            assert type(result) is RetainedServerContextResult
            assert result is owner.result
            assert owner.state == "active"
            assert result.materialization.export_receipt.ref_generation == (
                imported.generation
            )
            assert result.loaded_views == ("bm25",)
            assert result.view_error_items == ()
            assert owner.context.bm25 is not None
            hits = owner.context.bm25.search("VALUE", return_code_content=True)
            assert hits and hits[0].node_id == "sample.VALUE"
            assert hits[0].content is None
            assert owner.context.artifact == {
                "verified": True,
                "schema": "codenib.context-artifact.v1",
                "repository": "owner/repo",
                "commit": "a" * 40,
                "views": ["bm25"],
            }
            assert owner.context.source_verified is False
            assert owner._source_owner.closed
        finally:
            owner.close()
        assert owner.closed
        assert destination.is_dir()


def test_loads_retained_ref_with_reader_bound_source(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (fixture, imported, object_store, catalog):
        owner = RetainedServerContextOwner()
        destination = tmp_path / "context"
        captured_sources: list[object] = []
        real_bind = retained_context_module.bind_context_artifact_reader

        def capture_binding(*args: object, **kwargs: object):
            binding = real_bind(*args, **kwargs)
            source = binding.source_binding
            assert source is not None
            captured_sources.append(source)
            return binding

        try:
            with (
                patch(
                    "codenib.artifacts.runtime.reopen_authenticated_directory",
                    side_effect=AssertionError(
                        "reader-bound retained artifact reopened a path"
                    ),
                ),
                patch(
                    "codenib.mcp.context.reopen_authenticated_directory",
                    side_effect=AssertionError("retained BM25 reopened a path"),
                ),
                patch.object(
                    retained_context_module,
                    "bind_context_artifact_reader",
                    side_effect=capture_binding,
                ),
            ):
                result = load_retained_server_context_ref(
                    "owner/repo",
                    destination,
                    catalog=catalog,
                    object_store=object_store,
                    workspace_provider=_TestWorkspaceProvider(),
                    runtime_owner=owner,
                    expected_generation=imported.generation,
                    repo_path=fixture.repository,
                )

            assert result is owner.result
            assert owner.state == "active"
            assert len(captured_sources) == 1
            source = captured_sources[0]
            assert owner.context._source_binding is source
            assert owner._source_owner.pending_sources == (source,)
            assert owner.context.source_verified is True
            assert owner.context.source_verification_scope == "content-bytes"
            assert owner.context.commit_verified is False
            assert owner.context.manifest.repo_path == str(fixture.repository)
            assert (
                owner.context.read_source_bytes(
                    "sample.py",
                    max_bytes=1024,
                )
                == b"VALUE = 1\n"
            )
            hits = owner.context.bm25.search("VALUE", return_code_content=True)
            assert hits and hits[0].node_id == "sample.VALUE"
        finally:
            owner.close()
        assert owner.closed
        assert owner._source_owner.closed
        assert captured_sources and captured_sources[0].closed


def test_loads_retained_snapshot_with_vector_native_inert(
    tmp_path: Path,
) -> None:
    with _retained_fixture(tmp_path / "retained") as (
        _fixture,
        imported,
        object_store,
        catalog,
    ):
        owner = RetainedServerContextOwner()
        destination = tmp_path / "context"
        try:
            with patch(
                "codenib.mcp.context.require_authorized_vector_view"
            ) as native_gate:
                result = load_retained_server_context_snapshot(
                    "owner/repo",
                    imported.snapshot_id,
                    destination,
                    catalog=catalog,
                    object_store=object_store,
                    workspace_provider=_TestWorkspaceProvider(),
                    runtime_owner=owner,
                )

            native_gate.assert_not_called()
            assert result.materialization.export_receipt.ref_name is None
            assert result.loaded_views == ("bm25",)
            assert result.view_error_items == (
                (
                    "vector",
                    "portable artifact contexts cannot use external authorization "
                    "for native vector parsing",
                ),
            )
            assert owner.context.vector is None
            assert owner.context._native_index_authorization is None
        finally:
            owner.close()


def test_loads_retained_snapshot_with_source_and_vector_native_inert(
    tmp_path: Path,
) -> None:
    with _retained_fixture(tmp_path / "retained") as (
        fixture,
        imported,
        object_store,
        catalog,
    ):
        owner = RetainedServerContextOwner()
        destination = tmp_path / "context"
        try:
            with patch(
                "codenib.mcp.context.require_authorized_vector_view"
            ) as native_gate:
                result = load_retained_server_context_snapshot(
                    "owner/repo",
                    imported.snapshot_id,
                    destination,
                    catalog=catalog,
                    object_store=object_store,
                    workspace_provider=_TestWorkspaceProvider(),
                    runtime_owner=owner,
                    repo_path=fixture.repository,
                )

            native_gate.assert_not_called()
            assert result.loaded_views == ("bm25",)
            assert result.view_error_items == (
                (
                    "vector",
                    "portable artifact contexts cannot use external authorization "
                    "for native vector parsing",
                ),
            )
            assert owner.context.vector is None
            assert owner.context._native_index_authorization is None
            assert owner.context.source_verified is True
            assert (
                owner.context.read_source_bytes(
                    "sample.py",
                    max_bytes=1024,
                )
                == b"VALUE = 1\n"
            )
        finally:
            owner.close()


def test_vector_only_generation_fails_before_context_activation(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("vector",),
    ) as (_fixture, imported, object_store, catalog):
        owner = RetainedServerContextOwner()
        destination = tmp_path / "context"

        with pytest.raises(ValueError, match="require.*BM25"):
            load_retained_server_context_snapshot(
                "owner/repo",
                imported.snapshot_id,
                destination,
                catalog=catalog,
                object_store=object_store,
                workspace_provider=_TestWorkspaceProvider(),
                runtime_owner=owner,
            )

        assert owner.closed
        assert destination.is_dir()
        with pytest.raises(RuntimeError, match="expected active"):
            _ = owner.context


def test_expected_generation_conflict_never_reads_object_storage(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        owner = RetainedServerContextOwner()
        destination = tmp_path / "context"

        forbidden = AssertionError("generation conflict reached object storage")
        with (
            patch.object(object_store, "open", side_effect=forbidden),
            patch.object(object_store, "verify_receipt", side_effect=forbidden),
            patch.object(object_store, "retain_receipts", side_effect=forbidden),
            pytest.raises(PublishConflict, match="generation"),
        ):
            load_retained_server_context_ref(
                "owner/repo",
                destination,
                catalog=catalog,
                object_store=object_store,
                workspace_provider=_TestWorkspaceProvider(),
                runtime_owner=owner,
                expected_generation=imported.generation + 1,
            )

        assert owner.closed
        assert not destination.exists()


@pytest.mark.parametrize(
    ("repo_path", "error_type", "message"),
    [
        (object(), TypeError, "text or a Path"),
        ("", ValueError, "non-empty path text"),
        ("bad\x00path", ValueError, "without NUL"),
    ],
)
def test_invalid_source_repo_path_fails_before_materialization(
    tmp_path: Path,
    repo_path: object,
    error_type: type[BaseException],
    message: str,
) -> None:
    owner = RetainedServerContextOwner()
    with (
        patch.object(
            retained_context_module,
            "materialize_retained_repo_manifest_ref",
        ) as materialize,
        pytest.raises(error_type, match=message),
    ):
        load_retained_server_context_ref(
            "owner/repo",
            tmp_path / "context",
            catalog=object(),  # type: ignore[arg-type]
            object_store=object(),  # type: ignore[arg-type]
            workspace_provider=object(),  # type: ignore[arg-type]
            runtime_owner=owner,
            repo_path=repo_path,  # type: ignore[arg-type]
        )

    materialize.assert_not_called()
    assert owner.state == "empty"
    owner.close()
    assert owner.closed


@pytest.mark.parametrize("forgery", ["digest", "size"])
def test_export_receipt_must_match_materialized_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forgery: str,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        owner = RetainedServerContextOwner()
        destination = tmp_path / "context"
        real_materialize = (
            retained_context_module.materialize_retained_repo_manifest_ref
        )

        def forged_materialize(*args: object, **kwargs: object):
            result = real_materialize(*args, **kwargs)
            receipt_changes = (
                {"manifest_digest": "0" * 64}
                if forgery == "digest"
                else {
                    "manifest_byte_size": result.export_receipt.manifest_byte_size + 1
                }
            )
            return replace(
                result,
                export_receipt=replace(result.export_receipt, **receipt_changes),
            )

        monkeypatch.setattr(
            retained_context_module,
            "materialize_retained_repo_manifest_ref",
            forged_materialize,
        )
        with (
            patch.object(
                retained_context_module,
                "query_context_artifact_reader",
                side_effect=AssertionError(
                    "forged export receipt reached artifact verification"
                ),
            ) as query_reader,
            patch.object(
                ServerContext,
                "load",
                side_effect=AssertionError(
                    "forged export receipt reached context activation"
                ),
            ) as load,
            pytest.raises(RuntimeError, match="receipt differs"),
        ):
            load_retained_server_context_ref(
                "owner/repo",
                destination,
                catalog=catalog,
                object_store=object_store,
                workspace_provider=_TestWorkspaceProvider(),
                runtime_owner=owner,
                expected_generation=imported.generation,
            )

        query_reader.assert_not_called()
        load.assert_not_called()
        assert owner.closed
        assert destination.is_dir()


@pytest.mark.parametrize("forgery", ["ref", "generation", "namespace"])
def test_ref_result_must_match_the_exact_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forgery: str,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        owner = RetainedServerContextOwner()
        destination = tmp_path / "context"
        real_materialize = (
            retained_context_module.materialize_retained_repo_manifest_ref
        )

        def forged_materialize(*args: object, **kwargs: object):
            result = real_materialize(*args, **kwargs)
            if forgery == "ref":
                receipt = replace(result.export_receipt, ref_name="wrong-ref")
            elif forgery == "generation":
                receipt = replace(
                    result.export_receipt,
                    ref_generation=result.export_receipt.ref_generation + 1,
                )
            else:
                namespace_id = NamespaceIdentity("other").namespace_id
                repository_id = RepositoryIdentity(
                    namespace_id=namespace_id,
                    repository_key=result.export_receipt.repository_key,
                ).repository_id
                receipt = replace(
                    result.export_receipt,
                    namespace_id=namespace_id,
                    repository_id=repository_id,
                )
            return replace(result, export_receipt=receipt)

        monkeypatch.setattr(
            retained_context_module,
            "materialize_retained_repo_manifest_ref",
            forged_materialize,
        )
        with (
            patch.object(
                retained_context_module,
                "query_context_artifact_reader",
                side_effect=AssertionError(
                    "wrong retained selector reached artifact verification"
                ),
            ) as query_reader,
            pytest.raises(RuntimeError, match="observation|repository or namespace"),
        ):
            load_retained_server_context_ref(
                "owner/repo",
                destination,
                catalog=catalog,
                object_store=object_store,
                workspace_provider=_TestWorkspaceProvider(),
                runtime_owner=owner,
                expected_generation=imported.generation,
            )

        query_reader.assert_not_called()
        assert owner.closed
        assert destination.is_dir()


def test_snapshot_result_must_match_the_exact_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        owner = RetainedServerContextOwner()
        destination = tmp_path / "context"
        real_materialize = (
            retained_context_module.materialize_retained_repo_manifest_snapshot
        )

        def forged_materialize(*args: object, **kwargs: object):
            result = real_materialize(*args, **kwargs)
            return replace(
                result,
                export_receipt=replace(
                    result.export_receipt,
                    snapshot_id="wrong-snapshot",
                ),
            )

        monkeypatch.setattr(
            retained_context_module,
            "materialize_retained_repo_manifest_snapshot",
            forged_materialize,
        )
        with (
            patch.object(
                retained_context_module,
                "query_context_artifact_reader",
                side_effect=AssertionError(
                    "wrong retained snapshot reached artifact verification"
                ),
            ) as query_reader,
            pytest.raises(RuntimeError, match="snapshot observation"),
        ):
            load_retained_server_context_snapshot(
                "owner/repo",
                imported.snapshot_id,
                destination,
                catalog=catalog,
                object_store=object_store,
                workspace_provider=_TestWorkspaceProvider(),
                runtime_owner=owner,
            )

        query_reader.assert_not_called()
        assert owner.closed
        assert destination.is_dir()


def test_active_owner_is_one_shot_without_disturbing_first_context(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        owner = RetainedServerContextOwner()
        first = load_retained_server_context_ref(
            "owner/repo",
            tmp_path / "first",
            catalog=catalog,
            object_store=object_store,
            workspace_provider=_TestWorkspaceProvider(),
            runtime_owner=owner,
            expected_generation=imported.generation,
        )
        original = owner.context
        try:
            with (
                patch.object(
                    retained_context_module,
                    "materialize_retained_repo_manifest_ref",
                ) as materialize,
                pytest.raises(RuntimeError, match="expected empty"),
            ):
                load_retained_server_context_ref(
                    "owner/repo",
                    tmp_path / "second",
                    catalog=catalog,
                    object_store=object_store,
                    workspace_provider=_TestWorkspaceProvider(),
                    runtime_owner=owner,
                )
            materialize.assert_not_called()
            assert owner.state == "active"
            assert owner.context is original
            assert owner.result is first
        finally:
            owner.close()


class _RetryReceiptOwner:
    def __init__(self, events: list[str], *, fail_once: bool = False) -> None:
        self.events = events
        self.fail_once = fail_once
        self.closed = False

    def close(self) -> None:
        self.events.append("receipt")
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("receipt close failed")
        self.closed = True


class _RetrySourceOwner:
    def __init__(self, events: list[str], *, fail_once: bool = False) -> None:
        self.events = events
        self.fail_once = fail_once
        self.closed = False

    @property
    def pending_sources(self) -> tuple[object, ...]:
        return () if self.closed else (self,)

    def close(self) -> None:
        self.events.append("source")
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("source close failed")
        self.closed = True


class _InjectedSourceOwner:
    def __init__(
        self,
        events: list[str],
        failure: BaseException,
        *,
        closes_before_failure: bool,
    ) -> None:
        self.events = events
        self.failure: BaseException | None = failure
        self.closes_before_failure = closes_before_failure
        self.closed = False

    @property
    def pending_sources(self) -> tuple[object, ...]:
        return () if self.closed else (self,)

    def close(self) -> None:
        self.events.append("source")
        failure = self.failure
        self.failure = None
        if failure is not None:
            if self.closes_before_failure:
                self.closed = True
            raise failure
        self.closed = True


class _InjectedReceiptOwner:
    def __init__(
        self,
        events: list[str],
        failure: BaseException,
        *,
        closes_before_failure: bool,
    ) -> None:
        self.events = events
        self.failure: BaseException | None = failure
        self.closes_before_failure = closes_before_failure
        self.closed = False

    def close(self) -> None:
        self.events.append("receipt")
        failure = self.failure
        self.failure = None
        if failure is not None:
            if self.closes_before_failure:
                self.closed = True
            raise failure
        self.closed = True


def _cleanup_notes(error: BaseException) -> tuple[str, ...]:
    return (
        *getattr(error, "__notes__", ()),
        *getattr(error, "_codenib_cleanup_notes", ()),
    )


def _loading_owner_with_context() -> tuple[RetainedServerContextOwner, ServerContext]:
    owner = RetainedServerContextOwner()
    owner._begin_loading(object())
    context = ServerContext(manifest=RepoManifest(repo_path="/repo"))
    owner._install_context(context)
    return owner, context


def test_context_cleanup_failure_blocks_receipt_and_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, context = _loading_owner_with_context()
    events: list[str] = []
    source_owner = _RetrySourceOwner(events)
    receipt_owner = _RetryReceiptOwner(events)
    owner._source_owner = source_owner  # type: ignore[assignment]
    owner._receipt_owner = receipt_owner  # type: ignore[assignment]
    attempts = 0

    def close_context(observed: ServerContext) -> None:
        nonlocal attempts
        assert observed is context
        attempts += 1
        events.append("context")
        if attempts == 1:
            raise RuntimeError("context close failed")

    monkeypatch.setattr(ServerContext, "close", close_context)

    with pytest.raises(RuntimeError, match="context close failed") as caught:
        owner.close()
    assert owner.state == "close-failed"
    assert events == ["context", "source"]
    assert source_owner.closed
    assert not receipt_owner.closed
    assert caught.value.publication_cleanup_owners == (owner,)  # type: ignore[attr-defined]

    owner.close()
    assert events == ["context", "source", "context", "receipt"]
    assert source_owner.closed
    assert receipt_owner.closed
    assert owner.closed


def test_context_cleanup_failure_remains_primary_when_source_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, context = _loading_owner_with_context()
    events: list[str] = []
    source_owner = _RetrySourceOwner(events, fail_once=True)
    receipt_owner = _RetryReceiptOwner(events)
    owner._source_owner = source_owner  # type: ignore[assignment]
    owner._receipt_owner = receipt_owner  # type: ignore[assignment]
    context_failure = RuntimeError("context close failed")
    attempts = 0

    def close_context(observed: ServerContext) -> None:
        nonlocal attempts
        assert observed is context
        attempts += 1
        events.append("context")
        if attempts == 1:
            raise context_failure

    monkeypatch.setattr(ServerContext, "close", close_context)

    with pytest.raises(RuntimeError) as caught:
        owner.close()
    assert caught.value is context_failure
    assert owner.state == "close-failed"
    assert events == ["context", "source"]
    assert not source_owner.closed
    assert not receipt_owner.closed
    assert any(
        "retained source cleanup also failed" in note
        for note in _cleanup_notes(caught.value)
    )

    owner.close()
    assert events == ["context", "source", "context", "source", "receipt"]
    assert owner.closed


def test_source_cancellation_promotes_context_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, context = _loading_owner_with_context()
    events: list[str] = []
    context_failure = RuntimeError("context close failed")
    source_interruption = KeyboardInterrupt("source close interrupted")
    source_owner = _InjectedSourceOwner(
        events,
        source_interruption,
        closes_before_failure=False,
    )
    receipt_owner = _RetryReceiptOwner(events)
    owner._source_owner = source_owner  # type: ignore[assignment]
    owner._receipt_owner = receipt_owner  # type: ignore[assignment]
    attempts = 0

    def close_context(observed: ServerContext) -> None:
        nonlocal attempts
        assert observed is context
        attempts += 1
        events.append("context")
        if attempts == 1:
            raise context_failure

    monkeypatch.setattr(ServerContext, "close", close_context)

    with pytest.raises(KeyboardInterrupt) as caught:
        owner.close()
    assert caught.value is source_interruption
    assert owner.state == "close-failed"
    assert events == ["context", "source"]
    assert not source_owner.closed
    assert not receipt_owner.closed
    assert caught.value.publication_cleanup_owners == (owner,)  # type: ignore[attr-defined]
    assert any(
        "retained context cleanup also failed" in note
        and "context close failed" in note
        for note in _cleanup_notes(caught.value)
    )

    owner.close()
    assert events == ["context", "source", "context", "source", "receipt"]
    assert owner.closed


def test_receipt_cancellation_promotes_completed_source_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, context = _loading_owner_with_context()
    events: list[str] = []
    source_failure = RuntimeError("source close failed after completion")
    receipt_exit = SystemExit("receipt close interrupted")
    source_owner = _InjectedSourceOwner(
        events,
        source_failure,
        closes_before_failure=True,
    )
    receipt_owner = _InjectedReceiptOwner(
        events,
        receipt_exit,
        closes_before_failure=False,
    )
    owner._source_owner = source_owner  # type: ignore[assignment]
    owner._receipt_owner = receipt_owner  # type: ignore[assignment]

    def close_context(observed: ServerContext) -> None:
        assert observed is context
        events.append("context")

    monkeypatch.setattr(ServerContext, "close", close_context)

    with pytest.raises(SystemExit) as caught:
        owner.close()
    assert caught.value is receipt_exit
    assert owner.state == "close-failed"
    assert events == ["context", "source", "receipt"]
    assert source_owner.closed
    assert not receipt_owner.closed
    assert caught.value.publication_cleanup_owners == (owner,)  # type: ignore[attr-defined]
    assert any(
        "retained context or source cleanup also failed" in note
        and "source close failed after completion" in note
        for note in _cleanup_notes(caught.value)
    )

    owner.close()
    assert events == ["context", "source", "receipt", "receipt"]
    assert owner.closed


def test_inherited_receipt_cancellation_promotes_source_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = RetainedServerContextOwner()
    events: list[str] = []
    source_failure = RuntimeError("inherited source close failed")
    receipt_exit = SystemExit("inherited receipt close interrupted")
    source_owner = _InjectedSourceOwner(
        events,
        source_failure,
        closes_before_failure=True,
    )
    receipt_owner = _InjectedReceiptOwner(
        events,
        receipt_exit,
        closes_before_failure=True,
    )
    owner._source_owner = source_owner  # type: ignore[assignment]
    owner._receipt_owner = receipt_owner  # type: ignore[assignment]
    owner_pid = owner._owner_pid

    with monkeypatch.context() as child:
        child.setattr(retained_context_module.os, "getpid", lambda: owner_pid + 1)
        with pytest.raises(SystemExit) as caught:
            owner.close()

    assert caught.value is receipt_exit
    assert events == ["source", "receipt"]
    assert source_owner.closed
    assert receipt_owner.closed
    assert any(
        "retained source cleanup also failed" in note
        and "inherited source close failed" in note
        for note in _cleanup_notes(receipt_exit)
    )
    assert any(
        "PID boundary also observed" in note for note in _cleanup_notes(receipt_exit)
    )
    assert owner.closed


def test_receipt_cleanup_retry_does_not_reclose_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, context = _loading_owner_with_context()
    events: list[str] = []
    source_owner = _RetrySourceOwner(events)
    receipt_owner = _RetryReceiptOwner(events, fail_once=True)
    owner._source_owner = source_owner  # type: ignore[assignment]
    owner._receipt_owner = receipt_owner  # type: ignore[assignment]

    def close_context(observed: ServerContext) -> None:
        assert observed is context
        events.append("context")

    monkeypatch.setattr(ServerContext, "close", close_context)

    with pytest.raises(RuntimeError, match="receipt close failed") as caught:
        owner.close()
    assert owner.state == "close-failed"
    assert events == ["context", "source", "receipt"]
    assert caught.value.publication_cleanup_owners == (owner,)  # type: ignore[attr-defined]

    owner.close()
    assert events == ["context", "source", "receipt", "receipt"]
    assert source_owner.closed
    assert owner.closed


def test_source_cleanup_failure_blocks_receipt_and_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, context = _loading_owner_with_context()
    events: list[str] = []
    source_owner = _RetrySourceOwner(events, fail_once=True)
    receipt_owner = _RetryReceiptOwner(events)
    owner._source_owner = source_owner  # type: ignore[assignment]
    owner._receipt_owner = receipt_owner  # type: ignore[assignment]

    def close_context(observed: ServerContext) -> None:
        assert observed is context
        events.append("context")

    monkeypatch.setattr(ServerContext, "close", close_context)

    with pytest.raises(RuntimeError, match="source close failed") as caught:
        owner.close()
    assert owner.state == "close-failed"
    assert events == ["context", "source"]
    assert not source_owner.closed
    assert not receipt_owner.closed
    assert caught.value.publication_cleanup_owners == (owner,)  # type: ignore[attr-defined]

    owner.close()
    assert events == ["context", "source", "source", "receipt"]
    assert source_owner.closed
    assert receipt_owner.closed
    assert owner.closed


def test_owner_properties_are_state_and_pid_guarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = RetainedServerContextOwner()
    assert owner.state == "empty"
    with pytest.raises(RuntimeError, match="expected active"):
        _ = owner.context
    with pytest.raises(RuntimeError, match="expected active"):
        _ = owner.result

    owner_pid = owner._owner_pid
    with monkeypatch.context() as changed:
        changed.setattr(retained_context_module.os, "getpid", lambda: owner_pid + 1)
        with pytest.raises(RuntimeError, match="PID boundary"):
            _ = owner.state
        with pytest.raises(RuntimeError, match="PID boundary"):
            _ = owner.context
        with pytest.raises(RuntimeError, match="PID boundary"):
            _ = owner.result
        with pytest.raises(RuntimeError, match="PID boundary"):
            _ = owner.closed
        with pytest.raises(RuntimeError, match="PID boundary"):
            owner.close()

    owner.close()
    assert owner.closed


def test_interruption_after_loading_reservation_closes_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = RetainedServerContextOwner()
    interruption = KeyboardInterrupt("after loading reservation")
    begin_loading = RetainedServerContextOwner._begin_loading

    def begin_then_interrupt(
        observed: RetainedServerContextOwner,
        reservation: object,
    ) -> None:
        begin_loading(observed, reservation)
        raise interruption

    monkeypatch.setattr(
        RetainedServerContextOwner,
        "_begin_loading",
        begin_then_interrupt,
    )
    with (
        patch.object(
            retained_context_module,
            "materialize_retained_repo_manifest_ref",
        ) as materialize,
        pytest.raises(KeyboardInterrupt) as caught,
    ):
        load_retained_server_context_ref(
            "owner/repo",
            tmp_path / "context",
            catalog=object(),  # type: ignore[arg-type]
            object_store=object(),  # type: ignore[arg-type]
            workspace_provider=object(),  # type: ignore[arg-type]
            runtime_owner=owner,
        )

    assert caught.value is interruption
    materialize.assert_not_called()
    assert owner.closed


@pytest.mark.parametrize(
    ("seam", "failure"),
    [
        ("reader", RuntimeError("reader binding failed")),
        ("context", KeyboardInterrupt("context load interrupted")),
    ],
)
def test_postpublication_preinstall_failure_closes_receipt_and_keeps_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
    failure: BaseException,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        owner = RetainedServerContextOwner()
        destination = tmp_path / "context"

        def fail_after_publication(*_args: object, **_kwargs: object) -> None:
            assert destination.is_dir()
            assert owner._receipt_owner.active
            raise failure

        if seam == "reader":
            monkeypatch.setattr(
                retained_context_module,
                "query_context_artifact_reader",
                fail_after_publication,
            )
        else:
            monkeypatch.setattr(ServerContext, "load", fail_after_publication)

        with pytest.raises(type(failure)) as caught:
            load_retained_server_context_ref(
                "owner/repo",
                destination,
                catalog=catalog,
                object_store=object_store,
                workspace_provider=_TestWorkspaceProvider(),
                runtime_owner=owner,
                expected_generation=imported.generation,
            )

        assert caught.value is failure
        assert owner.closed
        assert owner._receipt_owner.closed
        with pytest.raises(RuntimeError, match="expected active"):
            _ = owner.context
        assert destination.is_dir()


def test_interruption_after_reader_source_bind_closes_source_before_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (fixture, imported, object_store, catalog):
        owner = RetainedServerContextOwner()
        destination = tmp_path / "context"
        interruption = KeyboardInterrupt("after reader source binding")
        captured_sources: list[object] = []
        cleanup_events: list[str] = []
        real_bind = retained_context_module.bind_context_artifact_reader
        receipt_type = type(owner._receipt_owner)
        real_receipt_close = receipt_type.close

        def bind_then_interrupt(*args: object, **kwargs: object) -> None:
            binding = real_bind(*args, **kwargs)
            source = binding.source_binding
            assert source is not None
            captured_sources.append(source)
            raise interruption

        def close_receipt(receipt: object) -> None:
            assert captured_sources and captured_sources[0].closed
            cleanup_events.append("receipt")
            real_receipt_close(receipt)

        monkeypatch.setattr(
            retained_context_module,
            "bind_context_artifact_reader",
            bind_then_interrupt,
        )
        monkeypatch.setattr(receipt_type, "close", close_receipt)

        with pytest.raises(KeyboardInterrupt) as caught:
            load_retained_server_context_ref(
                "owner/repo",
                destination,
                catalog=catalog,
                object_store=object_store,
                workspace_provider=_TestWorkspaceProvider(),
                runtime_owner=owner,
                expected_generation=imported.generation,
                repo_path=fixture.repository,
            )

        assert caught.value is interruption
        assert len(captured_sources) == 1
        assert captured_sources[0].closed
        assert owner._source_owner.closed
        assert owner._receipt_owner.closed
        assert cleanup_events == ["receipt"]
        assert owner.closed
        assert destination.is_dir()


def test_source_bound_result_interruption_closes_context_source_then_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (fixture, imported, object_store, catalog):
        owner = RetainedServerContextOwner()
        destination = tmp_path / "context"
        interruption = KeyboardInterrupt("after source-bound result installation")
        cleanup_events: list[str] = []
        installed: list[ServerContext] = []
        real_install_context = RetainedServerContextOwner._install_context
        real_install_result = RetainedServerContextOwner._install_result
        real_context_close = ServerContext.close
        source_owner_type = type(owner._source_owner)
        real_source_close = source_owner_type.close
        receipt_type = type(owner._receipt_owner)
        real_receipt_close = receipt_type.close

        def install_context(
            observed: RetainedServerContextOwner,
            context: ServerContext,
        ) -> None:
            installed.append(context)
            real_install_context(observed, context)

        def install_result_then_interrupt(
            observed: RetainedServerContextOwner,
            result: RetainedServerContextResult,
        ) -> None:
            real_install_result(observed, result)
            raise interruption

        def close_context(context: ServerContext) -> None:
            cleanup_events.append("context")
            real_context_close(context)

        def close_source(source_owner: object) -> None:
            assert installed and installed[0]._source_binding is None
            cleanup_events.append("source")
            real_source_close(source_owner)

        def close_receipt(receipt: object) -> None:
            assert owner._source_owner.closed
            cleanup_events.append("receipt")
            real_receipt_close(receipt)

        monkeypatch.setattr(
            RetainedServerContextOwner,
            "_install_context",
            install_context,
        )
        monkeypatch.setattr(
            RetainedServerContextOwner,
            "_install_result",
            install_result_then_interrupt,
        )
        monkeypatch.setattr(ServerContext, "close", close_context)
        monkeypatch.setattr(source_owner_type, "close", close_source)
        monkeypatch.setattr(receipt_type, "close", close_receipt)

        with pytest.raises(KeyboardInterrupt) as caught:
            load_retained_server_context_ref(
                "owner/repo",
                destination,
                catalog=catalog,
                object_store=object_store,
                workspace_provider=_TestWorkspaceProvider(),
                runtime_owner=owner,
                expected_generation=imported.generation,
                repo_path=fixture.repository,
            )

        assert caught.value is interruption
        assert len(installed) == 1
        assert cleanup_events == ["context", "source", "receipt"]
        assert owner.closed
        assert owner._source_owner.closed
        assert owner._receipt_owner.closed
        assert destination.is_dir()


@pytest.mark.parametrize("phase", ["context", "result"])
def test_late_activation_interruption_closes_context_before_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        owner = RetainedServerContextOwner()
        destination = tmp_path / "context"
        interruption = KeyboardInterrupt(f"after {phase} installation")
        installed: list[ServerContext] = []
        closed: list[ServerContext] = []
        cleanup_events: list[str] = []
        real_install_context = RetainedServerContextOwner._install_context
        real_install_result = RetainedServerContextOwner._install_result
        real_context_close = ServerContext.close
        receipt_type = type(owner._receipt_owner)
        real_receipt_close = receipt_type.close

        def install_context_then_maybe_interrupt(
            observed: RetainedServerContextOwner,
            context: ServerContext,
        ) -> None:
            installed.append(context)
            real_install_context(observed, context)
            if phase == "context":
                raise interruption

        def install_result_then_maybe_interrupt(
            observed: RetainedServerContextOwner,
            result: RetainedServerContextResult,
        ) -> None:
            real_install_result(observed, result)
            if phase == "result":
                raise interruption

        def close_context(context: ServerContext) -> None:
            cleanup_events.append("context")
            closed.append(context)
            real_context_close(context)

        def close_receipt(receipt: object) -> None:
            assert installed and installed[0] in closed
            cleanup_events.append("receipt")
            real_receipt_close(receipt)

        monkeypatch.setattr(
            RetainedServerContextOwner,
            "_install_context",
            install_context_then_maybe_interrupt,
        )
        monkeypatch.setattr(
            RetainedServerContextOwner,
            "_install_result",
            install_result_then_maybe_interrupt,
        )
        monkeypatch.setattr(ServerContext, "close", close_context)
        monkeypatch.setattr(receipt_type, "close", close_receipt)

        with pytest.raises(KeyboardInterrupt) as caught:
            load_retained_server_context_ref(
                "owner/repo",
                destination,
                catalog=catalog,
                object_store=object_store,
                workspace_provider=_TestWorkspaceProvider(),
                runtime_owner=owner,
                expected_generation=imported.generation,
            )

        assert caught.value is interruption
        assert len(installed) == 1
        assert installed[0] in closed
        assert owner.closed
        assert owner._context_close_complete
        assert owner._receipt_owner.closed
        assert cleanup_events[-1] == "receipt"
        assert destination.is_dir()


@pytest.mark.skipif(
    not hasattr(os, "fork") or not Path("/proc/self/fd").is_dir(),
    reason="requires fork and Linux descriptor inspection",
)
def test_fork_child_revokes_source_and_publication_without_context_lock(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (fixture, imported, object_store, catalog):
        # The fixture's import authority is unrelated to the runtime owner.
        # Release it before the fork so any remaining repository descriptor in
        # the child can only belong to the source-bound retained context.
        fixture.repository_source.close()
        owner = RetainedServerContextOwner()
        destination = tmp_path / "context"
        load_retained_server_context_ref(
            "owner/repo",
            destination,
            catalog=catalog,
            object_store=object_store,
            workspace_provider=_TestWorkspaceProvider(),
            runtime_owner=owner,
            expected_generation=imported.generation,
            repo_path=fixture.repository,
        )
        assert owner.context.source_verified

        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def hold_context_lock() -> None:
            with owner.context._view_lock:
                lock_acquired.set()
                assert release_lock.wait(10)

        holder = threading.Thread(target=hold_context_lock)
        holder.start()
        assert lock_acquired.wait(5)

        read_descriptor, write_descriptor = os.pipe()
        child_pid = -1
        try:
            child_pid = os.fork()
        finally:
            if child_pid != 0:
                release_lock.set()
                holder.join(5)
        if child_pid != 0:
            assert not holder.is_alive()
        if child_pid == 0:
            os.close(read_descriptor)
            try:
                signal.alarm(5)
                try:
                    _ = owner.state
                except RuntimeError as state_error:
                    state_report = str(state_error)
                else:  # pragma: no cover - PID guard must fail closed
                    state_report = "state unexpectedly escaped"
                try:
                    owner.close()
                except RuntimeError as close_error:
                    descriptor_targets: list[str] = []
                    for name in os.listdir("/proc/self/fd"):
                        try:
                            target = os.readlink(f"/proc/self/fd/{name}")
                        except OSError:
                            continue
                        if (
                            str(destination) in target
                            or str(fixture.repository) in target
                        ):
                            descriptor_targets.append(target)
                    report = repr((state_report, str(close_error), descriptor_targets))
                    os.write(write_descriptor, report.encode("utf-8"))
            finally:
                os.close(write_descriptor)
                os._exit(0)

        os.close(write_descriptor)
        readable, _writable, _exceptional = select.select(
            [read_descriptor],
            [],
            [],
            10,
        )
        if not readable:  # pragma: no cover - watchdog for lock regressions
            os.kill(child_pid, signal.SIGKILL)
            os.waitpid(child_pid, 0)
            os.close(read_descriptor)
            pytest.fail("fork child did not reconcile inherited authority")
        report = os.read(read_descriptor, 1 << 20).decode("utf-8")
        os.close(read_descriptor)
        waited_pid, status = os.waitpid(child_pid, 0)

        assert waited_pid == child_pid
        assert os.waitstatus_to_exitcode(status) == 0
        assert report == repr(
            (
                "retained server context owner cannot cross a PID boundary",
                "retained server context owner cannot cross a PID boundary",
                [],
            )
        )
        assert owner.state == "active"
        assert owner.context.bm25 is not None
        owner.close()
        assert owner.closed
