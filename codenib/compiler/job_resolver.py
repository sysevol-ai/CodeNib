# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Resource-scoped resolver for prepare-only compiler-cache jobs."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from ..storage.job_worker import (
    IndexJobExecutionContext,
    IndexJobExecutionResult,
    IndexJobExecutor,
)
from ..storage.models import (
    IndexJobRecord,
    IndexJobRequest,
    IndexJobRequestedMode,
    IndexJobStatus,
    IndexJobViewRecord,
    StorageIntegrityError,
    StorageValidationError,
)
from ..storage.protocols import RetainedImportObjectStore
from .cache_import import CompilerCacheJobExecutor
from .source_job import BM25SourceJobExecutor

_SUPPORTED_CACHE_VIEWS = frozenset({"bm25", "vector"})
_MISSING_EXECUTION_RESULT = object()


@dataclass(frozen=True, slots=True)
class CompilerCacheJobResourceScope:
    """Side-effect-free declaration for one attempt resource scope.

    The context manager must not acquire a source binding, workspace, receipt
    owner, or other attempt-local authority until ``__enter__`` is called.
    Its yielded executor must use the object store and view declared here.
    """

    object_store: RetainedImportObjectStore
    view_type: str
    resources: AbstractContextManager[CompilerCacheJobExecutor]

    def __post_init__(self) -> None:
        if type(self) is not CompilerCacheJobResourceScope:
            raise TypeError("compiler cache resource scope must use the exact type")
        if not isinstance(self.object_store, RetainedImportObjectStore):
            raise TypeError(
                "compiler cache resource scope requires a retained import store"
            )
        if (
            type(self.view_type) is not str
            or self.view_type not in _SUPPORTED_CACHE_VIEWS
        ):
            raise TypeError(
                "compiler cache resource scope requires a supported exact view"
            )
        if not isinstance(self.resources, AbstractContextManager):
            raise TypeError("compiler cache resource scope requires a context manager")


@dataclass(frozen=True, slots=True)
class BM25SourceJobResourceScope:
    """Side-effect-free declaration for one retained-source BM25 attempt."""

    object_store: RetainedImportObjectStore
    resources: AbstractContextManager[BM25SourceJobExecutor]

    def __post_init__(self) -> None:
        if type(self) is not BM25SourceJobResourceScope:
            raise TypeError("BM25 source resource scope must use the exact type")
        if not isinstance(self.object_store, RetainedImportObjectStore):
            raise TypeError(
                "BM25 source resource scope requires a retained import store"
            )
        if not isinstance(self.resources, AbstractContextManager):
            raise TypeError("BM25 source resource scope requires a context manager")


@runtime_checkable
class CompilerCacheJobResourceFactory(Protocol):
    """Declare fresh attempt-scoped authorities for one cache executor.

    ``create_scope`` must only assemble a side-effect-free scope descriptor;
    it must not open a source binding, workspace, receipt owner, or other
    attempt-local authority. Implementations own the descriptor's context
    manager and must settle every resource when its scope exits. The supplied
    object store is borrowed from the resolver and must be declared on the
    scope and attached unchanged to the yielded executor.
    """

    def create_scope(
        self,
        context: IndexJobExecutionContext,
        *,
        object_store: RetainedImportObjectStore,
    ) -> CompilerCacheJobResourceScope: ...


@runtime_checkable
class BM25SourceJobResourceFactory(Protocol):
    """Declare fresh attempt-scoped authorities for one BM25 source executor.

    ``create_scope`` must only assemble a side-effect-free descriptor. The
    returned context manager owns source capture, receipt owners, workspace
    destinations, and their ordered cleanup. The supplied object store must be
    declared unchanged on the scope and attached unchanged to its executor.
    """

    def create_scope(
        self,
        context: IndexJobExecutionContext,
        *,
        object_store: RetainedImportObjectStore,
    ) -> BM25SourceJobResourceScope: ...


def _add_cleanup_exception_note(
    primary: BaseException,
    secondary: BaseException,
    *,
    label: str,
) -> None:
    """Best-effort note cleanup failure without replacing the primary error."""

    try:
        add_note = getattr(primary, "add_note", None)
        if callable(add_note):
            add_note(f"{label} cleanup also failed: {type(secondary).__name__}")
    except BaseException:  # noqa: B036 - notes must not replace execution failure
        pass


