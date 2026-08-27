# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Resource-scoped resolvers for prepare-only cache and source jobs."""

from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
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
    """Best-effort note one cleanup failure without replacing the primary."""

    try:
        message = f"{label} cleanup also failed: {type(secondary).__name__}"
        add_note = getattr(BaseException, "add_note", None)
        if add_note is not None:
            notes_missing = False
            try:
                notes = BaseException.__getattribute__(primary, "__notes__")
            except AttributeError:
                notes_missing = True
                notes = None
            if notes_missing or type(notes) is list:
                if type(notes) is list and any(
                    type(note) is str and note == message for note in notes
                ):
                    return
                add_note(primary, message)
                return
        try:
            notes = BaseException.__getattribute__(
                primary,
                "_codenib_cleanup_notes",
            )
        except AttributeError:
            notes = ()
        if type(notes) is not tuple:
            notes = ()
        if any(type(note) is str and note == message for note in notes):
            return
        BaseException.__setattr__(
            primary,
            "_codenib_cleanup_notes",
            (*notes, message),
        )
    except BaseException:  # noqa: B036 - diagnostics cannot replace execution
        return


def _inherit_cleanup_owners(
    primary: BaseException,
    secondary: BaseException,
) -> bool:
    """Keep exact cleanup recovery handles reachable from the primary error."""

    try:
        try:
            inherited = BaseException.__getattribute__(
                secondary,
                "publication_cleanup_owners",
            )
        except AttributeError:
            return True
        if type(inherited) is not tuple:
            return True
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
        installed = BaseException.__getattribute__(
            primary,
            "publication_cleanup_owners",
        )
        return type(installed) is tuple and all(
            any(candidate is owner for candidate in installed) for owner in inherited
        )
    except BaseException:  # noqa: B036 - caller retries interrupted metadata
        return False


@dataclass(slots=True)
class _ScopedJobExecutionOutcome:
    """Durable operation and resource-exit state for one scoped executor."""

    result: object = _MISSING_EXECUTION_RESULT
    operation_error: BaseException | None = None
    scope_error: BaseException | None = None
    boundary_errors: list[BaseException] = field(default_factory=list)
    settled_primary: BaseException | None = None
    settlement_started: bool = False
    settlement_complete: bool = False


class _ScopedJobOperationCapture:
    """Store one operation failure before it crosses the resource boundary."""

    __slots__ = ("outcome",)

    def __init__(self, outcome: _ScopedJobExecutionOutcome) -> None:
        self.outcome = outcome

    def __enter__(self) -> None:
        return None

    def __exit__(self, _exc_type, error, _traceback) -> bool:
        if error is None:
            return False
        self.outcome.operation_error = error
        return False


class _ScopedJobResourceExitCapture:
    """Store one final resource-exit error for post-scope settlement."""

    __slots__ = ("outcome",)

    def __init__(self, outcome: _ScopedJobExecutionOutcome) -> None:
        self.outcome = outcome

    def __enter__(self) -> None:
        return None

    def __exit__(self, _exc_type, error, _traceback) -> bool:
        if error is None:
            return False
        self.outcome.scope_error = error
        return True


def _raw_exception_context(error: BaseException) -> BaseException | None:
    """Read ``__context__`` without invoking exception-subclass dispatch."""

    try:
        context = vars(BaseException)["__context__"].__get__(error, type(error))
    except BaseException:  # noqa: B036 - inspection cannot replace failure
        return None
    if not issubclass(type(context), BaseException):
        return None
    return context


def _has_scoped_job_provenance(
    error: BaseException,
    provenance: Callable[..., object],
    outcome: _ScopedJobExecutionOutcome,
) -> bool:
    """Require one exact outcome-bound frame in an exception traceback."""

    try:
        traceback = vars(BaseException)["__traceback__"].__get__(
            error,
            type(error),
        )
        while traceback is not None:
            frame = traceback.tb_frame
            if (
                frame.f_code is provenance.__code__
                and frame.f_locals.get("outcome") is outcome
            ):
                return True
            traceback = traceback.tb_next
    except BaseException:  # noqa: B036 - inspection cannot replace failure
        return False
    return False


def _has_current_scoped_job_execution_provenance(
    error: BaseException,
    outcome: _ScopedJobExecutionOutcome,
) -> bool:
    """Require the current execute frame without swallowing classification."""

    traceback = BaseException.__traceback__.__get__(error, type(error))
    while traceback is not None:
        frame = traceback.tb_frame
        if (
            frame.f_code is _execute_scoped_job_resource.__code__
            and frame.f_locals.get("outcome") is outcome
        ):
            return True
        traceback = traceback.tb_next
    return False


