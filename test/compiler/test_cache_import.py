# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import subprocess
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import pytest

import codenib.artifacts.portable_views as portable_views_module
import codenib.artifacts.strict_context as strict_context_module
import codenib.artifacts.strict_vector as strict_vector_module
import codenib.compiler as compiler_module
import codenib.compiler.cache_import as cache_import_module
import codenib.compiler.job_resources as job_resources_module
import codenib.index.embedding.vector_store as vector_store_module
import codenib.source_fingerprint as source_fingerprint_module
import codenib.storage.view_bundle as view_bundle_module
from codenib import LocalWorkspaceProvider
from codenib import cli as cli_module
from codenib._captured_directory import (
    OwnedWorkspaceAuthority,
    PublishedWorkspaceReceiptOwner,
    UnsupportedWorkspaceCreation,
)
from codenib._workspace_provider import (
    StrictWorkspaceRequest,
    StrictWorkspaceSession,
    run_adopted_workspace_operation,
)
from codenib.artifacts import query_context_artifact
from codenib.compiler.cache_import import (
    CompilerCacheBm25RecaptureResult,
    CompilerCacheImportResult,
    CompilerCacheJobExecutor,
    CompilerCacheJobPreparationResult,
    CompilerCacheJobPublicationResult,
    CompilerCacheMultiViewImportResult,
    CompilerCacheTopologyGuard,
    CompilerCacheVectorJobPublicationResult,
    CompilerCacheViewRecaptureResult,
    CompilerRetainedPublicationResult,
    compile_and_import_repo,
    compiler_cache_source_selection,
    import_compiler_cache,
    import_compiler_cache_bm25,
    prepare_compiler_cache_job_view,
    publish_compiler_cache_bm25_job,
    publish_compiler_cache_vector_job,
)
from codenib.compiler.cache_lock import compiler_cache_lock
from codenib.compiler.index_builders import (
    BM25IndexBuilder,
    IndexBuilderRegistry,
    VectorIndexBuilder,
)
from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig
from codenib.compiler.job_resolver import (
    CompilerCacheJobResolver,
    CompilerCacheJobResourceFactory,
    CompilerCacheJobResourceScope,
)
from codenib.compiler.job_resources import (
    LocalCompilerCacheJobResourceFactory,
    LocalCompilerCacheJobTarget,
)
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.compiler.manifest_import import RepoManifestImportResult
from codenib.compiler.manifest_materialization import (
    materialize_retained_repo_manifest_ref,
)
from codenib.compiler.manifest_storage import (
    VECTOR_PROFILE_AXES,
    RepoManifestImportPlan,
    plan_repo_manifest_import_bytes,
)
from codenib.compiler.retained_manifest_contract import REPO_MANIFEST_PROJECTION_VIEW
from codenib.index.embedding.vector_store import CodeVectorStore
from codenib.mcp.context import ServerContext
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import (
    RepositoryChangedError,
    RepositorySourceBinding,
    capture_repository_source,
    fingerprint_repository,
)
from codenib.storage import (
    INDEX_JOB_REQUEST_CONTRACT,
    RETAINED_IMPORT_CATALOG_CONTRACT,
    VIEW_BUNDLE_MEDIA_TYPE,
    VIEW_BUNDLE_SCHEMA,
    BlobInfo,
    IndexJobExecutionContext,
    IndexJobStatus,
    IndexJobStopReason,
    IndexJobWorker,
    IndexJobWorkerDisposition,
    InterruptibleReceiptVerifyingObjectStore,
    InterruptibleStreamingObjectStore,
    LocalCAS,
    PublishConflict,
    ReceiptRetainingObjectStore,
    RetainedImportObjectStore,
    SQLiteCatalog,
    StorageIntegrityError,
    StorageValidationError,
)

_Result = TypeVar("_Result")
_COMMIT = "a" * 40
_REPOSITORY_KEY = "owner/repo"


class _TestStopToken:
    def __init__(self, *, notify_after_checks: int | None = None) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._checks = 0
        self._notify_after_checks = notify_after_checks
        self.polled = threading.Event()

    @property
    def reason(self) -> IndexJobStopReason | None:
        if self._event.is_set():
            return IndexJobStopReason.CANCEL_REQUESTED
        return None

    def is_set(self) -> bool:
        with self._lock:
            self._checks += 1
            threshold = self._notify_after_checks
            if threshold is not None and self._checks >= threshold:
                self.polled.set()
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def set(self) -> None:
        self._event.set()


class _DeterministicEmbedding:
    """Small local embedding used to produce real schema-8 vector bytes."""

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [
            [float((index + offset) % 7 + 1) for offset in range(self.dimension)]
            for index, _text in enumerate(texts)
        ]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class _BackendTripwire:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def retained_import_contract(self) -> str:
        self.calls.append("contract")
        return RETAINED_IMPORT_CATALOG_CONTRACT

    def _unexpected(self, name: str):
        self.calls.append(name)
        raise AssertionError(f"unexpected backend data operation: {name}")

    def put_bytes(self, *args, **kwargs):
        return self._unexpected("put_bytes")

    def put_file(self, *args, **kwargs):
        return self._unexpected("put_file")

    def has(self, *args, **kwargs):
        return self._unexpected("has")

    def open(self, *args, **kwargs):
        return self._unexpected("open")

    def read_bytes(self, *args, **kwargs):
        return self._unexpected("read_bytes")

    def verify(self, *args, **kwargs):
        return self._unexpected("verify")

    def materialize(self, *args, **kwargs):
        return self._unexpected("materialize")

    def put_chunks(self, *args, **kwargs):
        return self._unexpected("put_chunks")

    def put_chunks_interruptibly(self, *args, **kwargs):
        return self._unexpected("put_chunks_interruptibly")

    def verify_receipt(self, *args, **kwargs):
        return self._unexpected("verify_receipt")

    def verify_receipt_interruptibly(self, *args, **kwargs):
        return self._unexpected("verify_receipt_interruptibly")

    def retain_receipts(self, *args, **kwargs):
        return self._unexpected("retain_receipts")

    def create_namespace(self, *args, **kwargs):
        return self._unexpected("create_namespace")

    def create_repository(self, *args, **kwargs):
        return self._unexpected("create_repository")

    def create_source_revision(self, *args, **kwargs):
        return self._unexpected("create_source_revision")

    def create_view_profile(self, *args, **kwargs):
        return self._unexpected("create_view_profile")

    def register_object(self, *args, **kwargs):
        return self._unexpected("register_object")

    def stage_view_generation(self, *args, **kwargs):
        return self._unexpected("stage_view_generation")

    def publish_snapshot(self, *args, **kwargs):
        return self._unexpected("publish_snapshot")

    def resolve_ref(self, *args, **kwargs):
        return self._unexpected("resolve_ref")

    def get_manifest_summary(self, *args, **kwargs):
        return self._unexpected("get_manifest_summary")


class _TestWorkspaceProvider:
    """Quiescent test provider exercising the real workspace authority."""

    def __init__(self) -> None:
        self.support_count = 0
        self.run_count = 0

    def require_support(self) -> None:
        self.support_count += 1

    def run_workspace(
        self,
        request: StrictWorkspaceRequest,
        *,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        operation: Callable[[StrictWorkspaceSession], _Result],
        check_cancelled: Callable[[], None] | None = None,
    ) -> _Result:
        self.run_count += 1
        if check_cancelled is not None:
            check_cancelled()
        plan = request.plan
        parent = request.destination.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stage = parent / (
            f".{request.destination.name}.cache-import-{id(self):x}-"
            f"{self.run_count}"
        )
        stage.mkdir(mode=plan.root_mode)
        for index, directory in enumerate(plan.directories):
            (stage / directory.path.as_posix()).mkdir(mode=directory.mode)
            if check_cancelled is not None and index + 1 < len(plan.directories):
                check_cancelled()

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        parent_descriptor = os.open(parent, flags)
        root_descriptor = os.open(stage, flags)
        directory_descriptors = {
            directory.path.as_posix(): os.open(stage / directory.path.as_posix(), flags)
            for directory in plan.directories
        }
        workspace = OwnedWorkspaceAuthority()
        try:
            workspace.adopt(
                destination=request.destination,
                stage_name=stage.name,
                parent_descriptor=parent_descriptor,
                root_descriptor=root_descriptor,
                directory_descriptors=directory_descriptors,
                plan=plan,
                destination_binding=None,
            )
            try:
                return run_adopted_workspace_operation(
                    request,
                    workspace=workspace,
                    receipt_owner=receipt_owner,
                    operation=operation,
                )
            except BaseException:
                if receipt_owner.state == "empty":
                    workspace.close()
                raise
        finally:
            for descriptor in directory_descriptors.values():
                os.close(descriptor)
            os.close(root_descriptor)
            os.close(parent_descriptor)


class _LockAwareCAS(LocalCAS):
    def __init__(self, root: Path, state: dict[str, object]) -> None:
        self._test_state = state
        super().__init__(root)

    def put_chunks(
        self,
        chunks,
        expected_digest: str,
        expected_size: int,
    ):
        assert self._test_state["phase"] == "released"
        self._test_state["events"].append(  # type: ignore[union-attr]
            ("cas.put_chunks", self._test_state["phase"])
        )
        return super().put_chunks(chunks, expected_digest, expected_size)

    def put_chunks_interruptibly(
        self,
        chunks,
        expected_digest: str,
        expected_size: int,
        *,
        check_cancelled,
    ):
        assert self._test_state["phase"] == "released"
        self._test_state["events"].append(  # type: ignore[union-attr]
            ("cas.put_chunks", self._test_state["phase"])
        )
        return super().put_chunks_interruptibly(
            chunks,
            expected_digest,
            expected_size,
            check_cancelled=check_cancelled,
        )


class _JobTrackingCAS(LocalCAS):
    def __init__(self, root: Path) -> None:
        self.put_chunk_receipts: list[BlobInfo] = []
        self.retained_receipt_sets: list[tuple[BlobInfo, ...]] = []
        self.retention_active = False
        self.after_put: Callable[[int], None] | None = None
        super().__init__(root)

    def put_chunks(
        self,
        chunks,
        expected_digest: str,
        expected_size: int,
    ) -> BlobInfo:
        receipt = super().put_chunks(chunks, expected_digest, expected_size)
        self.put_chunk_receipts.append(receipt)
        if self.after_put is not None:
            self.after_put(len(self.put_chunk_receipts))
        return receipt

    def put_chunks_interruptibly(
        self,
        chunks,
        expected_digest: str,
        expected_size: int,
        *,
        check_cancelled,
    ) -> BlobInfo:
        receipt = super().put_chunks_interruptibly(
            chunks,
            expected_digest,
            expected_size,
            check_cancelled=check_cancelled,
        )
        self.put_chunk_receipts.append(receipt)
        if self.after_put is not None:
            self.after_put(len(self.put_chunk_receipts))
        return receipt

    def retain_receipts(self, expected, callback):
        receipts = tuple(expected)
        self.retained_receipt_sets.append(receipts)
        assert not self.retention_active
        self.retention_active = True
        try:
            return super().retain_receipts(receipts, callback)
        finally:
            self.retention_active = False


class _ForgedPutReceiptCAS(_JobTrackingCAS):
    def put_chunks(
        self,
        chunks,
        expected_digest: str,
        expected_size: int,
    ) -> BlobInfo:
        receipt = super().put_chunks(chunks, expected_digest, expected_size)
        if len(self.put_chunk_receipts) == 1:
            return BlobInfo(
                digest=receipt.digest,
                byte_size=receipt.byte_size,
                storage_key="sha256/00/" + receipt.digest,
            )
        return receipt

    def put_chunks_interruptibly(
        self,
        chunks,
        expected_digest: str,
        expected_size: int,
        *,
        check_cancelled,
    ) -> BlobInfo:
        receipt = super().put_chunks_interruptibly(
            chunks,
            expected_digest,
            expected_size,
            check_cancelled=check_cancelled,
        )
        if len(self.put_chunk_receipts) == 1:
            return BlobInfo(
                digest=receipt.digest,
                byte_size=receipt.byte_size,
                storage_key="sha256/00/" + receipt.digest,
            )
        return receipt


class _SkippingRetentionCAS(_JobTrackingCAS):
    def retain_receipts(self, expected, callback):
        del callback
        receipts = tuple(expected)
        self.retained_receipt_sets.append(receipts)
        return object()


class _RetentionAwareJobCatalog(SQLiteCatalog):
    def __init__(self, path: Path, cas: _JobTrackingCAS) -> None:
        self._tracking_cas = cas
        self.job_publication_calls = 0
        super().__init__(path)

    def publish_job_outputs(self, *args, **kwargs):
        assert self._tracking_cas.retention_active
        self.job_publication_calls += 1
        return super().publish_job_outputs(*args, **kwargs)


class _CompilerCacheWorkerCatalog(SQLiteCatalog):
    def __init__(self, path: Path, publication_calls: list[str]) -> None:
        self._publication_calls = publication_calls
        super().__init__(path, create=False)

    def publish_job_outputs(self, job_id: str, **kwargs):
        self._publication_calls.append(job_id)
        return super().publish_job_outputs(job_id, **kwargs)


class _CompilerCacheWorkerFactory:
    def __init__(self, path: Path, publication_calls: list[str]) -> None:
        self.path = path
        self.publication_calls = publication_calls

    def __call__(self) -> _CompilerCacheWorkerCatalog:
        return _CompilerCacheWorkerCatalog(self.path, self.publication_calls)


class _ScopedCompilerCacheResources:
    def __init__(
        self,
        executor: CompilerCacheJobExecutor,
        *,
        close_resources: bool = True,
        suppress_failure: bool = False,
        cleanup_failure: BaseException | None = None,
    ) -> None:
        self.executor = executor
        self.close_resources = close_resources
        self.suppress_failure = suppress_failure
        self.cleanup_failure = cleanup_failure
        self.declarations = 0
        self.contexts = []
        self.object_stores = []
        self.exits = 0

    def create_scope(self, context, *, object_store):
        self.declarations += 1
        return CompilerCacheJobResourceScope(
            object_store=self.executor.object_store,
            view_type=self.executor.view_type,
            resources=self._open(context, object_store=object_store),
        )

    @contextmanager
    def _open(self, context, *, object_store):
        self.contexts.append(context)
        self.object_stores.append(object_store)
        try:
            try:
                yield self.executor
            except BaseException:
                if not self.suppress_failure:
                    raise
        finally:
            self.exits += 1
            if self.close_resources:
                self.executor.context_output_owner.close()
                self.executor.view_output_owner.close()
                self.executor.repository_source.close()
            if self.cleanup_failure is not None:
                raise self.cleanup_failure


class _UnusedCompilerCacheResources:
    def create_scope(self, context, *, object_store):
        raise AssertionError(context, object_store)


class _ReceiptRetainingOnlyStore:
    def put_bytes(self, data):
        raise NotImplementedError(data)

    def put_file(self, source):
        raise NotImplementedError(source)

    def has(self, digest):
        raise NotImplementedError(digest)

    def open(self, digest):
        raise NotImplementedError(digest)

    def read_bytes(self, digest):
        raise NotImplementedError(digest)

    def verify(self, digest):
        raise NotImplementedError(digest)

    def materialize(self, digest, destination):
        raise NotImplementedError(digest, destination)

    def verify_receipt(self, expected):
        raise NotImplementedError(expected)

    def retain_receipts(self, expected, callback):
        raise NotImplementedError(expected, callback)