def _inherit_cleanup_owners(
    primary: BaseException,
    secondary: BaseException,
) -> None:
    """Keep exact cleanup recovery handles reachable from the primary error."""

    try:
        try:
            inherited = BaseException.__getattribute__(
                secondary,
                "publication_cleanup_owners",
            )
        except AttributeError:
            return
        if type(inherited) is not tuple:
            return
        try:
            existing = BaseException.__getattribute__(
                primary,
                "publication_cleanup_owners",
            )
        except AttributeError:
            existing = ()
        if type(existing) is not tuple:
            existing = ()
        retained = existing
        for owner in inherited:
            if not any(candidate is owner for candidate in retained):
                retained = (*retained, owner)
        BaseException.__setattr__(
            primary,
            "publication_cleanup_owners",
            retained,
        )
    except BaseException:  # noqa: B036 - recovery metadata is best effort
        pass


def _execute_in_resource_scope(
    scope: CompilerCacheJobResourceScope,
    context: IndexJobExecutionContext,
) -> IndexJobExecutionResult:
    """Execute without permitting hostile cleanup to suppress the primary error."""

    result: object = _MISSING_EXECUTION_RESULT
    primary: BaseException | None = None
    try:
        with scope.resources as executor:
            try:
                if type(executor) is not CompilerCacheJobExecutor:
                    raise TypeError(
                        "compiler cache resource factory returned an invalid executor"
                    )
                if executor.object_store is not scope.object_store:
                    raise StorageIntegrityError(
                        "compiler cache executor differs from its declared object store"
                    )
                if executor.view_type != scope.view_type:
                    raise StorageIntegrityError(
                        "compiler cache executor differs from its declared job view"
                    )
                result = executor.execute(context)
                if type(result) is not IndexJobExecutionResult:
                    raise StorageIntegrityError(
                        "compiler cache executor returned an invalid result"
                    )
            except BaseException as exc:  # noqa: B036 - rethrown after cleanup
                primary = exc
                raise
    except BaseException as cleanup_exc:  # noqa: B036 - preserve primary failure
        if primary is None:
            raise
        if cleanup_exc is not primary:
            _inherit_cleanup_owners(primary, cleanup_exc)
            _add_cleanup_exception_note(
                primary,
                cleanup_exc,
                label="compiler cache resource",
            )
    if primary is not None:
        raise primary
    if type(result) is not IndexJobExecutionResult:
        raise StorageIntegrityError(
            "compiler cache resource scope returned no execution result"
        )
    return result


def _execute_in_bm25_source_resource_scope(
    scope: BM25SourceJobResourceScope,
    context: IndexJobExecutionContext,
) -> IndexJobExecutionResult:
    """Execute a source build without letting cleanup suppress its failure."""

    result: object = _MISSING_EXECUTION_RESULT
    primary: BaseException | None = None
    try:
        with scope.resources as executor:
            try:
                if type(executor) is not BM25SourceJobExecutor:
                    raise TypeError(
                        "BM25 source resource factory returned an invalid executor"
                    )
                if executor.object_store is not scope.object_store:
                    raise StorageIntegrityError(
                        "BM25 source executor differs from its declared object store"
                    )
                result = executor.execute(context)
                if type(result) is not IndexJobExecutionResult:
                    raise StorageIntegrityError(
                        "BM25 source executor returned an invalid result"
                    )
            except BaseException as exc:  # noqa: B036 - rethrown after cleanup
                primary = exc
                raise
    except BaseException as cleanup_exc:  # noqa: B036 - preserve primary failure
        if primary is None:
            raise
        if cleanup_exc is not primary:
            _inherit_cleanup_owners(primary, cleanup_exc)
            _add_cleanup_exception_note(
                primary,
                cleanup_exc,
                label="BM25 source resource",
            )
    if primary is not None:
        raise primary
    if type(result) is not IndexJobExecutionResult:
        raise StorageIntegrityError("BM25 source resource scope returned no result")
    return result


