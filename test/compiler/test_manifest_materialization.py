# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import inspect
import io
import json
import stat
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, BinaryIO, TypeVar
from unittest.mock import patch

import pytest

import codenib.compiler as compiler_module
import codenib.compiler.manifest_materialization as materialization_module
from codenib._captured_directory import PublishedWorkspaceReceiptOwner
from codenib._workspace_provider import StrictWorkspaceRequest, StrictWorkspaceSession
from codenib.artifacts import (
    bind_context_artifact,
    query_context_artifact,
    verify_context_artifact,
)
from codenib.compiler.manifest_export import (
    RepoManifestExportResult,
    export_retained_repo_manifest_snapshot,
)
from codenib.compiler.manifest_materialization import (
    materialize_retained_context_artifact,
)
from codenib.mcp.context import ServerContext
from codenib.mcp.tools.search import search_bm25_impl
from codenib.storage import (
    BlobInfo,
    LocalCAS,
    RepositoryIdentity,
    StorageIntegrityError,
)

from .test_manifest_export import _retained_fixture
from .test_manifest_import import _TestWorkspaceProvider

_Result = TypeVar("_Result")


class _RetainingReadStore:
    """Receipt-retaining read facade deliberately lacking ``put_chunks``."""

    def __init__(self, delegate: LocalCAS) -> None:
        self.delegate = delegate
        self.active = False
        self.opened: list[str] = []
        self.retention_requests: list[tuple[BlobInfo, ...]] = []
        self.final_verifications: list[str] = []

    def put_bytes(self, data: bytes) -> BlobInfo:
        del data
        raise AssertionError("retained materialization must not ingest objects")

    def put_file(self, source: str | Path) -> BlobInfo:
        del source
        raise AssertionError("retained materialization must not ingest objects")

    def has(self, digest: str) -> bool:
        del digest
        raise AssertionError("retained materialization must use exact receipts")

    def open(self, digest: str) -> BinaryIO:
        assert self.active
        self.opened.append(digest)
        return self.delegate.open(digest)

    def read_bytes(self, digest: str) -> bytes:
        del digest
        raise AssertionError("retained materialization must stream retained objects")

    def verify(self, digest: str) -> BlobInfo:
        del digest
        raise AssertionError("retained materialization must verify exact receipts")

    def materialize(self, digest: str, destination: str | Path) -> Path:
        del digest, destination
        raise AssertionError("retained materialization must publish one whole tree")

    def verify_receipt(self, expected: BlobInfo) -> BlobInfo:
        assert self.active
        self.final_verifications.append(expected.digest)
        return self.delegate.verify_receipt(expected)

    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], _Result],
    ) -> _Result:
        self.retention_requests.append(expected)

        def retained() -> _Result:
            assert not self.active
            self.active = True
            try:
                return callback()
            finally:
                self.active = False

        return self.delegate.retain_receipts(expected, retained)


class _ReceiptVerifyingOnlyStore:
    """Full object-store facade with no receipt-retention capability."""

    def __init__(self, delegate: LocalCAS) -> None:
        self.delegate = delegate
        self.calls = 0

    def put_bytes(self, data: bytes) -> BlobInfo:
        self.calls += 1
        return self.delegate.put_bytes(data)

    def put_file(self, source: str | Path) -> BlobInfo:
        self.calls += 1
        return self.delegate.put_file(source)

    def has(self, digest: str) -> bool:
        self.calls += 1
        return self.delegate.has(digest)

    def open(self, digest: str) -> BinaryIO:
        self.calls += 1
        return self.delegate.open(digest)

    def read_bytes(self, digest: str) -> bytes:
        self.calls += 1
        return self.delegate.read_bytes(digest)

    def verify(self, digest: str) -> BlobInfo:
        self.calls += 1
        return self.delegate.verify(digest)

    def materialize(self, digest: str, destination: str | Path) -> Path:
        self.calls += 1
        return self.delegate.materialize(digest, destination)

    def verify_receipt(self, expected: BlobInfo) -> BlobInfo:
        self.calls += 1
        return self.delegate.verify_receipt(expected)


