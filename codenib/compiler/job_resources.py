# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Trusted local resources for prepare-only cache and source job attempts."""

from __future__ import annotations

import logging
import os
import re
import secrets
from contextlib import contextmanager
from dataclasses import InitVar, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterator, Mapping, final

from .._atomic_directory import (
    DirectoryOrphan,
    QuiescentDirectoryReclaimer,
    _annotate_secondary_error,
    _attach_publication_cleanup_owner,
    _OrderedAction,
    _run_callback_with_post_validations,
    _run_context_with_cleanup_actions,
    directory_ownership_root_identity,
    discard_owned_directory,
    lexical_directory_path,
)
from .._captured_directory import (
    OwnedPathBuildDirectory,
    PublishedWorkspaceReceiptOwner,
    _retry_retained_owned_path_build_cleanup_for_group,
)
from .._local_workspace_provider import LocalWorkspaceProvider
from .._workspace_provider import StrictWorkspaceRequest, StrictWorkspaceSession
from ..artifacts.runtime import SourceBindingCleanupOwner
from ..repository_source_selection import RepositorySourceSelection
from ..source_fingerprint import (
    RepositorySourceBinding,
    RepositorySourceRootAuthority,
    capture_repository_source,
    lexical_repository_path,
)
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
    ViewProfile,
)
from ..storage.protocols import (
    InterruptibleReceiptVerifyingObjectStore,
    InterruptibleStreamingObjectStore,
    RetainedImportObjectStore,
)
from ._directory_lease import DirectoryLeaseMode, PrivateDirectoryLeaseOwner
from .bm25_attempt_pool import (
    _LocalBM25AttemptPoolReaperRoute,
    _LocalBM25AttemptPoolWriterRoute,
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
_BM25_ATTEMPT_NONCE = rb"[0-9a-f]{32}"
_BM25_STAGE_NONCE = rb"[0-9a-f]{24}"
_BM25_ATTEMPT_ROLE = rb"(?:attempt|bm25|context)"
_BM25_ATTEMPT_POOL_PATTERNS = (
    (
        re.compile(
            rb"\.codenib-source-job-"
            + _BM25_ATTEMPT_NONCE
            + rb"\.normalize-"
            + _BM25_STAGE_NONCE
            + rb"\Z"
        ),
        "current",
        False,
    ),
    (
        re.compile(
            rb"\.\.codenib-source-job-"
            + _BM25_ATTEMPT_NONCE
            + rb"\.normalize-"
            + _BM25_STAGE_NONCE
            + rb"\.discarded-"
            + _BM25_ATTEMPT_NONCE
            + rb"\Z"
        ),
        "current",
        True,
    ),
    (
        re.compile(
            rb"\.codenib-source-job-"
            + _BM25_ATTEMPT_NONCE
            + rb"-"
            + _BM25_ATTEMPT_ROLE
            + rb"\Z"
        ),
        "legacy",
        False,
    ),
    (
        re.compile(
            rb"\.\.codenib-source-job-"
            + _BM25_ATTEMPT_NONCE
            + rb"-"
            + _BM25_ATTEMPT_ROLE
            + rb"\.normalize-"
            + _BM25_STAGE_NONCE
            + rb"\Z"
        ),
        "legacy",
        False,
    ),
    (
        re.compile(
            rb"\.\.codenib-source-job-"
            + _BM25_ATTEMPT_NONCE
            + rb"-"
            + _BM25_ATTEMPT_ROLE
            + rb"\.discarded-"
            + _BM25_ATTEMPT_NONCE
            + rb"\Z"
        ),
        "legacy",
        True,
    ),
    (
        re.compile(
            rb"\.\.\.codenib-source-job-"
            + _BM25_ATTEMPT_NONCE
            + rb"-"
            + _BM25_ATTEMPT_ROLE
            + rb"\.normalize-"
            + _BM25_STAGE_NONCE
            + rb"\.discarded-"
            + _BM25_ATTEMPT_NONCE
            + rb"\Z"
        ),
        "legacy",
        True,
    ),
)
_BM25_ATTEMPT_POOL_RESERVED_PREFIXES = (
    b"codenib-source-job-",
    b"codenib-discarded-",
)


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
class _RetainedBM25WorkspaceProvider:
    """Bind every source-job provision to one retained worker topology."""

    delegate: LocalWorkspaceProvider
    parent_identity: tuple[int, ...] | None
    topology_verifier: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def _verify(self) -> None:
        if self.topology_verifier is not None:
            self.topology_verifier()

    def require_support(self) -> None:
        self._verify()
        self.delegate.require_support()
        self._verify()

    def run_workspace(
        self,
        request: StrictWorkspaceRequest,
        *,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        operation: Callable[[StrictWorkspaceSession], object],
        check_cancelled: Callable[[], None] | None = None,
    ) -> object:
        self._verify()
        arguments = {
            "receipt_owner": receipt_owner,
            "operation": operation,
            "_expected_parent_identity": self.parent_identity,
        }
        if check_cancelled is None:
            result = self.delegate.run_workspace(request, **arguments)
        else:
            result = self.delegate.run_workspace(
                request,
                **arguments,
                check_cancelled=check_cancelled,
            )
        self._verify()
        return result


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
    """One explicitly trusted source-builder and local attempt-pool binding.

    ``attempt_orphan_sink`` accepts authenticated cleanup receipts at least
    once.  A configured sink must therefore be idempotent and non-reentrant.
    Without one, receipts are logged for the owning pool's later quiescent
    reaper.
    """

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
    repository_root_authority: RepositorySourceRootAuthority | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    display_commit_resolver: Callable[[], str] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    workspace_parent_identity: tuple[int, ...] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    topology_verifier: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    attempt_orphan_sink: Callable[[DirectoryOrphan], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    attempt_pool_writer_route: _LocalBM25AttemptPoolWriterRoute | None = field(
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
        repository_authority = self.repository_root_authority
        if repository_authority is not None:
            if type(repository_authority) is not RepositorySourceRootAuthority:
                raise TypeError(
                    "BM25 source target repository authority has an invalid type"
                )
            repository_authority.verify()
            if repository_authority.root != repository_root:
                raise ValueError(
                    "BM25 source target repository authority differs from its root"
                )
        display_commit_resolver = self.display_commit_resolver
        if display_commit_resolver is not None and not callable(
            display_commit_resolver
        ):
            raise TypeError("BM25 source target display commit resolver is invalid")
        workspace_parent_identity = self.workspace_parent_identity
        if workspace_parent_identity is not None and (
            type(workspace_parent_identity) is not tuple
            or len(workspace_parent_identity) < 2
            or any(type(value) is not int for value in workspace_parent_identity)
        ):
            raise TypeError("BM25 source target workspace identity is invalid")
        topology_verifier = self.topology_verifier
        if topology_verifier is not None and not callable(topology_verifier):
            raise TypeError("BM25 source target topology verifier is invalid")
        attempt_orphan_sink = self.attempt_orphan_sink
        if attempt_orphan_sink is not None and not callable(attempt_orphan_sink):
            raise TypeError("BM25 source target orphan sink is invalid")
        attempt_pool_writer_route = self.attempt_pool_writer_route
        if (
            attempt_pool_writer_route is not None
            and type(attempt_pool_writer_route) is not _LocalBM25AttemptPoolWriterRoute
        ):
            raise TypeError("BM25 source target attempt-pool writer route is invalid")
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
        if attempt_pool_writer_route is not None:
            if repository_authority is None:
                raise StorageValidationError(
                    "routed BM25 source target requires a repository authority"
                )
            if workspace_parent_identity is None or len(workspace_parent_identity) != 4:
                raise StorageValidationError(
                    "routed BM25 source target requires an exact workspace identity"
                )
            attempt_pool_writer_route._verify()
            if (
                attempt_pool_writer_route._repository_id != repository.repository_id
                or attempt_pool_writer_route._repository_root != repository_root
                or attempt_pool_writer_route._repository_root_identity
                != tuple(repository_authority.root_identity)
                or attempt_pool_writer_route._workspace_root != workspace_root
                or attempt_pool_writer_route._workspace_identity
                != tuple(workspace_parent_identity)
            ):
                raise StorageValidationError(
                    "BM25 source target attempt-pool route binding is inconsistent"
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
        object.__setattr__(
            self,
            "workspace_parent_identity",
            (
                None
                if workspace_parent_identity is None
                else tuple(workspace_parent_identity)
            ),
        )

    @property
    def repository_id(self) -> str:
        return self._repository_id

    @property
    def workspace_root(self) -> Path:
        return self.workspace_provider.allowed_root

    @property
    def attempt_pool_root(self) -> Path:
        """Return the private parent that owns source-job attempt roots."""

        route = self.attempt_pool_writer_route
        if route is not None:
            return route._shard_path
        return self.workspace_provider.allowed_root

    @property
    def attempt_pool_identity(self) -> tuple[int, ...] | None:
        """Return the retained identity for the source-job attempt parent."""

        route = self.attempt_pool_writer_route
        if route is not None:
            return route._shard_identity
        return self.workspace_parent_identity

    @property
    def profile_id(self) -> str:
        return self._profile_id

    def verify_topology(self) -> None:
        """Recheck the optional caller-retained worker topology."""

        if self.topology_verifier is not None:
            self.topology_verifier()

    def current_display_commit(self) -> str:
        """Resolve provenance while the configured root/topology stays live."""

        self.verify_topology()
        authority = self.repository_root_authority
        if authority is not None:
            authority.verify()
        resolver = self.display_commit_resolver
        commit = self.display_commit if resolver is None else resolver()
        if authority is not None:
            authority.verify()
        self.verify_topology()
        return _exact_display_commit(commit)

    @property
    def profile(self) -> ViewProfile:
        """Return the frozen portable profile shared by planner and worker."""

        return self._builder.profile()

    @property
    def source_selection(self) -> RepositorySourceSelection:
        """Return a detached copy of the frozen source-selection policy."""

        return RepositorySourceSelection(
            self._builder.source_selection.exclude_subtrees
        )

    def capture_source(
        self,
        *,
        source_owner: SourceBindingCleanupOwner,
        check_cancelled: Callable[[], None] | None = None,
    ) -> RepositorySourceBinding:
        """Capture the exact source policy under the retained worker topology."""

        if type(source_owner) is not SourceBindingCleanupOwner:
            raise TypeError("BM25 source target requires an exact source cleanup owner")
        if check_cancelled is not None and not callable(check_cancelled):
            raise TypeError("BM25 source target stop check must be callable")
        self.verify_topology()
        self.workspace_provider.require_support()
        _require_physical_roots_disjoint(
            self.workspace_root,
            self.repository_root,
            label="BM25 source target physical workspace and repository",
        )
        self.verify_topology()
        source = capture_repository_source(
            self.repository_root,
            exclude_roots=(self.workspace_root,),
            selection=self._builder.source_selection,
            _source_owner=source_owner.retain,
            expected_root_authority=self.repository_root_authority,
            check_cancelled=check_cancelled,
        )
        self.verify_topology()
        return source

    def accept_attempt_orphan(self, orphan: DirectoryOrphan) -> None:
        """Persist or log one authenticated attempt-root cleanup receipt."""

        if type(orphan) is not DirectoryOrphan:
            raise TypeError("BM25 source target orphan receipt is invalid")
        sink = self.attempt_orphan_sink
        if sink is not None:
            sink(orphan)
            return
        logger.warning(
            "BM25 source job retained an attempt-root orphan for quiescent GC: "
            "path=%s digest=%s entries=%d bytes=%d verified=%s",
            orphan.path,
            orphan.ownership_digest,
            orphan.entries,
            orphan.byte_count,
            orphan.verified_at_isolation,
        )


@dataclass(frozen=True, slots=True)
class _BM25AttemptPoolChild:
    """One exact, policy-recognized source-job attempt child."""

    lineage: str
    discarded: bool


def _classify_bm25_attempt_pool_child_name(
    name: str,
) -> _BM25AttemptPoolChild | None:
    """Classify one bounded snapshot name without granting broad discovery."""

    if type(name) is not str:
        raise TypeError("BM25 attempt-pool child name must be exact text")
    raw_name = os.fsencode(name)
    for pattern, lineage, discarded in _BM25_ATTEMPT_POOL_PATTERNS:
        if pattern.fullmatch(raw_name) is not None:
            return _BM25AttemptPoolChild(
                lineage=lineage,
                discarded=discarded,
            )
    reserved = raw_name.lstrip(b".").lower()
    if reserved.startswith(_BM25_ATTEMPT_POOL_RESERVED_PREFIXES):
        raise StorageValidationError(
            "BM25 attempt pool contains an unrecognized reserved child"
        )
    return None


@dataclass(frozen=True, slots=True)
class BM25AttemptPoolReclamation:
    """Bounded count-only result from one explicit-quiescent attempt sweep."""

    scanned_children: int
    reclaimed_children: int
    current_children: int
    legacy_children: int
    discarded_children: int
    retained_unrelated_children: int


@final
class _BM25AttemptPoolReaperCleanupOwner:
    """Keep EX held until its exact reclaimer and topology checks settle."""

    __slots__ = ("_lease", "_reclaimer", "_route")

    def __init__(self, route: _LocalBM25AttemptPoolReaperRoute) -> None:
        if type(self) is not _BM25AttemptPoolReaperCleanupOwner:
            raise TypeError("BM25 attempt-pool reaper cleanup must use the exact type")
        if type(route) is not _LocalBM25AttemptPoolReaperRoute:
            raise TypeError("BM25 attempt-pool reaper cleanup route is invalid")
        self._route = route
        self._lease: PrivateDirectoryLeaseOwner | None = None
        self._reclaimer: QuiescentDirectoryReclaimer | None = None

    def _install_lease(self, lease: PrivateDirectoryLeaseOwner) -> None:
        if type(lease) is not PrivateDirectoryLeaseOwner:
            raise TypeError("BM25 attempt-pool reaper lease has an invalid type")
        if self._lease is not None:
            raise RuntimeError("BM25 attempt-pool reaper lease is already installed")
        if (
            lease.mode is not DirectoryLeaseMode.EXCLUSIVE
            or lease.path != self._route._shard_path
            or lease.identity != self._route._shard_identity
        ):
            raise StorageIntegrityError(
                "BM25 attempt-pool reaper lease authority is inconsistent"
            )
        self._lease = lease

    def _acquire(self) -> None:
        if self._lease is not None:
            raise RuntimeError("BM25 attempt-pool reaper lease is already acquired")
        lease = self._route._acquire(
            blocking=True,
            check_cancelled=None,
            construction_owner=self._install_lease,
        )
        if self._lease is not lease:
            raise StorageIntegrityError(
                "BM25 attempt-pool reaper lease handoff is inconsistent"
            )

    def _install_reclaimer(self, reclaimer: QuiescentDirectoryReclaimer) -> None:
        if type(reclaimer) is not QuiescentDirectoryReclaimer:
            raise TypeError("BM25 attempt-pool reclaimer has an invalid type")
        if self._reclaimer is not None:
            raise RuntimeError("BM25 attempt-pool reclaimer is already installed")
        self._reclaimer = reclaimer

    def _open_reclaimer(self) -> QuiescentDirectoryReclaimer:
        lease = self._lease
        if lease is None or lease.closed:
            raise StorageIntegrityError(
                "BM25 attempt-pool reclaimer requires an active exclusive lease"
            )
        reclaimer = QuiescentDirectoryReclaimer(
            self._route._shard_path,
            expected_parent_identity=self._route._shard_identity,
            _construction_owner=self._install_reclaimer,
        )
        if self._reclaimer is not reclaimer:
            raise StorageIntegrityError(
                "BM25 attempt-pool reclaimer handoff is inconsistent"
            )
        return reclaimer

    @property
    def closed(self) -> bool:
        lease = self._lease
        reclaimer = self._reclaimer
        return bool(
            (lease is None or lease.closed) and (reclaimer is None or reclaimer.closed)
        )

    def close(self) -> None:
        lease = self._lease
        if lease is None:
            return
        reclaimer = self._reclaimer
        if lease.closed and reclaimer is not None and not reclaimer.closed:
            raise StorageIntegrityError(
                "BM25 attempt-pool EX lease closed before its reclaimer"
            )
        primary_error: BaseException | None = None
        if reclaimer is not None and not reclaimer.closed:
            try:
                reclaimer.close()
            except BaseException as error:  # noqa: B036 - inspect exact settlement
                if not reclaimer.closed:
                    raise
                primary_error = error
        if reclaimer is not None and not reclaimer.closed:
            raise StorageIntegrityError(
                "BM25 attempt-pool reclaimer remained active under its EX lease"
            )
        try:
            self._route._verify()
        except BaseException as error:  # noqa: B036 - unlock after validation fault
            if primary_error is None:
                primary_error = error
            else:
                _annotate_secondary_error(
                    primary_error,
                    "BM25 attempt-pool route validation also failed",
                    error,
                )
        try:
            lease.close()
        except BaseException as cleanup_error:  # noqa: B036 - preserve validation
            if primary_error is not None:
                raise primary_error from cleanup_error
            raise
        if primary_error is not None:
            raise primary_error
        if not self.closed:
            raise RuntimeError("BM25 attempt-pool reaper cleanup did not settle")


@dataclass(frozen=True, slots=True)
class LocalBM25AttemptPoolCoordinator:
    """Apply exact BM25 name policy under a caller-asserted quiescent boundary."""

    target: LocalBM25SourceJobTarget
    reaper_route: _LocalBM25AttemptPoolReaperRoute | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _legacy_workspace: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self) is not LocalBM25AttemptPoolCoordinator:
            raise TypeError("local BM25 attempt-pool coordinator must use exact type")
        if type(self.target) is not LocalBM25SourceJobTarget:
            raise TypeError(
                "local BM25 attempt-pool coordinator requires an exact target"
            )
        if (
            self.reaper_route is not None
            and type(self.reaper_route) is not _LocalBM25AttemptPoolReaperRoute
        ):
            raise TypeError("local BM25 attempt-pool reaper route is invalid")
        if type(self._legacy_workspace) is not bool:
            raise TypeError("local BM25 legacy-pool selection must be exact bool")
        if self.reaper_route is not None and self._legacy_workspace:
            raise StorageValidationError(
                "leased and legacy BM25 attempt-pool routes are mutually exclusive"
            )

    def _require_target(
        self,
    ) -> tuple[
        Path,
        tuple[int, ...],
        _BM25AttemptPoolReaperCleanupOwner | None,
    ]:
        target = self.target
        if target.topology_verifier is None:
            raise StorageValidationError(
                "BM25 attempt-pool reclamation requires retained topology"
            )
        reaper_route = self.reaper_route
        writer_route = target.attempt_pool_writer_route
        if reaper_route is None and writer_route is not None:
            if not self._legacy_workspace:
                raise StorageValidationError(
                    "routed BM25 attempt-pool reclamation requires its reaper route"
                )
        elif self._legacy_workspace:
            raise StorageValidationError(
                "legacy BM25 attempt-pool selection requires a routed target"
            )
        if reaper_route is None:
            attempt_pool_root = target.workspace_root
            identity = target.workspace_parent_identity
        else:
            if type(writer_route) is not _LocalBM25AttemptPoolWriterRoute:
                raise StorageValidationError(
                    "leased BM25 attempt-pool reclamation requires its writer route"
                )
            writer_route._verify()
            reaper_route._verify()
            if (
                writer_route._route_token is not reaper_route._route_token
                or writer_route._repository_id != reaper_route._repository_id
                or writer_route._shard_path != reaper_route._shard_path
                or writer_route._shard_identity != reaper_route._shard_identity
            ):
                raise StorageValidationError(
                    "BM25 attempt-pool writer and reaper routes are inconsistent"
                )
            attempt_pool_root = reaper_route._shard_path
            identity = reaper_route._shard_identity
        if (
            reaper_route is None
            and target.attempt_pool_writer_route is None
            and target.attempt_pool_root != target.workspace_root
        ):
            raise StorageValidationError(
                "BM25 attempt-pool reclamation target changed its workspace root"
            )
        if (
            type(identity) is not tuple
            or len(identity) != 4
            or any(type(value) is not int for value in identity)
        ):
            raise StorageValidationError(
                "BM25 attempt-pool reclamation requires an exact parent identity"
            )
        cleanup = (
            None
            if reaper_route is None
            else _BM25AttemptPoolReaperCleanupOwner(reaper_route)
        )
        return attempt_pool_root, identity, cleanup

    def reclaim(
        self,
        *,
        caller_asserts_quiescence: bool = False,
    ) -> BM25AttemptPoolReclamation:
        """Reclaim recognized stale attempts after an exact caller assertion."""

        if type(caller_asserts_quiescence) is not bool:
            raise TypeError("BM25 attempt-pool quiescence assertion must be exact bool")
        if caller_asserts_quiescence is not True:
            raise StorageValidationError(
                "BM25 attempt-pool reclamation requires caller-asserted quiescence"
            )
        attempt_pool_root, identity, reaper_cleanup = self._require_target()
        target = self.target

        def verify_topology() -> None:
            target.verify_topology()
            if self.reaper_route is not None:
                self.reaper_route._verify()

        def run_with_topology(label: str, callback):
            verify_topology()
            return _run_callback_with_post_validations(
                callback,
                (
                    (
                        f"BM25 attempt-pool {label} topology validation also failed",
                        verify_topology,
                    ),
                ),
            )

        def sweep() -> BM25AttemptPoolReclamation:
            if reaper_cleanup is None:
                reclaimer = QuiescentDirectoryReclaimer(
                    attempt_pool_root,
                    expected_parent_identity=identity,
                )
            else:
                reclaimer = reaper_cleanup._open_reclaimer()
            with reclaimer:
                child_names = run_with_topology(
                    "snapshot",
                    reclaimer.snapshot_child_names,
                )

                classified = tuple(
                    (name, _classify_bm25_attempt_pool_child_name(name))
                    for name in child_names
                )
                candidates = tuple(
                    (name, child) for name, child in classified if child is not None
                )
                retained = tuple(name for name, child in classified if child is None)

                for name, child in candidates:
                    if child.discarded:
                        reclaim = reclaimer.reclaim_quarantined_child
                    else:
                        reclaim = reclaimer.reclaim_child
                    reclaimed = run_with_topology(
                        "child reclamation",
                        lambda name=name, reclaim=reclaim: reclaim(name),
                    )
                    if reclaimed is not True:
                        raise StorageIntegrityError(
                            "BM25 attempt-pool child disappeared despite quiescence"
                        )

                final_names = run_with_topology(
                    "final snapshot",
                    reclaimer.snapshot_child_names,
                )
                if final_names != retained:
                    raise StorageIntegrityError(
                        "BM25 attempt pool changed during quiescent reclamation"
                    )

            current_children = sum(
                child.lineage == "current" for _name, child in candidates
            )
            legacy_children = len(candidates) - current_children
            discarded_children = sum(child.discarded for _name, child in candidates)
            return BM25AttemptPoolReclamation(
                scanned_children=len(child_names),
                reclaimed_children=len(candidates),
                current_children=current_children,
                legacy_children=legacy_children,
                discarded_children=discarded_children,
                retained_unrelated_children=len(retained),
            )

        if reaper_cleanup is None:
            return run_with_topology(
                "reclaimer lifetime",
                sweep,
            )
        cleanup_action = _OrderedAction(
            label="BM25 attempt-pool leased reclaimer cleanup also failed",
            action=reaper_cleanup.close,
            complete=lambda: reaper_cleanup.closed,
            retry_incomplete="cancellation",
            incomplete_owner=reaper_cleanup,
        )
        with _run_context_with_cleanup_actions((cleanup_action,)):
            reaper_cleanup._acquire()
            return run_with_topology(
                "reclaimer lifetime",
                sweep,
            )


@dataclass(slots=True)
class _AttemptWorkspaceCleanupOwner:
    """Close a receipt and optionally isolate its exact published tree."""

    owner: PublishedWorkspaceReceiptOwner
    destination: Path | None
    label: str
    job_label: str = "Compiler-cache job"
    isolate_destination: bool = True
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
                destination = self.destination
                if destination is None:
                    raise StorageIntegrityError(
                        f"{self.job_label} workspace destination is not installed"
                    )
                binding = self.owner.destination_binding
                if binding.destination != destination:
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
        if self._ownership is not None and self.isolate_destination:
            destination = self.destination
            if destination is None:  # pragma: no cover - active state checked above
                raise AssertionError("workspace cleanup destination is absent")
            orphan = discard_owned_directory(destination, self._ownership)
            self._record_orphan(orphan, label=self.label)
        self._closed = True


@dataclass(slots=True)
class _BM25AttemptRootCleanupOwner:
    """Deliver one outer attempt receipt after every child receipt closes."""

    child_owners: tuple[_AttemptWorkspaceCleanupOwner, ...]
    accept_orphan: Callable[[DirectoryOrphan], None]
    owner: OwnedPathBuildDirectory | None = field(default=None, init=False)
    orphan: DirectoryOrphan | None = field(default=None, init=False)
    accepted: bool = field(default=False, init=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def closed(self) -> bool:
        return self._closed

    def install(self, owner: OwnedPathBuildDirectory) -> None:
        if type(owner) is not OwnedPathBuildDirectory:
            raise TypeError("BM25 attempt root requires an exact owned path")
        if self.owner is not None:
            raise RuntimeError("BM25 attempt root owner is already installed")
        self.owner = owner

    def close(self) -> None:
        if self._closed:
            return
        owner = self.owner
        if owner is None:
            self._closed = True
            return
        if any(not child.closed for child in self.child_owners):
            raise StorageIntegrityError(
                "BM25 source job child receipts remain active before root isolation"
            )
        orphan = self.orphan
        if orphan is None:
            orphan = owner.isolate()
            self.orphan = orphan
        if not self.accepted:
            self.accept_orphan(orphan)
            self.accepted = True
        owner.close()
        if not owner.closed:
            raise RuntimeError("BM25 source job attempt root did not close")
        self._closed = True


@final
class _BM25AttemptPoolWriterCleanupOwner:
    """Settle one routed attempt completely before releasing its shared lease."""

    __slots__ = (
        "_accept_orphan",
        "_admitted",
        "_attempt_root",
        "_child_owners",
        "_lease",
        "_retention_group",
        "_route",
    )

    def __init__(
        self,
        *,
        route: _LocalBM25AttemptPoolWriterRoute,
        retention_group: object,
        child_owners: tuple[_AttemptWorkspaceCleanupOwner, ...],
        attempt_root: _BM25AttemptRootCleanupOwner,
        accept_orphan: Callable[[DirectoryOrphan], None],
    ) -> None:
        if type(self) is not _BM25AttemptPoolWriterCleanupOwner:
            raise TypeError("BM25 attempt-pool writer cleanup must use the exact type")
        if type(route) is not _LocalBM25AttemptPoolWriterRoute:
            raise TypeError("BM25 attempt-pool writer cleanup route is invalid")
        if type(child_owners) is not tuple or any(
            type(owner) is not _AttemptWorkspaceCleanupOwner for owner in child_owners
        ):
            raise TypeError("BM25 attempt-pool child cleanup owners are invalid")
        if type(attempt_root) is not _BM25AttemptRootCleanupOwner:
            raise TypeError("BM25 attempt-pool root cleanup owner is invalid")
        if not callable(accept_orphan):
            raise TypeError("BM25 attempt-pool orphan sink is invalid")
        self._route = route
        self._retention_group = retention_group
        self._child_owners = child_owners
        self._attempt_root = attempt_root
        self._accept_orphan = accept_orphan
        self._lease: PrivateDirectoryLeaseOwner | None = None
        self._admitted = False

    def _install_lease(self, lease: PrivateDirectoryLeaseOwner) -> None:
        if type(lease) is not PrivateDirectoryLeaseOwner:
            raise TypeError("BM25 attempt-pool writer lease has an invalid type")
        if self._lease is not None:
            raise RuntimeError("BM25 attempt-pool writer lease is already installed")
        if (
            lease.mode is not DirectoryLeaseMode.SHARED
            or lease.path != self._route._shard_path
            or lease.identity != self._route._shard_identity
        ):
            raise StorageIntegrityError(
                "BM25 attempt-pool writer lease authority is inconsistent"
            )
        self._lease = lease

    def _acquire(self, check_cancelled: Callable[[], None]) -> None:
        if not callable(check_cancelled):
            raise TypeError("BM25 attempt-pool writer stop check must be callable")
        if self._lease is not None:
            raise RuntimeError("BM25 attempt-pool writer lease is already acquired")
        lease = self._route._acquire(
            check_cancelled=check_cancelled,
            construction_owner=self._install_lease,
        )
        if self._lease is not lease:
            raise StorageIntegrityError(
                "BM25 attempt-pool writer lease handoff is inconsistent"
            )
        self._admitted = True

    @property
    def closed(self) -> bool:
        lease = self._lease
        if lease is None:
            return True
        return bool(
            lease.closed
            and all(owner.closed for owner in self._child_owners)
            and self._attempt_root.closed
        )

    def _settle_attempt(self) -> None:
        for child in self._child_owners:
            if not child.closed:
                child.close()
        if self._attempt_root.owner is None:
            _retry_retained_owned_path_build_cleanup_for_group(
                self._retention_group,
                self._accept_orphan,
            )
        if not self._attempt_root.closed:
            self._attempt_root.close()
        if any(not child.closed for child in self._child_owners):
            raise StorageIntegrityError(
                "BM25 source job child cleanup remained active under its writer lease"
            )
        if not self._attempt_root.closed:
            raise StorageIntegrityError(
                "BM25 source job attempt root remained active under its writer lease"
            )

    def _settle_unstarted_attempt(self) -> None:
        for child in self._child_owners:
            if not child.closed:
                child.close()
        if not self._attempt_root.closed:
            self._attempt_root.close()

    def close(self) -> None:
        lease = self._lease
        if lease is None:
            return
        if not self._admitted:
            # The route publishes the live lease before returning it.  A
            # cancellation at that return handoff therefore owns no attempt
            # yet and must not enter the process-global retained-build retry
            # boundary merely to release SH.
            self._settle_unstarted_attempt()
        elif lease.closed:
            if not self.closed:
                raise StorageIntegrityError(
                    "BM25 attempt-pool writer lease closed before attempt cleanup"
                )
            return
        else:
            self._settle_attempt()
        if lease.closed:
            if not self.closed:
                raise StorageIntegrityError(
                    "BM25 attempt-pool writer lease closed before attempt cleanup"
                )
            return
        validation_error: BaseException | None = None
        try:
            self._route._verify()
        except BaseException as error:  # noqa: B036 - unlock after validation fault
            validation_error = error
        try:
            lease.close()
        except BaseException as cleanup_error:  # noqa: B036 - preserve validation
            if validation_error is not None:
                raise validation_error from cleanup_error
            raise
        if validation_error is not None:
            raise validation_error
        if not lease.closed:
            raise RuntimeError("BM25 attempt-pool writer lease did not close")


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
        prefix = f"codenib-source-job-{nonce}"
        attempt_owner = PublishedWorkspaceReceiptOwner()
        view_owner = PublishedWorkspaceReceiptOwner()
        context_owner = PublishedWorkspaceReceiptOwner()
        attempt_cleanup = _AttemptWorkspaceCleanupOwner(
            attempt_owner,
            None,
            "source attempt",
            job_label="BM25 source job",
            isolate_destination=False,
        )
        view_cleanup = _AttemptWorkspaceCleanupOwner(
            view_owner,
            None,
            "source BM25",
            job_label="BM25 source job",
            isolate_destination=False,
        )
        context_cleanup = _AttemptWorkspaceCleanupOwner(
            context_owner,
            None,
            "source context",
            job_label="BM25 source job",
            isolate_destination=False,
        )
        child_cleanups = (context_cleanup, view_cleanup, attempt_cleanup)
        attempt_root_cleanup = _BM25AttemptRootCleanupOwner(
            child_cleanups,
            target.accept_attempt_orphan,
        )
        source_owner = SourceBindingCleanupOwner()
        writer_route = target.attempt_pool_writer_route
        retention_group = object()
        writer_cleanup = (
            None
            if writer_route is None
            else _BM25AttemptPoolWriterCleanupOwner(
                route=writer_route,
                retention_group=retention_group,
                child_owners=child_cleanups,
                attempt_root=attempt_root_cleanup,
                accept_orphan=target.accept_attempt_orphan,
            )
        )
        source_cleanup_action = _OrderedAction(
            label="BM25 source job source cleanup also failed",
            action=source_owner.close,
            complete=lambda: source_owner.closed,
            retry_incomplete="cancellation",
            incomplete_owner=source_owner,
        )
        if writer_cleanup is None:
            cleanup_owners = (
                *child_cleanups,
                attempt_root_cleanup,
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
                for cleanup in child_cleanups
            ) + (
                _OrderedAction(
                    label="BM25 source job attempt root cleanup also failed",
                    action=attempt_root_cleanup.close,
                    complete=lambda: attempt_root_cleanup.closed,
                    retry_incomplete="cancellation",
                    incomplete_owner=attempt_root_cleanup,
                ),
                source_cleanup_action,
            )
        else:
            cleanup_owners = (writer_cleanup, source_owner)
            cleanup_actions = (
                _OrderedAction(
                    label="BM25 source job leased attempt cleanup also failed",
                    action=writer_cleanup.close,
                    complete=lambda: writer_cleanup.closed,
                    retry_incomplete="cancellation",
                    incomplete_owner=writer_cleanup,
                ),
                source_cleanup_action,
            )

        try:
            with _run_context_with_cleanup_actions(cleanup_actions):
                check_cancelled()
                display_commit = target.current_display_commit()
                repository_source = target.capture_source(
                    source_owner=source_owner,
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
                repository_source.verify_snapshot(check_cancelled=check_cancelled)
                if target.current_display_commit() != display_commit:
                    raise StorageValidationError(
                        "BM25 source job Git HEAD changed during source capture"
                    )
                check_cancelled()
                target.verify_topology()
                if writer_cleanup is None:
                    attempt_pool_root = target.attempt_pool_root
                    attempt_pool_identity = target.workspace_parent_identity
                    retained_group = None
                else:
                    writer_cleanup._acquire(check_cancelled)
                    attempt_pool_root = writer_route._shard_path
                    attempt_pool_identity = writer_route._shard_identity
                    retained_group = retention_group
                attempt_root = OwnedPathBuildDirectory.prepare(
                    attempt_pool_root / prefix,
                    expected_parent_identity=attempt_pool_identity,
                    _retention_group=retained_group,
                )
                attempt_root_cleanup.install(attempt_root)
                target.verify_topology()
                attempt_root_identity = directory_ownership_root_identity(
                    attempt_root.capture_ownership()
                )
                nested_provider = LocalWorkspaceProvider(
                    attempt_root.path,
                    provision_timeout_ns=target.workspace_provider.provision_timeout_ns,
                )
                attempt_destination = attempt_root.path / "attempt"
                view_destination = attempt_root.path / "bm25"
                context_destination = attempt_root.path / "context"
                attempt_cleanup.destination = attempt_destination
                view_cleanup.destination = view_destination
                context_cleanup.destination = context_destination
                workspace_provider = _RetainedBM25WorkspaceProvider(
                    delegate=nested_provider,
                    parent_identity=attempt_root_identity,
                    topology_verifier=target.topology_verifier,
                )
                with attempt_root.path_operation("BM25 source job execution"):
                    check_cancelled()
                    yield BM25SourceJobExecutor(
                        attempt_generation=attempt_destination,
                        display_commit=display_commit,
                        builder=target._builder.builder(),
                        attempt_output_owner=attempt_owner,
                        attempt_workspace_provider=nested_provider,
                        repository_source=repository_source,
                        view_output_owner=view_owner,
                        context_output_owner=context_owner,
                        view_destination=view_destination,
                        context_destination=context_destination,
                        workspace_provider=workspace_provider,
                        repository_key=target.repository_key,
                        object_store=object_store,
                        namespace_name=target.namespace_name,
                        environ=target.environ,
                        attempt_parent_identity=attempt_root_identity,
                        attempt_topology_verifier=target.topology_verifier,
                    )
                    check_cancelled()
                    repository_source.verify_snapshot(check_cancelled=check_cancelled)
                    if target.current_display_commit() != display_commit:
                        raise StorageValidationError(
                            "BM25 source job Git HEAD changed before publication"
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
    "BM25AttemptPoolReclamation",
    "LocalBM25AttemptPoolCoordinator",
    "LocalBM25SourceJobResourceFactory",
    "LocalBM25SourceJobTarget",
    "LocalCompilerCacheJobResourceFactory",
    "LocalCompilerCacheJobTarget",
]