def _detach_resolved_job(value: object) -> IndexJobRecord:
    if type(value) is not IndexJobRecord:
        raise StorageValidationError(
            "compiler cache resolver requires an exact job record"
        )
    try:
        detached = replace(value)
    except StorageValidationError:
        raise
    except Exception as exc:
        raise StorageValidationError(
            "compiler cache resolver job record is structurally damaged"
        ) from exc
    if detached != value or type(detached.status) is not IndexJobStatus:
        raise StorageValidationError(
            "compiler cache resolver job record is not canonical"
        )
    return detached


def _detach_resolved_views(value: object) -> tuple[IndexJobViewRecord, ...]:
    if type(value) is not tuple or any(
        type(item) is not IndexJobViewRecord for item in value
    ):
        raise StorageValidationError(
            "compiler cache resolver requires exact job view records"
        )
    try:
        detached = tuple(
            IndexJobViewRecord(
                job_id=item.job_id,
                view_type=item.view_type,
                profile_id=item.profile_id,
                requested_mode=item.requested_mode,
                required=item.required,
            )
            for item in value
        )
    except StorageValidationError:
        raise
    except Exception as exc:
        raise StorageValidationError(
            "compiler cache resolver job views are structurally damaged"
        ) from exc
    if detached != value:
        raise StorageValidationError(
            "compiler cache resolver job views are not canonical"
        )
    return detached


def _canonical_resolved_views(job: IndexJobRecord) -> tuple[IndexJobViewRecord, ...]:
    request = IndexJobRequest(
        repository_id=job.repository_id,
        source_revision_id=job.source_revision_id,
        ref_name=job.ref_name,
        idempotency_key=job.idempotency_key,
        expected_ref_generation=job.expected_ref_generation,
        max_attempts=job.max_attempts,
        request_json=job.request_json,
    )
    if request.job_id != job.job_id or request.request_digest != job.request_digest:
        raise StorageIntegrityError(
            "compiler cache resolver job request identity is inconsistent"
        )
    return request.view_requests


@dataclass(frozen=True, slots=True)
class _ScopedCompilerCacheJobExecutor:
    job: IndexJobRecord
    views: tuple[IndexJobViewRecord, ...]
    resource_factory: CompilerCacheJobResourceFactory
    object_store: RetainedImportObjectStore

    def execute(
        self,
        context: IndexJobExecutionContext,
    ) -> IndexJobExecutionResult:
        if type(context) is not IndexJobExecutionContext:
            raise TypeError("scoped compiler cache executor requires an exact context")
        if context.job != self.job or context.views != self.views:
            raise StorageIntegrityError(
                "scoped compiler cache executor received a different job attempt"
            )
        scope = self.resource_factory.create_scope(
            context,
            object_store=self.object_store,
        )
        if type(scope) is not CompilerCacheJobResourceScope:
            raise TypeError(
                "compiler cache resource factory must return an exact scope"
            )
        if scope.object_store is not self.object_store:
            raise StorageIntegrityError(
                "compiler cache resource scope must use the resolver object store"
            )
        if scope.view_type != self.views[0].view_type:
            raise StorageIntegrityError(
                "compiler cache resource scope resolved a different job view"
            )
        return _execute_in_resource_scope(scope, context)


@dataclass(frozen=True, slots=True)
class _ScopedBM25SourceJobExecutor:
    job: IndexJobRecord
    views: tuple[IndexJobViewRecord, ...]
    resource_factory: BM25SourceJobResourceFactory
    object_store: RetainedImportObjectStore

    def execute(
        self,
        context: IndexJobExecutionContext,
    ) -> IndexJobExecutionResult:
        if type(context) is not IndexJobExecutionContext:
            raise TypeError("scoped BM25 source executor requires an exact context")
        if context.job != self.job or context.views != self.views:
            raise StorageIntegrityError(
                "scoped BM25 source executor received a different job attempt"
            )
        scope = self.resource_factory.create_scope(
            context,
            object_store=self.object_store,
        )
        if type(scope) is not BM25SourceJobResourceScope:
            raise TypeError("BM25 source resource factory must return an exact scope")
        if scope.object_store is not self.object_store:
            raise StorageIntegrityError(
                "BM25 source resource scope must use the resolver object store"
            )
        return _execute_in_bm25_source_resource_scope(scope, context)


