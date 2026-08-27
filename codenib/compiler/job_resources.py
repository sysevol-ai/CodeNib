# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Trusted local resources for prepare-only cache and source job attempts."""

from __future__ import annotations

import logging
import secrets
from contextlib import contextmanager
from dataclasses import InitVar, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Mapping

from .._atomic_directory import (
    DirectoryOrphan,
    _attach_publication_cleanup_owner,
    _OrderedAction,
    _run_context_with_cleanup_actions,
    discard_owned_directory,
    lexical_directory_path,
)
from .._captured_directory import PublishedWorkspaceReceiptOwner
from .._local_workspace_provider import LocalWorkspaceProvider
from ..artifacts.runtime import SourceBindingCleanupOwner
from ..source_fingerprint import capture_repository_source, lexical_repository_path
from ..storage.job_worker import IndexJobExecutionContext
from ..storage.models import (
    DEFAULT_NAMESPACE_NAME,
    IndexJobRecord,
    IndexJobRequest,
    IndexJobRequestedMode,
    NamespaceIdentity,
    RepositoryIdentity,
    SourceRevision,
    StorageIntegrityError,
    StorageValidationError,
)
from ..storage.protocols import (
    InterruptibleReceiptVerifyingObjectStore,
    InterruptibleStreamingObjectStore,
    RetainedImportObjectStore,
)
from .cache_import import (
    CompilerCacheJobExecutor,
    _compiler_cache_job_stop_check,
    compiler_cache_source_selection,
)
from .index_builders import BM25IndexBuilder
from .job_resolver import BM25SourceJobResourceScope, CompilerCacheJobResourceScope
from .manifest_import import _require_static_methods, _snapshot_environment
from .snapshot_store import normalize_repo
from .source_job import (
    BM25SourceJobExecutor,
    _BM25BuilderConfiguration,
    _exact_display_commit,
    _snapshot_builder,
)

logger = logging.getLogger(__name__)

_MAX_LOCAL_TARGETS = 4_096
_NONCE_BYTES = 16
_SUPPORTED_CACHE_VIEWS = frozenset({"bm25", "vector"})


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _require_physical_roots_disjoint(
    first: Path,
    second: Path,
    *,
    label: str,
) -> None:
    try:
        physical_first = first.resolve(strict=True)
        physical_second = second.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} cannot be authenticated") from exc
    if _paths_overlap(physical_first, physical_second):
        raise ValueError(f"{label} must not overlap")