def _is_inherited_scoped_job_ambient(
    error: BaseException | None,
    ambient_error: BaseException | None,
    ambient_traceback: object,
    outcome: _ScopedJobExecutionOutcome,
) -> bool:
    """Reject caller exception state that never escaped this execution frame."""

    if error is not ambient_error or ambient_error is None:
        return False
    current_traceback = BaseException.__traceback__.__get__(
        error,
        type(error),
    )
    if current_traceback is ambient_traceback:
        return True
    return not _has_current_scoped_job_execution_provenance(error, outcome)


def _captured_exception_context(
    error: BaseException,
    capture: Callable[..., object],
    *,
    provenance: Callable[..., object] | None = None,
    outcome: _ScopedJobExecutionOutcome | None = None,
) -> BaseException | None:
    """Recover an interrupted handler's exact error from one capture frame."""

    context = _raw_exception_context(error)
    if context is None:
        return None
    try:
        error_traceback = vars(BaseException)["__traceback__"].__get__(
            error,
            type(error),
        )
        capture_frames = []
        while error_traceback is not None:
            frame = error_traceback.tb_frame
            if frame.f_code is capture.__code__:
                capture_frames.append(frame)
            error_traceback = error_traceback.tb_next
        if not capture_frames:
            return None
        context_traceback = vars(BaseException)["__traceback__"].__get__(
            context,
            type(context),
        )
        while context_traceback is not None:
            frame = context_traceback.tb_frame
            if any(frame is capture_frame for capture_frame in capture_frames):
                return context
            context_traceback = context_traceback.tb_next
        if (
            provenance is not None
            and outcome is not None
            and _has_scoped_job_provenance(context, provenance, outcome)
        ):
            return context
    except BaseException:  # noqa: B036 - inspection cannot replace failure
        return None
    return None


def _call_scoped_job_operation(
    executor: object,
    operation: Callable[[object], IndexJobExecutionResult],
    outcome: _ScopedJobExecutionOutcome,
) -> IndexJobExecutionResult:
    """Bind one operation failure to its exact preinstalled outcome."""

    return operation(executor)


def _capture_scoped_job_execution(
    executor: object,
    operation: Callable[[object], IndexJobExecutionResult],
    outcome: _ScopedJobExecutionOutcome,
) -> None:
    """Capture one executor result without crossing the resource boundary."""

    with _ScopedJobOperationCapture(outcome):
        outcome.result = _call_scoped_job_operation(executor, operation, outcome)


def _invoke_scoped_job_execution(
    executor: object,
    operation: Callable[[object], IndexJobExecutionResult],
    outcome: _ScopedJobExecutionOutcome,
) -> None:
    """Commit an interrupted capture, then rethrow inside the native scope."""

    boundary_error: BaseException | None = None
    try:
        _capture_scoped_job_execution(executor, operation, outcome)
    except BaseException as error:  # noqa: B036 - one-shot capture recovery
        boundary_error = error
    if (
        outcome.operation_error is None
        and outcome.result is _MISSING_EXECUTION_RESULT
        and boundary_error is not None
    ):
        recovered = _captured_exception_context(
            boundary_error,
            _capture_scoped_job_execution,
        )
        if recovered is not None:
            outcome.operation_error = recovered
    if boundary_error is not None:
        outcome.boundary_errors.append(boundary_error)
    if outcome.operation_error is not None:
        raise outcome.operation_error
    if boundary_error is not None:
        raise boundary_error


def _run_scoped_job_resource(
    resources: AbstractContextManager[object],
    operation: Callable[[object], IndexJobExecutionResult],
    outcome: _ScopedJobExecutionOutcome,
) -> None:
    """Recover operation provenance before leaving its resource scope."""

    with resources as executor:
        try:
            _invoke_scoped_job_execution(executor, operation, outcome)
        except BaseException as boundary_error:  # noqa: B036 - exact recovery
            if outcome.operation_error is None:
                outcome.operation_error = boundary_error
            if outcome.result is _MISSING_EXECUTION_RESULT:
                recovered = _captured_exception_context(
                    boundary_error,
                    _capture_scoped_job_execution,
                    provenance=_call_scoped_job_operation,
                    outcome=outcome,
                )
                if recovered is not None:
                    outcome.operation_error = recovered
            if outcome.operation_error is boundary_error:
                raise
            raise outcome.operation_error from boundary_error