@dataclass(frozen=True, slots=True)
class CompilerCacheJobResolver:
    """Resolve one supported cache job through fresh scoped authorities.

    This resolver deliberately has no catalog capability. It binds the same
    retained-import object-store instance used by the enclosing worker and
    defers attempt-local authority creation until execution begins.
    """

    resource_factory: CompilerCacheJobResourceFactory
    object_store: RetainedImportObjectStore

    def __post_init__(self) -> None:
        if type(self) is not CompilerCacheJobResolver:
            raise TypeError("compiler cache job resolver must use the exact type")
        if not isinstance(self.resource_factory, CompilerCacheJobResourceFactory):
            raise TypeError("compiler cache job resolver requires a resource factory")
        if not isinstance(self.object_store, RetainedImportObjectStore):
            raise TypeError(
                "compiler cache job resolver requires a retained import store"
            )

    def resolve(
        self,
        job: IndexJobRecord,
        views: tuple[IndexJobViewRecord, ...],
    ) -> IndexJobExecutor:
        detached_job = _detach_resolved_job(job)
        detached_views = _detach_resolved_views(views)
        if detached_views != _canonical_resolved_views(detached_job):
            raise StorageIntegrityError(
                "compiler cache resolver views differ from the job request"
            )
        if (
            detached_job.status is not IndexJobStatus.RUNNING
            or detached_job.cancel_requested
            or len(detached_views) != 1
            or detached_views[0].job_id != detached_job.job_id
            or detached_views[0].view_type not in _SUPPORTED_CACHE_VIEWS
            or detached_views[0].requested_mode is not IndexJobRequestedMode.FULL
            or detached_views[0].required is not True
        ):
            raise StorageValidationError(
                "compiler cache resolver requires one active required FULL "
                "BM25 or vector view"
            )
        return _ScopedCompilerCacheJobExecutor(
            job=detached_job,
            views=detached_views,
            resource_factory=self.resource_factory,
            object_store=self.object_store,
        )


@dataclass(frozen=True, slots=True)
class BM25SourceJobResolver:
    """Resolve one retained-source BM25 job through fresh scoped authorities."""

    resource_factory: BM25SourceJobResourceFactory
    object_store: RetainedImportObjectStore

    def __post_init__(self) -> None:
        if type(self) is not BM25SourceJobResolver:
            raise TypeError("BM25 source job resolver must use the exact type")
        if not isinstance(self.resource_factory, BM25SourceJobResourceFactory):
            raise TypeError("BM25 source job resolver requires a resource factory")
        if not isinstance(self.object_store, RetainedImportObjectStore):
            raise TypeError("BM25 source job resolver requires a retained import store")

    def resolve(
        self,
        job: IndexJobRecord,
        views: tuple[IndexJobViewRecord, ...],
    ) -> IndexJobExecutor:
        detached_job = _detach_resolved_job(job)
        detached_views = _detach_resolved_views(views)
        if detached_views != _canonical_resolved_views(detached_job):
            raise StorageIntegrityError(
                "BM25 source resolver views differ from the job request"
            )
        if (
            detached_job.status is not IndexJobStatus.RUNNING
            or detached_job.cancel_requested
            or len(detached_views) != 1
            or detached_views[0].job_id != detached_job.job_id
            or detached_views[0].view_type != "bm25"
            or detached_views[0].requested_mode is not IndexJobRequestedMode.FULL
            or detached_views[0].required is not True
        ):
            raise StorageValidationError(
                "BM25 source resolver requires one active required FULL BM25 view"
            )
        return _ScopedBM25SourceJobExecutor(
            job=detached_job,
            views=detached_views,
            resource_factory=self.resource_factory,
            object_store=self.object_store,
        )


__all__ = [
    "BM25SourceJobResolver",
    "BM25SourceJobResourceFactory",
    "BM25SourceJobResourceScope",
    "CompilerCacheJobResolver",
    "CompilerCacheJobResourceFactory",
    "CompilerCacheJobResourceScope",
]