@dataclass(frozen=True, slots=True)
class LocalCompilerCacheJobTarget:
    """One explicitly trusted local repository/cache/workspace binding.

    The target is configuration, not discovery.  Callers must construct it
    from an already-authorized repository registry or CLI selection; the job
    resolver receives no catalog capability and cannot turn arbitrary durable
    repository IDs into filesystem paths.
    """

    repository_root: Path
    cache_dir: Path
    workspace_provider: LocalWorkspaceProvider
    repository_key: str
    namespace_name: str = DEFAULT_NAMESPACE_NAME
    environ: Mapping[str, str] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _repository_id: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self) is not LocalCompilerCacheJobTarget:
            raise TypeError("local compiler cache target must use the exact type")
        if not isinstance(self.repository_root, Path):
            raise TypeError("compiler cache target repository root must be a Path")
        if not isinstance(self.cache_dir, Path):
            raise TypeError("compiler cache target cache directory must be a Path")
        if type(self.workspace_provider) is not LocalWorkspaceProvider:
            raise TypeError(
                "compiler cache target requires an exact local workspace provider"
            )
        if type(self.repository_key) is not str or type(self.namespace_name) is not str:
            raise TypeError(
                "compiler cache target namespace and repository key must be exact text"
            )

        repository_root = lexical_repository_path(self.repository_root)
        cache_dir = lexical_directory_path(self.cache_dir)
        workspace_root = self.workspace_provider.allowed_root
        if repository_root == repository_root.parent:
            raise ValueError(
                "compiler cache target repository cannot be a filesystem root"
            )
        if cache_dir == repository_root or cache_dir in repository_root.parents:
            raise ValueError(
                "compiler cache target cache cannot contain the repository"
            )
        if _paths_overlap(workspace_root, repository_root):
            raise ValueError(
                "compiler cache target workspace must not overlap the repository"
            )
        if _paths_overlap(workspace_root, cache_dir):
            raise ValueError(
                "compiler cache target workspace must not overlap the cache"
            )

        namespace = NamespaceIdentity(self.namespace_name)
        repository = RepositoryIdentity(
            namespace_id=namespace.namespace_id,
            repository_key=self.repository_key,
        )
        if (
            namespace.name != self.namespace_name
            or repository.repository_key != self.repository_key
        ):
            raise StorageValidationError(
                "compiler cache target namespace and repository key must be canonical"
            )
        try:
            normalized_repository = normalize_repo(repository.repository_key)
        except ValueError as exc:
            raise StorageValidationError(
                "compiler cache target repository key is not canonical"
            ) from exc
        if normalized_repository != repository.repository_key:
            raise StorageValidationError(
                "compiler cache target repository key is not canonical"
            )
        object.__setattr__(self, "repository_root", repository_root)
        object.__setattr__(self, "cache_dir", cache_dir)
        object.__setattr__(self, "repository_key", repository.repository_key)
        object.__setattr__(self, "namespace_name", namespace.name)
        object.__setattr__(
            self,
            "environ",
            _snapshot_environment(self.environ),
        )
        object.__setattr__(self, "_repository_id", repository.repository_id)

    @property
    def repository_id(self) -> str:
        return self._repository_id

    @property
    def workspace_root(self) -> Path:
        return self.workspace_provider.allowed_root


@dataclass(frozen=True, slots=True)
class LocalBM25SourceJobTarget:
    """One explicitly trusted source-builder and local workspace binding."""

    repository_root: Path
    workspace_provider: LocalWorkspaceProvider
    repository_key: str
    display_commit: str
    builder: InitVar[BM25IndexBuilder]
    namespace_name: str = DEFAULT_NAMESPACE_NAME
    environ: Mapping[str, str] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _repository_id: str = field(init=False, repr=False)
    _builder: _BM25BuilderConfiguration = field(init=False, repr=False)
    _profile_id: str = field(init=False, repr=False)

    def __post_init__(self, builder: BM25IndexBuilder) -> None:
        if type(self) is not LocalBM25SourceJobTarget:
            raise TypeError("local BM25 source target must use the exact type")
        if not isinstance(self.repository_root, Path):
            raise TypeError("BM25 source target repository root must be a Path")
        if type(self.workspace_provider) is not LocalWorkspaceProvider:
            raise TypeError(
                "BM25 source target requires an exact local workspace provider"
            )
        if type(self.repository_key) is not str or type(self.namespace_name) is not str:
            raise TypeError(
                "BM25 source target namespace and repository key must be exact text"
            )

        repository_root = lexical_repository_path(self.repository_root)
        workspace_root = self.workspace_provider.allowed_root
        if repository_root == repository_root.parent:
            raise ValueError(
                "BM25 source target repository cannot be a filesystem root"
            )
        if _paths_overlap(workspace_root, repository_root):
            raise ValueError(
                "BM25 source target workspace must not overlap the repository"
            )
        namespace = NamespaceIdentity(self.namespace_name)
        repository = RepositoryIdentity(
            namespace_id=namespace.namespace_id,
            repository_key=self.repository_key,
        )
        if (
            namespace.name != self.namespace_name
            or repository.repository_key != self.repository_key
        ):
            raise StorageValidationError(
                "BM25 source target namespace and repository key must be canonical"
            )
        try:
            normalized_repository = normalize_repo(repository.repository_key)
        except ValueError as exc:
            raise StorageValidationError(
                "BM25 source target repository key is not canonical"
            ) from exc
        if normalized_repository != repository.repository_key:
            raise StorageValidationError(
                "BM25 source target repository key is not canonical"
            )
        configuration = _snapshot_builder(builder)
        profile = configuration.profile()
        object.__setattr__(self, "repository_root", repository_root)
        object.__setattr__(self, "repository_key", repository.repository_key)
        object.__setattr__(self, "namespace_name", namespace.name)
        object.__setattr__(
            self, "display_commit", _exact_display_commit(self.display_commit)
        )
        object.__setattr__(self, "environ", _snapshot_environment(self.environ))
        object.__setattr__(self, "_repository_id", repository.repository_id)
        object.__setattr__(self, "_builder", configuration)
        object.__setattr__(self, "_profile_id", profile.profile_id)

    @property
    def repository_id(self) -> str:
        return self._repository_id

    @property
    def workspace_root(self) -> Path:
        return self.workspace_provider.allowed_root

    @property
    def profile_id(self) -> str:
        return self._profile_id