def _capture_scoped_job_resource_exit(
    resources: AbstractContextManager[object],
    operation: Callable[[object], IndexJobExecutionResult],
    outcome: _ScopedJobExecutionOutcome,
) -> None:
    """Capture one native resource scope's final exit outcome."""

    with _ScopedJobResourceExitCapture(outcome):
        _run_scoped_job_resource(resources, operation, outcome)


def _invoke_scoped_job_resource_exit(
    resources: AbstractContextManager[object],
    operation: Callable[[object], IndexJobExecutionResult],
    outcome: _ScopedJobExecutionOutcome,
) -> None:
    """Recover one interrupted resource-exit capture after cleanup finished."""

    boundary_error: BaseException | None = None
    try:
        _capture_scoped_job_resource_exit(resources, operation, outcome)
    except BaseException as error:  # noqa: B036 - one-shot capture recovery
        boundary_error = error
    if outcome.scope_error is None and boundary_error is not None:
        recovered = _captured_exception_context(
            boundary_error,
            _capture_scoped_job_resource_exit,
        )
        if recovered is not None:
            outcome.scope_error = recovered
    if boundary_error is not None:
        outcome.boundary_errors.append(boundary_error)
        if outcome.scope_error is None:
            raise boundary_error


def _recover_scoped_job_resource_exit(
    resources: AbstractContextManager[object],
    operation: Callable[[object], IndexJobExecutionResult],
    outcome: _ScopedJobExecutionOutcome,
) -> None:
    """Recover provenance after one interrupted resource capture has unwound."""

    boundary_error: BaseException | None = None
    try:
        _invoke_scoped_job_resource_exit(resources, operation, outcome)
    except BaseException as error:  # noqa: B036 - deferred capture recovery
        boundary_error = error
    if outcome.scope_error is None and boundary_error is not None:
        recovered = _captured_exception_context(
            boundary_error,
            _capture_scoped_job_resource_exit,
            provenance=_run_scoped_job_resource,
            outcome=outcome,
        )
        outcome.scope_error = recovered if recovered is not None else boundary_error
    if outcome.operation_error is None:
        outcome.operation_error = _scoped_job_operation_context(outcome)


def _iter_exception_contexts(error: BaseException):
    """Yield one raw exception-context chain without following cycles."""

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = _raw_exception_context(current)


def _scoped_job_operation_context(
    outcome: _ScopedJobExecutionOutcome,
) -> BaseException | None:
    """Recover one exact operation error after the resource scope unwinds."""

    roots = (
        (outcome.scope_error, *outcome.boundary_errors)
        if outcome.scope_error is not None
        else tuple(outcome.boundary_errors)
    )
    seen: set[int] = set()
    for root in roots:
        for error in _iter_exception_contexts(root):
            if id(error) in seen:
                continue
            seen.add(id(error))
            if _has_scoped_job_provenance(
                error,
                _call_scoped_job_operation,
                outcome,
            ):
                return error
    return None


def _settle_scoped_job_execution_once(
    outcome: _ScopedJobExecutionOutcome,
    *,
    label: str,
) -> None:
    """Idempotently commit priority and recovery metadata without raising."""

    if outcome.settlement_complete:
        return

    primary = outcome.operation_error
    if primary is None:
        primary = outcome.scope_error
    if primary is None and outcome.boundary_errors:
        primary = outcome.boundary_errors[0]
    outcome.settled_primary = primary
    retained_secondary_ids: set[int] = set()
    secondary_roots = (
        (outcome.scope_error, *outcome.boundary_errors)
        if outcome.scope_error is not None
        else tuple(outcome.boundary_errors)
    )
    for secondary_root in secondary_roots:
        if secondary_root is None:
            continue
        for secondary in _iter_exception_contexts(secondary_root):
            if secondary is primary:
                break
            if id(secondary) in retained_secondary_ids:
                continue
            retained_secondary_ids.add(id(secondary))
            if not _inherit_cleanup_owners(primary, secondary):
                # Retry one interrupted attachment inline.  A hostile primary
                # can still reject the attribute permanently; in that case the
                # exact secondary and its owner remain reachable from outcome.
                _inherit_cleanup_owners(primary, secondary)
            _add_cleanup_exception_note(primary, secondary, label=label)
    outcome.settlement_complete = True