class _RetentionCheckingProvider(_TestWorkspaceProvider):
    def __init__(self, store: _RetainingReadStore) -> None:
        super().__init__()
        self.store = store
        self.returned_while_retained = False

    def run_workspace(
        self,
        request: StrictWorkspaceRequest,
        *,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        operation: Callable[[StrictWorkspaceSession], _Result],
    ) -> _Result:
        assert self.store.active
        result = super().run_workspace(
            request,
            receipt_owner=receipt_owner,
            operation=operation,
        )
        assert self.store.active
        self.returned_while_retained = True
        return result


class _PostCommitFailureProvider(_TestWorkspaceProvider):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def run_workspace(
        self,
        request: StrictWorkspaceRequest,
        *,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        operation: Callable[[StrictWorkspaceSession], _Result],
    ) -> _Result:
        super().run_workspace(
            request,
            receipt_owner=receipt_owner,
            operation=operation,
        )
        assert receipt_owner.active
        raise self.error


class _TrackedBytesIO(io.BytesIO):
    close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        super().close()


class _CloseFailingBytesIO(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.allow_close = False
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if not self.allow_close:
            raise OSError("injected reader close failure")
        super().close()


class _TruncatingBundleStore(_RetainingReadStore):
    def __init__(self, delegate: LocalCAS, digest: str) -> None:
        super().__init__(delegate)
        self.digest = digest
        self.source: _TrackedBytesIO | None = None

    def open(self, digest: str) -> BinaryIO:
        if digest != self.digest:
            return super().open(digest)
        assert self.active
        self.opened.append(digest)
        payload = self.delegate.read_bytes(digest)
        self.source = _TrackedBytesIO(payload[:-1])
        return self.source


class _CloseFailingBundleStore(_RetainingReadStore):
    def __init__(self, delegate: LocalCAS, digest: str) -> None:
        super().__init__(delegate)
        self.digest = digest
        self.source: _CloseFailingBytesIO | None = None

    def open(self, digest: str) -> BinaryIO:
        if digest != self.digest:
            return super().open(digest)
        assert self.active
        self.opened.append(digest)
        self.source = _CloseFailingBytesIO(self.delegate.read_bytes(digest))
        return self.source


class _FinalSweepFailingStore(_RetainingReadStore):
    def __init__(self, delegate: LocalCAS, digest: str) -> None:
        super().__init__(delegate)
        self.digest = digest

    def verify_receipt(self, expected: BlobInfo) -> BlobInfo:
        if expected.digest == self.digest:
            raise StorageIntegrityError("injected final receipt sweep failure")
        return super().verify_receipt(expected)


class _DeferredRetentionStore(_RetainingReadStore):
    """Invalid backend which saves the callback instead of invoking it inline."""

    def __init__(self, delegate: LocalCAS) -> None:
        super().__init__(delegate)
        self.callback: Callable[[], Any] | None = None

    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], _Result],
    ) -> _Result:
        self.retention_requests.append(expected)
        self.callback = callback  # type: ignore[assignment]
        return object()  # type: ignore[return-value]


class _RaisingDeferredRetentionStore(_DeferredRetentionStore):
    def __init__(self, delegate: LocalCAS, error: BaseException) -> None:
        super().__init__(delegate)
        self.error = error

    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], _Result],
    ) -> _Result:
        self.retention_requests.append(expected)
        self.callback = callback  # type: ignore[assignment]
        raise self.error


class _DoubleInvokingRetentionStore(_RetainingReadStore):
    def __init__(self, delegate: LocalCAS) -> None:
        super().__init__(delegate)
        self.second_error: BaseException | None = None

    def retain_receipts(
        self,
        expected: tuple[BlobInfo, ...],
        callback: Callable[[], _Result],
    ) -> _Result:
        self.retention_requests.append(expected)

        def retained() -> _Result:
            self.active = True
            try:
                result = callback()
                try:
                    callback()
                except BaseException as error:  # noqa: B036 - hostile backend
                    self.second_error = error
                return result
            finally:
                self.active = False

        return self.delegate.retain_receipts(expected, retained)