@dataclass(slots=True)
class _AttemptWorkspaceCleanupOwner:
    """Close one receipt authority, then isolate its exact published tree."""

    owner: PublishedWorkspaceReceiptOwner
    destination: Path
    label: str
    job_label: str = "Compiler-cache job"
    _ownership: object | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def closed(self) -> bool:
        return self._closed

    def _record_orphan(
        self,
        orphan: DirectoryOrphan | None,
        *,
        label: str,
    ) -> None:
        if orphan is None:
            return
        logger.warning(
            "%s %s retained an orphan for quiescent GC: "
            "path=%s digest=%s entries=%d bytes=%d verified=%s",
            self.job_label,
            label,
            orphan.path,
            orphan.ownership_digest,
            orphan.entries,
            orphan.byte_count,
            orphan.verified_at_isolation,
        )

    def close(self) -> None:
        if self._closed:
            return
        if self._ownership is None:
            state = self.owner.state
            if state == "active":
                binding = self.owner.destination_binding
                if binding.destination != self.destination:
                    raise StorageIntegrityError(
                        f"{self.job_label} workspace receipt changed destination"
                    )
                self._ownership = binding.ownership
            elif state == "closed":
                self._closed = True
                return
            elif state != "empty":
                raise StorageIntegrityError(
                    f"{self.job_label} workspace receipt has an invalid state"
                )

        self.owner.close()
        if not self.owner.closed:
            raise RuntimeError(f"{self.job_label} workspace receipt did not close")
        if self._ownership is not None:
            orphan = discard_owned_directory(self.destination, self._ownership)
            self._record_orphan(orphan, label=self.label)
        self._closed = True


def _cleanup_owner_pending(owner: object) -> bool:
    try:
        return not bool(owner.closed)  # type: ignore[attr-defined]
    except BaseException:  # noqa: B036 - uncertain cleanup must fail closed
        return True


def _inherit_cleanup_owners(
    target: BaseException,
    source: BaseException,
) -> None:
    try:
        owners = BaseException.__getattribute__(
            source,
            "publication_cleanup_owners",
        )
    except BaseException:  # noqa: B036 - diagnostics are best effort
        return
    if type(owners) is not tuple:
        return
    for owner in owners:
        _attach_publication_cleanup_owner(target, owner)


def _attempt_nonce() -> str:
    nonce = secrets.token_hex(_NONCE_BYTES)
    if len(nonce) != 2 * _NONCE_BYTES or any(
        character not in "0123456789abcdef" for character in nonce
    ):
        raise RuntimeError("job destination nonce is invalid")
    return nonce