def _settle_scoped_job_execution(
    outcome: _ScopedJobExecutionOutcome,
    active_error: BaseException | None,
    *,
    label: str,
) -> None:
    """Retry one interrupted non-raising settlement before final delivery."""

    if active_error is not None and not any(
        active_error is candidate for candidate in outcome.boundary_errors
    ):
        outcome.boundary_errors.append(active_error)
    try:
        _settle_scoped_job_execution_once(outcome, label=label)
    except BaseException as settlement_error:  # noqa: B036 - retry once
        if not any(
            settlement_error is candidate for candidate in outcome.boundary_errors
        ):
            outcome.boundary_errors.append(settlement_error)
        _settle_scoped_job_execution_once(outcome, label=label)


def _execute_scoped_job_resource(
    resources: AbstractContextManager[object],
    operation: Callable[[object], IndexJobExecutionResult],
    *,
    label: str,
    missing_result_message: str,
) -> IndexJobExecutionResult:
    """Execute and settle one scope using a preinstalled outcome carrier."""

    outcome = _ScopedJobExecutionOutcome()
    ambient_error = sys.exc_info()[1]
    ambient_traceback = (
        None
        if ambient_error is None
        else BaseException.__traceback__.__get__(
            ambient_error,
            type(ambient_error),
        )
    )
    try:
        try:
            _recover_scoped_job_resource_exit(resources, operation, outcome)
        finally:
            active_error = sys.exc_info()[1]
            if _is_inherited_scoped_job_ambient(
                active_error,
                ambient_error,
                ambient_traceback,
                outcome,
            ):
                active_error = None
            elif (
                active_error is not None
                and outcome.operation_error is None
                and outcome.scope_error is None
            ):
                outcome.operation_error = active_error
            outcome.settlement_started = True
            _settle_scoped_job_execution(outcome, active_error, label=label)
    finally:
        if not outcome.settlement_complete:
            active_error = sys.exc_info()[1]
            if _is_inherited_scoped_job_ambient(
                active_error,
                ambient_error,
                ambient_traceback,
                outcome,
            ):
                active_error = None
            elif (
                not outcome.settlement_started
                and active_error is not None
                and outcome.operation_error is None
            ):
                outcome.operation_error = active_error
            outcome.settlement_started = True
            _settle_scoped_job_execution(outcome, active_error, label=label)
        # This is the outermost Python delivery boundary.  Settlement above is
        # complete before a primary reaches it: exact cleanup owners and notes
        # are already attached, and ``outcome`` remains reachable from the
        # traceback if a later asynchronous exception supersedes this raise.
        # No finite Python helper/finally can protect its own last opcode.
        if outcome.settled_primary is not None and sys.exc_info()[1] is not None:
            raise outcome.settled_primary
    if outcome.settled_primary is not None:
        raise outcome.settled_primary
    if type(outcome.result) is not IndexJobExecutionResult:
        raise StorageIntegrityError(missing_result_message)
    return outcome.result


def _execute_in_resource_scope(
    scope: CompilerCacheJobResourceScope,
    context: IndexJobExecutionContext,
) -> IndexJobExecutionResult:
    """Execute without permitting hostile cleanup to suppress the primary error."""

    def execute(executor: object) -> IndexJobExecutionResult:
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
        return result

    return _execute_scoped_job_resource(
        scope.resources,
        execute,
        label="compiler cache resource",
        missing_result_message=(
            "compiler cache resource scope returned no execution result"
        ),
    )


def _execute_in_bm25_source_resource_scope(
    scope: BM25SourceJobResourceScope,
    context: IndexJobExecutionContext,
) -> IndexJobExecutionResult:
    """Execute a source build without letting cleanup suppress its failure."""

    def execute(executor: object) -> IndexJobExecutionResult:
        if type(executor) is not BM25SourceJobExecutor:
            raise TypeError("BM25 source resource factory returned an invalid executor")
        if executor.object_store is not scope.object_store:
            raise StorageIntegrityError(
                "BM25 source executor differs from its declared object store"
            )
        result = executor.execute(context)
        if type(result) is not IndexJobExecutionResult:
            raise StorageIntegrityError(
                "BM25 source executor returned an invalid result"
            )
        return result

    return _execute_scoped_job_resource(
        scope.resources,
        execute,
        label="BM25 source resource",
        missing_result_message="BM25 source resource scope returned no result",
    )


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