def _export_snapshot(
    imported: Any,
    object_store: LocalCAS,
    catalog: Any,
) -> RepoManifestExportResult:
    return export_retained_repo_manifest_snapshot(
        imported.repository_id,
        imported.snapshot_id,
        catalog=catalog,
        object_store=object_store,
    )


def test_materializer_is_available_through_the_lazy_compiler_api() -> None:
    assert (
        compiler_module.materialize_retained_context_artifact
        is materialize_retained_context_artifact
    )


def test_materializes_retained_export_as_exact_query_compatible_artifact(
    tmp_path: Path,
) -> None:
    with _retained_fixture(tmp_path / "retained") as (
        fixture,
        imported,
        object_store,
        catalog,
    ):
        exported = _export_snapshot(imported, object_store, catalog)
        store = _RetainingReadStore(object_store)
        provider = _RetentionCheckingProvider(store)
        owner = PublishedWorkspaceReceiptOwner()
        destination = tmp_path / "artifact"
        try:
            result = materialize_retained_context_artifact(
                exported,
                destination,
                object_store=store,
                workspace_provider=provider,
                output_receipt_owner=owner,
            )

            assert result.output_dir == destination
            assert result.repository == "owner/repo"
            assert result.commit == "a" * 40
            assert result.views == ("bm25", "vector")
            assert (
                result.manifest_path.read_bytes() == exported.canonical_manifest_bytes
            )
            assert owner.active
            assert owner.receipt.path == destination
            assert provider.returned_while_retained
            assert not store.active
            assert len(store.retention_requests) == 1
            retained = store.retention_requests[0]
            assert retained
            assert len({receipt.digest for receipt in retained}) == len(retained)
            assert store.opened[0] == exported.receipt.projection_receipt.digest
            assert tuple(store.final_verifications) == tuple(
                receipt.digest for receipt in retained
            )

            verified = verify_context_artifact(
                destination,
                expected_repository="owner/repo",
                expected_commit="a" * 40,
            )
            assert verified.views == ("bm25", "vector")
            assert verified.file_count == result.file_count
            assert verified.byte_count == result.byte_count
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            assert metadata["manifest"]["path"] == "repo_manifest.json"
            assert metadata["views"] == ["bm25", "vector"]
            assert stat.S_IMODE(result.metadata_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(result.manifest_path.stat().st_mode) == 0o600
            view_files = sorted(
                path for path in (destination / "views").rglob("*") if path.is_file()
            )
            assert view_files
            assert {stat.S_IMODE(path.stat().st_mode) for path in view_files} <= {
                0o644,
                0o755,
            }
            bm25_documents = destination / "views/bm25/documents.json"
            vector_documents = next(
                (destination / "views/vector").rglob("documents_*.json")
            )
            assert bm25_documents.read_bytes() == vector_documents.read_bytes()

            binding = query_context_artifact(
                destination,
                expected_repository="owner/repo",
                expected_commit="a" * 40,
            )
            context: ServerContext | None = None
            try:
                assert not binding.source_bound
                assert binding.manifest.repo_path == "source"
                assert tuple(binding.manifest.indexes) == ("bm25", "vector")
                with (
                    patch(
                        "codenib.index.embedding.artifact_integrity."
                        "capture_authenticated_vector_view"
                    ) as capture_vector,
                    patch(
                        "codenib.native_index_authorization."
                        "_mint_trusted_local_admin_authorization"
                    ) as mint_authorization,
                ):
                    context = ServerContext.load(
                        binding.manifest,
                        views=("bm25", "vector"),
                        artifact_binding=binding,
                    )
                capture_vector.assert_not_called()
                mint_authorization.assert_not_called()
                assert context.bm25 is not None
                assert context.vector is None
                assert "external authorization" in context.errors["vector"]
                results = search_bm25_impl(context, "VALUE")
                assert results
                assert all(result.get("content") is None for result in results)
            finally:
                if context is not None:
                    context.close()
                binding.close()

            source_binding = bind_context_artifact(
                destination,
                fixture.repository,
                expected_repository="owner/repo",
                expected_commit="a" * 40,
            )
            bound_context: ServerContext | None = None
            try:
                bound_context = ServerContext.load(
                    source_binding.manifest,
                    views=("bm25",),
                    artifact_binding=source_binding,
                )
                assert bound_context.source_verified
                assert bound_context.bm25 is not None
                bound_results = bound_context.bm25.search(
                    "VALUE",
                    return_code_content=True,
                )
                assert bound_results
                assert b"VALUE" in bound_context.read_source_bytes(
                    "sample.py",
                    max_bytes=1024,
                )
            finally:
                if bound_context is not None:
                    bound_context.close()
                source_binding.close()
        finally:
            owner.close()
        assert owner.closed
        assert destination.is_dir()


def test_materializer_requires_retention_without_requiring_ingestion(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        exported = _export_snapshot(imported, object_store, catalog)
        provider = _TestWorkspaceProvider()
        owner = PublishedWorkspaceReceiptOwner()
        destination = tmp_path / "artifact"
        verifying_only = _ReceiptVerifyingOnlyStore(object_store)
        retaining_read_store = _RetainingReadStore(object_store)
        assert not hasattr(retaining_read_store, "put_chunks")
        try:
            with pytest.raises(TypeError, match="retention|retain"):
                materialize_retained_context_artifact(
                    exported,
                    destination,
                    object_store=verifying_only,
                    workspace_provider=provider,
                    output_receipt_owner=owner,
                )
            assert verifying_only.calls == 0
            assert provider.run_count == 0
            assert owner.state == "empty"

            materialize_retained_context_artifact(
                exported,
                destination,
                object_store=retaining_read_store,
                workspace_provider=provider,
                output_receipt_owner=owner,
            )
            assert owner.active
        finally:
            owner.close()


def test_conflicting_duplicate_receipt_fails_before_retention_or_provider(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        exported = _export_snapshot(imported, object_store, catalog)
        member = exported.receipt.view_receipts[0].member_receipt_items[0][1]
        conflict = BlobInfo(
            member.digest,
            member.byte_size + 1,
            member.storage_key,
        )
        forged = replace(
            exported,
            receipt=replace(exported.receipt, projection_receipt=conflict),
        )
        store = _RetainingReadStore(object_store)
        provider = _TestWorkspaceProvider()
        owner = PublishedWorkspaceReceiptOwner()
        try:
            with pytest.raises(StorageIntegrityError, match="conflict|receipt"):
                materialize_retained_context_artifact(
                    forged,
                    tmp_path / "artifact",
                    object_store=store,
                    workspace_provider=provider,
                    output_receipt_owner=owner,
                )
            assert store.retention_requests == []
            assert provider.run_count == 0
            assert owner.state == "empty"
        finally:
            owner.close()


def test_retained_projection_rejects_forged_repository_relabeling(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        exported = _export_snapshot(imported, object_store, catalog)
        forged_identity = RepositoryIdentity(
            namespace_id=exported.receipt.namespace_id,
            repository_key="victim/repo",
        )
        forged = replace(
            exported,
            receipt=replace(
                exported.receipt,
                repository_id=forged_identity.repository_id,
                repository_key=forged_identity.repository_key,
            ),
        )
        provider = _TestWorkspaceProvider()
        owner = PublishedWorkspaceReceiptOwner()
        destination = tmp_path / "artifact"
        try:
            with pytest.raises(StorageIntegrityError, match="projection|identity"):
                materialize_retained_context_artifact(
                    forged,
                    destination,
                    object_store=object_store,
                    workspace_provider=provider,
                    output_receipt_owner=owner,
                )
            assert provider.run_count == 0
            assert owner.state == "empty"
            assert not destination.exists()
        finally:
            owner.close()


def test_retention_callback_is_revoked_before_backend_return_escapes(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        exported = _export_snapshot(imported, object_store, catalog)
        store = _DeferredRetentionStore(object_store)
        provider = _TestWorkspaceProvider()
        owner = PublishedWorkspaceReceiptOwner()
        destination = tmp_path / "artifact"
        try:
            with pytest.raises(RuntimeError, match="did not invoke|retention callback"):
                materialize_retained_context_artifact(
                    exported,
                    destination,
                    object_store=store,
                    workspace_provider=provider,
                    output_receipt_owner=owner,
                )
            assert store.callback is not None
            with pytest.raises(RuntimeError, match="no longer active"):
                store.callback()
            assert store.opened == []
            assert provider.run_count == 0
            assert owner.state == "empty"
            assert not destination.exists()
        finally:
            owner.close()


def test_retention_callback_revocation_preserves_backend_primary(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        exported = _export_snapshot(imported, object_store, catalog)
        failure = KeyboardInterrupt("retention backend failed")
        store = _RaisingDeferredRetentionStore(object_store, failure)
        provider = _TestWorkspaceProvider()
        owner = PublishedWorkspaceReceiptOwner()
        destination = tmp_path / "artifact"
        try:
            with pytest.raises(KeyboardInterrupt) as raised:
                materialize_retained_context_artifact(
                    exported,
                    destination,
                    object_store=store,
                    workspace_provider=provider,
                    output_receipt_owner=owner,
                )
            assert raised.value is failure
            assert store.callback is not None
            with pytest.raises(RuntimeError, match="no longer active"):
                store.callback()
            assert store.opened == []
            assert provider.run_count == 0
            assert owner.state == "empty"
            assert not destination.exists()
        finally:
            owner.close()


@pytest.mark.parametrize(
    ("backend_raises", "source_fragment", "backend_remains_primary"),
    (
        (False, "revoke_owner.close()", False),
        (False, "with _run_context_with_cleanup_actions((revoke_action,))", False),
        (False, "outcome_read_error: BaseException | None = None", False),
        (True, "except BaseException as error", False),
        (True, "primary_error = error", False),
        (True, "revoke_owner.close()", True),
        (True, "with _run_context_with_cleanup_actions((revoke_action,))", True),
    ),
)
def test_retention_callback_revocation_survives_post_backend_line_cancellation(
    tmp_path: Path,
    backend_raises: bool,
    source_fragment: str,
    backend_remains_primary: bool,
) -> None:
    backend_error = RuntimeError("retention backend failed")
    if backend_raises:
        store: _DeferredRetentionStore = _RaisingDeferredRetentionStore(
            LocalCAS(tmp_path / "cas"),
            backend_error,
        )
    else:
        store = _DeferredRetentionStore(LocalCAS(tmp_path / "cas"))
    cancellation = KeyboardInterrupt("retention post-backend cancellation")
    source, first_line = inspect.getsourcelines(
        materialization_module._run_retention_callback
    )
    target_line = first_line + next(
        index for index, line in enumerate(source) if source_fragment in line
    )

    def inject(frame: Any, event: str, _argument: Any) -> Any:
        if (
            frame.f_code is materialization_module._run_retention_callback.__code__
            and event == "line"
            and frame.f_lineno == target_line
            and store.callback is not None
        ):
            sys.settrace(None)
            raise cancellation
        return inject

    try:
        sys.settrace(inject)
        with pytest.raises(BaseException) as raised:
            materialization_module._run_retention_callback(
                store,
                (),
                object,
            )
        expected = backend_error if backend_remains_primary else cancellation
        assert raised.value is expected
    finally:
        sys.settrace(None)
    assert store.callback is not None
    with pytest.raises(RuntimeError, match="no longer active"):
        store.callback()
    assert store.opened == []


def test_retention_callback_is_revoked_before_cleanup_finalizer_interrupts(
    tmp_path: Path,
) -> None:
    store = _DeferredRetentionStore(LocalCAS(tmp_path / "cas"))
    cancellation = KeyboardInterrupt("retention cleanup finalizer cancellation")
    cleanup_function = inspect.unwrap(
        materialization_module._run_context_with_cleanup_actions
    )
    source, first_line = inspect.getsourcelines(cleanup_function)
    target_line = first_line + next(
        index
        for index, line in enumerate(source)
        if "locally_unwinding = context_error is not None" in line
    )

    def inject(frame: Any, event: str, _argument: Any) -> Any:
        if (
            frame.f_code is cleanup_function.__code__
            and event == "line"
            and frame.f_lineno == target_line
            and store.callback is not None
        ):
            sys.settrace(None)
            raise cancellation
        return inject

    try:
        sys.settrace(inject)
        with pytest.raises(KeyboardInterrupt) as raised:
            materialization_module._run_retention_callback(store, (), object)
        assert raised.value is cancellation
    finally:
        sys.settrace(None)
    assert store.callback is not None
    with pytest.raises(RuntimeError, match="no longer active"):
        store.callback()
    assert store.opened == []


def test_second_retention_callback_attempt_is_detected_after_publication(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        exported = _export_snapshot(imported, object_store, catalog)
        store = _DoubleInvokingRetentionStore(object_store)
        provider = _TestWorkspaceProvider()
        owner = PublishedWorkspaceReceiptOwner()
        destination = tmp_path / "artifact"
        try:
            with pytest.raises(RuntimeError, match="misused|one-shot"):
                materialize_retained_context_artifact(
                    exported,
                    destination,
                    object_store=store,
                    workspace_provider=provider,
                    output_receipt_owner=owner,
                )
            assert isinstance(store.second_error, RuntimeError)
            assert "one-shot" in str(store.second_error)
            assert provider.run_count == 1
            assert owner.active
            assert owner.receipt.path == destination
            assert verify_context_artifact(destination).views == ("bm25",)
        finally:
            owner.close()


def test_truncated_bundle_stream_is_closed_and_never_published(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        exported = _export_snapshot(imported, object_store, catalog)
        bundle = exported.receipt.view_receipts[0].bundle_receipt
        store = _TruncatingBundleStore(object_store, bundle.digest)
        provider = _TestWorkspaceProvider()
        owner = PublishedWorkspaceReceiptOwner()
        try:
            with pytest.raises(StorageIntegrityError, match="truncated"):
                materialize_retained_context_artifact(
                    exported,
                    tmp_path / "artifact",
                    object_store=store,
                    workspace_provider=provider,
                    output_receipt_owner=owner,
                )
            assert store.source is not None
            assert store.source.closed
            assert store.source.close_calls >= 1
            assert provider.run_count == 0
            assert owner.state == "empty"
        finally:
            owner.close()


def test_bundle_reader_close_failure_remains_explicitly_retryable(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        exported = _export_snapshot(imported, object_store, catalog)
        bundle = exported.receipt.view_receipts[0].bundle_receipt
        store = _CloseFailingBundleStore(object_store, bundle.digest)
        provider = _TestWorkspaceProvider()
        output_owner = PublishedWorkspaceReceiptOwner()
        try:
            with pytest.raises(
                StorageIntegrityError,
                match="object-store reader close failed",
            ) as raised:
                materialize_retained_context_artifact(
                    exported,
                    tmp_path / "artifact",
                    object_store=store,
                    workspace_provider=provider,
                    output_receipt_owner=output_owner,
                )
            assert store.source is not None
            assert not store.source.closed
            cleanup_owners = BaseException.__getattribute__(
                raised.value,
                "publication_cleanup_owners",
            )
            assert len(cleanup_owners) == 1
            store.source.allow_close = True
            cleanup_owners[0].close()
            assert store.source.closed
            assert provider.run_count == 0
            assert output_owner.state == "empty"
        finally:
            output_owner.close()


def test_bundle_inventory_mismatch_fails_before_workspace_provisioning(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        exported = _export_snapshot(imported, object_store, catalog)
        view = exported.receipt.view_receipts[0]
        first_path, first_receipt = view.member_receipt_items[0]
        assert first_path != "aaa-unexpected.json"
        forged_view = replace(
            view,
            member_receipt_items=(
                ("aaa-unexpected.json", first_receipt),
                *view.member_receipt_items[1:],
            ),
        )
        forged = replace(
            exported,
            receipt=replace(exported.receipt, view_receipts=(forged_view,)),
        )
        provider = _TestWorkspaceProvider()
        owner = PublishedWorkspaceReceiptOwner()
        try:
            with pytest.raises(StorageIntegrityError, match="inventory|member|bundle"):
                materialize_retained_context_artifact(
                    forged,
                    tmp_path / "artifact",
                    object_store=object_store,
                    workspace_provider=provider,
                    output_receipt_owner=owner,
                )
            assert provider.run_count == 0
            assert owner.state == "empty"
        finally:
            owner.close()


def test_final_receipt_sweep_precedes_atomic_publication(tmp_path: Path) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        exported = _export_snapshot(imported, object_store, catalog)
        store = _FinalSweepFailingStore(
            object_store,
            exported.receipt.projection_receipt.digest,
        )
        provider = _TestWorkspaceProvider()
        owner = PublishedWorkspaceReceiptOwner()
        destination = tmp_path / "artifact"
        try:
            with pytest.raises(StorageIntegrityError, match="final receipt sweep"):
                materialize_retained_context_artifact(
                    exported,
                    destination,
                    object_store=store,
                    workspace_provider=provider,
                    output_receipt_owner=owner,
                )
            assert provider.run_count == 1
            assert owner.state == "empty"
            assert not destination.exists()
            assert not store.active
        finally:
            owner.close()


def test_existing_destination_and_nonempty_owner_fail_before_cas_reads(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        exported = _export_snapshot(imported, object_store, catalog)
        store = _RetainingReadStore(object_store)
        provider = _TestWorkspaceProvider()
        owner = PublishedWorkspaceReceiptOwner()
        existing = tmp_path / "existing"
        existing.mkdir()
        try:
            with pytest.raises(ValueError, match="destination|missing|exist"):
                materialize_retained_context_artifact(
                    exported,
                    existing,
                    object_store=store,
                    workspace_provider=provider,
                    output_receipt_owner=owner,
                )
            assert store.retention_requests == []
            assert provider.run_count == 0

            materialize_retained_context_artifact(
                exported,
                tmp_path / "first",
                object_store=store,
                workspace_provider=provider,
                output_receipt_owner=owner,
            )
            assert owner.active
            retain_count = len(store.retention_requests)
            with pytest.raises(RuntimeError, match="empty"):
                materialize_retained_context_artifact(
                    exported,
                    tmp_path / "second",
                    object_store=store,
                    workspace_provider=provider,
                    output_receipt_owner=owner,
                )
            assert len(store.retention_requests) == retain_count
        finally:
            owner.close()


def test_post_commit_cancellation_keeps_output_authority_reachable(
    tmp_path: Path,
) -> None:
    with _retained_fixture(
        tmp_path / "retained",
        views=("bm25",),
    ) as (_fixture, imported, object_store, catalog):
        exported = _export_snapshot(imported, object_store, catalog)
        cancellation = KeyboardInterrupt("after commit")
        provider = _PostCommitFailureProvider(cancellation)
        owner = PublishedWorkspaceReceiptOwner()
        destination = tmp_path / "artifact"
        try:
            with pytest.raises(KeyboardInterrupt) as raised:
                materialize_retained_context_artifact(
                    exported,
                    destination,
                    object_store=object_store,
                    workspace_provider=provider,
                    output_receipt_owner=owner,
                )
            assert raised.value is cancellation
            assert owner.active
            assert owner.receipt.path == destination
            assert verify_context_artifact(destination).views == ("bm25",)
            receipt = exported.receipt.projection_receipt
            assert object_store.retain_receipts((receipt,), lambda: True) is True
        finally:
            owner.close()
        assert owner.closed
        assert destination.is_dir()