@dataclass(frozen=True, slots=True)
class LocalCompilerCacheJobResourceFactory:
    """Resolve configured local targets into fresh attempt-scoped resources."""

    targets: tuple[LocalCompilerCacheJobTarget, ...]
    _targets_by_repository_id: Mapping[str, LocalCompilerCacheJobTarget] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if type(self) is not LocalCompilerCacheJobResourceFactory:
            raise TypeError(
                "local compiler cache resource factory must use the exact type"
            )
        if type(self.targets) is not tuple or not (
            1 <= len(self.targets) <= _MAX_LOCAL_TARGETS
        ):
            raise ValueError(
                "local compiler cache resource factory requires bounded targets"
            )
        targets_by_repository_id: dict[str, LocalCompilerCacheJobTarget] = {}
        for target in self.targets:
            if type(target) is not LocalCompilerCacheJobTarget:
                raise TypeError(
                    "local compiler cache resource factory target is invalid"
                )
            if target.repository_id in targets_by_repository_id:
                raise ValueError(
                    "local compiler cache resource factory has duplicate repository IDs"
                )
            targets_by_repository_id[target.repository_id] = target
        object.__setattr__(
            self,
            "_targets_by_repository_id",
            MappingProxyType(targets_by_repository_id),
        )

    def accepts_candidate(self, job: IndexJobRecord) -> bool:
        """Return exact pre-claim eligibility for this configured target set."""

        if type(job) is not IndexJobRecord:
            raise StorageValidationError(
                "local compiler cache candidate must be an exact job record"
            )
        if job.repository_id not in self._targets_by_repository_id:
            return False
        try:
            request = IndexJobRequest(
                repository_id=job.repository_id,
                source_revision_id=job.source_revision_id,
                ref_name=job.ref_name,
                idempotency_key=job.idempotency_key,
                expected_ref_generation=job.expected_ref_generation,
                max_attempts=job.max_attempts,
                request_json=job.request_json,
            )
        except StorageValidationError as exc:
            raise StorageIntegrityError(
                "local compiler cache candidate request is invalid"
            ) from exc
        if request.job_id != job.job_id or request.request_digest != job.request_digest:
            raise StorageIntegrityError(
                "local compiler cache candidate request identity is inconsistent"
            )
        views = request.view_requests
        return (
            len(views) == 1
            and views[0].job_id == job.job_id
            and views[0].view_type in _SUPPORTED_CACHE_VIEWS
            and views[0].requested_mode is IndexJobRequestedMode.FULL
            and views[0].required is True
        )

    def create_scope(
        self,
        context: IndexJobExecutionContext,
        *,
        object_store: RetainedImportObjectStore,
    ) -> CompilerCacheJobResourceScope:
        if type(context) is not IndexJobExecutionContext:
            raise TypeError(
                "local compiler cache resource factory requires an exact context"
            )
        if not isinstance(object_store, RetainedImportObjectStore):
            raise TypeError(
                "local compiler cache resource factory requires a retained import store"
            )
        if not isinstance(object_store, InterruptibleReceiptVerifyingObjectStore):
            raise TypeError(
                "local compiler cache resource factory requires interruptible "
                "receipt verification"
            )
        if not isinstance(object_store, InterruptibleStreamingObjectStore):
            raise TypeError(
                "local compiler cache resource factory requires interruptible "
                "streaming ingestion"
            )
        _require_static_methods(
            object_store,
            label="local compiler cache object store",
            names=(
                "put_chunks_interruptibly",
                "verify_receipt_interruptibly",
            ),
        )
        target = self._targets_by_repository_id.get(context.job.repository_id)
        if target is None:
            raise StorageValidationError(
                "compiler cache job repository has no trusted local target"
            )
        if (
            len(context.views) != 1
            or context.views[0].view_type not in _SUPPORTED_CACHE_VIEWS
        ):
            raise StorageValidationError(
                "local compiler cache resource factory requires one supported job view"
            )
        view = context.views[0]
        return CompilerCacheJobResourceScope(
            object_store=object_store,
            view_type=view.view_type,
            resources=self._open(
                context,
                object_store=object_store,
                target=target,
            ),
        )

    @contextmanager
    def _open(
        self,
        context: IndexJobExecutionContext,
        *,
        object_store: RetainedImportObjectStore,
        target: LocalCompilerCacheJobTarget,
    ) -> Iterator[CompilerCacheJobExecutor]:
        check_cancelled = _compiler_cache_job_stop_check(context.control.stop_token)
        if check_cancelled is None:  # pragma: no cover - context invariant
            raise AssertionError("compiler cache job context has no stop check")
        nonce = _attempt_nonce()
        prefix = f".codenib-cache-job-{nonce}"
        view = context.views[0]
        view_destination = target.workspace_root / f"{prefix}-{view.view_type}"
        context_destination = target.workspace_root / f"{prefix}-context"
        view_owner = PublishedWorkspaceReceiptOwner()
        context_owner = PublishedWorkspaceReceiptOwner()
        view_cleanup = _AttemptWorkspaceCleanupOwner(
            view_owner,
            view_destination,
            view.view_type,
        )
        context_cleanup = _AttemptWorkspaceCleanupOwner(
            context_owner,
            context_destination,
            "context",
        )
        source_owner = SourceBindingCleanupOwner()
        cleanup_owners = (context_cleanup, view_cleanup, source_owner)
        cleanup_actions = (
            _OrderedAction(
                label="compiler cache job context cleanup also failed",
                action=context_cleanup.close,
                complete=lambda: context_cleanup.closed,
                retry_incomplete="cancellation",
                incomplete_owner=context_cleanup,
            ),
            _OrderedAction(
                label="compiler cache job view cleanup also failed",
                action=view_cleanup.close,
                complete=lambda: view_cleanup.closed,
                retry_incomplete="cancellation",
                incomplete_owner=view_cleanup,
            ),
            _OrderedAction(
                label="compiler cache job source cleanup also failed",
                action=source_owner.close,
                complete=lambda: source_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=source_owner,
            ),
        )

        try:
            with _run_context_with_cleanup_actions(cleanup_actions):
                check_cancelled()
                target.workspace_provider.require_support()
                source_selection = compiler_cache_source_selection(
                    target.cache_dir,
                    check_cancelled=check_cancelled,
                )
                check_cancelled()
                repository_source = capture_repository_source(
                    target.repository_root,
                    exclude_roots=(target.cache_dir, target.workspace_root),
                    selection=source_selection,
                    _source_owner=source_owner.retain,
                    check_cancelled=check_cancelled,
                )
                source_owner.retain(repository_source)
                source_revision = SourceRevision.dirty(
                    target.repository_id,
                    source_fingerprint=repository_source.fingerprint,
                    commit_sha=None,
                )
                if source_revision.source_revision_id != context.job.source_revision_id:
                    raise StorageValidationError(
                        "compiler cache job source has no current trusted local target"
                    )
                check_cancelled()
                yield CompilerCacheJobExecutor(
                    cache_dir=target.cache_dir,
                    view_type=view.view_type,
                    repository_source=repository_source,
                    view_output_owner=view_owner,
                    context_output_owner=context_owner,
                    view_destination=view_destination,
                    context_destination=context_destination,
                    workspace_provider=target.workspace_provider,
                    repository_key=target.repository_key,
                    object_store=object_store,
                    namespace_name=target.namespace_name,
                    forbidden_paths=(target.cache_dir, target.workspace_root),
                    environ=target.environ,
                )
        except BaseException as error:  # noqa: B036 - retain cleanup authority
            pending = tuple(
                owner for owner in cleanup_owners if _cleanup_owner_pending(owner)
            )
            if pending and isinstance(error, Exception):
                wrapped = StorageIntegrityError(
                    "compiler cache job attempt resource cleanup did not settle"
                )
                _inherit_cleanup_owners(wrapped, error)
                for owner in pending:
                    _attach_publication_cleanup_owner(wrapped, owner)
                raise wrapped from error
            raise