class _LegacyRetainedImportStore(_ReceiptRetainingOnlyStore):
    """Old retained-import shape without either interruptible capability."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def put_chunks(self, chunks, expected_digest, expected_size):
        self.calls.append("put_chunks")
        raise AssertionError(chunks, expected_digest, expected_size)


class _NoncallableInterruptibleReceiptStore(_LegacyRetainedImportStore):
    verify_receipt_interruptibly = object()

    def put_chunks_interruptibly(
        self,
        chunks,
        expected_digest,
        expected_size,
        *,
        check_cancelled,
    ):
        raise AssertionError(
            chunks,
            expected_digest,
            expected_size,
            check_cancelled,
        )


class _ReceiptInterruptibleOnlyStore(_LegacyRetainedImportStore):
    def verify_receipt_interruptibly(self, expected, *, check_cancelled):
        raise AssertionError(expected, check_cancelled)


class _NoncallableInterruptibleStreamingStore(_ReceiptInterruptibleOnlyStore):
    put_chunks_interruptibly = object()


class _RetryableCleanupOwner:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _LockAwareCatalog(SQLiteCatalog):
    def __init__(self, path: Path, state: dict[str, object]) -> None:
        self._test_state = state
        self.publications: list[dict[str, object]] = []
        super().__init__(path)

    def retained_import_contract(self) -> str:
        assert self._test_state["phase"] != "locked"
        self._test_state["events"].append(  # type: ignore[union-attr]
            ("catalog.retained_import_contract", self._test_state["phase"])
        )
        return super().retained_import_contract()

    def _record_data(self, name: str) -> None:
        assert self._test_state["phase"] == "released"
        self._test_state["events"].append(  # type: ignore[union-attr]
            (f"catalog.data.{name}", self._test_state["phase"])
        )

    def create_namespace(self, *args, **kwargs):
        self._record_data("create_namespace")
        return super().create_namespace(*args, **kwargs)

    def create_repository(self, *args, **kwargs):
        self._record_data("create_repository")
        return super().create_repository(*args, **kwargs)

    def create_source_revision(self, *args, **kwargs):
        self._record_data("create_source_revision")
        return super().create_source_revision(*args, **kwargs)

    def create_view_profile(self, *args, **kwargs):
        self._record_data("create_view_profile")
        return super().create_view_profile(*args, **kwargs)

    def register_object(self, *args, **kwargs):
        self._record_data("register_object")
        return super().register_object(*args, **kwargs)

    def stage_view_generation(self, *args, **kwargs):
        self._record_data("stage_view_generation")
        return super().stage_view_generation(*args, **kwargs)

    def publish_snapshot(self, *args, **kwargs):
        self._record_data("publish_snapshot")
        publication = super().publish_snapshot(*args, **kwargs)
        self.publications.append(copy.deepcopy(publication))
        return publication

    def resolve_ref(self, *args, **kwargs):
        self._record_data("resolve_ref")
        return super().resolve_ref(*args, **kwargs)

    def get_manifest_summary(self, *args, **kwargs):
        self._record_data("get_manifest_summary")
        return super().get_manifest_summary(*args, **kwargs)


class _PostCommitInterruptCatalog(SQLiteCatalog):
    def __init__(self, path: Path) -> None:
        self._interrupt_after_first_publish = True
        self.interrupted_publication: dict[str, object] | None = None
        super().__init__(path)

    def publish_snapshot(self, *args, **kwargs):
        publication = super().publish_snapshot(*args, **kwargs)
        if self._interrupt_after_first_publish:
            self._interrupt_after_first_publish = False
            self.interrupted_publication = copy.deepcopy(publication)
            raise RuntimeError("postcommit interruption")
        return publication


def _json_bytes(value: object) -> bytes:
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


def _file_fingerprint(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _fake_import_result() -> RepoManifestImportResult:
    return RepoManifestImportResult(
        repository_id="repository-test",
        source_revision_id="source-test",
        snapshot_id="snapshot-test",
        ref_name="main",
        generation=1,
        changed=True,
        views=("bm25",),
        skipped_items=(),
        view_generation_items=(("bm25", "generation-test"),),
    )


@dataclass
class _CacheFixture:
    repository: Path
    cache: Path
    source: RepositorySourceBinding
    workspace: Path
    provider: _TestWorkspaceProvider
    bm25_owner: PublishedWorkspaceReceiptOwner
    context_owner: PublishedWorkspaceReceiptOwner

    @property
    def bm25_destination(self) -> Path:
        return self.workspace / "published-bm25"

    @property
    def context_destination(self) -> Path:
        return self.workspace / "published-context"

    def close(self) -> None:
        self.context_owner.close()
        self.bm25_owner.close()
        self.source.close()


def _cache_fixture(tmp_path: Path) -> _CacheFixture:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    source_file = repository / "sample.py"
    source_text = "VALUE = 1\n"
    source_file.write_text(source_text, encoding="utf-8")

    cache = repository / ".codenib_cache"
    bm25 = cache / "bm25"
    bm25.mkdir(mode=0o700, parents=True)
    # Mirror a real IndexCompiler cache: its fixed coordination inode already
    # exists before the existing-only import bridge borrows it.
    with cache_import_module.compiler_cache_lock(cache):
        pass
    documents = [
        {
            "page_content": source_text,
            "metadata": {
                "file": str(source_file),
                "node_id": "sample.py",
                "start_line": 0,
                "end_line": 0,
                "type": "file",
            },
        }
    ]
    (bm25 / "documents.json").write_bytes(_json_bytes(documents))
    (bm25 / "bm25_metadata.json").write_bytes(
        _json_bytes(
            {
                "project_root": str(repository),
                "max_k": 17,
                "language": "python",
            }
        )
    )
    for path in bm25.iterdir():
        path.chmod(0o600)

    source = capture_repository_source(repository, exclude_roots=(cache,))
    fingerprints = {
        name: _file_fingerprint(bm25 / name)
        for name in ("bm25_metadata.json", "documents.json")
    }
    config = BM25IndexBuilder(
        languages=["python"],
        max_k=17,
        max_lines_per_chunk=300,
    ).artifact_identity()
    config.update(
        {
            "artifact_file_fingerprints": fingerprints,
            "chunk_count": 1,
            "source_file_count": 1,
            "file_count": 1,
        }
    )
    entry = IndexEntry(
        index_type="bm25",
        path=str(bm25),
        built_at="2026-01-01T00:00:00+00:00",
        built_at_epoch=1.0,
        status="fresh",
        config=copy.deepcopy(config),
        metadata={**copy.deepcopy(config), "build_duration_seconds": 0.1},
        commit=_COMMIT,
        source_fingerprint=source.fingerprint,
    )
    manifest = RepoManifest(
        repo_path=str(repository),
        commit=_COMMIT,
        last_indexed_commit=_COMMIT,
        source_fingerprint=source.fingerprint,
        last_indexed_source_fingerprint=source.fingerprint,
        languages=["python"],
        file_count=source.file_count,
        indexes={"bm25": entry},
        compiled_at="2026-01-01T00:00:00+00:00",
        compiled_at_epoch=1.0,
    )
    manifest.derive_capabilities()
    manifest.save(cache / "repo_manifest.json")
    (cache / "repo_manifest.json").chmod(0o600)

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    return _CacheFixture(
        repository=repository,
        cache=cache,
        source=source,
        workspace=workspace,
        provider=_TestWorkspaceProvider(),
        bm25_owner=PublishedWorkspaceReceiptOwner(),
        context_owner=PublishedWorkspaceReceiptOwner(),
    )


def _call(
    fixture: _CacheFixture,
    *,
    catalog: object | None = None,
    object_store: object | None = None,
    **overrides,
) -> CompilerCacheImportResult:
    if catalog is None:
        catalog = _BackendTripwire()
    if object_store is None:
        object_store = catalog
    arguments = {
        "repository_source": fixture.source,
        "bm25_output_owner": fixture.bm25_owner,
        "context_output_owner": fixture.context_owner,
        "bm25_destination": fixture.bm25_destination,
        "context_destination": fixture.context_destination,
        "workspace_provider": fixture.provider,
        "repository_key": _REPOSITORY_KEY,
        "catalog": catalog,
        "object_store": object_store,
        "environ": {},
    }
    arguments.update(overrides)
    return import_compiler_cache_bm25(fixture.cache, **arguments)  # type: ignore[arg-type]


def _call_generic(
    fixture: _CacheFixture,
    *,
    catalog: object | None = None,
    object_store: object | None = None,
    **overrides,
) -> CompilerCacheMultiViewImportResult:
    if catalog is None:
        catalog = _BackendTripwire()
    if object_store is None:
        object_store = catalog
    arguments = {
        "views": ("bm25",),
        "repository_source": fixture.source,
        "view_output_owners": {"bm25": fixture.bm25_owner},
        "context_output_owner": fixture.context_owner,
        "view_destinations": {"bm25": fixture.bm25_destination},
        "context_destination": fixture.context_destination,
        "workspace_provider": fixture.provider,
        "repository_key": _REPOSITORY_KEY,
        "catalog": catalog,
        "object_store": object_store,
        "environ": {},
    }
    arguments.update(overrides)
    return import_compiler_cache(fixture.cache, **arguments)  # type: ignore[arg-type]


def test_compiler_cache_import_is_a_lazy_public_export() -> None:
    assert (
        compiler_module.CompilerRetainedPublicationResult
        is CompilerRetainedPublicationResult
    )
    assert compiler_module.CompilerCacheTopologyGuard is CompilerCacheTopologyGuard
    assert (
        compiler_module.CompilerCacheJobPublicationResult
        is CompilerCacheJobPublicationResult
    )
    assert compiler_module.CompilerCacheJobExecutor is CompilerCacheJobExecutor
    assert compiler_module.CompilerCacheJobResolver is CompilerCacheJobResolver
    assert (
        compiler_module.CompilerCacheJobResourceFactory
        is CompilerCacheJobResourceFactory
    )
    assert (
        compiler_module.CompilerCacheJobResourceScope is CompilerCacheJobResourceScope
    )
    assert (
        compiler_module.LocalCompilerCacheJobResourceFactory
        is LocalCompilerCacheJobResourceFactory
    )
    assert compiler_module.LocalCompilerCacheJobTarget is LocalCompilerCacheJobTarget
    assert (
        compiler_module.CompilerCacheJobPreparationResult
        is CompilerCacheJobPreparationResult
    )
    assert (
        compiler_module.CompilerCacheVectorJobPublicationResult
        is CompilerCacheVectorJobPublicationResult
    )
    assert compiler_module.compile_and_import_repo is compile_and_import_repo
    assert compiler_module.CompilerCacheImportResult is CompilerCacheImportResult
    assert (
        compiler_module.CompilerCacheMultiViewImportResult
        is CompilerCacheMultiViewImportResult
    )
    assert (
        compiler_module.CompilerCacheViewRecaptureResult
        is CompilerCacheViewRecaptureResult
    )
    assert (
        compiler_module.CompilerCacheBm25RecaptureResult
        is CompilerCacheBm25RecaptureResult
    )
    assert compiler_module.import_compiler_cache is import_compiler_cache
    assert compiler_module.import_compiler_cache_bm25 is import_compiler_cache_bm25
    assert (
        compiler_module.publish_compiler_cache_bm25_job
        is publish_compiler_cache_bm25_job
    )
    assert (
        compiler_module.publish_compiler_cache_vector_job
        is publish_compiler_cache_vector_job
    )
    assert (
        compiler_module.prepare_compiler_cache_job_view
        is prepare_compiler_cache_job_view
    )
    assert list(inspect.signature(import_compiler_cache).parameters)[:11] == [
        "cache_dir",
        "views",
        "repository_source",
        "view_output_owners",
        "context_output_owner",
        "view_destinations",
        "context_destination",
        "workspace_provider",
        "repository_key",
        "catalog",
        "object_store",
    ]
    assert list(inspect.signature(import_compiler_cache_bm25).parameters)[:10] == [
        "cache_dir",
        "repository_source",
        "bm25_output_owner",
        "context_output_owner",
        "bm25_destination",
        "context_destination",
        "workspace_provider",
        "repository_key",
        "catalog",
        "object_store",
    ]
    assert list(inspect.signature(publish_compiler_cache_bm25_job).parameters)[:14] == [
        "cache_dir",
        "job_id",
        "owner_id",
        "fencing_token",
        "repository_source",
        "bm25_output_owner",
        "context_output_owner",
        "bm25_destination",
        "context_destination",
        "workspace_provider",
        "repository_key",
        "catalog",
        "object_store",
        "namespace_name",
    ]
    vector_job_signature = inspect.signature(publish_compiler_cache_vector_job)
    assert list(vector_job_signature.parameters)[:14] == [
        "cache_dir",
        "job_id",
        "owner_id",
        "fencing_token",
        "repository_source",
        "vector_output_owner",
        "context_output_owner",
        "vector_destination",
        "context_destination",
        "workspace_provider",
        "repository_key",
        "catalog",
        "object_store",
        "namespace_name",
    ]
    assert not {
        "ref_name",
        "expected_generation",
        "embedding_provider",
        "embedding_model",
        "embedding_revision",
        "embedding_load_policy",
        "native_index_authorization",
    } & set(vector_job_signature.parameters)
    preparation_signature = inspect.signature(prepare_compiler_cache_job_view)
    assert list(preparation_signature.parameters)[:13] == [
        "cache_dir",
        "view_type",
        "job",
        "views",
        "repository_source",
        "view_output_owner",
        "context_output_owner",
        "view_destination",
        "context_destination",
        "workspace_provider",
        "repository_key",
        "object_store",
        "namespace_name",
    ]
    assert not {
        "catalog",
        "owner_id",
        "fencing_token",
        "ref_name",
        "expected_generation",
        "embedding_provider",
        "embedding_model",
        "native_index_authorization",
    } & set(preparation_signature.parameters)
    assert "stop_token" in preparation_signature.parameters
    executor_signature = inspect.signature(CompilerCacheJobExecutor)
    assert not {
        "catalog",
        "owner_id",
        "fencing_token",
        "ref_name",
        "expected_generation",
    } & set(executor_signature.parameters)
    compile_signature = inspect.signature(compile_and_import_repo)
    assert list(compile_signature.parameters)[:4] == [
        "compiler",
        "repo_path",
        "cache_dir",
        "views",
    ]
    assert compile_signature.parameters["cache_dir"].kind is (
        inspect.Parameter.KEYWORD_ONLY
    )
    assert compile_signature.parameters["cache_dir"].default is inspect.Parameter.empty
    assert "rebuild" not in compile_signature.parameters


def _expected_bm25_job_plan(fixture: _CacheFixture) -> RepoManifestImportPlan:
    manifest = RepoManifest.load(fixture.cache / "repo_manifest.json")
    entry = manifest.indexes["bm25"]
    planned = cache_import_module._plan_cache_view(
        "bm25",
        fixture.cache / "bm25",
        fixture.bm25_destination,
        repository_source=fixture.source,
        view_config=entry.config,
        forbidden_paths=(),
        environ={},
    )
    _portable, payload = cache_import_module._portable_manifest(
        manifest,
        views=("bm25",),
        planned_views={"bm25": planned},
    )
    return plan_repo_manifest_import_bytes(payload, views=("bm25",))


def _register_bm25_job_subject(
    catalog: SQLiteCatalog,
    fixture: _CacheFixture,
    plan: RepoManifestImportPlan,
) -> tuple[str, str, str]:
    repository_id = catalog.create_repository(_REPOSITORY_KEY)
    source_revision_id = catalog.create_source_revision(
        repository_id,
        commit_sha=None,
        dirty=True,
        source_fingerprint=fixture.source.fingerprint,
    )
    intent = plan.views[0]
    profile_id = catalog.create_view_profile(
        "bm25",
        intent.profile.config,
        name=intent.profile.name,
    )
    assert profile_id == intent.profile_id
    return repository_id, source_revision_id, profile_id


def _create_bm25_job(
    catalog: SQLiteCatalog,
    *,
    repository_id: str,
    source_revision_id: str,
    profile_id: str,
    idempotency_key: str = "compiler-cache-bm25",
    requested_mode: str = "full",
    required: bool = True,
    expected_ref_generation: int = 0,
    extra_views: dict[str, dict[str, object]] | None = None,
):
    views: dict[str, dict[str, object]] = {
        "bm25": {
            "profile_id": profile_id,
            "requested_mode": requested_mode,
            "required": required,
        }
    }
    if extra_views:
        views.update(copy.deepcopy(extra_views))
    return catalog.create_job(
        repository_id,
        source_revision_id,
        idempotency_key,
        {"contract": INDEX_JOB_REQUEST_CONTRACT, "views": views},
        expected_ref_generation=expected_ref_generation,
    )


def _publish_bm25_job(
    fixture: _CacheFixture,
    *,
    job_id: str,
    owner_id: str,
    fencing_token: int,
    catalog: SQLiteCatalog,
    cas: LocalCAS,
    bm25_owner: PublishedWorkspaceReceiptOwner | None = None,
    context_owner: PublishedWorkspaceReceiptOwner | None = None,
    bm25_destination: Path | None = None,
    context_destination: Path | None = None,
) -> CompilerCacheJobPublicationResult:
    return publish_compiler_cache_bm25_job(
        fixture.cache,
        job_id=job_id,
        owner_id=owner_id,
        fencing_token=fencing_token,
        repository_source=fixture.source,
        bm25_output_owner=bm25_owner or fixture.bm25_owner,
        context_output_owner=context_owner or fixture.context_owner,
        bm25_destination=bm25_destination or fixture.bm25_destination,
        context_destination=context_destination or fixture.context_destination,
        workspace_provider=fixture.provider,
        repository_key=_REPOSITORY_KEY,
        catalog=catalog,
        object_store=cas,
        environ={},
    )


def _prepare_bm25_job(
    fixture: _CacheFixture,
    *,
    job,
    views,
    cas: LocalCAS,
    stop_token: _TestStopToken | None = None,
) -> CompilerCacheJobPreparationResult:
    return prepare_compiler_cache_job_view(
        fixture.cache,
        view_type="bm25",
        job=job,
        views=views,
        repository_source=fixture.source,
        view_output_owner=fixture.bm25_owner,
        context_output_owner=fixture.context_owner,
        view_destination=fixture.bm25_destination,
        context_destination=fixture.context_destination,
        workspace_provider=fixture.provider,
        repository_key=_REPOSITORY_KEY,
        object_store=cas,
        stop_token=stop_token,
        environ={},
    )


def test_prepare_compiler_cache_job_view_leaves_catalog_running(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            queued = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            catalog.acquire_job_lease(
                queued.job_id,
                owner_id="prepare-only-worker",
                lease_duration_ms=60_000,
            )
            running = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)

            result = _prepare_bm25_job(
                fixture,
                job=running,
                views=views,
                cas=cas,
            )

            assert type(result) is CompilerCacheJobPreparationResult
            assert result.job == running
            assert result.view == views[0]
            assert result.manifest.to_dict() == result.import_plan.manifest.to_dict()
            assert result.recapture.view_type == "bm25"
            assert result.context_artifact.views == ("bm25",)
            assert result.artifact.view_type == "bm25"
            assert result.artifact.profile_id == profile_id
            assert result.artifact.schema_version == VIEW_BUNDLE_SCHEMA
            assert len(result.artifact.member_artifacts) == 2
            assert catalog.get_job(queued.job_id) == running
            assert catalog.get_job(queued.job_id).status is IndexJobStatus.RUNNING
            assert len(cas.put_chunk_receipts) == 3
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 2
    finally:
        fixture.close()


@pytest.mark.timeout(10)
def test_prepare_compiler_cache_job_stops_while_waiting_for_cache_lock(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    token = _TestStopToken(notify_after_checks=4)
    failures: list[BaseException] = []
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            queued = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            catalog.acquire_job_lease(
                queued.job_id,
                owner_id="cancelled-prepare-worker",
                lease_duration_ms=60_000,
            )
            running = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)

            def prepare() -> None:
                try:
                    _prepare_bm25_job(
                        fixture,
                        job=running,
                        views=views,
                        cas=cas,
                        stop_token=token,
                    )
                except BaseException as exc:  # noqa: B036 - asserted below
                    failures.append(exc)

            with compiler_cache_lock(fixture.cache, create=False):
                thread = threading.Thread(target=prepare)
                thread.start()
                assert token.polled.wait(timeout=3)
                token.set()
                thread.join(timeout=3)
                assert not thread.is_alive()

            assert len(failures) == 1
            assert type(failures[0]).__name__ == "_CompilerCacheJobStopped"
            assert str(failures[0]) == "compiler cache job preparation stopped"
            assert catalog.get_job(queued.job_id) == running
            assert catalog.get_job(queued.job_id).status is IndexJobStatus.RUNNING
            assert cas.put_chunk_receipts == []
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 0
            assert fixture.bm25_owner.state == "empty"
            assert fixture.context_owner.state == "empty"
            assert not fixture.bm25_destination.exists()
            assert not fixture.context_destination.exists()
    finally:
        fixture.close()


def test_compiler_cache_job_stop_check_reuses_exact_failure() -> None:
    token = _TestStopToken()
    check_cancelled = cache_import_module._compiler_cache_job_stop_check(token)
    assert check_cancelled is not None
    check_cancelled()

    token.set()
    with pytest.raises(RuntimeError) as first:
        check_cancelled()
    with pytest.raises(RuntimeError) as replay:
        check_cancelled()

    assert replay.value is first.value
    assert type(first.value).__name__ == "_CompilerCacheJobStopped"
    assert str(first.value) == "compiler cache job preparation stopped"


def test_prepare_compiler_cache_job_stops_inside_source_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    token = _TestStopToken()
    real_scan = source_fingerprint_module._scan_pinned_repository
    real_update = source_fingerprint_module._update_inventory_record
    callbacks: list[Callable[[], None]] = []
    seen: list[str] = []
    active = True

    def observe_scan(*args, **kwargs):
        callback = kwargs.get("check_cancelled")
        if active and callback is not None:
            callbacks.append(callback)
        return real_scan(*args, **kwargs)

    def stop_after_first_record(*args, **kwargs):
        real_update(*args, **kwargs)
        if not active:
            return
        if seen:
            raise AssertionError(
                "cancelled job source inventory consumed another record"
            )
        seen.append(kwargs["relative"])
        token.set()

    monkeypatch.setattr(
        source_fingerprint_module,
        "_scan_pinned_repository",
        observe_scan,
    )
    monkeypatch.setattr(
        source_fingerprint_module,
        "_update_inventory_record",
        stop_after_first_record,
    )
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            queued = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            catalog.acquire_job_lease(
                queued.job_id,
                owner_id="source-cancelled-prepare-worker",
                lease_duration_ms=60_000,
            )
            running = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)

            with pytest.raises(RuntimeError) as stopped:
                _prepare_bm25_job(
                    fixture,
                    job=running,
                    views=views,
                    cas=cas,
                    stop_token=token,
                )

            assert type(stopped.value).__name__ == "_CompilerCacheJobStopped"
            assert str(stopped.value) == "compiler cache job preparation stopped"
            assert seen == ["sample.py"]
            assert callbacks
            with pytest.raises(RuntimeError) as replay:
                callbacks[0]()
            assert replay.value is stopped.value
            assert catalog.get_job(queued.job_id) == running
            assert catalog.get_job(queued.job_id).status is IndexJobStatus.RUNNING
            assert cas.put_chunk_receipts == []
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 0
            assert fixture.bm25_owner.state == "empty"
            assert fixture.context_owner.state == "empty"
            assert not fixture.bm25_destination.exists()
            assert not fixture.context_destination.exists()
            assert fixture.source.usable
            active = False
            fixture.source.verify_snapshot()
    finally:
        active = False
        fixture.close()


def test_prepare_compiler_cache_job_stops_inside_source_read_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    token = _TestStopToken()
    real_session = RepositorySourceBinding.read_session
    real_scan = source_fingerprint_module._scan_pinned_repository
    active_depth = 0
    session_calls = 0
    scan_calls = 0
    triggered = False
    exact_stop: BaseException | None = None

    class TrackingSession:
        def __init__(self, inner) -> None:
            self._inner = inner

        def __enter__(self):
            nonlocal active_depth
            active_depth += 1
            try:
                return self._inner.__enter__()
            except BaseException:  # noqa: B036 - mirror failed enter cleanup
                active_depth -= 1
                raise

        def __exit__(self, exc_type, exc, traceback):
            nonlocal active_depth
            try:
                return self._inner.__exit__(exc_type, exc, traceback)
            finally:
                active_depth -= 1

    def tracked_session(binding, *args, **kwargs):
        nonlocal session_calls
        inner = real_session(binding, *args, **kwargs)
        if binding is not fixture.source:
            return inner
        if not callable(kwargs.get("check_cancelled")):
            raise AssertionError("job source read session omitted cancellation")
        session_calls += 1
        return TrackingSession(inner)

    def tracked_scan(*args, **kwargs):
        nonlocal scan_calls, triggered
        if active_depth and not triggered:
            callback = kwargs.get("check_cancelled")
            if not callable(callback):
                raise AssertionError("job source session scan omitted cancellation")

            def stop_on_first_scan_poll() -> None:
                nonlocal exact_stop, triggered
                triggered = True
                token.set()
                try:
                    callback()
                except BaseException as exc:  # noqa: B036 - exact identity
                    exact_stop = exc
                    raise

            scan_calls += 1
            kwargs["check_cancelled"] = stop_on_first_scan_poll
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(RepositorySourceBinding, "read_session", tracked_session)
    monkeypatch.setattr(
        source_fingerprint_module,
        "_scan_pinned_repository",
        tracked_scan,
    )
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            queued = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            catalog.acquire_job_lease(
                queued.job_id,
                owner_id="source-session-cancelled-worker",
                lease_duration_ms=60_000,
            )
            running = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)

            with pytest.raises(RuntimeError) as stopped:
                _prepare_bm25_job(
                    fixture,
                    job=running,
                    views=views,
                    cas=cas,
                    stop_token=token,
                )

            assert stopped.value is exact_stop
            assert type(stopped.value).__name__ == "_CompilerCacheJobStopped"
            assert str(stopped.value) == "compiler cache job preparation stopped"
            assert session_calls == 1
            assert scan_calls == 1
            assert triggered
            assert active_depth == 0
            assert catalog.get_job(queued.job_id) == running
            assert catalog.get_job(queued.job_id).status is IndexJobStatus.RUNNING
            assert cas.put_chunk_receipts == []
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 0
            assert fixture.source.usable
            fixture.source.authenticated_identity_snapshot()
    finally:
        fixture.close()


def test_prepare_compiler_cache_job_attests_artifact_before_final_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    token = _TestStopToken()
    real_snapshot = RepositorySourceBinding.authenticated_identity_snapshot
    forged_returned = False
    final_scan_attempted = False

    def forged_artifact(*args, **kwargs):
        nonlocal forged_returned
        forged_returned = True
        return (object(),)

    def stop_before_final_source_scan(binding, *args, **kwargs):
        nonlocal final_scan_attempted
        if forged_returned:
            final_scan_attempted = True
            callback = kwargs.get("check_cancelled")
            if not callable(callback):
                raise AssertionError("final source scan omitted cancellation")
            token.set()
            callback()
        return real_snapshot(binding, *args, **kwargs)

    monkeypatch.setattr(
        cache_import_module,
        "_prepare_job_view_artifacts_inside_authority",
        forged_artifact,
    )
    monkeypatch.setattr(
        RepositorySourceBinding,
        "authenticated_identity_snapshot",
        stop_before_final_source_scan,
    )
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            queued = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            catalog.acquire_job_lease(
                queued.job_id,
                owner_id="forged-artifact-prepare-worker",
                lease_duration_ms=60_000,
            )
            running = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)

            with pytest.raises(
                StorageIntegrityError,
                match="ingestion returned a different requested view",
            ):
                _prepare_bm25_job(
                    fixture,
                    job=running,
                    views=views,
                    cas=cas,
                    stop_token=token,
                )

            assert forged_returned
            assert not final_scan_attempted
            assert catalog.get_job(queued.job_id) == running
            assert catalog.get_job(queued.job_id).status is IndexJobStatus.RUNNING
            assert cas.put_chunk_receipts == []
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 2
    finally:
        fixture.close()


@pytest.mark.parametrize("phase", ("plan", "publish"))
def test_prepare_compiler_cache_job_stops_during_context_full_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    token = _TestStopToken()
    active_phase: list[str | None] = [None]
    scan_cancelled = [False]
    real_capture_ownership = (
        strict_context_module.PublicationDirectoryReader.capture_ownership
    )
    real_context_plan = cache_import_module.plan_context_artifact_strict
    real_context_publish = cache_import_module.publish_planned_context_artifact_strict

    def tracked_context_plan(*args, **kwargs):
        active_phase[0] = "plan"
        try:
            return real_context_plan(*args, **kwargs)
        finally:
            active_phase[0] = None

    def tracked_context_publish(*args, **kwargs):
        active_phase[0] = "publish"
        try:
            return real_context_publish(*args, **kwargs)
        finally:
            active_phase[0] = None

    def cancel_inside_capture(publication, *args, **kwargs):
        if active_phase[0] != phase or scan_cancelled[0]:
            return real_capture_ownership(publication, *args, **kwargs)
        check_cancelled = kwargs.get("check_cancelled")
        if not callable(check_cancelled):
            raise AssertionError("context owner scan omitted its cancellation check")

        def stop_on_first_scan_poll() -> None:
            scan_cancelled[0] = True
            token.set()
            check_cancelled()

        kwargs["check_cancelled"] = stop_on_first_scan_poll
        real_capture_ownership(publication, *args, **kwargs)
        raise AssertionError("context full scan completed after cancellation") from None

    monkeypatch.setattr(
        cache_import_module,
        "plan_context_artifact_strict",
        tracked_context_plan,
    )
    monkeypatch.setattr(
        cache_import_module,
        "publish_planned_context_artifact_strict",
        tracked_context_publish,
    )
    monkeypatch.setattr(
        strict_context_module.PublicationDirectoryReader,
        "capture_ownership",
        cancel_inside_capture,
    )
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            queued = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            catalog.acquire_job_lease(
                queued.job_id,
                owner_id=f"stopped-context-{phase}-worker",
                lease_duration_ms=60_000,
            )
            running = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)

            with pytest.raises(
                RuntimeError,
                match="compiler cache job preparation stopped",
            ):
                _prepare_bm25_job(
                    fixture,
                    job=running,
                    views=views,
                    cas=cas,
                    stop_token=token,
                )

            assert scan_cancelled == [True]
            assert token.reason is IndexJobStopReason.CANCEL_REQUESTED
            assert catalog.get_job(queued.job_id) == running
            assert cas.put_chunk_receipts == []
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 1
            assert fixture.bm25_owner.active
            assert fixture.context_owner.state == "empty"
            assert fixture.bm25_destination.is_dir()
            assert not fixture.context_destination.exists()
    finally:
        fixture.close()


@pytest.mark.parametrize("pass_name", ("crc", "hash"))
def test_prepare_compiler_cache_job_stops_during_view_bundle_full_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pass_name: str,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    token = _TestStopToken()
    crc_polled = [False]
    hash_poison_consumed = [False]

    if pass_name == "crc":
        real_crc32 = view_bundle_module._publication_file_crc32

        def cancel_during_crc(*args, **kwargs):
            check_cancelled = kwargs.get("check_cancelled")
            if not callable(check_cancelled):
                raise AssertionError("bundle CRC pass omitted its cancellation check")

            def stop_on_first_crc_poll() -> None:
                crc_polled[0] = True
                token.set()
                check_cancelled()

            kwargs["check_cancelled"] = stop_on_first_crc_poll
            real_crc32(*args, **kwargs)
            raise AssertionError(
                "bundle CRC pass completed after cancellation"
            ) from None

        monkeypatch.setattr(
            view_bundle_module,
            "_publication_file_crc32",
            cancel_during_crc,
        )
    else:
        real_bundle_bytes = view_bundle_module._iter_planned_view_bundle_bytes

        def poisoned_bundle_bytes(*args, **kwargs):
            iterator = iter(real_bundle_bytes(*args, **kwargs))
            first = next(iterator)
            token.set()
            yield first
            hash_poison_consumed[0] = True
            raise AssertionError("bundle hash pass consumed its poisoned tail")

        monkeypatch.setattr(
            view_bundle_module,
            "_iter_planned_view_bundle_bytes",
            poisoned_bundle_bytes,
        )
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            queued = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            catalog.acquire_job_lease(
                queued.job_id,
                owner_id=f"stopped-bundle-{pass_name}-worker",
                lease_duration_ms=60_000,
            )
            running = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)

            with pytest.raises(
                RuntimeError,
                match="compiler cache job preparation stopped",
            ):
                _prepare_bm25_job(
                    fixture,
                    job=running,
                    views=views,
                    cas=cas,
                    stop_token=token,
                )

            if pass_name == "crc":
                assert crc_polled == [True]
            else:
                assert hash_poison_consumed == [False]
            assert token.reason is IndexJobStopReason.CANCEL_REQUESTED
            assert catalog.get_job(queued.job_id) == running
            assert cas.put_chunk_receipts == []
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 2
            assert fixture.bm25_owner.active
            assert fixture.context_owner.active
            assert fixture.bm25_destination.is_dir()
            assert fixture.context_destination.is_dir()
    finally:
        fixture.close()


@pytest.mark.parametrize("case", ("profile", "source", "request"))
def test_prepare_compiler_cache_job_rejects_mismatch_before_workspace_or_cas(
    tmp_path: Path,
    case: str,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            requested_mode = "full"
            if case == "profile":
                profile_id = catalog.create_view_profile(
                    "bm25",
                    {"builder": "incompatible"},
                )
            elif case == "source":
                source_revision_id = catalog.create_source_revision(
                    repository_id,
                    commit_sha=None,
                    dirty=True,
                    source_fingerprint="other-source-fingerprint",
                )
            else:
                requested_mode = "incremental"
            queued = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
                requested_mode=requested_mode,
            )
            catalog.acquire_job_lease(
                queued.job_id,
                owner_id="prepare-only-worker",
                lease_duration_ms=60_000,
            )

            with pytest.raises(StorageValidationError):
                _prepare_bm25_job(
                    fixture,
                    job=catalog.get_job(queued.job_id),
                    views=catalog.get_job_views(queued.job_id),
                    cas=cas,
                )

            assert cas.put_chunk_receipts == []
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 0
            assert fixture.bm25_owner.state == "empty"
            assert fixture.context_owner.state == "empty"
            assert not fixture.bm25_destination.exists()
            assert not fixture.context_destination.exists()
    finally:
        fixture.close()


def test_compiler_cache_resolver_requires_streaming_import_capability() -> None:
    object_store = _ReceiptRetainingOnlyStore()

    assert isinstance(object_store, ReceiptRetainingObjectStore)
    assert not isinstance(object_store, RetainedImportObjectStore)
    with pytest.raises(TypeError, match="retained import store"):
        CompilerCacheJobResolver(
            resource_factory=_UnusedCompilerCacheResources(),
            object_store=object_store,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("object_store_factory", "message"),
    [
        (_LegacyRetainedImportStore, "interruptible receipt verification"),
        (_ReceiptInterruptibleOnlyStore, "interruptible streaming ingestion"),
    ],
)
def test_prepare_compiler_cache_job_requires_interruptible_capabilities_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    object_store_factory,
    message: str,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    object_store = object_store_factory()
    token = _TestStopToken()
    try:
        with SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog:
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            queued = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            catalog.acquire_job_lease(
                queued.job_id,
                owner_id="legacy-receipt-worker",
                lease_duration_ms=60_000,
            )
            running = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)

        def forbidden_source_read(*_args, **_kwargs):
            raise AssertionError("source authority was read before capability gate")

        monkeypatch.setattr(
            RepositorySourceBinding,
            "authenticated_identity_snapshot",
            forbidden_source_read,
        )

        assert isinstance(object_store, RetainedImportObjectStore)
        with pytest.raises(TypeError, match=message):
            prepare_compiler_cache_job_view(
                fixture.cache,
                view_type="bm25",
                job=running,
                views=views,
                repository_source=fixture.source,
                view_output_owner=fixture.bm25_owner,
                context_output_owner=fixture.context_owner,
                view_destination=fixture.bm25_destination,
                context_destination=fixture.context_destination,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                object_store=object_store,  # type: ignore[arg-type]
                stop_token=token,
                environ={},
            )

        assert object_store.calls == []
        assert fixture.provider.run_count == 0
        assert fixture.source.usable
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
        assert not fixture.bm25_destination.exists()
        assert not fixture.context_destination.exists()
    finally:
        fixture.close()


@pytest.mark.parametrize(
    ("object_store_factory", "missing_method"),
    [
        (
            _NoncallableInterruptibleReceiptStore,
            "verify_receipt_interruptibly",
        ),
        (
            _NoncallableInterruptibleStreamingStore,
            "put_chunks_interruptibly",
        ),
    ],
)
def test_local_job_resource_factory_requires_callable_interruptible_capabilities(
    tmp_path: Path,
    object_store_factory,
    missing_method: str,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    object_store = object_store_factory()
    try:
        with SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog:
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            queued = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            lease = catalog.acquire_job_lease(
                queued.job_id,
                owner_id="legacy-resource-worker",
                lease_duration_ms=60_000,
            )
            running = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)
            attempt = catalog.get_job_attempt(
                queued.job_id,
                running.attempt_count,
            )

        class ValidationControl:
            @property
            def stop_token(self):
                return _TestStopToken()

            def append_progress(self, *_args, **_kwargs):
                raise AssertionError("resource capability gate appended progress")

        context = IndexJobExecutionContext(
            job=running,
            views=views,
            attempt=attempt,
            lease=lease,
            control=ValidationControl(),
        )
        target = LocalCompilerCacheJobTarget(
            repository_root=fixture.repository,
            cache_dir=fixture.cache,
            workspace_provider=LocalWorkspaceProvider(fixture.workspace),
            repository_key=_REPOSITORY_KEY,
            environ={},
        )
        resources = LocalCompilerCacheJobResourceFactory((target,))

        assert isinstance(
            object_store,
            InterruptibleReceiptVerifyingObjectStore,
        )
        assert isinstance(object_store, InterruptibleStreamingObjectStore)
        with pytest.raises(TypeError, match=f"provide {missing_method}"):
            resources.create_scope(
                context,
                object_store=object_store,  # type: ignore[arg-type]
            )

        assert object_store.calls == []
        assert fixture.source.usable
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
    finally:
        fixture.close()


def test_worker_rejects_a_different_compiler_resolver_store(tmp_path: Path) -> None:
    with (
        LocalCAS(tmp_path / "worker-cas") as worker_store,
        LocalCAS(tmp_path / "resolver-cas") as resolver_store,
    ):
        resolver = CompilerCacheJobResolver(
            resource_factory=_UnusedCompilerCacheResources(),
            object_store=resolver_store,
        )

        with pytest.raises(StorageValidationError, match="same object store"):
            IndexJobWorker(
                catalog_factory=lambda: None,  # type: ignore[arg-type]
                object_store=worker_store,
                resolver=resolver,
                lease_duration_ms=300,
                heartbeat_interval_ms=50,
            )


def test_prepare_compiler_cache_job_rejects_forged_receipt_before_stop(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    token = _TestStopToken()
    try:
        with (
            _ForgedPutReceiptCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            queued = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            catalog.acquire_job_lease(
                queued.job_id,
                owner_id="forged-receipt-worker",
                lease_duration_ms=60_000,
            )
            running = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)
            cas.after_put = lambda count: token.set() if count == 1 else None

            with pytest.raises(
                StorageValidationError,
                match="object receipt is not canonical",
            ):
                _prepare_bm25_job(
                    fixture,
                    job=running,
                    views=views,
                    cas=cas,
                    stop_token=token,
                )

            assert token.reason is IndexJobStopReason.CANCEL_REQUESTED
            assert catalog.get_job(queued.job_id) == running
            assert len(cas.put_chunk_receipts) == 1
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 2
            assert fixture.bm25_owner.active
            assert fixture.context_owner.active
    finally:
        fixture.close()


def test_compiler_cache_executor_delegates_only_final_publish_to_worker(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    catalog_path = tmp_path / "worker.sqlite"
    publication_calls: list[str] = []
    try:
        with _JobTrackingCAS(tmp_path / "cas") as cas:
            with SQLiteCatalog(catalog_path) as catalog:
                (
                    repository_id,
                    source_revision_id,
                    profile_id,
                ) = _register_bm25_job_subject(catalog, fixture, plan)
                queued = _create_bm25_job(
                    catalog,
                    repository_id=repository_id,
                    source_revision_id=source_revision_id,
                    profile_id=profile_id,
                )

            executor = CompilerCacheJobExecutor(
                cache_dir=fixture.cache,
                view_type="bm25",
                repository_source=fixture.source,
                view_output_owner=fixture.bm25_owner,
                context_output_owner=fixture.context_owner,
                view_destination=fixture.bm25_destination,
                context_destination=fixture.context_destination,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                object_store=cas,
                environ={},
            )
            resources = _ScopedCompilerCacheResources(executor)
            assert isinstance(resources, CompilerCacheJobResourceFactory)
            resolver = CompilerCacheJobResolver(
                resource_factory=resources,
                object_store=cas,
            )
            worker = IndexJobWorker(
                catalog_factory=_CompilerCacheWorkerFactory(
                    catalog_path,
                    publication_calls,
                ),
                object_store=cas,
                resolver=resolver,
                lease_duration_ms=60_000,
                heartbeat_interval_ms=5,
                owner_id_factory=lambda: "compiler-cache-worker",
            )

            outcome = worker.run_once()

            assert outcome.disposition is IndexJobWorkerDisposition.SUCCEEDED
            assert outcome.job_id == queued.job_id
            assert resources.declarations == 1
            assert len(resources.contexts) == 1
            assert resources.contexts[0].job.job_id == queued.job_id
            assert resources.object_stores == [cas]
            assert resources.exits == 1
            assert fixture.source.closed
            assert fixture.bm25_owner.closed
            assert fixture.context_owner.closed
            assert publication_calls == [queued.job_id]
            assert len(cas.put_chunk_receipts) == 3
            assert len(cas.retained_receipt_sets) == 1
            with SQLiteCatalog(catalog_path, create=False) as catalog:
                completed = catalog.get_job(queued.job_id)
                assert completed.status is IndexJobStatus.SUCCEEDED
                assert completed.result_snapshot_id is not None
                summary = catalog.get_manifest_summary(completed.result_snapshot_id)
                assert tuple(summary["views"]) == ("bm25",)
                assert summary["views"]["bm25"]["profile"]["profile_id"] == (profile_id)
    finally:
        fixture.close()


def test_local_compiler_cache_job_target_is_frozen_and_bounded(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    environment = {"CODENIB_TEST_SETTING": "trusted"}
    try:
        target = LocalCompilerCacheJobTarget(
            repository_root=fixture.repository,
            cache_dir=fixture.cache,
            workspace_provider=LocalWorkspaceProvider(fixture.workspace),
            repository_key=_REPOSITORY_KEY,
            environ=environment,
        )
        environment["CODENIB_TEST_SETTING"] = "changed"

        assert target.repository_root == fixture.repository
        assert target.cache_dir == fixture.cache
        assert target.workspace_root == fixture.workspace
        assert target.environ == {"CODENIB_TEST_SETTING": "trusted"}
        assert isinstance(
            LocalCompilerCacheJobResourceFactory((target,)),
            CompilerCacheJobResourceFactory,
        )
        with pytest.raises(ValueError, match="duplicate repository IDs"):
            LocalCompilerCacheJobResourceFactory((target, target))
        with pytest.raises(ValueError, match="must not overlap the repository"):
            LocalCompilerCacheJobTarget(
                repository_root=fixture.repository,
                cache_dir=fixture.cache,
                workspace_provider=LocalWorkspaceProvider(
                    fixture.repository / "job-workspace"
                ),
                repository_key=_REPOSITORY_KEY,
            )
    finally:
        fixture.close()


def test_local_compiler_cache_job_target_freezes_default_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    variable = "CODENIB_TEST_CACHE_CREDENTIAL"
    initial = "configured-secret-value"
    try:
        monkeypatch.setenv(variable, initial)
        target = LocalCompilerCacheJobTarget(
            repository_root=fixture.repository,
            cache_dir=fixture.cache,
            workspace_provider=LocalWorkspaceProvider(fixture.workspace),
            repository_key=_REPOSITORY_KEY,
        )
        explicit_empty_target = LocalCompilerCacheJobTarget(
            repository_root=fixture.repository,
            cache_dir=fixture.cache,
            workspace_provider=LocalWorkspaceProvider(fixture.workspace),
            repository_key=_REPOSITORY_KEY,
            environ={},
        )
        monkeypatch.setenv(variable, "changed-after-target-construction")

        assert target.environ[variable] == initial
        assert explicit_empty_target.environ == {}
    finally:
        fixture.close()


def test_local_compiler_cache_cleanup_owner_import_rejects_tuple_subclasses() -> None:
    class HostileOwners(tuple):
        def __iter__(self):
            raise AssertionError("hostile cleanup owners iterated")

    source = RuntimeError("source")
    target = RuntimeError("target")
    BaseException.__setattr__(
        source,
        "publication_cleanup_owners",
        HostileOwners((object(),)),
    )

    job_resources_module._inherit_cleanup_owners(target, source)

    with pytest.raises(AttributeError):
        BaseException.__getattribute__(target, "publication_cleanup_owners")


@pytest.mark.parametrize(
    ("case", "expected"),
    (
        ("full_bm25", True),
        ("full_vector", True),
        ("incremental", False),
        ("optional", False),
        ("multi_view", False),
        ("graph", False),
    ),
)
def test_local_compiler_cache_candidate_filter_accepts_only_supported_shape(
    tmp_path: Path,
    case: str,
    expected: bool,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    try:
        with SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog:
            repository_id, source_revision_id, bm25_profile_id = (
                _register_bm25_job_subject(catalog, fixture, plan)
            )
            view_type = "bm25"
            profile_id = bm25_profile_id
            if case in {"full_vector", "multi_view"}:
                vector_profile_id = catalog.create_view_profile(
                    "vector",
                    {"test_profile": case},
                )
                if case == "full_vector":
                    view_type = "vector"
                    profile_id = vector_profile_id
            elif case == "graph":
                view_type = "graph"
                profile_id = catalog.create_view_profile(
                    "graph",
                    {"test_profile": case},
                )
            views = {
                view_type: {
                    "profile_id": profile_id,
                    "requested_mode": (
                        "incremental" if case == "incremental" else "full"
                    ),
                    "required": case != "optional",
                }
            }
            if case == "multi_view":
                views["vector"] = {
                    "profile_id": vector_profile_id,
                    "requested_mode": "full",
                    "required": True,
                }
            queued = catalog.create_job(
                repository_id,
                source_revision_id,
                f"candidate-{case}",
                {"contract": INDEX_JOB_REQUEST_CONTRACT, "views": views},
            )

        target = LocalCompilerCacheJobTarget(
            repository_root=fixture.repository,
            cache_dir=fixture.cache,
            workspace_provider=LocalWorkspaceProvider(fixture.workspace),
            repository_key=_REPOSITORY_KEY,
            environ={},
        )
        resources = LocalCompilerCacheJobResourceFactory((target,))

        assert resources.accepts_candidate(queued) is expected
    finally:
        fixture.close()


def test_local_compiler_cache_job_factory_runs_and_isolates_attempt_workspaces(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    catalog_path = tmp_path / "worker.sqlite"
    publication_calls: list[str] = []
    provider = LocalWorkspaceProvider(fixture.workspace)
    try:
        try:
            provider.require_support()
        except UnsupportedWorkspaceCreation as exc:
            pytest.skip(str(exc))
        with _JobTrackingCAS(tmp_path / "cas") as cas:
            with SQLiteCatalog(catalog_path) as catalog:
                (
                    repository_id,
                    source_revision_id,
                    profile_id,
                ) = _register_bm25_job_subject(catalog, fixture, plan)
                queued = _create_bm25_job(
                    catalog,
                    repository_id=repository_id,
                    source_revision_id=source_revision_id,
                    profile_id=profile_id,
                )
                foreign_repository_id = catalog.create_repository("owner/foreign")
                foreign_source_revision_id = catalog.create_source_revision(
                    foreign_repository_id,
                    commit_sha=None,
                    dirty=True,
                    source_fingerprint=fixture.source.fingerprint,
                )
                foreign = _create_bm25_job(
                    catalog,
                    repository_id=foreign_repository_id,
                    source_revision_id=foreign_source_revision_id,
                    profile_id=profile_id,
                    idempotency_key="foreign-compiler-cache-bm25",
                )

            target = LocalCompilerCacheJobTarget(
                repository_root=fixture.repository,
                cache_dir=fixture.cache,
                workspace_provider=provider,
                repository_key=_REPOSITORY_KEY,
                environ={},
            )
            assert target.repository_id == repository_id
            resources = LocalCompilerCacheJobResourceFactory((target,))
            assert resources.accepts_candidate(queued)
            assert not resources.accepts_candidate(foreign)
            worker = IndexJobWorker(
                catalog_factory=_CompilerCacheWorkerFactory(
                    catalog_path,
                    publication_calls,
                ),
                object_store=cas,
                resolver=CompilerCacheJobResolver(
                    resource_factory=resources,
                    object_store=cas,
                ),
                candidate_filter=resources.accepts_candidate,
                lease_duration_ms=60_000,
                heartbeat_interval_ms=5,
                owner_id_factory=lambda: "local-compiler-cache-worker",
            )

            outcome = worker.run_once()

            assert outcome.disposition is IndexJobWorkerDisposition.SUCCEEDED
            assert outcome.job_id == queued.job_id
            assert publication_calls == [queued.job_id]
            assert len(cas.put_chunk_receipts) == 3
            assert len(cas.retained_receipt_sets) == 1
            assert not tuple(fixture.workspace.glob(".codenib-cache-job-*"))
            orphans = tuple(fixture.workspace.glob(".*.discarded-*"))
            assert len(orphans) == 2
            assert all(orphan.is_dir() for orphan in orphans)
            with SQLiteCatalog(catalog_path, create=False) as catalog:
                completed = catalog.get_job(queued.job_id)
                assert completed.status is IndexJobStatus.SUCCEEDED
                assert completed.result_snapshot_id is not None
                untouched = catalog.get_job(foreign.job_id)
                assert untouched.status is IndexJobStatus.QUEUED
                assert untouched.attempt_count == 0
    finally:
        fixture.close()


def test_local_compiler_cache_scope_entry_stop_is_exact_and_mutation_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    catalog_path = tmp_path / "worker.sqlite"
    publication_calls: list[str] = []
    provider = LocalWorkspaceProvider(fixture.workspace)
    scan_reached = threading.Event()
    release_scan = threading.Event()
    real_capture = job_resources_module.capture_repository_source
    real_update = source_fingerprint_module._update_inventory_record
    callbacks: list[Callable[[], None]] = []
    failures: list[BaseException] = []
    cleanup_resources: list[object] = []
    outcomes: list[object] = []
    worker_failures: list[BaseException] = []
    active = True

    def observe_capture(*args, **kwargs):
        callback = kwargs.get("check_cancelled")
        owner_sink = kwargs.get("_source_owner")
        assert callable(callback)
        assert callable(owner_sink)
        callbacks.append(callback)

        def observe_owner(resource: object) -> None:
            cleanup_resources.append(resource)
            owner_sink(resource)

        kwargs["_source_owner"] = observe_owner
        try:
            return real_capture(*args, **kwargs)
        except BaseException as exc:  # noqa: B036 - exact identity asserted below
            failures.append(exc)
            raise

    def pause_after_first_record(*args, **kwargs):
        real_update(*args, **kwargs)
        if not active:
            return
        if scan_reached.is_set():
            raise AssertionError("cancelled scope-entry scan consumed another record")
        scan_reached.set()
        assert release_scan.wait(timeout=3)

    monkeypatch.setattr(
        job_resources_module,
        "capture_repository_source",
        observe_capture,
    )
    monkeypatch.setattr(
        source_fingerprint_module,
        "_update_inventory_record",
        pause_after_first_record,
    )
    try:
        try:
            provider.require_support()
        except UnsupportedWorkspaceCreation as exc:
            pytest.skip(str(exc))
        with _JobTrackingCAS(tmp_path / "cas") as cas:
            with SQLiteCatalog(catalog_path) as catalog:
                repository_id, source_revision_id, profile_id = (
                    _register_bm25_job_subject(catalog, fixture, plan)
                )
                queued = _create_bm25_job(
                    catalog,
                    repository_id=repository_id,
                    source_revision_id=source_revision_id,
                    profile_id=profile_id,
                )

            target = LocalCompilerCacheJobTarget(
                repository_root=fixture.repository,
                cache_dir=fixture.cache,
                workspace_provider=provider,
                repository_key=_REPOSITORY_KEY,
                environ={},
            )
            worker = IndexJobWorker(
                catalog_factory=_CompilerCacheWorkerFactory(
                    catalog_path,
                    publication_calls,
                ),
                object_store=cas,
                resolver=CompilerCacheJobResolver(
                    resource_factory=LocalCompilerCacheJobResourceFactory((target,)),
                    object_store=cas,
                ),
                lease_duration_ms=60_000,
                heartbeat_interval_ms=5,
                owner_id_factory=lambda: "scope-entry-stop-worker",
            )

            def run_worker() -> None:
                try:
                    outcomes.append(worker.run_once())
                except BaseException as exc:  # noqa: B036 - asserted below
                    worker_failures.append(exc)

            thread = threading.Thread(target=run_worker)
            thread.start()
            assert scan_reached.wait(timeout=3)
            assert len(callbacks) == 1
            with SQLiteCatalog(catalog_path, create=False) as catalog:
                catalog.request_job_cancel(queued.job_id)

            expected_stop: BaseException | None = None
            for _attempt in range(300):
                try:
                    callbacks[0]()
                except BaseException as exc:  # noqa: B036 - exact stop probe
                    expected_stop = exc
                    break
                threading.Event().wait(0.01)
            assert expected_stop is not None
            release_scan.set()
            thread.join(timeout=5)
            assert not thread.is_alive()

            assert worker_failures == []
            assert len(outcomes) == 1
            assert outcomes[0].disposition is IndexJobWorkerDisposition.CANCELLED
            assert len(failures) == 1
            assert failures[0] is expected_stop
            assert type(expected_stop).__name__ == "_CompilerCacheJobStopped"
            with pytest.raises(RuntimeError) as replay:
                callbacks[0]()
            assert replay.value is expected_stop
            assert cleanup_resources
            assert all(resource.closed for resource in cleanup_resources)
            assert publication_calls == []
            assert cas.put_chunk_receipts == []
            assert cas.retained_receipt_sets == []
            assert not tuple(fixture.workspace.glob(".codenib-cache-job-*"))
            assert not tuple(fixture.workspace.glob(".*.discarded-*"))
            with SQLiteCatalog(catalog_path, create=False) as catalog:
                cancelled = catalog.get_job(queued.job_id)
                assert cancelled.status is IndexJobStatus.CANCELLED
                assert cancelled.result_snapshot_id is None

            active = False
            with capture_repository_source(
                fixture.repository,
                exclude_roots=(fixture.cache,),
            ) as reusable:
                assert reusable.fingerprint == fixture.source.fingerprint
    finally:
        active = False
        release_scan.set()
        fixture.close()


@pytest.mark.parametrize("executor_fails", (False, True))
def test_local_compiler_cache_job_factory_retains_failed_cleanup_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executor_fails: bool,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    catalog_path = tmp_path / "worker.sqlite"
    publication_calls: list[str] = []
    provider = LocalWorkspaceProvider(fixture.workspace)
    real_discard = job_resources_module.discard_owned_directory
    primary = StorageIntegrityError("primary local executor integrity failure")
    try:
        try:
            provider.require_support()
        except UnsupportedWorkspaceCreation as exc:
            pytest.skip(str(exc))
        with _JobTrackingCAS(tmp_path / "cas") as cas:
            with SQLiteCatalog(catalog_path) as catalog:
                (
                    repository_id,
                    source_revision_id,
                    profile_id,
                ) = _register_bm25_job_subject(catalog, fixture, plan)
                queued = _create_bm25_job(
                    catalog,
                    repository_id=repository_id,
                    source_revision_id=source_revision_id,
                    profile_id=profile_id,
                )

            target = LocalCompilerCacheJobTarget(
                repository_root=fixture.repository,
                cache_dir=fixture.cache,
                workspace_provider=provider,
                repository_key=_REPOSITORY_KEY,
                environ={},
            )

            def fail_context_discard(path: Path, ownership: object):
                if path.name.endswith("-context"):
                    raise OSError("injected context isolation failure")
                return real_discard(path, ownership)  # type: ignore[arg-type]

            monkeypatch.setattr(
                job_resources_module,
                "discard_owned_directory",
                fail_context_discard,
            )
            if executor_fails:
                real_execute = CompilerCacheJobExecutor.execute

                def fail_execute(
                    executor: CompilerCacheJobExecutor,
                    context,
                ):
                    real_execute(executor, context)
                    raise primary

                monkeypatch.setattr(
                    CompilerCacheJobExecutor,
                    "execute",
                    fail_execute,
                )
            worker = IndexJobWorker(
                catalog_factory=_CompilerCacheWorkerFactory(
                    catalog_path,
                    publication_calls,
                ),
                object_store=cas,
                resolver=CompilerCacheJobResolver(
                    resource_factory=LocalCompilerCacheJobResourceFactory((target,)),
                    object_store=cas,
                ),
                lease_duration_ms=60_000,
                heartbeat_interval_ms=5,
                owner_id_factory=lambda: "cleanup-failure-worker",
            )

            if executor_fails:
                with pytest.raises(StorageIntegrityError) as caught:
                    worker.run_once()
                assert caught.value is primary
            else:
                with pytest.raises(
                    StorageIntegrityError,
                    match="resource cleanup did not settle",
                ) as caught:
                    worker.run_once()

            assert publication_calls == []
            with SQLiteCatalog(catalog_path, create=False) as catalog:
                running = catalog.get_job(queued.job_id)
                assert running.status is IndexJobStatus.RUNNING
                assert running.result_snapshot_id is None
            cleanup_owners = caught.value.publication_cleanup_owners
            pending = tuple(
                owner
                for owner in cleanup_owners
                if type(owner).__name__ == "_AttemptWorkspaceCleanupOwner"
                and not owner.closed
            )
            assert len(pending) == 1
            monkeypatch.setattr(
                job_resources_module,
                "discard_owned_directory",
                real_discard,
            )
            pending[0].close()
            assert pending[0].closed
            assert not tuple(fixture.workspace.glob(".codenib-cache-job-*"))
            assert len(tuple(fixture.workspace.glob(".*.discarded-*"))) == 2
    finally:
        fixture.close()


@pytest.mark.parametrize("cleanup_mode", ("suppress", "raise"))
def test_compiler_cache_scope_cleanup_cannot_replace_executor_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_mode: str,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    catalog_path = tmp_path / "worker.sqlite"
    publication_calls: list[str] = []
    primary = StorageIntegrityError("primary executor integrity failure")

    def fail_execute(
        _executor: CompilerCacheJobExecutor,
        _context,
    ):
        raise primary

    monkeypatch.setattr(CompilerCacheJobExecutor, "execute", fail_execute)
    try:
        with _JobTrackingCAS(tmp_path / "cas") as cas:
            with SQLiteCatalog(catalog_path) as catalog:
                (
                    repository_id,
                    source_revision_id,
                    profile_id,
                ) = _register_bm25_job_subject(catalog, fixture, plan)
                queued = _create_bm25_job(
                    catalog,
                    repository_id=repository_id,
                    source_revision_id=source_revision_id,
                    profile_id=profile_id,
                )

            executor = CompilerCacheJobExecutor(
                cache_dir=fixture.cache,
                view_type="bm25",
                repository_source=fixture.source,
                view_output_owner=fixture.bm25_owner,
                context_output_owner=fixture.context_owner,
                view_destination=fixture.bm25_destination,
                context_destination=fixture.context_destination,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                object_store=cas,
                environ={},
            )
            cleanup_owner = _RetryableCleanupOwner()
            cleanup_failure = None
            if cleanup_mode == "raise":
                cleanup_failure = RuntimeError("resource cleanup failed")
                BaseException.__setattr__(
                    cleanup_failure,
                    "publication_cleanup_owners",
                    (cleanup_owner,),
                )
            resources = _ScopedCompilerCacheResources(
                executor,
                suppress_failure=cleanup_mode == "suppress",
                cleanup_failure=cleanup_failure,
            )
            worker = IndexJobWorker(
                catalog_factory=_CompilerCacheWorkerFactory(
                    catalog_path,
                    publication_calls,
                ),
                object_store=cas,
                resolver=CompilerCacheJobResolver(
                    resource_factory=resources,
                    object_store=cas,
                ),
                lease_duration_ms=60_000,
                heartbeat_interval_ms=5,
                owner_id_factory=lambda: "compiler-cache-worker",
            )

            with pytest.raises(StorageIntegrityError) as caught:
                worker.run_once()

            assert caught.value is primary
            if cleanup_mode == "raise":
                assert BaseException.__getattribute__(
                    primary,
                    "publication_cleanup_owners",
                ) == (cleanup_owner,)
            assert resources.declarations == 1
            assert len(resources.contexts) == 1
            assert resources.exits == 1
            assert fixture.source.closed
            assert fixture.bm25_owner.closed
            assert fixture.context_owner.closed
            assert cas.put_chunk_receipts == []
            assert cas.retained_receipt_sets == []
            assert publication_calls == []
            with SQLiteCatalog(catalog_path, create=False) as catalog:
                running = catalog.get_job(queued.job_id)
                assert running.status is IndexJobStatus.RUNNING
                assert running.result_snapshot_id is None
    finally:
        fixture.close()


def test_jobs_run_once_cli_publishes_one_trusted_local_cache_job(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    catalog_path = tmp_path / "worker.sqlite"
    cas_root = tmp_path / "cas"
    provider = LocalWorkspaceProvider(fixture.workspace)
    try:
        try:
            provider.require_support()
        except UnsupportedWorkspaceCreation as exc:
            pytest.skip(str(exc))
        with LocalCAS.provision(cas_root):
            pass
        with SQLiteCatalog(catalog_path) as catalog:
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog,
                fixture,
                plan,
            )
            queued = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
        args = cli_module.build_parser().parse_args(
            [
                "jobs",
                "run-once",
                str(fixture.repository),
                "--cache-dir",
                str(fixture.cache),
                "--catalog",
                str(catalog_path),
                "--cas-root",
                str(cas_root),
                "--workspace-root",
                str(fixture.workspace),
                "--repository",
                _REPOSITORY_KEY,
                "--lease-duration-ms",
                "60000",
                "--heartbeat-interval-ms",
                "5",
                "--json",
            ]
        )

        assert args.handler is cli_module._run_jobs_run_once
        assert cli_module._run_jobs_run_once(args) == 0

        assert json.loads(capsys.readouterr().out) == {
            "attempt_count": 1,
            "disposition": "succeeded",
            "job_id": queued.job_id,
        }
        assert not tuple(fixture.workspace.glob(".codenib-cache-job-*"))
        assert len(tuple(fixture.workspace.glob(".*.discarded-*"))) == 2
        with SQLiteCatalog(catalog_path, create=False) as catalog:
            completed = catalog.get_job(queued.job_id)
            assert completed.status is IndexJobStatus.SUCCEEDED
            assert completed.result_snapshot_id is not None
    finally:
        fixture.close()


def test_jobs_run_cli_completes_one_cursor_fair_cycle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    catalog_path = tmp_path / "worker.sqlite"
    cas_root = tmp_path / "cas"
    provider = LocalWorkspaceProvider(fixture.workspace)
    try:
        try:
            provider.require_support()
        except UnsupportedWorkspaceCreation as exc:
            pytest.skip(str(exc))
        with LocalCAS.provision(cas_root):
            pass
        with SQLiteCatalog(catalog_path) as catalog:
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            queued = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
        args = cli_module.build_parser().parse_args(
            [
                "jobs",
                "run",
                str(fixture.repository),
                "--cache-dir",
                str(fixture.cache),
                "--catalog",
                str(catalog_path),
                "--cas-root",
                str(cas_root),
                "--workspace-root",
                str(fixture.workspace),
                "--repository",
                _REPOSITORY_KEY,
                "--lease-duration-ms",
                "60000",
                "--heartbeat-interval-ms",
                "5",
                "--scan-limit",
                "1",
                "--max-cycles",
                "1",
                "--json",
            ]
        )

        assert args.handler is cli_module._run_jobs_continuous
        assert cli_module._run_jobs_continuous(args) == 0

        output = tuple(
            json.loads(line) for line in capsys.readouterr().out.splitlines()
        )
        assert output == (
            {
                "attempt_count": 1,
                "disposition": "succeeded",
                "job_id": queued.job_id,
                "type": "job",
            },
            {
                "cycle_count": 1,
                "job_count": 1,
                "page_count": 2,
                "type": "summary",
            },
        )
        assert not tuple(fixture.workspace.glob(".codenib-cache-job-*"))
        assert len(tuple(fixture.workspace.glob(".*.discarded-*"))) == 2
        with SQLiteCatalog(catalog_path, create=False) as catalog:
            completed = catalog.get_job(queued.job_id)
            assert completed.status is IndexJobStatus.SUCCEEDED
            assert completed.result_snapshot_id is not None
    finally:
        fixture.close()


def test_compiler_cache_resolver_rejects_declared_foreign_store_before_scope(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    catalog_path = tmp_path / "worker.sqlite"
    publication_calls: list[str] = []
    try:
        with (
            _JobTrackingCAS(tmp_path / "worker-cas") as worker_cas,
            _JobTrackingCAS(tmp_path / "foreign-cas") as foreign_cas,
        ):
            with SQLiteCatalog(catalog_path) as catalog:
                (
                    repository_id,
                    source_revision_id,
                    profile_id,
                ) = _register_bm25_job_subject(catalog, fixture, plan)
                queued = _create_bm25_job(
                    catalog,
                    repository_id=repository_id,
                    source_revision_id=source_revision_id,
                    profile_id=profile_id,
                )

            resources = _ScopedCompilerCacheResources(
                CompilerCacheJobExecutor(
                    cache_dir=fixture.cache,
                    view_type="bm25",
                    repository_source=fixture.source,
                    view_output_owner=fixture.bm25_owner,
                    context_output_owner=fixture.context_owner,
                    view_destination=fixture.bm25_destination,
                    context_destination=fixture.context_destination,
                    workspace_provider=fixture.provider,
                    repository_key=_REPOSITORY_KEY,
                    object_store=foreign_cas,
                    environ={},
                )
            )
            worker = IndexJobWorker(
                catalog_factory=_CompilerCacheWorkerFactory(
                    catalog_path,
                    publication_calls,
                ),
                object_store=worker_cas,
                resolver=CompilerCacheJobResolver(
                    resource_factory=resources,
                    object_store=worker_cas,
                ),
                lease_duration_ms=60_000,
                heartbeat_interval_ms=5,
                owner_id_factory=lambda: "compiler-cache-worker",
            )

            with pytest.raises(
                StorageIntegrityError,
                match="resolver object store",
            ):
                worker.run_once()

            assert resources.declarations == 1
            assert resources.contexts == []
            assert resources.object_stores == []
            assert resources.exits == 0
            assert fixture.source.usable
            assert fixture.bm25_owner.state == "empty"
            assert fixture.context_owner.state == "empty"
            assert worker_cas.put_chunk_receipts == []
            assert foreign_cas.put_chunk_receipts == []
            assert worker_cas.retained_receipt_sets == []
            assert publication_calls == []
            with SQLiteCatalog(catalog_path, create=False) as catalog:
                running = catalog.get_job(queued.job_id)
                assert running.status is IndexJobStatus.RUNNING
                assert running.result_snapshot_id is None
    finally:
        fixture.close()


@pytest.mark.parametrize("case", ("incremental", "optional", "queued"))
def test_compiler_cache_resolver_rejects_unsupported_job_before_scope(
    tmp_path: Path,
    case: str,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            queued = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
                requested_mode="incremental" if case == "incremental" else "full",
                required=case != "optional",
            )
            if case != "queued":
                catalog.acquire_job_lease(
                    queued.job_id,
                    owner_id="scoped-resolver-worker",
                    lease_duration_ms=60_000,
                )
            job = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)
            resources = _ScopedCompilerCacheResources(
                CompilerCacheJobExecutor(
                    cache_dir=fixture.cache,
                    view_type="bm25",
                    repository_source=fixture.source,
                    view_output_owner=fixture.bm25_owner,
                    context_output_owner=fixture.context_owner,
                    view_destination=fixture.bm25_destination,
                    context_destination=fixture.context_destination,
                    workspace_provider=fixture.provider,
                    repository_key=_REPOSITORY_KEY,
                    object_store=cas,
                    environ={},
                ),
                close_resources=False,
            )
            resolver = CompilerCacheJobResolver(
                resource_factory=resources,
                object_store=cas,
            )

            with pytest.raises(StorageValidationError, match="active required FULL"):
                resolver.resolve(job, views)

            assert resources.contexts == []
            assert resources.object_stores == []
            assert resources.exits == 0
            assert resources.declarations == 0
            assert cas.put_chunk_receipts == []
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 0
            assert fixture.source.usable
            assert fixture.bm25_owner.state == "empty"
            assert fixture.context_owner.state == "empty"
    finally:
        fixture.close()


def _seed_bm25_snapshot(
    catalog: SQLiteCatalog,
    cas: LocalCAS,
    *,
    repository_id: str,
    source_revision_id: str,
    profile_id: str,
    payload: bytes,
    expected_generation: int,
) -> tuple[dict[str, object], str]:
    receipt = cas.put_bytes(payload)
    catalog.register_object(
        receipt.digest,
        storage_key=receipt.storage_key,
        byte_size=receipt.byte_size,
        media_type="application/x-test-old-bm25",
    )
    generation_id = catalog.stage_view_generation(
        repository_id,
        source_revision_id,
        profile_id,
        "bm25",
        receipt.digest,
        schema_version=f"old-bm25-{expected_generation + 1}",
        metadata={"seed_generation": expected_generation + 1},
    )
    publication = catalog.publish_snapshot(
        repository_id,
        source_revision_id,
        (generation_id,),
        expected_generation=expected_generation,
    )
    return publication, receipt.digest


def test_compiler_cache_bm25_job_publishes_only_exact_bundle_and_replays(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    retry_bm25_owner = PublishedWorkspaceReceiptOwner()
    retry_context_owner = PublishedWorkspaceReceiptOwner()
    plan = _expected_bm25_job_plan(fixture)
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            _RetentionAwareJobCatalog(
                tmp_path / "catalog.sqlite",
                cas,
            ) as catalog,
        ):
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            job = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            lease = catalog.acquire_job_lease(
                job.job_id,
                owner_id="worker-1",
                lease_duration_ms=60_000,
            )

            result = _publish_bm25_job(
                fixture,
                job_id=job.job_id,
                owner_id=lease.owner_id,
                fencing_token=lease.fencing_token,
                catalog=catalog,
                cas=cas,
            )

            assert type(result) is CompilerCacheJobPublicationResult
            assert result.job.status is IndexJobStatus.SUCCEEDED
            assert result.job.job_id == job.job_id
            assert result.job.result_snapshot_id is not None
            assert result.manifest.to_dict() == result.import_plan.manifest.to_dict()
            assert result.manifest.repo_path == "source"
            assert tuple(result.manifest.indexes) == ("bm25",)
            assert result.import_plan.plan_digest == plan.plan_digest
            assert result.recapture.output_view == fixture.bm25_destination
            assert result.recapture.canonical_manifest_bytes == (
                cache_import_module._pretty_manifest_bytes(result.import_plan.manifest)
            )
            assert len(cas.put_chunk_receipts) == 3
            assert len({receipt.digest for receipt in cas.put_chunk_receipts}) == 3
            expected_receipts = tuple(
                sorted(cas.put_chunk_receipts, key=lambda receipt: receipt.digest)
            )
            assert cas.retained_receipt_sets == [expected_receipts]
            assert catalog.job_publication_calls == 1
            summary = catalog.get_manifest_summary(result.job.result_snapshot_id)
            assert tuple(summary["views"]) == ("bm25",)
            assert REPO_MANIFEST_PROJECTION_VIEW not in summary["views"]
            bm25 = summary["views"]["bm25"]
            assert bm25["profile"]["profile_id"] == profile_id
            assert bm25["schema_version"] == VIEW_BUNDLE_SCHEMA
            assert bm25["object"]["media_type"] == VIEW_BUNDLE_MEDIA_TYPE
            assert len(bm25["member_objects"]) == 2
            assert all(
                member["media_type"] == "application/octet-stream"
                for member in bm25["member_objects"]
            )
            for persisted in (bm25["object"], *bm25["member_objects"]):
                receipt = cas.verify(persisted["digest"])
                assert receipt.byte_size == persisted["byte_size"]
                assert receipt.storage_key == persisted["storage_key"]
            first_ref = catalog.resolve_ref(repository_id)
            _seed_bm25_snapshot(
                catalog,
                cas,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
                payload=b"newer BM25 snapshot after completed job",
                expected_generation=1,
            )
            advanced_ref = catalog.resolve_ref(repository_id)
            assert advanced_ref["generation"] == first_ref["generation"] + 1
            assert advanced_ref["snapshot_id"] != first_ref["snapshot_id"]

            retry = _publish_bm25_job(
                fixture,
                job_id=job.job_id,
                owner_id=lease.owner_id,
                fencing_token=lease.fencing_token,
                catalog=catalog,
                cas=cas,
                bm25_owner=retry_bm25_owner,
                context_owner=retry_context_owner,
                bm25_destination=fixture.workspace / "retry-bm25",
                context_destination=fixture.workspace / "retry-context",
            )
            assert retry.job == result.job
            assert retry.import_plan.plan_digest == result.import_plan.plan_digest
            assert retry.job.result_snapshot_id == first_ref["snapshot_id"]
            assert catalog.resolve_ref(repository_id) == advanced_ref
            assert tuple(cas.put_chunk_receipts[:3]) == tuple(
                cas.put_chunk_receipts[3:]
            )
            assert cas.retained_receipt_sets == [
                expected_receipts,
                expected_receipts,
            ]
            assert catalog.job_publication_calls == 2
            assert fixture.bm25_owner.active
            assert fixture.context_owner.active
            assert retry_bm25_owner.active
            assert retry_context_owner.active
    finally:
        retry_context_owner.close()
        retry_bm25_owner.close()
        fixture.close()


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("wrong_owner", PublishConflict),
        ("wrong_fence", PublishConflict),
        ("expired_lease", PublishConflict),
        ("cancelled", StorageValidationError),
        ("ref_drift", PublishConflict),
    ],
)
def test_compiler_cache_bm25_job_failure_preserves_current_ref(
    tmp_path: Path,
    case: str,
    error: type[Exception],
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            clock = {"ms": 1_000}
            catalog._connection.create_function(
                "julianday",
                1,
                lambda _value: 2440587.5 + clock["ms"] / 86_400_000,
            )
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            _seed_bm25_snapshot(
                catalog,
                cas,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
                payload=b"usable old BM25 snapshot",
                expected_generation=0,
            )
            job = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
                expected_ref_generation=1,
            )
            lease = catalog.acquire_job_lease(
                job.job_id,
                owner_id="worker-1",
                lease_duration_ms=1 if case == "expired_lease" else 60_000,
            )
            token = lease.fencing_token
            owner_id = lease.owner_id
            if case == "wrong_owner":
                owner_id = "other-worker"
            elif case == "wrong_fence":
                token += 1
            elif case == "expired_lease":
                clock["ms"] = lease.lease_expires_at_ms + 1_000
            elif case == "cancelled":
                catalog.request_job_cancel(job.job_id)
            elif case == "ref_drift":
                _seed_bm25_snapshot(
                    catalog,
                    cas,
                    repository_id=repository_id,
                    source_revision_id=source_revision_id,
                    profile_id=profile_id,
                    payload=b"newer still-usable BM25 snapshot",
                    expected_generation=1,
                )
            preserved = catalog.resolve_ref(repository_id)
            preserved_view = preserved["manifest"]["views"]["bm25"]
            preserved_payload = cas.read_bytes(preserved_view["object"]["digest"])

            with pytest.raises(error):
                _publish_bm25_job(
                    fixture,
                    job_id=job.job_id,
                    owner_id=owner_id,
                    fencing_token=token,
                    catalog=catalog,
                    cas=cas,
                )

            assert catalog.resolve_ref(repository_id) == preserved
            assert cas.read_bytes(preserved_view["object"]["digest"]) == (
                preserved_payload
            )
            assert catalog.get_job(job.job_id).status is not IndexJobStatus.SUCCEEDED
    finally:
        fixture.close()


@pytest.mark.parametrize(
    "case",
    (
        "not_full",
        "optional",
        "extra_view",
        "profile_mismatch",
        "source_mismatch",
        "repository_mismatch",
        "not_acquired",
    ),
)
def test_compiler_cache_bm25_job_rejects_incompatible_request_before_cas(
    tmp_path: Path,
    case: str,
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            requested_mode = "incremental" if case == "not_full" else "full"
            required = case != "optional"
            extra_views = None
            if case == "extra_view":
                vector_profile = catalog.create_view_profile(
                    "vector",
                    {"test_profile": "extra"},
                )
                extra_views = {
                    "vector": {
                        "profile_id": vector_profile,
                        "requested_mode": "full",
                        "required": False,
                    }
                }
            if case == "profile_mismatch":
                profile_id = catalog.create_view_profile(
                    "bm25",
                    {"test_profile": "wrong"},
                )
            if case == "source_mismatch":
                source_revision_id = catalog.create_source_revision(
                    repository_id,
                    commit_sha=None,
                    dirty=True,
                    source_fingerprint="other-source-fingerprint",
                )
            if case == "repository_mismatch":
                repository_id = catalog.create_repository("owner/other")
                source_revision_id = catalog.create_source_revision(
                    repository_id,
                    commit_sha=None,
                    dirty=True,
                    source_fingerprint=fixture.source.fingerprint,
                )
            job = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
                requested_mode=requested_mode,
                required=required,
                extra_views=extra_views,
            )
            if case == "not_acquired":
                owner_id = "worker-1"
                token = 1
            else:
                lease = catalog.acquire_job_lease(
                    job.job_id,
                    owner_id="worker-1",
                    lease_duration_ms=60_000,
                )
                owner_id = lease.owner_id
                token = lease.fencing_token
            before_files = tuple(
                sorted(
                    path.relative_to(tmp_path / "cas").as_posix()
                    for path in (tmp_path / "cas").rglob("*")
                    if path.is_file()
                )
            )

            with pytest.raises(StorageValidationError):
                _publish_bm25_job(
                    fixture,
                    job_id=job.job_id,
                    owner_id=owner_id,
                    fencing_token=token,
                    catalog=catalog,
                    cas=cas,
                )

            after_files = tuple(
                sorted(
                    path.relative_to(tmp_path / "cas").as_posix()
                    for path in (tmp_path / "cas").rglob("*")
                    if path.is_file()
                )
            )
            assert after_files == before_files
            assert fixture.provider.run_count == 0
            assert fixture.bm25_owner.state == "empty"
            assert fixture.context_owner.state == "empty"
            assert not fixture.bm25_destination.exists()
            assert not fixture.context_destination.exists()
    finally:
        fixture.close()


@pytest.mark.parametrize(
    ("case", "cas_type", "error"),
    (
        ("source_drift", _JobTrackingCAS, RepositoryChangedError),
        ("job_drift", _JobTrackingCAS, StorageValidationError),
        ("receipt_substitution", _ForgedPutReceiptCAS, StorageValidationError),
        ("callback_skipped", _SkippingRetentionCAS, StorageIntegrityError),
    ),
)
def test_compiler_cache_bm25_job_rejects_post_ingest_drift_or_substitution(
    tmp_path: Path,
    case: str,
    cas_type: type[_JobTrackingCAS],
    error: type[Exception],
) -> None:
    fixture = _cache_fixture(tmp_path)
    plan = _expected_bm25_job_plan(fixture)
    try:
        with (
            cas_type(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = _register_bm25_job_subject(
                catalog, fixture, plan
            )
            _publication, preserved_digest = _seed_bm25_snapshot(
                catalog,
                cas,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
                payload=b"retained old snapshot before adversarial drift",
                expected_generation=0,
            )
            job = _create_bm25_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
                expected_ref_generation=1,
            )
            lease = catalog.acquire_job_lease(
                job.job_id,
                owner_id="worker-1",
                lease_duration_ms=60_000,
            )
            preserved_ref = catalog.resolve_ref(repository_id)

            if case == "source_drift":

                def drift_source(put_count: int) -> None:
                    if put_count == 3:
                        (fixture.repository / "sample.py").write_text(
                            "VALUE = 2\n",
                            encoding="utf-8",
                        )

                cas.after_put = drift_source
            elif case == "job_drift":

                def drift_job(put_count: int) -> None:
                    if put_count == 3:
                        catalog.request_job_cancel(job.job_id)

                cas.after_put = drift_job

            with pytest.raises(error):
                _publish_bm25_job(
                    fixture,
                    job_id=job.job_id,
                    owner_id=lease.owner_id,
                    fencing_token=lease.fencing_token,
                    catalog=catalog,
                    cas=cas,
                )

            expected_put_count = 1 if case == "receipt_substitution" else 3
            assert len(cas.put_chunk_receipts) == expected_put_count
            assert catalog.resolve_ref(repository_id) == preserved_ref
            assert cas.read_bytes(preserved_digest) == (
                b"retained old snapshot before adversarial drift"
            )
            assert catalog.get_job(job.job_id).status is IndexJobStatus.RUNNING
            publication_count = catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_publications WHERE job_id = ?",
                (job.job_id,),
            ).fetchone()[0]
            assert publication_count == 0
            if case in {"source_drift", "job_drift", "receipt_substitution"}:
                assert cas.retained_receipt_sets == []
            else:
                assert len(cas.retained_receipt_sets) == 1
    finally:
        fixture.close()


def test_compile_and_import_fresh_cache_uses_one_lease_and_storage_after_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "CodeNib Test"],
        check=True,
    )
    (repository / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    commit = _git_commit(repository, "fresh-retained-route")
    cache = tmp_path / "cache"
    source = capture_repository_source(repository, exclude_roots=(cache,))
    registry = IndexBuilderRegistry()
    registry.register("bm25", BM25IndexBuilder(languages=["python"], max_k=17))
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(index_types=["bm25"], languages=["python"]),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    provider = _TestWorkspaceProvider()
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    state: dict[str, object] = {
        "phase": "before",
        "events": [],
        "lock_count": 0,
    }
    real_lock = cache_import_module.compiler_cache_lock

    @contextmanager
    def tracked_lock(cache_path: Path, *, create: bool = True):
        assert create is True
        state["lock_count"] = int(state["lock_count"]) + 1
        with real_lock(cache_path, create=create):
            state["phase"] = "locked"
            state["events"].append("lock:acquired")  # type: ignore[union-attr]
            try:
                yield
            finally:
                state["phase"] = "released"
                state["events"].append("lock:released")  # type: ignore[union-attr]

    monkeypatch.setattr(cache_import_module, "compiler_cache_lock", tracked_lock)
    real_update = compiler._update_repo_locked

    def tracked_update(*args, **kwargs):
        assert state["phase"] == "locked"
        state["events"].append("compile")  # type: ignore[union-attr]
        return real_update(*args, **kwargs)

    monkeypatch.setattr(compiler, "_update_repo_locked", tracked_update)

    class Guard:
        def __init__(self) -> None:
            self.identity: tuple[int, int] | None = None
            self.verify_count = 0

        def capture(self, observed_cache: Path) -> None:
            assert state["phase"] == "locked"
            assert observed_cache == cache
            metadata = observed_cache.lstat()
            self.identity = (metadata.st_dev, metadata.st_ino)
            state["events"].append("guard:capture")  # type: ignore[union-attr]

        def verify(self, observed_cache: Path) -> None:
            assert state["phase"] == "locked"
            metadata = observed_cache.lstat()
            assert (metadata.st_dev, metadata.st_ino) == self.identity
            self.verify_count += 1
            state["events"].append("guard:verify")  # type: ignore[union-attr]

    guard = Guard()
    try:
        with (
            _LockAwareCAS(tmp_path / "cas", state) as cas,
            _LockAwareCatalog(tmp_path / "catalog.sqlite", state) as catalog,
        ):
            result = compile_and_import_repo(
                compiler,
                repository,
                cache_dir=cache,
                views=("bm25",),
                repository_source=source,
                view_output_owners={"bm25": view_owner},
                context_output_owner=context_owner,
                view_destinations={"bm25": workspace / "published-bm25"},
                context_destination=workspace / "published-context",
                workspace_provider=provider,
                repository_key=_REPOSITORY_KEY,
                catalog=catalog,
                object_store=cas,
                cache_topology_guard=guard,
                environ={},
            )

        assert type(result) is CompilerRetainedPublicationResult
        assert result.manifest.commit == commit
        assert result.views == ("bm25",)
        assert result.import_result.generation == 1
        assert result.import_result.changed is True
        assert state["lock_count"] == 1
        assert guard.verify_count == 2
        events = state["events"]
        assert [event for event in events if isinstance(event, str)] == [
            "lock:acquired",
            "guard:capture",
            "compile",
            "guard:verify",
            "guard:verify",
            "lock:released",
        ]
        storage_events = [
            event
            for event in events
            if isinstance(event, tuple)
            and len(event) == 2
            and isinstance(event[0], str)
            and (event[0].startswith("cas.") or event[0].startswith("catalog.data."))
        ]
        assert storage_events
        assert all(event[1] == "released" for event in storage_events)
    finally:
        context_owner.close()
        view_owner.close()
        source.close()


def test_compile_and_import_fresh_nested_cache_fails_before_workspace_or_data_io(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    (repository / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    cache = repository / ".codenib_cache"
    source = capture_repository_source(repository, exclude_roots=(cache,))
    compiler = IndexCompiler(IndexBuilderRegistry())
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    provider = _TestWorkspaceProvider()
    view_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    backend = _BackendTripwire()
    try:
        # Creating a previously absent child changes the retained repository
        # root.  The low-level route must fail closed before compiler or
        # workspace mutation; CLI topology preflight prevents this layout.
        with pytest.raises(
            RepositoryChangedError,
            match="repository source changed after it was authenticated",
        ):
            compile_and_import_repo(
                compiler,
                repository,
                cache_dir=cache,
                views=("bm25",),
                repository_source=source,
                view_output_owners={"bm25": view_owner},
                context_output_owner=context_owner,
                view_destinations={"bm25": workspace / "published-bm25"},
                context_destination=workspace / "published-context",
                workspace_provider=provider,
                repository_key=_REPOSITORY_KEY,
                catalog=backend,
                object_store=backend,
                environ={},
            )

        assert cache.is_dir()
        assert not (cache / "repo_manifest.json").exists()
        assert not (cache / "bm25").exists()
        assert not (workspace / "published-bm25").exists()
        assert not (workspace / "published-context").exists()
        assert backend.calls == ["contract"]
        assert provider.support_count == 1
        assert provider.run_count == 0
        assert view_owner.state == "empty"
        assert context_owner.state == "empty"
    finally:
        context_owner.close()
        view_owner.close()
        source.close()


@pytest.mark.parametrize("failure", ["source", "config", "failed-view"])
def test_compile_and_import_rejects_drift_or_failed_view_before_data_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    fixture = _cache_fixture(tmp_path)
    compiler = IndexCompiler(IndexBuilderRegistry())
    manifest_path = fixture.cache / "repo_manifest.json"
    manifest = RepoManifest.load(manifest_path)
    if failure == "source":
        returned_manifest = manifest
        expected_error = RepositoryChangedError
        expected_message = "repository source inventory changed"
    elif failure == "config":
        returned_manifest = copy.deepcopy(manifest)
        returned_manifest.indexes["bm25"].config["max_k"] = 23
        expected_error = StorageIntegrityError
        expected_message = "differs from its exact serialized manifest"
    else:
        manifest.indexes["bm25"].status = "failed"
        manifest.save(manifest_path)
        manifest_path.chmod(0o600)
        returned_manifest = manifest
        expected_error = ValueError
        expected_message = "no exact current bm25"

    def fake_update(*args, **kwargs):
        if failure == "source":
            (fixture.repository / "sample.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            )
        return returned_manifest

    monkeypatch.setattr(compiler, "_update_repo_locked", fake_update)
    monkeypatch.setattr(
        cache_import_module,
        "_plan_cache_view",
        lambda *args, **kwargs: pytest.fail("view planning must not run"),
    )
    backend = _BackendTripwire()
    try:
        with pytest.raises(expected_error, match=expected_message):
            compile_and_import_repo(
                compiler,
                fixture.repository,
                cache_dir=fixture.cache,
                views=("bm25",),
                repository_source=fixture.source,
                view_output_owners={"bm25": fixture.bm25_owner},
                context_output_owner=fixture.context_owner,
                view_destinations={"bm25": fixture.bm25_destination},
                context_destination=fixture.context_destination,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                catalog=backend,
                object_store=backend,
                environ={},
            )
        assert backend.calls == ["contract"]
        assert fixture.provider.support_count == 1
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
        assert not fixture.bm25_destination.exists()
        assert not fixture.context_destination.exists()
    finally:
        fixture.close()


def test_compile_and_import_postcommit_retry_reuses_exact_current_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "CodeNib Test"],
        check=True,
    )
    (repository / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    commit = _git_commit(repository, "retained-postcommit-retry")
    cache = tmp_path / "cache"
    source = capture_repository_source(repository, exclude_roots=(cache,))
    builder = BM25IndexBuilder(languages=["python"], max_k=17)
    registry = IndexBuilderRegistry()
    registry.register("bm25", builder)
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(index_types=["bm25"], languages=["python"]),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    provider = _TestWorkspaceProvider()
    first_owner = PublishedWorkspaceReceiptOwner()
    first_context_owner = PublishedWorkspaceReceiptOwner()
    retry_owner = PublishedWorkspaceReceiptOwner()
    retry_context_owner = PublishedWorkspaceReceiptOwner()
    first_destination = workspace / "first-bm25"
    first_context_destination = workspace / "first-context"
    retry_destination = workspace / "retry-bm25"
    retry_context_destination = workspace / "retry-context"

    def publish(
        *,
        view_owner: PublishedWorkspaceReceiptOwner,
        context_owner: PublishedWorkspaceReceiptOwner,
        view_destination: Path,
        context_destination: Path,
        catalog: SQLiteCatalog,
        object_store: LocalCAS,
    ) -> CompilerRetainedPublicationResult:
        return compile_and_import_repo(
            compiler,
            repository,
            cache_dir=cache,
            views=("bm25",),
            repository_source=source,
            view_output_owners={"bm25": view_owner},
            context_output_owner=context_owner,
            view_destinations={"bm25": view_destination},
            context_destination=context_destination,
            workspace_provider=provider,
            repository_key=_REPOSITORY_KEY,
            catalog=catalog,
            object_store=object_store,
            expected_generation=0,
            environ={},
        )

    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            _PostCommitInterruptCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            with pytest.raises(RuntimeError, match="postcommit interruption"):
                publish(
                    view_owner=first_owner,
                    context_owner=first_context_owner,
                    view_destination=first_destination,
                    context_destination=first_context_destination,
                    catalog=catalog,
                    object_store=cas,
                )

            manifest_path = cache / "repo_manifest.json"
            manifest_bytes = manifest_path.read_bytes()
            metadata = manifest_path.stat()
            manifest_identity = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            )

            def unexpected_build(*args, **kwargs):
                pytest.fail("an exact current-cache retry must not call the builder")

            monkeypatch.setattr(builder, "build", unexpected_build)
            retry = publish(
                view_owner=retry_owner,
                context_owner=retry_context_owner,
                view_destination=retry_destination,
                context_destination=retry_context_destination,
                catalog=catalog,
                object_store=cas,
            )

            assert catalog.interrupted_publication is not None
            assert catalog.interrupted_publication["changed"] is True
            assert retry.manifest.commit == commit
            assert retry.import_result.snapshot_id == (
                catalog.interrupted_publication["snapshot_id"]
            )
            assert retry.import_result.generation == (
                catalog.interrupted_publication["generation"]
            )
            assert retry.import_result.generation == 1
            assert retry.import_result.changed is False
            assert manifest_path.read_bytes() == manifest_bytes
            retry_metadata = manifest_path.stat()
            assert (
                retry_metadata.st_dev,
                retry_metadata.st_ino,
                retry_metadata.st_size,
                retry_metadata.st_mtime_ns,
            ) == manifest_identity
            assert first_destination.is_dir()
            assert first_context_destination.is_dir()
            assert retry_destination.is_dir()
            assert retry_context_destination.is_dir()
            assert first_owner.active
            assert first_context_owner.active
            assert retry_owner.active
            assert retry_context_owner.active
            assert source.usable
    finally:
        retry_context_owner.close()
        retry_owner.close()
        first_context_owner.close()
        first_owner.close()
        source.close()


def _persist_fixture_source_selection(
    fixture: _CacheFixture,
    selection: RepositorySourceSelection,
):
    identity = fingerprint_repository(
        fixture.repository,
        exclude_roots=(fixture.cache,),
        selection=selection,
    )
    manifest_path = fixture.cache / "repo_manifest.json"
    manifest = RepoManifest.load(manifest_path)
    manifest.source_selection = selection
    manifest.last_indexed_source_selection_digest = selection.digest
    manifest.source_fingerprint = identity.value
    manifest.last_indexed_source_fingerprint = identity.value
    manifest.file_count = identity.file_count
    entry = manifest.indexes["bm25"]
    entry.source_selection_digest = selection.digest
    entry.source_fingerprint = identity.value
    entry.config["source_selection_digest"] = selection.digest
    entry.metadata["source_selection_digest"] = selection.digest
    manifest.save(manifest_path)
    manifest_path.chmod(0o600)
    return identity


def test_compiler_cache_source_selection_reads_exact_persisted_policy(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    selection = RepositorySourceSelection(("generated",))
    _persist_fixture_source_selection(fixture, selection)

    observed = compiler_cache_source_selection(fixture.cache)

    assert observed == selection
    assert observed is not selection


def test_nondefault_source_selection_survives_portable_multiview_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    selection = RepositorySourceSelection(("generated",))
    persisted_identity = _persist_fixture_source_selection(fixture, selection)
    fixture.source.close()
    fixture.source = capture_repository_source(
        fixture.repository,
        exclude_roots=(fixture.cache,),
        selection=selection,
    )
    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        lambda *args, **kwargs: _fake_import_result(),
    )

    try:
        result = _call_generic(fixture)

        assert fixture.source.fingerprint == persisted_identity.value
        assert result.import_plan.manifest.source_selection == selection
        assert result.import_plan.source.source_selection == selection
        entry = result.import_plan.manifest.indexes["bm25"]
        assert entry.source_selection_digest == selection.digest
        assert entry.config["source_selection_digest"] == selection.digest
        assert (
            RepoManifest.load(result.context_artifact.manifest_path).source_selection
            == selection
        )
    finally:
        fixture.close()


@pytest.mark.parametrize(
    ("mismatch", "error_type", "message"),
    [
        ("binding", StorageIntegrityError, "source selection differs"),
        ("config", ValueError, "no exact current bm25"),
    ],
)
def test_source_selection_mismatch_fails_before_workspace_or_storage_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
    error_type: type[Exception],
    message: str,
) -> None:
    fixture = _cache_fixture(tmp_path)
    selection = RepositorySourceSelection(("generated",))
    if mismatch == "binding":
        # Exercise the independent identity axis after the byte fingerprint was
        # captured; the coordinator must not authorize a selection mismatch
        # merely because the retained bytes still match the manifest.
        fixture.source._source_selection_identity = selection  # type: ignore[attr-defined]
    else:
        manifest_path = fixture.cache / "repo_manifest.json"
        manifest = RepoManifest.load(manifest_path)
        manifest.indexes["bm25"].config["source_selection_digest"] = selection.digest
        manifest.save(manifest_path)
        manifest_path.chmod(0o600)

    backend = _BackendTripwire()
    planned = False

    def unexpected_plan(*args, **kwargs):
        nonlocal planned
        planned = True
        raise AssertionError("cache view planning must not run")

    monkeypatch.setattr(cache_import_module, "_plan_cache_view", unexpected_plan)
    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        lambda *args, **kwargs: pytest.fail("storage import must not run"),
    )
    try:
        with pytest.raises(error_type, match=message):
            _call_generic(fixture, catalog=backend, object_store=backend)
        assert not planned
        assert backend.calls == ["contract"]
        assert fixture.provider.support_count == 1
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
    finally:
        fixture.close()


def test_generic_bm25_import_returns_ordered_detached_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        lambda *args, **kwargs: _fake_import_result(),
    )
    try:
        result = _call_generic(fixture)

        assert type(result) is CompilerCacheMultiViewImportResult
        assert result.views == ("bm25",)
        assert tuple(result.recapture_map) == ("bm25",)
        recapture = result.recaptures[0]
        assert type(recapture) is CompilerCacheViewRecaptureResult
        assert recapture.view_type == "bm25"
        assert recapture.source_view == fixture.cache / "bm25"
        assert recapture.output_view == fixture.bm25_destination
        assert recapture.output_file_fingerprints == {
            record.path: {"size": record.size, "sha256": record.sha256}
            for record in recapture.output_records
        }
        assert result.context_artifact.manifest_path.read_bytes() == (
            result.canonical_manifest_bytes
        )
        assert result.import_plan.selection.selected_views == ("bm25",)
        assert result.import_result == _fake_import_result()
    finally:
        fixture.close()


def test_generic_import_rejects_nonexact_or_misaligned_view_mappings(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    backend = _BackendTripwire()

    class DictSubclass(dict):
        pass

    extra_owner = PublishedWorkspaceReceiptOwner()
    cases = (
        (
            {"views": ["bm25"]},
            (TypeError, "exact tuple"),
        ),
        (
            {"views": ("vector", "bm25")},
            (ValueError, "canonical portable subset"),
        ),
        (
            {"view_output_owners": DictSubclass({"bm25": fixture.bm25_owner})},
            (TypeError, "exact dict"),
        ),
        (
            {
                "view_destinations": {
                    "bm25": fixture.bm25_destination,
                    "vector": fixture.workspace / "vector",
                }
            },
            (ValueError, "exact selected keys and order"),
        ),
        (
            {
                "views": ("bm25", "vector"),
                "view_output_owners": {
                    "vector": extra_owner,
                    "bm25": fixture.bm25_owner,
                },
                "view_destinations": {
                    "bm25": fixture.bm25_destination,
                    "vector": fixture.workspace / "vector",
                },
            },
            (ValueError, "exact selected keys and order"),
        ),
        (
            {
                "views": ("bm25", "vector"),
                "view_output_owners": {
                    "bm25": fixture.bm25_owner,
                    "vector": fixture.bm25_owner,
                },
                "view_destinations": {
                    "bm25": fixture.bm25_destination,
                    "vector": fixture.workspace / "vector",
                },
            },
            (ValueError, "distinct receipt owners"),
        ),
        (
            {"view_output_owners": {"bm25": fixture.context_owner}},
            (ValueError, "distinct receipt owners"),
        ),
        (
            {"view_destinations": {"bm25": str(fixture.bm25_destination)}},
            (TypeError, "exact paths"),
        ),
    )
    try:
        for overrides, (error_type, message) in cases:
            with pytest.raises(error_type, match=message):
                _call_generic(
                    fixture,
                    catalog=backend,
                    object_store=backend,
                    **overrides,
                )
        assert backend.calls == []
        assert fixture.provider.support_count == 0
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
        assert extra_owner.state == "empty"
    finally:
        extra_owner.close()
        fixture.close()


def test_import_recaptures_inside_cache_lock_and_imports_after_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    lock_state = {"held": False}
    lock_calls: list[tuple[Path, bool]] = []
    real_lock = cache_import_module.compiler_cache_lock
    real_plan = cache_import_module.plan_repo_manifest_import_bytes
    planned_before_publish = {"value": False}

    @contextmanager
    def tracked_lock(cache: Path, *, create: bool = True):
        lock_calls.append((cache, create))
        with real_lock(cache, create=create):
            lock_state["held"] = True
            try:
                yield
            finally:
                lock_state["held"] = False

    def tracked_plan(*args, **kwargs):
        assert lock_state["held"]
        planned_before_publish["value"] = True
        return real_plan(*args, **kwargs)

    real_publish = cache_import_module._publish_recaptured_bm25_view
    real_context_plan = cache_import_module.plan_context_artifact_strict
    real_context_publish = cache_import_module.publish_planned_context_artifact_strict
    context_planned = {"value": False}

    def tracked_publish(*args, **kwargs):
        assert planned_before_publish["value"]
        return real_publish(*args, **kwargs)

    def tracked_context_plan(*args, **kwargs):
        assert lock_state["held"]
        assert fixture.bm25_owner.active
        assert fixture.context_owner.state == "empty"
        context_planned["value"] = True
        return real_context_plan(*args, **kwargs)

    def tracked_context_publish(*args, **kwargs):
        assert lock_state["held"]
        assert context_planned["value"]
        return real_context_publish(*args, **kwargs)

    def imported(plan, **kwargs):
        assert not lock_state["held"]
        assert plan.selection.selected_views == ("bm25",)
        assert kwargs["artifact_owner"] is fixture.context_owner
        assert kwargs["repository_source"] is fixture.source
        return _fake_import_result()

    monkeypatch.setattr(cache_import_module, "compiler_cache_lock", tracked_lock)
    monkeypatch.setattr(
        cache_import_module,
        "plan_repo_manifest_import_bytes",
        tracked_plan,
    )
    monkeypatch.setattr(
        cache_import_module,
        "_publish_recaptured_bm25_view",
        tracked_publish,
    )
    monkeypatch.setattr(
        cache_import_module,
        "plan_context_artifact_strict",
        tracked_context_plan,
    )
    monkeypatch.setattr(
        cache_import_module,
        "publish_planned_context_artifact_strict",
        tracked_context_publish,
    )
    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        imported,
    )

    try:
        result = _call(fixture)

        assert result.import_result == _fake_import_result()
        assert result.import_plan.selection.selected_views == ("bm25",)
        assert result.context_artifact.manifest_path.read_bytes() == (
            result.recapture.canonical_manifest_bytes
        )
        assert result.recapture.output_view == fixture.bm25_destination
        assert lock_calls == [(fixture.cache, False)]
        assert fixture.provider.support_count == 3
        assert fixture.provider.run_count == 2
        assert fixture.bm25_owner.active
        assert fixture.context_owner.active
        assert fixture.source.usable
    finally:
        fixture.close()


def test_manifest_fingerprint_mismatch_fails_before_workspace_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    manifest = RepoManifest.load(fixture.cache / "repo_manifest.json")
    manifest.indexes["bm25"].config["artifact_file_fingerprints"]["documents.json"][
        "sha256"
    ] = ("0" * 64)
    manifest.save(fixture.cache / "repo_manifest.json")
    (fixture.cache / "repo_manifest.json").chmod(0o600)
    imported = False

    def unexpected_import(*args, **kwargs):
        nonlocal imported
        imported = True
        raise AssertionError("storage import must not run")

    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        unexpected_import,
    )
    try:
        with pytest.raises(StorageIntegrityError, match="manifest fingerprints"):
            _call(fixture)
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
        assert not imported
    finally:
        fixture.close()


@pytest.mark.parametrize(
    ("tamper", "error"),
    [
        ("source", "repository source"),
        ("documents", "manifest fingerprints"),
        ("manifest", "manifest fingerprints"),
    ],
)
def test_source_and_cache_tamper_fail_before_storage_or_workspace_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    error: str,
) -> None:
    fixture = _cache_fixture(tmp_path)
    if tamper == "source":
        (fixture.repository / "sample.py").write_text(
            "SOURCE_CHANGED = True\n",
            encoding="utf-8",
        )
    elif tamper == "documents":
        with (fixture.cache / "bm25/documents.json").open("ab") as handle:
            handle.write(b"\n")
    else:
        manifest = RepoManifest.load(fixture.cache / "repo_manifest.json")
        manifest.indexes["bm25"].metadata["artifact_file_fingerprints"][
            "documents.json"
        ]["sha256"] = ("f" * 64)
        manifest.save(fixture.cache / "repo_manifest.json")
        (fixture.cache / "repo_manifest.json").chmod(0o600)

    storage_calls: list[str] = []

    def unexpected_import(*args, **kwargs):
        storage_calls.append("import")
        raise AssertionError("storage import must not run")

    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        unexpected_import,
    )
    backend = _BackendTripwire()
    try:
        with pytest.raises((RuntimeError, ValueError), match=error):
            _call(fixture, catalog=backend, object_store=backend)
        assert storage_calls == []
        assert backend.calls == ["contract"]
        assert fixture.provider.support_count == 1
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
        fixture.bm25_owner.close()
        fixture.context_owner.close()
        assert fixture.bm25_owner.closed
        assert fixture.context_owner.closed
    finally:
        fixture.close()


def test_existing_context_destination_denies_before_bm25_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    fixture.context_destination.mkdir()
    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        lambda *args, **kwargs: pytest.fail("storage import must not run"),
    )
    try:
        with pytest.raises(FileExistsError, match="must be missing"):
            _call(fixture)
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
    finally:
        fixture.close()


@pytest.mark.parametrize(
    ("overrides", "error", "contract_probes"),
    [
        ({"repository_key": "Owner/Repo"}, "repository key", 0),
        ({"max_context_files": 0}, "context file limit", 0),
        ({"expected_generation": -1}, "expected ref generation", 0),
    ],
)
def test_invalid_import_inputs_fail_before_support_contract_or_workspace(
    tmp_path: Path,
    overrides: dict[str, object],
    error: str,
    contract_probes: int,
) -> None:
    fixture = _cache_fixture(tmp_path)
    backend = _BackendTripwire()
    try:
        with pytest.raises(StorageValidationError, match=error):
            _call(
                fixture,
                catalog=backend,
                object_store=backend,
                **overrides,
            )
        assert backend.calls == ["contract"] * contract_probes
        assert fixture.provider.support_count == 0
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
    finally:
        fixture.close()


def test_short_manifest_commit_fails_after_contract_probe_without_data_io(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    manifest = RepoManifest.load(fixture.cache / "repo_manifest.json")
    manifest.commit = "abc123"
    manifest.last_indexed_commit = "abc123"
    manifest.indexes["bm25"].commit = "abc123"
    manifest.save(fixture.cache / "repo_manifest.json")
    (fixture.cache / "repo_manifest.json").chmod(0o600)
    backend = _BackendTripwire()
    try:
        with pytest.raises(ValueError, match="full lowercase Git SHA"):
            _call(fixture, catalog=backend, object_store=backend)
        assert backend.calls == ["contract"]
        assert fixture.provider.support_count == 1
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
    finally:
        fixture.close()


def test_nested_cache_must_be_absent_from_retained_source_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    fixture.source.close()
    included_cache = fixture.repository / "compiler-state"
    fixture.cache.rename(included_cache)
    fixture.cache = included_cache
    # Stabilize the lock file, then deliberately capture the cache as source.
    with cache_import_module.compiler_cache_lock(fixture.cache):
        pass
    fixture.source = capture_repository_source(fixture.repository)
    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        lambda *args, **kwargs: pytest.fail("storage import must not run"),
    )
    try:
        with pytest.raises(StorageIntegrityError, match="source records"):
            _call(fixture)
        assert fixture.provider.run_count == 0
    finally:
        fixture.close()


def test_manifest_is_read_bounded_without_following_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)
    manifest = fixture.cache / "repo_manifest.json"
    target = fixture.cache / "manifest-target.json"
    manifest.rename(target)
    manifest.symlink_to(target.name)
    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        lambda *args, **kwargs: pytest.fail("storage import must not run"),
    )
    try:
        with pytest.raises(ValueError, match="not a private regular file"):
            _call(fixture)
        assert fixture.provider.run_count == 0
    finally:
        fixture.close()


def test_missing_cache_fails_without_creating_directory_or_lock(tmp_path: Path) -> None:
    fixture = _cache_fixture(tmp_path)
    missing_cache = tmp_path / "missing-cache"
    fixture.cache = missing_cache
    backend = _BackendTripwire()
    try:
        with pytest.raises(RuntimeError, match="open compiler cache directory"):
            _call(fixture, catalog=backend, object_store=backend)
        assert not missing_cache.exists()
        assert backend.calls == ["contract"]
        assert fixture.provider.support_count == 1
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
    finally:
        fixture.close()


def test_missing_fixed_lock_fails_without_mutating_existing_cache(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    lock_path = fixture.cache / ".index-compiler.lock"
    lock_path.unlink()
    before = tuple(
        sorted(
            path.relative_to(fixture.cache).as_posix()
            for path in fixture.cache.rglob("*")
        )
    )
    backend = _BackendTripwire()
    try:
        with pytest.raises(RuntimeError, match="lock does not exist"):
            _call(fixture, catalog=backend, object_store=backend)
        after = tuple(
            sorted(
                path.relative_to(fixture.cache).as_posix()
                for path in fixture.cache.rglob("*")
            )
        )
        assert after == before
        assert not lock_path.exists()
        assert backend.calls == ["contract"]
        assert fixture.provider.support_count == 1
        assert fixture.provider.run_count == 0
        assert fixture.bm25_owner.state == "empty"
        assert fixture.context_owner.state == "empty"
    finally:
        fixture.close()


def test_import_failure_preserves_published_evidence_and_caller_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _cache_fixture(tmp_path)

    def fail_import(*args, **kwargs):
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(
        cache_import_module,
        "import_retained_repo_manifest",
        fail_import,
    )
    try:
        with pytest.raises(RuntimeError, match="catalog unavailable"):
            _call(fixture)
        assert fixture.bm25_destination.is_dir()
        assert fixture.context_destination.is_dir()
        assert fixture.bm25_owner.active
        assert fixture.context_owner.active
        assert fixture.source.usable
    finally:
        fixture.close()


def test_postcommit_interruption_retries_as_exact_unchanged_import(
    tmp_path: Path,
) -> None:
    fixture = _cache_fixture(tmp_path)
    first_bm25_owner = fixture.bm25_owner
    first_context_owner = fixture.context_owner
    first_bm25_destination = fixture.bm25_destination
    first_context_destination = fixture.context_destination
    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            _PostCommitInterruptCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            with pytest.raises(RuntimeError, match="postcommit interruption"):
                _call(fixture, catalog=catalog, object_store=cas)

            assert first_bm25_destination.is_dir()
            assert first_context_destination.is_dir()
            assert first_bm25_owner.active
            assert first_context_owner.active
            assert fixture.source.usable

            fixture.workspace = tmp_path / "retry-workspace"
            fixture.workspace.mkdir(mode=0o700)
            fixture.bm25_owner = PublishedWorkspaceReceiptOwner()
            fixture.context_owner = PublishedWorkspaceReceiptOwner()
            retry = _call(fixture, catalog=catalog, object_store=cas)

            assert catalog.interrupted_publication is not None
            assert catalog.interrupted_publication["changed"] is True
            assert retry.import_result.generation == 1
            assert retry.import_result.changed is False
            assert (
                retry.import_result.snapshot_id
                == catalog.interrupted_publication["snapshot_id"]
            )
            assert (
                retry.import_result.generation
                == catalog.interrupted_publication["generation"]
            )
            assert fixture.provider.support_count == 6
            assert fixture.provider.run_count == 4
            assert first_bm25_owner.active
            assert first_context_owner.active
    finally:
        fixture.close()
        first_context_owner.close()
        first_bm25_owner.close()


def _git_commit(repository: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", message],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@dataclass(frozen=True)
class _MultiViewCompilerFixture:
    repository: Path
    source_file: Path
    cache: Path
    compiler: IndexCompiler
    commit: str


def _multiview_compiler_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    marker: str,
) -> _MultiViewCompilerFixture:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "CodeNib Test"],
        check=True,
    )
    source_file = repository / "sample.py"
    source_file.write_text(
        f"MULTIVIEW_CACHE_MARKER = {marker!r}\n",
        encoding="utf-8",
    )
    commit = _git_commit(repository, f"multiview-{marker.lower()}")

    vector_builder = VectorIndexBuilder(
        languages=["python"],
        embedding_model="test/model",
        embedding_dimension=4,
        build_levels=["l2"],
    )

    def build_vector(**kwargs):
        assert kwargs["artifact_metadata"]["builder_schema"] == 8
        assert kwargs["_atomic_publish"] is False
        store = CodeVectorStore(
            embedding_model=kwargs["embedding_model"],
            embedding_provider=kwargs["embedding_provider"],
            dimension=kwargs["embedding_dimension"],
            index_metric=kwargs["index_metric"],
            store_path=kwargs["index_path"],
            embedding=_DeterministicEmbedding(kwargs["embedding_dimension"]),
            artifact_metadata=kwargs["artifact_metadata"],
            **kwargs["embedding_kwargs"],
        )
        store.add_code_chunks(
            [
                {
                    "content": source_file.read_text(encoding="utf-8"),
                    "chunk_type": "file",
                    "name": "sample",
                    "file": "sample.py",
                    "start_line": 0,
                    "end_line": 0,
                    "node_id": "sample.py",
                }
            ],
            level="l2",
        )
        store.save(kwargs["index_path"])
        return store

    monkeypatch.setattr(
        "codenib.index.embedding.builders.build_hierarchical_vector_store",
        build_vector,
    )
    registry = IndexBuilderRegistry()
    registry.register(
        "bm25",
        BM25IndexBuilder(languages=["python"], max_k=17),
    )
    registry.register("vector", vector_builder)
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(
            index_types=["bm25", "vector"],
            languages=["python"],
        ),
    )
    cache = repository / ".codenib_cache"
    manifest = compiler.compile_repo(
        str(repository),
        index_types=["bm25", "vector"],
        cache_dir=str(cache),
    )
    assert manifest.commit == commit
    assert manifest.indexes["vector"].config["builder_schema"] == 8
    assert (cache / "vector/l2/documents_test__model.json").is_file()
    assert not (cache / "vector/l2/documents_test__model.pkl").exists()
    return _MultiViewCompilerFixture(
        repository=repository,
        source_file=source_file,
        cache=cache,
        compiler=compiler,
        commit=commit,
    )


@dataclass
class _VectorJobFixture:
    compiled: _MultiViewCompilerFixture
    source: RepositorySourceBinding
    workspace: Path
    provider: _TestWorkspaceProvider
    vector_owner: PublishedWorkspaceReceiptOwner
    context_owner: PublishedWorkspaceReceiptOwner

    @property
    def cache(self) -> Path:
        return self.compiled.cache

    @property
    def repository(self) -> Path:
        return self.compiled.repository

    @property
    def vector_destination(self) -> Path:
        return self.workspace / "published-vector"

    @property
    def context_destination(self) -> Path:
        return self.workspace / "published-vector-context"

    def close(self) -> None:
        self.context_owner.close()
        self.vector_owner.close()
        self.source.close()


def _vector_job_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    marker: str,
) -> _VectorJobFixture:
    compiled = _multiview_compiler_fixture(
        tmp_path,
        monkeypatch,
        marker=marker,
    )
    source = capture_repository_source(
        compiled.repository,
        exclude_roots=(compiled.cache,),
    )
    workspace = tmp_path / "vector-job-workspace"
    workspace.mkdir(mode=0o700)
    return _VectorJobFixture(
        compiled=compiled,
        source=source,
        workspace=workspace,
        provider=_TestWorkspaceProvider(),
        vector_owner=PublishedWorkspaceReceiptOwner(),
        context_owner=PublishedWorkspaceReceiptOwner(),
    )


def _expected_vector_job_plan(
    fixture: _VectorJobFixture,
) -> RepoManifestImportPlan:
    manifest = RepoManifest.load(fixture.cache / "repo_manifest.json")
    entry = manifest.indexes["vector"]
    planned = cache_import_module._plan_cache_view(
        "vector",
        fixture.cache / "vector",
        fixture.vector_destination,
        repository_source=fixture.source,
        view_config=entry.config,
        forbidden_paths=(),
        environ={},
    )
    _portable, payload = cache_import_module._portable_manifest(
        manifest,
        views=("vector",),
        planned_views={"vector": planned},
    )
    return plan_repo_manifest_import_bytes(payload, views=("vector",))


def _register_vector_job_subject(
    catalog: SQLiteCatalog,
    fixture: _VectorJobFixture,
    plan: RepoManifestImportPlan,
) -> tuple[str, str, str]:
    repository_id = catalog.create_repository(_REPOSITORY_KEY)
    source_revision_id = catalog.create_source_revision(
        repository_id,
        commit_sha=None,
        dirty=True,
        source_fingerprint=fixture.source.fingerprint,
    )
    intent = plan.views[0]
    profile_id = catalog.create_view_profile(
        "vector",
        intent.profile.config,
        name=intent.profile.name,
    )
    assert profile_id == intent.profile_id
    return repository_id, source_revision_id, profile_id


def _create_vector_job(
    catalog: SQLiteCatalog,
    *,
    repository_id: str,
    source_revision_id: str,
    profile_id: str,
    idempotency_key: str = "compiler-cache-vector",
    view_type: str = "vector",
    requested_mode: str = "full",
    required: bool = True,
    expected_ref_generation: int = 0,
    extra_views: dict[str, dict[str, object]] | None = None,
):
    views: dict[str, dict[str, object]] = {
        view_type: {
            "profile_id": profile_id,
            "requested_mode": requested_mode,
            "required": required,
        }
    }
    if extra_views:
        views.update(copy.deepcopy(extra_views))
    return catalog.create_job(
        repository_id,
        source_revision_id,
        idempotency_key,
        {"contract": INDEX_JOB_REQUEST_CONTRACT, "views": views},
        expected_ref_generation=expected_ref_generation,
    )


def _publish_vector_job(
    fixture: _VectorJobFixture,
    *,
    job_id: str,
    owner_id: str,
    fencing_token: int,
    catalog: SQLiteCatalog,
    cas: LocalCAS,
    vector_owner: PublishedWorkspaceReceiptOwner | None = None,
    context_owner: PublishedWorkspaceReceiptOwner | None = None,
    vector_destination: Path | None = None,
    context_destination: Path | None = None,
) -> CompilerCacheVectorJobPublicationResult:
    return publish_compiler_cache_vector_job(
        fixture.cache,
        job_id=job_id,
        owner_id=owner_id,
        fencing_token=fencing_token,
        repository_source=fixture.source,
        vector_output_owner=vector_owner or fixture.vector_owner,
        context_output_owner=context_owner or fixture.context_owner,
        vector_destination=vector_destination or fixture.vector_destination,
        context_destination=context_destination or fixture.context_destination,
        workspace_provider=fixture.provider,
        repository_key=_REPOSITORY_KEY,
        catalog=catalog,
        object_store=cas,
        environ={},
    )


def _prepare_vector_job(
    fixture: _VectorJobFixture,
    *,
    job,
    views,
    cas: LocalCAS,
    stop_token: _TestStopToken | None = None,
) -> CompilerCacheJobPreparationResult:
    return prepare_compiler_cache_job_view(
        fixture.cache,
        view_type="vector",
        job=job,
        views=views,
        repository_source=fixture.source,
        view_output_owner=fixture.vector_owner,
        context_output_owner=fixture.context_owner,
        view_destination=fixture.vector_destination,
        context_destination=fixture.context_destination,
        workspace_provider=fixture.provider,
        repository_key=_REPOSITORY_KEY,
        object_store=cas,
        stop_token=stop_token,
        environ={},
    )


def test_prepare_compiler_cache_vector_job_uses_only_portable_schema8(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _vector_job_fixture(tmp_path, monkeypatch, marker="PREPARE_VECTOR")
    plan = _expected_vector_job_plan(fixture)

    def native_or_builder_must_not_run(*_args, **_kwargs):
        raise AssertionError("vector preparation invoked a native parser or builder")

    monkeypatch.setattr(
        vector_store_module.faiss,
        "read_index",
        native_or_builder_must_not_run,
    )
    monkeypatch.setattr(
        vector_store_module.compat_pickle,
        "load",
        native_or_builder_must_not_run,
    )
    monkeypatch.setattr(
        "codenib.index.embedding.builders.build_hierarchical_vector_store",
        native_or_builder_must_not_run,
    )
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = (
                _register_vector_job_subject(catalog, fixture, plan)
            )
            queued = _create_vector_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            catalog.acquire_job_lease(
                queued.job_id,
                owner_id="prepare-vector-worker",
                lease_duration_ms=60_000,
            )
            running = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)

            result = prepare_compiler_cache_job_view(
                fixture.cache,
                view_type="vector",
                job=running,
                views=views,
                repository_source=fixture.source,
                view_output_owner=fixture.vector_owner,
                context_output_owner=fixture.context_owner,
                view_destination=fixture.vector_destination,
                context_destination=fixture.context_destination,
                workspace_provider=fixture.provider,
                repository_key=_REPOSITORY_KEY,
                object_store=cas,
                environ={},
            )

            assert type(result) is CompilerCacheJobPreparationResult
            assert result.job == running
            assert result.view == views[0]
            assert result.recapture.view_type == "vector"
            assert result.import_plan.views[0].profile.config["builder_schema"] == 8
            assert result.artifact.schema_version == VIEW_BUNDLE_SCHEMA
            assert len(result.artifact.member_artifacts) == 4
            assert len(cas.put_chunk_receipts) == 5
            assert cas.retained_receipt_sets == []
            assert catalog.get_job(queued.job_id) == running
            assert fixture.provider.run_count == 2
    finally:
        fixture.close()


def test_prepare_compiler_cache_vector_job_stops_during_document_recapture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _vector_job_fixture(tmp_path, monkeypatch, marker="STOP_RECAPTURE")
    plan = _expected_vector_job_plan(fixture)
    token = _TestStopToken()
    real_normalized_documents = strict_vector_module._normalized_documents

    def stop_after_first_document(*args, **kwargs):
        for document in real_normalized_documents(*args, **kwargs):
            token.set()
            yield document

    monkeypatch.setattr(
        strict_vector_module,
        "_normalized_documents",
        stop_after_first_document,
    )
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = (
                _register_vector_job_subject(catalog, fixture, plan)
            )
            queued = _create_vector_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            catalog.acquire_job_lease(
                queued.job_id,
                owner_id="stopped-vector-worker",
                lease_duration_ms=60_000,
            )
            running = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)

            with pytest.raises(
                RuntimeError,
                match="compiler cache job preparation stopped",
            ):
                _prepare_vector_job(
                    fixture,
                    job=running,
                    views=views,
                    cas=cas,
                    stop_token=token,
                )

            assert token.reason is IndexJobStopReason.CANCEL_REQUESTED
            assert catalog.get_job(queued.job_id) == running
            assert catalog.get_job(queued.job_id).status is IndexJobStatus.RUNNING
            assert cas.put_chunk_receipts == []
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 0
            assert fixture.vector_owner.state == "empty"
            assert fixture.context_owner.state == "empty"
            assert not fixture.vector_destination.exists()
            assert not fixture.context_destination.exists()
    finally:
        fixture.close()


def test_prepare_compiler_cache_vector_job_stops_during_candidate_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _vector_job_fixture(tmp_path, monkeypatch, marker="STOP_VALIDATE")
    plan = _expected_vector_job_plan(fixture)
    token = _TestStopToken()
    candidate_scan_reached = threading.Event()
    candidate_scan_resumed = [False]
    real_iter_documents = portable_views_module.iter_bounded_json_array

    def stop_when_candidate_document_is_parsed(*args, **kwargs):
        for document in real_iter_documents(*args, **kwargs):
            token.set()
            candidate_scan_reached.set()
            yield document
            candidate_scan_resumed[0] = True
            pytest.fail("candidate validation requested another document after stop")

    monkeypatch.setattr(
        portable_views_module,
        "iter_bounded_json_array",
        stop_when_candidate_document_is_parsed,
    )
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = (
                _register_vector_job_subject(catalog, fixture, plan)
            )
            queued = _create_vector_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            catalog.acquire_job_lease(
                queued.job_id,
                owner_id="stopped-validation-worker",
                lease_duration_ms=60_000,
            )
            running = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)

            with pytest.raises(
                RuntimeError,
                match="compiler cache job preparation stopped",
            ):
                _prepare_vector_job(
                    fixture,
                    job=running,
                    views=views,
                    cas=cas,
                    stop_token=token,
                )

            assert candidate_scan_reached.is_set()
            assert candidate_scan_resumed == [False]
            assert token.reason is IndexJobStopReason.CANCEL_REQUESTED
            assert catalog.get_job(queued.job_id) == running
            assert catalog.get_job(queued.job_id).status is IndexJobStatus.RUNNING
            assert cas.put_chunk_receipts == []
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 1
            # Validation began after the workspace was staged, so the receipt
            # owner retains its cleanup authority until the fixture closes it.
            assert fixture.vector_owner.state == "cleanup"
            assert fixture.context_owner.state == "empty"
            assert not fixture.vector_destination.exists()
            assert not fixture.context_destination.exists()
    finally:
        fixture.close()


def test_prepare_compiler_cache_vector_job_stops_before_canonical_chunk_poison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _vector_job_fixture(tmp_path, monkeypatch, marker="STOP_VECTOR_CHUNK")
    plan = _expected_vector_job_plan(fixture)
    token = _TestStopToken()
    poison_consumed = [False]

    def poisoned_canonical_chunks(_values):
        token.set()
        yield b"["
        poison_consumed[0] = True
        yield object()

    monkeypatch.setattr(
        strict_vector_module,
        "canonical_json_array_chunks",
        poisoned_canonical_chunks,
    )
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = (
                _register_vector_job_subject(catalog, fixture, plan)
            )
            queued = _create_vector_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            catalog.acquire_job_lease(
                queued.job_id,
                owner_id="stopped-vector-chunk-worker",
                lease_duration_ms=60_000,
            )
            running = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)

            with pytest.raises(
                RuntimeError,
                match="compiler cache job preparation stopped",
            ):
                _prepare_vector_job(
                    fixture,
                    job=running,
                    views=views,
                    cas=cas,
                    stop_token=token,
                )

            assert poison_consumed == [False]
            assert token.reason is IndexJobStopReason.CANCEL_REQUESTED
            assert catalog.get_job(queued.job_id) == running
            assert cas.put_chunk_receipts == []
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 0
            assert fixture.vector_owner.state == "empty"
            assert fixture.context_owner.state == "empty"
            assert not fixture.vector_destination.exists()
            assert not fixture.context_destination.exists()
    finally:
        fixture.close()


def test_prepare_compiler_cache_vector_job_stops_between_cas_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _vector_job_fixture(tmp_path, monkeypatch, marker="STOP_CAS")
    plan = _expected_vector_job_plan(fixture)
    token = _TestStopToken()
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = (
                _register_vector_job_subject(catalog, fixture, plan)
            )
            queued = _create_vector_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            catalog.acquire_job_lease(
                queued.job_id,
                owner_id="stopped-cas-worker",
                lease_duration_ms=60_000,
            )
            running = catalog.get_job(queued.job_id)
            views = catalog.get_job_views(queued.job_id)
            cas.after_put = lambda count: token.set() if count == 1 else None

            with pytest.raises(
                RuntimeError,
                match="compiler cache job preparation stopped",
            ):
                _prepare_vector_job(
                    fixture,
                    job=running,
                    views=views,
                    cas=cas,
                    stop_token=token,
                )

            assert token.reason is IndexJobStopReason.CANCEL_REQUESTED
            assert catalog.get_job(queued.job_id) == running
            assert catalog.get_job(queued.job_id).status is IndexJobStatus.RUNNING
            assert len(cas.put_chunk_receipts) == 1
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 2
    finally:
        fixture.close()


def _seed_vector_snapshot(
    catalog: SQLiteCatalog,
    cas: LocalCAS,
    *,
    repository_id: str,
    source_revision_id: str,
    profile_id: str,
    payload: bytes,
    expected_generation: int,
) -> tuple[dict[str, object], str]:
    receipt = cas.put_bytes(payload)
    catalog.register_object(
        receipt.digest,
        storage_key=receipt.storage_key,
        byte_size=receipt.byte_size,
        media_type="application/x-test-old-vector",
    )
    generation_id = catalog.stage_view_generation(
        repository_id,
        source_revision_id,
        profile_id,
        "vector",
        receipt.digest,
        schema_version=f"old-vector-{expected_generation + 1}",
        metadata={"seed_generation": expected_generation + 1},
    )
    publication = catalog.publish_snapshot(
        repository_id,
        source_revision_id,
        (generation_id,),
        expected_generation=expected_generation,
    )
    return publication, receipt.digest


def test_compiler_cache_vector_job_publishes_schema8_closure_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _vector_job_fixture(tmp_path, monkeypatch, marker="VECTOR_JOB")
    retry_vector_owner = PublishedWorkspaceReceiptOwner()
    retry_context_owner = PublishedWorkspaceReceiptOwner()
    plan = _expected_vector_job_plan(fixture)

    def native_or_builder_must_not_run(*_args, **_kwargs):
        raise AssertionError("vector job adapter invoked a native parser or builder")

    monkeypatch.setattr(
        vector_store_module.faiss,
        "read_index",
        native_or_builder_must_not_run,
    )
    monkeypatch.setattr(
        vector_store_module.compat_pickle,
        "load",
        native_or_builder_must_not_run,
    )
    monkeypatch.setattr(
        "codenib.index.embedding.builders.build_hierarchical_vector_store",
        native_or_builder_must_not_run,
    )
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            _RetentionAwareJobCatalog(
                tmp_path / "catalog.sqlite",
                cas,
            ) as catalog,
        ):
            repository_id, source_revision_id, profile_id = (
                _register_vector_job_subject(catalog, fixture, plan)
            )
            job = _create_vector_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            lease = catalog.acquire_job_lease(
                job.job_id,
                owner_id="vector-worker-1",
                lease_duration_ms=60_000,
            )

            result = _publish_vector_job(
                fixture,
                job_id=job.job_id,
                owner_id=lease.owner_id,
                fencing_token=lease.fencing_token,
                catalog=catalog,
                cas=cas,
            )

            assert type(result) is CompilerCacheVectorJobPublicationResult
            assert result.job.status is IndexJobStatus.SUCCEEDED
            assert result.job.job_id == job.job_id
            assert result.job.result_snapshot_id is not None
            assert result.manifest.to_dict() == result.import_plan.manifest.to_dict()
            assert result.manifest.repo_path == "source"
            assert tuple(result.manifest.indexes) == ("vector",)
            assert result.import_plan.plan_digest == plan.plan_digest
            assert result.import_plan.views[0].profile.config["builder_schema"] == 8
            compatibility = result.import_plan.views[0].profile.config["compatibility"]
            assert set(VECTOR_PROFILE_AXES) - {"builder_schema"} <= set(compatibility)
            assert compatibility["embedding_load_policy"] == {
                "revision": None,
                "trust_remote_code": False,
            }
            assert result.recapture.output_view == fixture.vector_destination
            expected_paths = (
                "config_test__model.json",
                "l2/config_test__model.json",
                "l2/documents_test__model.json",
                "l2/index_test__model.faiss",
            )
            assert tuple(record.path for record in result.recapture.output_records) == (
                expected_paths
            )
            assert not any(
                record.path.endswith(".pkl")
                or "cache" in record.path
                or "incremental_state" in record.path
                for record in result.recapture.output_records
            )
            assert len(cas.put_chunk_receipts) == len(expected_paths) + 1
            assert len({receipt.digest for receipt in cas.put_chunk_receipts}) == 5
            expected_receipts = tuple(
                sorted(cas.put_chunk_receipts, key=lambda receipt: receipt.digest)
            )
            assert cas.retained_receipt_sets == [expected_receipts]
            assert catalog.job_publication_calls == 1
            summary = catalog.get_manifest_summary(result.job.result_snapshot_id)
            assert tuple(summary["views"]) == ("vector",)
            assert REPO_MANIFEST_PROJECTION_VIEW not in summary["views"]
            vector = summary["views"]["vector"]
            assert vector["profile"]["profile_id"] == profile_id
            assert vector["schema_version"] == VIEW_BUNDLE_SCHEMA
            assert vector["object"]["media_type"] == VIEW_BUNDLE_MEDIA_TYPE
            assert len(vector["member_objects"]) == len(expected_paths)
            for persisted in (vector["object"], *vector["member_objects"]):
                receipt = cas.verify(persisted["digest"])
                assert receipt.byte_size == persisted["byte_size"]
                assert receipt.storage_key == persisted["storage_key"]

            first_ref = catalog.resolve_ref(repository_id)
            _seed_vector_snapshot(
                catalog,
                cas,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
                payload=b"newer vector snapshot after completed job",
                expected_generation=1,
            )
            advanced_ref = catalog.resolve_ref(repository_id)
            assert advanced_ref["generation"] == first_ref["generation"] + 1
            assert advanced_ref["snapshot_id"] != first_ref["snapshot_id"]

            retry = _publish_vector_job(
                fixture,
                job_id=job.job_id,
                owner_id=lease.owner_id,
                fencing_token=lease.fencing_token,
                catalog=catalog,
                cas=cas,
                vector_owner=retry_vector_owner,
                context_owner=retry_context_owner,
                vector_destination=fixture.workspace / "retry-vector",
                context_destination=fixture.workspace / "retry-vector-context",
            )
            assert retry.job == result.job
            assert retry.import_plan.plan_digest == result.import_plan.plan_digest
            assert retry.job.result_snapshot_id == first_ref["snapshot_id"]
            assert catalog.resolve_ref(repository_id) == advanced_ref
            assert tuple(cas.put_chunk_receipts[:5]) == tuple(
                cas.put_chunk_receipts[5:]
            )
            assert cas.retained_receipt_sets == [
                expected_receipts,
                expected_receipts,
            ]
            assert catalog.job_publication_calls == 2
            assert fixture.vector_owner.active
            assert fixture.context_owner.active
            assert retry_vector_owner.active
            assert retry_context_owner.active
    finally:
        retry_context_owner.close()
        retry_vector_owner.close()
        fixture.close()


@pytest.mark.parametrize(
    "case",
    (
        "wrong_view",
        "not_full",
        "optional",
        "extra_view",
        "profile_mismatch",
        "source_mismatch",
        "repository_mismatch",
        "not_acquired",
    ),
)
def test_compiler_cache_vector_job_rejects_incompatible_request_before_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    fixture = _vector_job_fixture(tmp_path, monkeypatch, marker=f"REQUEST_{case}")
    plan = _expected_vector_job_plan(fixture)
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = (
                _register_vector_job_subject(catalog, fixture, plan)
            )
            view_type = "vector"
            requested_mode = "incremental" if case == "not_full" else "full"
            required = case != "optional"
            extra_views = None
            if case == "wrong_view":
                view_type = "bm25"
                profile_id = catalog.create_view_profile(
                    "bm25",
                    {"test_profile": "wrong-view"},
                )
            elif case == "extra_view":
                bm25_profile = catalog.create_view_profile(
                    "bm25",
                    {"test_profile": "extra"},
                )
                extra_views = {
                    "bm25": {
                        "profile_id": bm25_profile,
                        "requested_mode": "full",
                        "required": False,
                    }
                }
            elif case == "profile_mismatch":
                profile_id = catalog.create_view_profile(
                    "vector",
                    {"test_profile": "wrong"},
                )
            elif case == "source_mismatch":
                source_revision_id = catalog.create_source_revision(
                    repository_id,
                    commit_sha=None,
                    dirty=True,
                    source_fingerprint="other-source-fingerprint",
                )
            elif case == "repository_mismatch":
                repository_id = catalog.create_repository("owner/other")
                source_revision_id = catalog.create_source_revision(
                    repository_id,
                    commit_sha=None,
                    dirty=True,
                    source_fingerprint=fixture.source.fingerprint,
                )
            job = _create_vector_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
                view_type=view_type,
                requested_mode=requested_mode,
                required=required,
                extra_views=extra_views,
            )
            if case == "not_acquired":
                owner_id = "vector-worker-1"
                token = 1
            else:
                lease = catalog.acquire_job_lease(
                    job.job_id,
                    owner_id="vector-worker-1",
                    lease_duration_ms=60_000,
                )
                owner_id = lease.owner_id
                token = lease.fencing_token

            with pytest.raises(StorageValidationError):
                _publish_vector_job(
                    fixture,
                    job_id=job.job_id,
                    owner_id=owner_id,
                    fencing_token=token,
                    catalog=catalog,
                    cas=cas,
                )

            assert cas.put_chunk_receipts == []
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 0
            assert fixture.vector_owner.state == "empty"
            assert fixture.context_owner.state == "empty"
            assert not fixture.vector_destination.exists()
            assert not fixture.context_destination.exists()
    finally:
        fixture.close()


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("wrong_owner", PublishConflict),
        ("wrong_fence", PublishConflict),
        ("expired_lease", PublishConflict),
        ("cancelled", StorageValidationError),
        ("ref_drift", PublishConflict),
    ],
)
def test_compiler_cache_vector_job_failure_preserves_current_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    error: type[Exception],
) -> None:
    fixture = _vector_job_fixture(tmp_path, monkeypatch, marker=f"LEASE_{case}")
    plan = _expected_vector_job_plan(fixture)
    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            clock = {"ms": 1_000}
            catalog._connection.create_function(
                "julianday",
                1,
                lambda _value: 2440587.5 + clock["ms"] / 86_400_000,
            )
            repository_id, source_revision_id, profile_id = (
                _register_vector_job_subject(catalog, fixture, plan)
            )
            _seed_vector_snapshot(
                catalog,
                cas,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
                payload=b"usable old vector snapshot",
                expected_generation=0,
            )
            job = _create_vector_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
                expected_ref_generation=1,
            )
            lease = catalog.acquire_job_lease(
                job.job_id,
                owner_id="vector-worker-1",
                lease_duration_ms=1 if case == "expired_lease" else 60_000,
            )
            token = lease.fencing_token
            owner_id = lease.owner_id
            if case == "wrong_owner":
                owner_id = "other-vector-worker"
            elif case == "wrong_fence":
                token += 1
            elif case == "expired_lease":
                clock["ms"] = lease.lease_expires_at_ms + 1_000
            elif case == "cancelled":
                catalog.request_job_cancel(job.job_id)
            elif case == "ref_drift":
                _seed_vector_snapshot(
                    catalog,
                    cas,
                    repository_id=repository_id,
                    source_revision_id=source_revision_id,
                    profile_id=profile_id,
                    payload=b"newer still-usable vector snapshot",
                    expected_generation=1,
                )
            preserved = catalog.resolve_ref(repository_id)
            preserved_view = preserved["manifest"]["views"]["vector"]
            preserved_payload = cas.read_bytes(preserved_view["object"]["digest"])

            with pytest.raises(error):
                _publish_vector_job(
                    fixture,
                    job_id=job.job_id,
                    owner_id=owner_id,
                    fencing_token=token,
                    catalog=catalog,
                    cas=cas,
                )

            assert catalog.resolve_ref(repository_id) == preserved
            assert cas.read_bytes(preserved_view["object"]["digest"]) == (
                preserved_payload
            )
            assert catalog.get_job(job.job_id).status is not IndexJobStatus.SUCCEEDED
    finally:
        fixture.close()


def test_compiler_cache_vector_job_profile_binds_every_semantic_axis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _vector_job_fixture(tmp_path, monkeypatch, marker="PROFILE_AXES")
    plan = _expected_vector_job_plan(fixture)
    intent = plan.views[0]
    axes = (*VECTOR_PROFILE_AXES, "embedding_load_policy")
    try:
        for index, axis in enumerate(axes):
            vector_owner = PublishedWorkspaceReceiptOwner()
            context_owner = PublishedWorkspaceReceiptOwner()
            try:
                with (
                    _JobTrackingCAS(tmp_path / f"cas-{index}") as cas,
                    SQLiteCatalog(tmp_path / f"catalog-{index}.sqlite") as catalog,
                ):
                    repository_id, source_revision_id, _profile_id = (
                        _register_vector_job_subject(catalog, fixture, plan)
                    )
                    wrong_config = copy.deepcopy(intent.profile.config)
                    if axis == "builder_schema":
                        wrong_config[axis] = 9
                    else:
                        wrong_config["compatibility"][axis] = {"mismatched_axis": axis}
                    wrong_profile_id = catalog.create_view_profile(
                        "vector",
                        wrong_config,
                        name=intent.profile.name,
                    )
                    assert wrong_profile_id != intent.profile_id
                    job = _create_vector_job(
                        catalog,
                        repository_id=repository_id,
                        source_revision_id=source_revision_id,
                        profile_id=wrong_profile_id,
                        idempotency_key=f"wrong-vector-profile-{index}",
                    )
                    lease = catalog.acquire_job_lease(
                        job.job_id,
                        owner_id=f"vector-profile-worker-{index}",
                        lease_duration_ms=60_000,
                    )

                    with pytest.raises(
                        StorageValidationError,
                        match="vector profile does not match",
                    ):
                        _publish_vector_job(
                            fixture,
                            job_id=job.job_id,
                            owner_id=lease.owner_id,
                            fencing_token=lease.fencing_token,
                            catalog=catalog,
                            cas=cas,
                            vector_owner=vector_owner,
                            context_owner=context_owner,
                            vector_destination=fixture.workspace
                            / f"wrong-profile-vector-{index}",
                            context_destination=fixture.workspace
                            / f"wrong-profile-context-{index}",
                        )

                    assert cas.put_chunk_receipts == []
                    assert cas.retained_receipt_sets == []
                    assert vector_owner.state == "empty"
                    assert context_owner.state == "empty"
            finally:
                context_owner.close()
                vector_owner.close()
        assert fixture.provider.run_count == 0
    finally:
        fixture.close()


def test_compiler_cache_vector_job_rejects_schema7_before_workspace_or_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _vector_job_fixture(tmp_path, monkeypatch, marker="SCHEMA7")
    plan = _expected_vector_job_plan(fixture)
    manifest_path = fixture.cache / "repo_manifest.json"
    manifest = RepoManifest.load(manifest_path)
    manifest.indexes["vector"].config["builder_schema"] = 7
    manifest.indexes["vector"].metadata["builder_schema"] = 7
    manifest.save(manifest_path)
    manifest_path.chmod(0o600)
    try:
        with (
            _JobTrackingCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = (
                _register_vector_job_subject(catalog, fixture, plan)
            )
            job = _create_vector_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
            )
            lease = catalog.acquire_job_lease(
                job.job_id,
                owner_id="vector-worker-1",
                lease_duration_ms=60_000,
            )

            with pytest.raises(ValueError, match="no exact current vector"):
                _publish_vector_job(
                    fixture,
                    job_id=job.job_id,
                    owner_id=lease.owner_id,
                    fencing_token=lease.fencing_token,
                    catalog=catalog,
                    cas=cas,
                )

            assert cas.put_chunk_receipts == []
            assert cas.retained_receipt_sets == []
            assert fixture.provider.run_count == 0
            assert fixture.vector_owner.state == "empty"
            assert fixture.context_owner.state == "empty"
            assert not fixture.vector_destination.exists()
            assert not fixture.context_destination.exists()
    finally:
        fixture.close()


@pytest.mark.parametrize(
    ("case", "cas_type", "error"),
    (
        ("source_drift", _JobTrackingCAS, RepositoryChangedError),
        ("job_drift", _JobTrackingCAS, StorageValidationError),
        ("receipt_substitution", _ForgedPutReceiptCAS, StorageValidationError),
        ("callback_skipped", _SkippingRetentionCAS, StorageIntegrityError),
    ),
)
def test_compiler_cache_vector_job_rejects_post_ingest_drift_or_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    cas_type: type[_JobTrackingCAS],
    error: type[Exception],
) -> None:
    fixture = _vector_job_fixture(tmp_path, monkeypatch, marker=f"DRIFT_{case}")
    plan = _expected_vector_job_plan(fixture)
    try:
        with (
            cas_type(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            repository_id, source_revision_id, profile_id = (
                _register_vector_job_subject(catalog, fixture, plan)
            )
            _publication, preserved_digest = _seed_vector_snapshot(
                catalog,
                cas,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
                payload=b"retained old vector snapshot before adversarial drift",
                expected_generation=0,
            )
            job = _create_vector_job(
                catalog,
                repository_id=repository_id,
                source_revision_id=source_revision_id,
                profile_id=profile_id,
                expected_ref_generation=1,
            )
            lease = catalog.acquire_job_lease(
                job.job_id,
                owner_id="vector-worker-1",
                lease_duration_ms=60_000,
            )
            preserved_ref = catalog.resolve_ref(repository_id)

            if case == "source_drift":

                def drift_source(put_count: int) -> None:
                    if put_count == 5:
                        fixture.compiled.source_file.write_text(
                            "VECTOR_JOB_SOURCE_DRIFT = True\n",
                            encoding="utf-8",
                        )

                cas.after_put = drift_source
            elif case == "job_drift":

                def drift_job(put_count: int) -> None:
                    if put_count == 5:
                        catalog.request_job_cancel(job.job_id)

                cas.after_put = drift_job

            with pytest.raises(error):
                _publish_vector_job(
                    fixture,
                    job_id=job.job_id,
                    owner_id=lease.owner_id,
                    fencing_token=lease.fencing_token,
                    catalog=catalog,
                    cas=cas,
                )

            expected_put_count = 1 if case == "receipt_substitution" else 5
            assert len(cas.put_chunk_receipts) == expected_put_count
            assert catalog.resolve_ref(repository_id) == preserved_ref
            assert cas.read_bytes(preserved_digest) == (
                b"retained old vector snapshot before adversarial drift"
            )
            assert catalog.get_job(job.job_id).status is IndexJobStatus.RUNNING
            publication_count = catalog._connection.execute(
                "SELECT COUNT(*) FROM index_job_publications WHERE job_id = ?",
                (job.job_id,),
            ).fetchone()[0]
            assert publication_count == 0
            if case in {"source_drift", "job_drift", "receipt_substitution"}:
                assert cas.retained_receipt_sets == []
            else:
                assert len(cas.retained_receipt_sets) == 1
    finally:
        fixture.close()


def test_real_compiler_cache_import_retry_update_and_latest_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "CodeNib Test"],
        check=True,
    )
    source_file = repository / "sample.py"
    source_file.write_text(
        'GENERATION_MARKER = "GENERATION_A_ONLY"\n',
        encoding="utf-8",
    )
    commit_a = _git_commit(repository, "generation-a")

    registry = IndexBuilderRegistry()
    registry.register(
        "bm25",
        BM25IndexBuilder(languages=["python"], max_k=17),
    )
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(index_types=["bm25"], languages=["python"]),
    )
    cache = repository / ".codenib_cache"
    manifest_a = compiler.compile_repo(
        str(repository),
        index_types=["bm25"],
        cache_dir=str(cache),
    )
    assert manifest_a.commit == commit_a

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    provider = _TestWorkspaceProvider()
    state: dict[str, object] = {"phase": "before", "events": []}
    real_lock = cache_import_module.compiler_cache_lock

    @contextmanager
    def tracked_lock(cache_path: Path, *, create: bool = True):
        assert create is False
        with real_lock(cache_path, create=create):
            state["phase"] = "locked"
            state["events"].append(  # type: ignore[union-attr]
                ("cache.lock.acquired", state["phase"])
            )
            try:
                yield
            finally:
                state["phase"] = "released"
                state["events"].append(  # type: ignore[union-attr]
                    ("cache.lock.released", state["phase"])
                )

    monkeypatch.setattr(cache_import_module, "compiler_cache_lock", tracked_lock)

    owners: list[PublishedWorkspaceReceiptOwner] = []
    source_a = capture_repository_source(repository, exclude_roots=(cache,))
    source_b: RepositorySourceBinding | None = None
    materialized_owner = PublishedWorkspaceReceiptOwner()
    binding = None
    context: ServerContext | None = None

    def import_generation(
        name: str,
        source: RepositorySourceBinding,
        *,
        expected_generation: int,
        catalog: SQLiteCatalog,
        cas: LocalCAS,
    ) -> CompilerCacheImportResult:
        bm25_owner = PublishedWorkspaceReceiptOwner()
        context_owner = PublishedWorkspaceReceiptOwner()
        owners.extend((bm25_owner, context_owner))
        return import_compiler_cache_bm25(
            cache,
            repository_source=source,
            bm25_output_owner=bm25_owner,
            context_output_owner=context_owner,
            bm25_destination=workspace / f"{name}-bm25",
            context_destination=workspace / f"{name}-context",
            workspace_provider=provider,
            repository_key=_REPOSITORY_KEY,
            catalog=catalog,
            object_store=cas,
            expected_generation=expected_generation,
            environ={},
        )

    try:
        with (
            _LockAwareCAS(tmp_path / "cas", state) as cas,
            _LockAwareCatalog(tmp_path / "catalog.sqlite", state) as catalog,
        ):
            first = import_generation(
                "a-first",
                source_a,
                expected_generation=0,
                catalog=catalog,
                cas=cas,
            )
            assert first.import_result.generation == 1
            assert first.import_result.changed is True
            assert state["events"][:4] == [  # type: ignore[index]
                ("catalog.retained_import_contract", "before"),
                ("cache.lock.acquired", "locked"),
                ("cache.lock.released", "released"),
                ("catalog.retained_import_contract", "released"),
            ]
            assert all(
                phase == "released"
                for event, phase in state["events"]  # type: ignore[union-attr]
                if event.startswith(("cas.", "catalog.data."))
            )

            retry = import_generation(
                "a-retry",
                source_a,
                expected_generation=0,
                catalog=catalog,
                cas=cas,
            )
            assert retry.import_plan.plan_digest == first.import_plan.plan_digest
            assert retry.import_result.snapshot_id == first.import_result.snapshot_id
            assert retry.import_result.generation == 1
            assert retry.import_result.changed is False

            source_a.close()
            source_file.write_text(
                'GENERATION_MARKER = "GENERATION_B_ONLY"\n',
                encoding="utf-8",
            )
            commit_b = _git_commit(repository, "generation-b")
            manifest_b = compiler.update_repo(
                str(repository),
                index_types=["bm25"],
                cache_dir=str(cache),
            )
            assert commit_b != commit_a
            assert manifest_b.commit == commit_b
            source_b = capture_repository_source(repository, exclude_roots=(cache,))

            updated = import_generation(
                "b-updated",
                source_b,
                expected_generation=1,
                catalog=catalog,
                cas=cas,
            )
            assert updated.import_result.generation == 2
            assert updated.import_result.changed is True
            assert updated.import_result.snapshot_id != first.import_result.snapshot_id

            materialized = materialize_retained_repo_manifest_ref(
                _REPOSITORY_KEY,
                workspace / "latest-materialized",
                catalog=catalog,
                object_store=cas,
                workspace_provider=provider,
                output_receipt_owner=materialized_owner,
                expected_generation=2,
                environ={},
            )
            assert materialized.export_receipt.ref_generation == 2
            assert materialized.artifact.commit == commit_b

            binding = query_context_artifact(
                materialized.artifact.output_dir,
                expected_repository=_REPOSITORY_KEY,
                expected_commit=commit_b,
            )
            context = ServerContext.load(
                binding.manifest,
                views=("bm25",),
                artifact_binding=binding,
            )
            assert context.loaded_views == frozenset({"bm25"})
            assert context.errors == {}
            assert context.bm25 is not None
            assert any(
                "generation b only" in document.page_content
                for document in context.bm25.documents
            )
            assert all(
                "generation a only" not in document.page_content
                for document in context.bm25.documents
            )
            assert context.bm25.search("GENERATION_B_ONLY", top_k=5)
    finally:
        if context is not None:
            context.close()
        if binding is not None:
            binding.close()
        materialized_owner.close()
        for owner in reversed(owners):
            owner.close()
        if source_b is not None:
            source_b.close()
        source_a.close()


def test_real_multiview_compiler_cache_imports_one_context_and_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _multiview_compiler_fixture(
        tmp_path,
        monkeypatch,
        marker="GENERATION_A_ONLY",
    )
    repository = compiled.repository
    source_file = compiled.source_file
    cache = compiled.cache
    compiler = compiled.compiler
    commit_a = compiled.commit
    source_a = capture_repository_source(repository, exclude_roots=(cache,))
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    provider = _TestWorkspaceProvider()
    retained_owners: list[PublishedWorkspaceReceiptOwner] = []
    materialized_owner = PublishedWorkspaceReceiptOwner()
    source_b: RepositorySourceBinding | None = None
    state: dict[str, object] = {"phase": "before", "events": []}
    real_lock = cache_import_module.compiler_cache_lock

    @contextmanager
    def tracked_lock(cache_path: Path, *, create: bool = True):
        assert create is False
        with real_lock(cache_path, create=create):
            state["phase"] = "locked"
            state["events"].append(  # type: ignore[union-attr]
                ("cache.lock.acquired", state["phase"])
            )
            try:
                yield
            finally:
                state["phase"] = "released"
                state["events"].append(  # type: ignore[union-attr]
                    ("cache.lock.released", state["phase"])
                )

    monkeypatch.setattr(cache_import_module, "compiler_cache_lock", tracked_lock)
    events: list[str] = []
    real_plan_view = cache_import_module._plan_cache_view
    real_import_plan = cache_import_module.plan_repo_manifest_import_bytes
    real_publish_view = cache_import_module._publish_cache_view
    real_context_plan = cache_import_module.plan_context_artifact_strict
    real_context_publish = cache_import_module.publish_planned_context_artifact_strict

    def tracked_plan_view(view, *args, **kwargs):
        events.append(f"plan:{view}")
        return real_plan_view(view, *args, **kwargs)

    def tracked_import_plan(*args, **kwargs):
        events.append("plan:retained")
        return real_import_plan(*args, **kwargs)

    def tracked_publish_view(view, *args, **kwargs):
        expected = ["plan:bm25", "plan:vector", "plan:retained"]
        if view == "vector":
            expected.append("publish:bm25")
        assert events == expected
        events.append(f"publish:{view}")
        return real_publish_view(view, *args, **kwargs)

    def tracked_context_plan(*args, **kwargs):
        assert events == [
            "plan:bm25",
            "plan:vector",
            "plan:retained",
            "publish:bm25",
            "publish:vector",
        ]
        events.append("plan:context")
        return real_context_plan(*args, **kwargs)

    def tracked_context_publish(*args, **kwargs):
        assert events[-1] == "plan:context"
        events.append("publish:context")
        return real_context_publish(*args, **kwargs)

    monkeypatch.setattr(cache_import_module, "_plan_cache_view", tracked_plan_view)
    monkeypatch.setattr(
        cache_import_module,
        "plan_repo_manifest_import_bytes",
        tracked_import_plan,
    )
    monkeypatch.setattr(
        cache_import_module,
        "_publish_cache_view",
        tracked_publish_view,
    )
    monkeypatch.setattr(
        cache_import_module,
        "plan_context_artifact_strict",
        tracked_context_plan,
    )
    monkeypatch.setattr(
        cache_import_module,
        "publish_planned_context_artifact_strict",
        tracked_context_publish,
    )

    expected_events = [
        "plan:bm25",
        "plan:vector",
        "plan:retained",
        "publish:bm25",
        "publish:vector",
        "plan:context",
        "publish:context",
    ]

    def import_generation(
        name: str,
        source: RepositorySourceBinding,
        *,
        expected_generation: int,
        catalog: SQLiteCatalog,
        cas: LocalCAS,
    ) -> CompilerCacheMultiViewImportResult:
        view_owners = {
            "bm25": PublishedWorkspaceReceiptOwner(),
            "vector": PublishedWorkspaceReceiptOwner(),
        }
        context_owner = PublishedWorkspaceReceiptOwner()
        retained_owners.extend((*view_owners.values(), context_owner))
        events.clear()
        result = import_compiler_cache(
            cache,
            views=("bm25", "vector"),
            repository_source=source,
            view_output_owners=view_owners,
            context_output_owner=context_owner,
            view_destinations={
                "bm25": workspace / f"{name}-bm25",
                "vector": workspace / f"{name}-vector",
            },
            context_destination=workspace / f"{name}-context",
            workspace_provider=provider,
            repository_key=_REPOSITORY_KEY,
            catalog=catalog,
            object_store=cas,
            expected_generation=expected_generation,
            environ={},
        )
        assert events == expected_events
        return result

    binding = None
    context: ServerContext | None = None
    try:
        with (
            _LockAwareCAS(tmp_path / "cas", state) as cas,
            _LockAwareCatalog(tmp_path / "catalog.sqlite", state) as catalog,
        ):
            first = import_generation(
                "a-first",
                source_a,
                expected_generation=0,
                catalog=catalog,
                cas=cas,
            )
            assert first.views == ("bm25", "vector")
            assert tuple(first.recapture_map) == ("bm25", "vector")
            assert first.import_plan.selection.selected_views == (
                "bm25",
                "vector",
            )
            assert first.context_artifact.views == ("bm25", "vector")
            assert first.import_result.views == ("bm25", "vector")
            assert first.import_result.generation == 1
            assert first.import_result.changed is True
            assert tuple(
                view for view, _generation in first.import_result.view_generation_items
            ) == ("bm25", REPO_MANIFEST_PROJECTION_VIEW, "vector")
            assert provider.support_count == 4
            assert provider.run_count == 3
            first_view_generations = {
                view: generation
                for view, generation in first.import_result.view_generation_items
                if view in first.views
            }

            retry = import_generation(
                "a-retry",
                source_a,
                expected_generation=0,
                catalog=catalog,
                cas=cas,
            )
            assert retry.import_plan.plan_digest == first.import_plan.plan_digest
            assert retry.import_result.snapshot_id == first.import_result.snapshot_id
            assert retry.import_result.generation == 1
            assert retry.import_result.changed is False
            assert {
                view: generation
                for view, generation in retry.import_result.view_generation_items
                if view in retry.views
            } == first_view_generations

            source_a.close()
            source_file.write_text(
                'MULTIVIEW_CACHE_MARKER = "GENERATION_B_ONLY"\n',
                encoding="utf-8",
            )
            commit_b = _git_commit(repository, "multiview-generation-b")
            manifest_b = compiler.update_repo(
                str(repository),
                index_types=["bm25", "vector"],
                cache_dir=str(cache),
            )
            assert commit_b != commit_a
            assert manifest_b.commit == commit_b
            assert manifest_b.indexes["vector"].config["builder_schema"] == 8
            source_b = capture_repository_source(
                repository,
                exclude_roots=(cache,),
            )

            updated = import_generation(
                "b-updated",
                source_b,
                expected_generation=1,
                catalog=catalog,
                cas=cas,
            )
            assert updated.import_result.generation == 2
            assert updated.import_result.changed is True
            assert updated.import_result.snapshot_id != first.import_result.snapshot_id
            updated_view_generations = {
                view: generation
                for view, generation in updated.import_result.view_generation_items
                if view in updated.views
            }
            assert tuple(updated_view_generations) == ("bm25", "vector")
            assert all(
                updated_view_generations[view] != first_view_generations[view]
                for view in ("bm25", "vector")
            )
            assert [
                (publication["generation"], publication["changed"])
                for publication in catalog.publications
            ] == [(1, True), (1, False), (2, True)]
            assert provider.support_count == 12
            assert provider.run_count == 9
            lock_events = [
                event
                for event, _phase in state["events"]  # type: ignore[union-attr]
                if event.startswith("cache.lock.")
            ]
            assert (
                lock_events
                == [
                    "cache.lock.acquired",
                    "cache.lock.released",
                ]
                * 3
            )
            assert all(
                phase == "released"
                for event, phase in state["events"]  # type: ignore[union-attr]
                if event.startswith(("cas.", "catalog.data."))
            )

            materialized = materialize_retained_repo_manifest_ref(
                _REPOSITORY_KEY,
                workspace / "latest-materialized",
                catalog=catalog,
                object_store=cas,
                workspace_provider=provider,
                output_receipt_owner=materialized_owner,
                expected_generation=2,
                environ={},
            )
            assert materialized.export_receipt.ref_generation == 2
            assert materialized.artifact.commit == commit_b
            assert materialized.artifact.views == ("bm25", "vector")
            binding = query_context_artifact(
                materialized.artifact.output_dir,
                expected_repository=_REPOSITORY_KEY,
                expected_commit=commit_b,
            )
            assert tuple(sorted(binding.manifest.indexes)) == ("bm25", "vector")
            context = ServerContext.load(
                binding.manifest,
                views=("bm25",),
                artifact_binding=binding,
            )
            assert context.errors == {}
            assert context.bm25 is not None
            assert any(
                "generation b only" in document.page_content
                for document in context.bm25.documents
            )
            assert all(
                "generation a only" not in document.page_content
                for document in context.bm25.documents
            )
            assert context.bm25.search("GENERATION_B_ONLY", top_k=5)
    finally:
        if context is not None:
            context.close()
        if binding is not None:
            binding.close()
        materialized_owner.close()
        for owner in reversed(retained_owners):
            owner.close()
        if source_b is not None:
            source_b.close()
        source_a.close()


def test_real_vector_only_cache_import_materializes_exact_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _multiview_compiler_fixture(
        tmp_path,
        monkeypatch,
        marker="VECTOR_ONLY",
    )
    source = capture_repository_source(
        compiled.repository,
        exclude_roots=(compiled.cache,),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    provider = _TestWorkspaceProvider()
    vector_owner = PublishedWorkspaceReceiptOwner()
    context_owner = PublishedWorkspaceReceiptOwner()
    materialized_owner = PublishedWorkspaceReceiptOwner()
    binding = None
    vector_destination = workspace / "vector-only-vector"
    context_destination = workspace / "vector-only-context"
    materialized_destination = workspace / "vector-only-materialized"
    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            result = import_compiler_cache(
                compiled.cache,
                views=("vector",),
                repository_source=source,
                view_output_owners={"vector": vector_owner},
                context_output_owner=context_owner,
                view_destinations={"vector": vector_destination},
                context_destination=context_destination,
                workspace_provider=provider,
                repository_key=_REPOSITORY_KEY,
                catalog=catalog,
                object_store=cas,
                expected_generation=0,
                environ={},
            )

            assert result.views == ("vector",)
            assert tuple(result.recapture_map) == ("vector",)
            assert tuple(result.import_plan.manifest.indexes) == ("vector",)
            assert result.import_plan.manifest.capabilities == {
                "sparse_search": False,
                "dense_search": True,
                "hybrid_search": False,
                "symbol_navigation": False,
            }
            assert result.context_artifact.views == ("vector",)
            assert result.context_artifact.output_dir == context_destination
            assert result.import_result.views == ("vector",)
            assert result.import_result.generation == 1
            assert result.import_result.changed is True
            assert tuple(
                view for view, _generation in result.import_result.view_generation_items
            ) == (REPO_MANIFEST_PROJECTION_VIEW, "vector")
            resolved = catalog.resolve_ref(result.import_result.repository_id)
            assert resolved["ref_name"] == "main"
            assert resolved["snapshot_id"] == result.import_result.snapshot_id
            assert resolved["generation"] == 1
            assert vector_destination.is_dir()
            assert context_destination.is_dir()
            assert not (workspace / "vector-only-bm25").exists()

            materialized = materialize_retained_repo_manifest_ref(
                _REPOSITORY_KEY,
                materialized_destination,
                catalog=catalog,
                object_store=cas,
                workspace_provider=provider,
                output_receipt_owner=materialized_owner,
                expected_generation=1,
                environ={},
            )
            assert materialized.export_receipt.snapshot_id == (
                result.import_result.snapshot_id
            )
            assert materialized.artifact.views == ("vector",)
            assert (materialized_destination / "views/vector").is_dir()
            assert not (materialized_destination / "views/bm25").exists()
            binding = query_context_artifact(
                materialized_destination,
                expected_repository=_REPOSITORY_KEY,
                expected_commit=compiled.commit,
            )
            assert tuple(binding.manifest.indexes) == ("vector",)
            assert binding.manifest.capabilities == {
                "sparse_search": False,
                "dense_search": True,
                "hybrid_search": False,
                "symbol_navigation": False,
            }
            assert provider.run_count == 3
    finally:
        if binding is not None:
            binding.close()
        materialized_owner.close()
        context_owner.close()
        vector_owner.close()
        source.close()


def test_multiview_postcommit_interruption_retries_with_fresh_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _multiview_compiler_fixture(
        tmp_path,
        monkeypatch,
        marker="POSTCOMMIT",
    )
    source = capture_repository_source(
        compiled.repository,
        exclude_roots=(compiled.cache,),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    provider = _TestWorkspaceProvider()
    first_owners = {
        "bm25": PublishedWorkspaceReceiptOwner(),
        "vector": PublishedWorkspaceReceiptOwner(),
    }
    first_context_owner = PublishedWorkspaceReceiptOwner()
    retry_owners = {
        "bm25": PublishedWorkspaceReceiptOwner(),
        "vector": PublishedWorkspaceReceiptOwner(),
    }
    retry_context_owner = PublishedWorkspaceReceiptOwner()
    first_destinations = {
        "bm25": workspace / "first-bm25",
        "vector": workspace / "first-vector",
    }
    first_context_destination = workspace / "first-context"
    retry_destinations = {
        "bm25": workspace / "retry-bm25",
        "vector": workspace / "retry-vector",
    }
    retry_context_destination = workspace / "retry-context"
    try:
        with (
            LocalCAS(tmp_path / "cas") as cas,
            _PostCommitInterruptCatalog(tmp_path / "catalog.sqlite") as catalog,
        ):
            with pytest.raises(RuntimeError, match="postcommit interruption"):
                import_compiler_cache(
                    compiled.cache,
                    views=("bm25", "vector"),
                    repository_source=source,
                    view_output_owners=first_owners,
                    context_output_owner=first_context_owner,
                    view_destinations=first_destinations,
                    context_destination=first_context_destination,
                    workspace_provider=provider,
                    repository_key=_REPOSITORY_KEY,
                    catalog=catalog,
                    object_store=cas,
                    expected_generation=0,
                    environ={},
                )

            assert all(path.is_dir() for path in first_destinations.values())
            assert first_context_destination.is_dir()
            assert all(owner.active for owner in first_owners.values())
            assert first_context_owner.active
            assert all(not path.exists() for path in retry_destinations.values())
            assert not retry_context_destination.exists()

            retry = import_compiler_cache(
                compiled.cache,
                views=("bm25", "vector"),
                repository_source=source,
                view_output_owners=retry_owners,
                context_output_owner=retry_context_owner,
                view_destinations=retry_destinations,
                context_destination=retry_context_destination,
                workspace_provider=provider,
                repository_key=_REPOSITORY_KEY,
                catalog=catalog,
                object_store=cas,
                expected_generation=0,
                environ={},
            )

            assert catalog.interrupted_publication is not None
            assert catalog.interrupted_publication["changed"] is True
            assert retry.import_result.generation == 1
            assert retry.import_result.changed is False
            assert (
                retry.import_result.snapshot_id
                == catalog.interrupted_publication["snapshot_id"]
            )
            assert (
                retry.import_result.generation
                == catalog.interrupted_publication["generation"]
            )
            assert retry.import_result.views == ("bm25", "vector")
            assert tuple(
                view for view, _generation in retry.import_result.view_generation_items
            ) == ("bm25", REPO_MANIFEST_PROJECTION_VIEW, "vector")
            assert all(path.is_dir() for path in retry_destinations.values())
            assert retry_context_destination.is_dir()
            assert all(owner.active for owner in retry_owners.values())
            assert retry_context_owner.active
            assert all(owner.active for owner in first_owners.values())
            assert first_context_owner.active
            assert provider.support_count == 8
            assert provider.run_count == 6
    finally:
        retry_context_owner.close()
        for owner in reversed(tuple(retry_owners.values())):
            owner.close()
        first_context_owner.close()
        for owner in reversed(tuple(first_owners.values())):
            owner.close()
        source.close()


def test_real_cli_import_cache_materialize_snapshot_and_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace.chmod(0o700)
    provider = LocalWorkspaceProvider(workspace)
    try:
        provider.require_support()
    except UnsupportedWorkspaceCreation as error:
        pytest.skip(f"native local workspace provider is unavailable: {error}")

    compiled = _multiview_compiler_fixture(
        tmp_path,
        monkeypatch,
        marker="PRODUCTION_SHAPED_CACHE_IMPORT",
    )
    repository = compiled.repository
    cache = compiled.cache
    commit = compiled.commit

    catalog_path = tmp_path / "catalog.sqlite"
    with SQLiteCatalog(catalog_path):
        pass
    cas_root = tmp_path / "cas"
    with LocalCAS.provision(cas_root):
        pass

    nonce = "0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(cli_module, "_new_compiler_cache_import_nonce", lambda: nonce)
    parser = cli_module.build_parser()
    import_args = parser.parse_args(
        [
            "artifact",
            "import-cache",
            os.fspath(repository),
            "--cache-dir",
            os.fspath(cache),
            "--catalog",
            os.fspath(catalog_path),
            "--cas-root",
            os.fspath(cas_root),
            "--workspace-root",
            os.fspath(workspace),
            "--repository",
            _REPOSITORY_KEY,
            "--view",
            "vector,bm25",
        ]
    )
    assert import_args.handler is cli_module._run_artifact_import_cache
    assert import_args.handler(import_args) == 0

    import_output = capsys.readouterr().out
    snapshot_id = next(
        line.partition(":")[2].strip()
        for line in import_output.splitlines()
        if line.startswith("Snapshot:")
    )
    assert snapshot_id
    assert "Generation:         1" in import_output
    assert "Views:              bm25,vector" in import_output

    prefix = f".codenib-cache-import-{nonce}"
    bm25_generation = workspace / f"{prefix}-bm25"
    vector_generation = workspace / f"{prefix}-vector"
    context_generation = workspace / f"{prefix}-context"
    suggested_output = workspace / f"{prefix}-materialized"
    materialized_output = workspace / "materialized"
    assert bm25_generation.is_dir()
    assert vector_generation.is_dir()
    assert (vector_generation / "l2/documents_test__model.json").is_file()
    assert not (vector_generation / "l2/documents_test__model.pkl").exists()
    assert context_generation.is_dir()
    assert not suggested_output.exists()
    assert not materialized_output.exists()

    materialize_args = parser.parse_args(
        [
            "artifact",
            "materialize",
            "--catalog",
            os.fspath(catalog_path),
            "--cas-root",
            os.fspath(cas_root),
            "--workspace-root",
            os.fspath(workspace),
            "--repository",
            _REPOSITORY_KEY,
            "--snapshot",
            snapshot_id,
            "--output",
            os.fspath(materialized_output),
        ]
    )
    assert materialize_args.handler is cli_module._run_artifact_materialize
    assert materialize_args.handler(materialize_args) == 0

    materialize_output = capsys.readouterr().out
    assert f"Snapshot:         {snapshot_id}" in materialize_output
    assert "Views:            bm25, vector" in materialize_output
    assert materialized_output.is_dir()
    assert (materialized_output / "views/bm25").is_dir()
    assert (materialized_output / "views/vector").is_dir()
    assert bm25_generation.is_dir()
    assert vector_generation.is_dir()
    assert context_generation.is_dir()

    binding = query_context_artifact(
        materialized_output,
        expected_repository=_REPOSITORY_KEY,
        expected_commit=commit,
    )
    context: ServerContext | None = None
    try:
        assert tuple(sorted(binding.manifest.indexes)) == ("bm25", "vector")
        context = ServerContext.load(
            binding.manifest,
            views=("bm25",),
            artifact_binding=binding,
        )
        assert context.errors == {}
        assert context.loaded_views == frozenset({"bm25"})
        assert context.bm25 is not None
        hits = context.bm25.search(
            "PRODUCTION_SHAPED_CACHE_IMPORT",
            top_k=5,
            return_code_content=True,
        )
        assert hits
        assert hits[0].file == "sample.py"
        assert any(
            "production shaped cache import" in document.page_content
            for document in context.bm25.documents
        )
    finally:
        if context is not None:
            context.close()
        binding.close()