@dataclass(frozen=True, slots=True)
class LocalBM25SourceJobResourceFactory:
    """Resolve configured local repositories into fresh BM25 source attempts."""

    targets: tuple[LocalBM25SourceJobTarget, ...]
    _targets_by_repository_id: Mapping[str, LocalBM25SourceJobTarget] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if type(self) is not LocalBM25SourceJobResourceFactory:
            raise TypeError(
                "local BM25 source resource factory must use the exact type"
            )
        if type(self.targets) is not tuple or not (
            1 <= len(self.targets) <= _MAX_LOCAL_TARGETS
        ):
            raise ValueError(
                "local BM25 source resource factory requires bounded targets"
            )
        targets_by_repository_id: dict[str, LocalBM25SourceJobTarget] = {}
        for target in self.targets:
            if type(target) is not LocalBM25SourceJobTarget:
                raise TypeError("local BM25 source resource target is invalid")
            if target.repository_id in targets_by_repository_id:
                raise ValueError(
                    "local BM25 source resource factory has duplicate repository IDs"
                )
            targets_by_repository_id[target.repository_id] = target
        object.__setattr__(
            self,
            "_targets_by_repository_id",
            MappingProxyType(targets_by_repository_id),
        )

    def accepts_candidate(self, job: IndexJobRecord) -> bool:
        """Return exact pre-claim eligibility for this source target set."""

        if type(job) is not IndexJobRecord:
            raise StorageValidationError(
                "local BM25 source candidate must be an exact job record"
            )
        target = self._targets_by_repository_id.get(job.repository_id)
        if target is None:
            return False
        try:
            request = IndexJobRequest(
                repository_id=job.repository_id,
                source_revision_id=job.source_revision_id,
                ref_name=job.ref_name,
                idempotency_key=job.idempotency_key,
                expected_ref_generation=job.expected_ref_generation,
                max_attempts=job.max_attempts,
                request_json=job.request_json,
            )
        except StorageValidationError as exc:
            raise StorageIntegrityError(
                "local BM25 source candidate request is invalid"
            ) from exc
        if request.job_id != job.job_id or request.request_digest != job.request_digest:
            raise StorageIntegrityError(
                "local BM25 source candidate request identity is inconsistent"
            )
        views = request.view_requests
        return (
            len(views) == 1
            and views[0].job_id == job.job_id
            and views[0].view_type == "bm25"
            and views[0].profile_id == target.profile_id
            and views[0].requested_mode is IndexJobRequestedMode.FULL
            and views[0].required is True
        )

    def create_scope(
        self,
        context: IndexJobExecutionContext,
        *,
        object_store: RetainedImportObjectStore,
    ) -> BM25SourceJobResourceScope:
        if type(context) is not IndexJobExecutionContext:
            raise TypeError(
                "local BM25 source resource factory requires an exact context"
            )
        if not isinstance(object_store, RetainedImportObjectStore):
            raise TypeError(
                "local BM25 source resource factory requires a retained import store"
            )
        if not isinstance(object_store, InterruptibleReceiptVerifyingObjectStore):
            raise TypeError(
                "local BM25 source resource factory requires interruptible receipt "
                "verification"
            )
        if not isinstance(object_store, InterruptibleStreamingObjectStore):
            raise TypeError(
                "local BM25 source resource factory requires interruptible streaming "
                "ingestion"
            )
        _require_static_methods(
            object_store,
            label="local BM25 source object store",
            names=(
                "put_chunks_interruptibly",
                "verify_receipt_interruptibly",
            ),
        )
        target = self._targets_by_repository_id.get(context.job.repository_id)
        if target is None:
            raise StorageValidationError(
                "BM25 source job repository has no trusted local target"
            )
        if (
            len(context.views) != 1
            or context.views[0].view_type != "bm25"
            or context.views[0].profile_id != target.profile_id
            or context.views[0].requested_mode is not IndexJobRequestedMode.FULL
            or context.views[0].required is not True
        ):
            raise StorageValidationError(
                "local BM25 source resource factory requires one matching FULL view"
            )
        return BM25SourceJobResourceScope(
            object_store=object_store,
            resources=self._open(
                context,
                object_store=object_store,
                target=target,
            ),
        )

    @contextmanager
    def _open(
        self,
        context: IndexJobExecutionContext,
        *,
        object_store: RetainedImportObjectStore,
        target: LocalBM25SourceJobTarget,
    ) -> Iterator[BM25SourceJobExecutor]:
        check_cancelled = _compiler_cache_job_stop_check(context.control.stop_token)
        if check_cancelled is None:  # pragma: no cover - context invariant
            raise AssertionError("BM25 source job context has no stop check")
        nonce = _attempt_nonce()
        prefix = f".codenib-source-job-{nonce}"
        attempt_destination = target.workspace_root / f"{prefix}-attempt"
        view_destination = target.workspace_root / f"{prefix}-bm25"
        context_destination = target.workspace_root / f"{prefix}-context"
        attempt_owner = PublishedWorkspaceReceiptOwner()
        view_owner = PublishedWorkspaceReceiptOwner()
        context_owner = PublishedWorkspaceReceiptOwner()
        attempt_cleanup = _AttemptWorkspaceCleanupOwner(
            attempt_owner,
            attempt_destination,
            "source attempt",
            job_label="BM25 source job",
        )
        view_cleanup = _AttemptWorkspaceCleanupOwner(
            view_owner,
            view_destination,
            "source BM25",
            job_label="BM25 source job",
        )
        context_cleanup = _AttemptWorkspaceCleanupOwner(
            context_owner,
            context_destination,
            "source context",
            job_label="BM25 source job",
        )
        source_owner = SourceBindingCleanupOwner()
        cleanup_owners = (
            context_cleanup,
            view_cleanup,
            attempt_cleanup,
            source_owner,
        )
        cleanup_actions = tuple(
            _OrderedAction(
                label=f"BM25 source job {cleanup.label} cleanup also failed",
                action=cleanup.close,
                complete=lambda cleanup=cleanup: cleanup.closed,
                retry_incomplete="cancellation",
                incomplete_owner=cleanup,
            )
            for cleanup in (context_cleanup, view_cleanup, attempt_cleanup)
        ) + (
            _OrderedAction(
                label="BM25 source job source cleanup also failed",
                action=source_owner.close,
                complete=lambda: source_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=source_owner,
            ),
        )

        try:
            with _run_context_with_cleanup_actions(cleanup_actions):
                check_cancelled()
                target.workspace_provider.require_support()
                _require_physical_roots_disjoint(
                    target.workspace_root,
                    target.repository_root,
                    label="BM25 source target physical workspace and repository",
                )
                repository_source = capture_repository_source(
                    target.repository_root,
                    exclude_roots=(target.workspace_root,),
                    selection=target._builder.source_selection,
                    _source_owner=source_owner.retain,
                    check_cancelled=check_cancelled,
                )
                source_owner.retain(repository_source)
                source_revision = SourceRevision.dirty(
                    target.repository_id,
                    source_fingerprint=repository_source.fingerprint,
                    commit_sha=None,
                )
                if source_revision.source_revision_id != context.job.source_revision_id:
                    raise StorageValidationError(
                        "BM25 source job source has no current trusted local target"
                    )
                check_cancelled()
                yield BM25SourceJobExecutor(
                    attempt_generation=attempt_destination,
                    display_commit=target.display_commit,
                    builder=target._builder.builder(),
                    attempt_output_owner=attempt_owner,
                    attempt_workspace_provider=target.workspace_provider,
                    repository_source=repository_source,
                    view_output_owner=view_owner,
                    context_output_owner=context_owner,
                    view_destination=view_destination,
                    context_destination=context_destination,
                    workspace_provider=target.workspace_provider,
                    repository_key=target.repository_key,
                    object_store=object_store,
                    namespace_name=target.namespace_name,
                    environ=target.environ,
                )
        except BaseException as error:  # noqa: B036 - retain cleanup authority
            pending = tuple(
                owner for owner in cleanup_owners if _cleanup_owner_pending(owner)
            )
            if pending and isinstance(error, Exception):
                wrapped = StorageIntegrityError(
                    "BM25 source job attempt resource cleanup did not settle"
                )
                _inherit_cleanup_owners(wrapped, error)
                for owner in pending:
                    _attach_publication_cleanup_owner(wrapped, owner)
                raise wrapped from error
            raise


__all__ = [
    "LocalBM25SourceJobResourceFactory",
    "LocalBM25SourceJobTarget",
    "LocalCompilerCacheJobResourceFactory",
    "LocalCompilerCacheJobTarget",
]
