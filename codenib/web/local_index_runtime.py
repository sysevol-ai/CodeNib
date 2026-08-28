# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Production composition for the explicit local Web indexing service."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from threading import get_ident
from time import monotonic
from types import MappingProxyType
from typing import Callable, Iterator, Mapping

from .._atomic_directory import (
    DirectoryOrphan,
    QuiescentDirectoryReclaimer,
    _OrderedAction,
    _run_context_with_cleanup_actions,
)
from .._local_workspace_provider import LocalWorkspaceProvider
from .._owned_file_publication import _CancellationSafeRLock
from ..compiler.cache_lock import compiler_cache_lock
from ..compiler.checkout_identity import checkout_commit
from ..compiler.index_builders import BM25IndexBuilder
from ..compiler.job_resolver import BM25SourceJobResolver
from ..compiler.job_resources import (
    LocalBM25AttemptPoolCoordinator,
    LocalBM25SourceJobResourceFactory,
    LocalBM25SourceJobTarget,
)
from ..languages import normalize_chunker_language
from ..log_utils import get_logger
from ..repository_source_selection import RepositorySourceSelection
from ..storage import (
    IndexJobWorker,
    IndexJobWorkerRunResult,
    IndexJobWorkerScheduler,
    LocalCAS,
    StorageIntegrityError,
)
from .config import LocalIndexStorageConfig, LocalIndexStorageRepository
from .index_job_activation import (
    CatalogIndexJobRuntimeReconciler,
    IndexJobActivationError,
)
from .index_job_planning import LocalBM25SourceJobPlanner
from .index_job_runtime import IndexJobRuntimeReconciliationLoop
from .index_job_service import IndexJobBackgroundService
from .index_job_writes import CatalogIndexJobWriter, IndexJobWriter
from .index_jobs import CatalogIndexJobReader, IndexJobReader, IndexJobRepoBinding
from .index_status import IndexUpdateCapability
from .local_index_service import (
    LocalIndexServiceError,
    LocalIndexStorageTopology,
    LocalIndexStorageTopologyOwner,
)
from .repo_registry import RepoBundle, RepoRegistry
from .retained_bm25_activation import (
    LocalRetainedBm25RuntimeTarget,
    LocalRetainedBm25SnapshotLoader,
    RepoRegistryIndexJobRuntimePublisher,
)

_MISSING_RESOURCE = object()
_LOCAL_RUNTIME_TOKEN = object()
_FULL_GIT_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_MAX_TARGET_LANGUAGES = 64
_MAX_PENDING_ATTEMPT_ORPHANS = 4_096
_MIN_ATTEMPT_POOL_LEASE_WAIT_SECONDS = 0.1

logger = get_logger(__name__)


class _LocalRuntimeResourceOwner:
    """Keep one acquired service resource reachable until cleanup settles."""

    __slots__ = ("_resource",)

    def __init__(self) -> None:
        self._resource: object = _MISSING_RESOURCE

    @property
    def closed(self) -> bool:
        return self._resource is _MISSING_RESOURCE

    def acquire(self, factory):
        if self._resource is not _MISSING_RESOURCE:
            raise RuntimeError("local index runtime resource is already acquired")
        # Keep the owner reachable before construction. A direct attribute store
        # minimizes the native-return handoff window in the same way as the
        # retained catalog and CLI resource owners.
        self._resource = factory()
        return self._resource

    def close(self) -> None:
        resource = self._resource
        if resource is _MISSING_RESOURCE:
            return
        resource.close()  # type: ignore[attr-defined]
        self._resource = _MISSING_RESOURCE


class _LocalAttemptPoolLeaseOwner:
    """Retain the cooperative worker/reaper lease across background threads."""

    __slots__ = (
        "_context",
        "_entered",
        "_owner_pid",
        "_owner_thread_id",
        "_topology_verifier",
    )

    def __init__(self) -> None:
        self._context: object = _MISSING_RESOURCE
        self._entered = False
        self._owner_pid: int | None = None
        self._owner_thread_id: int | None = None
        self._topology_verifier: Callable[[], None] | None = None

    @property
    def closed(self) -> bool:
        return self._context is _MISSING_RESOURCE

    def acquire(
        self,
        root: Path,
        *,
        wait_timeout_ms: int,
        topology_verifier: Callable[[], None],
    ) -> None:
        if self._context is not _MISSING_RESOURCE:
            raise RuntimeError("local attempt-pool lease is already acquired")
        if type(root) is not type(Path()):
            raise TypeError("local attempt-pool lease root must be an exact Path")
        if type(wait_timeout_ms) is not int or wait_timeout_ms < 0:
            raise TypeError("local attempt-pool lease timeout is invalid")
        if not callable(topology_verifier):
            raise TypeError("local attempt-pool topology verifier is invalid")

        deadline = monotonic() + max(
            _MIN_ATTEMPT_POOL_LEASE_WAIT_SECONDS,
            wait_timeout_ms / 1_000,
        )

        def check_wait() -> None:
            if monotonic() >= deadline:
                raise LocalIndexServiceError(
                    "local worker workspace is already owned by another process"
                )

        # Reject topology drift before publishing an unentered generator into
        # cleanup ownership. Later checks are protected by the retained owner.
        topology_verifier()
        context = compiler_cache_lock(
            root,
            create=True,
            check_cancelled=check_wait,
        )
        # Publish the context before entering it. If cancellation lands after
        # its generator yields but before this method returns, close() can
        # still drive the retained generator through its finally block.
        self._context = context
        self._owner_pid = os.getpid()
        self._owner_thread_id = get_ident()
        self._topology_verifier = topology_verifier
        try:
            context.__enter__()
            self._entered = True
            topology_verifier()
        except LocalIndexServiceError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise LocalIndexServiceError(
                "local worker workspace lease could not be acquired"
            ) from exc

    def require_held(self) -> None:
        verifier = self._topology_verifier
        if self._context is _MISSING_RESOURCE or not self._entered or verifier is None:
            raise StorageIntegrityError("local attempt-pool lease is not held")
        if self._owner_pid != os.getpid():
            raise StorageIntegrityError("local attempt-pool lease crossed a process")
        verifier()

    def _release(self) -> None:
        context = self._context
        if context is _MISSING_RESOURCE:
            return
        if self._owner_pid != os.getpid():
            raise StorageIntegrityError("local attempt-pool lease crossed a process")
        if self._owner_thread_id != get_ident():
            raise StorageIntegrityError("local attempt-pool lease crossed a thread")
        context.__exit__(None, None, None)  # type: ignore[attr-defined]
        self._context = _MISSING_RESOURCE
        self._entered = False
        self._owner_pid = None
        self._owner_thread_id = None
        self._topology_verifier = None

    def close(self) -> None:
        if self._context is _MISSING_RESOURCE:
            return
        verifier = self._topology_verifier
        cleanup_actions = (
            *(
                (
                    (
                        "local attempt-pool pre-unlock topology validation also "
                        "failed",
                        verifier,
                    ),
                )
                if verifier is not None and self._entered
                else ()
            ),
            _OrderedAction(
                label="local attempt-pool lease release also failed",
                action=self._release,
                complete=lambda: self.closed,
                retry_incomplete="cancellation",
                incomplete_owner=self,
            ),
        )
        with _run_context_with_cleanup_actions(cleanup_actions):
            pass


class _LocalBM25AttemptPoolReaper:
    """Retain exact attempt receipts and reclaim only while the lease is held."""

    __slots__ = ("_lease", "_lock", "_orphans", "_state", "_target")

    def __init__(self, lease: _LocalAttemptPoolLeaseOwner) -> None:
        self._lease = lease
        self._lock = _CancellationSafeRLock()
        self._orphans: list[DirectoryOrphan] = []
        self._state = "open"
        self._target: LocalBM25SourceJobTarget | None = None

    @property
    def closed(self) -> bool:
        return self._lock.run(lambda: self._state == "closed")

    def install(self, target: LocalBM25SourceJobTarget) -> None:
        if type(target) is not LocalBM25SourceJobTarget:
            raise TypeError("local attempt-pool reaper target is invalid")

        def commit() -> None:
            if self._state != "open" or self._target is not None:
                raise RuntimeError("local attempt-pool reaper is already configured")
            self._target = target

        self._lock.run(commit)

    def accept(self, orphan: DirectoryOrphan) -> None:
        """Accept an authenticated receipt at least once without reentrancy."""

        if type(orphan) is not DirectoryOrphan:
            raise TypeError("local attempt-pool orphan receipt is invalid")
        self._lease.require_held()

        def commit() -> None:
            target = self._target
            if self._state != "open" or target is None:
                raise StorageIntegrityError("local attempt-pool reaper is unavailable")
            if (
                orphan.path.parent != target.attempt_pool_root
                or orphan.parent_identity != target.attempt_pool_identity
            ):
                raise StorageIntegrityError(
                    "local attempt-pool receipt uses another authority"
                )
            if any(candidate == orphan for candidate in self._orphans):
                return
            if len(self._orphans) >= _MAX_PENDING_ATTEMPT_ORPHANS:
                raise StorageIntegrityError(
                    "local attempt-pool receipt queue exceeds its bound"
                )
            self._orphans.append(orphan)

        self._lock.run(commit)

    def _snapshot(self) -> tuple[LocalBM25SourceJobTarget, tuple[DirectoryOrphan, ...]]:
        def read():
            target = self._target
            if self._state not in {"open", "closing"} or target is None:
                raise StorageIntegrityError("local attempt-pool reaper is unavailable")
            return target, tuple(self._orphans)

        return self._lock.run(read)

    def _forget(self, reclaimed: tuple[DirectoryOrphan, ...]) -> None:
        if not reclaimed:
            return

        def commit() -> None:
            self._orphans[:] = [
                candidate
                for candidate in self._orphans
                if not any(candidate == receipt for receipt in reclaimed)
            ]

        self._lock.run(commit)

    def reclaim_pending(self) -> None:
        """Reclaim receipts at the scheduler's between-attempt boundary."""

        target, receipts = self._snapshot()
        if not receipts:
            return
        identity = target.attempt_pool_identity
        if type(identity) is not tuple or len(identity) != 4:
            raise StorageIntegrityError(
                "local attempt-pool target has no retained parent identity"
            )
        self._lease.require_held()
        target.verify_topology()
        with QuiescentDirectoryReclaimer(
            target.attempt_pool_root,
            expected_parent_identity=identity,
        ) as reclaimer:
            for orphan in receipts:
                target.verify_topology()
                reclaimed = reclaimer.reclaim_orphan(orphan)
                if reclaimed is not True:  # pragma: no cover - exact API invariant
                    raise StorageIntegrityError(
                        "local attempt-pool receipt did not reclaim"
                    )
                target.verify_topology()
        self._lease.require_held()
        self._forget(receipts)

    def reclaim_stale(self) -> None:
        """Sweep recognized crash leftovers at an explicit quiescent boundary."""

        target, _receipts = self._snapshot()
        self._lease.require_held()
        LocalBM25AttemptPoolCoordinator(target).reclaim(
            caller_asserts_quiescence=True,
        )
        self._lease.require_held()

    def close(self) -> None:
        def begin() -> bool:
            if self._state == "closed":
                return False
            if self._target is None:
                if self._orphans:
                    raise StorageIntegrityError(
                        "unconfigured local attempt-pool reaper retained receipts"
                    )
                self._state = "closed"
                return False
            if self._state == "open":
                self._state = "closing"
            return True

        if not self._lock.run(begin):
            return
        self.reclaim_pending()
        self.reclaim_stale()

        def finish() -> None:
            self._state = "closed"

        self._lock.run(finish)


class _TopologyBoundWorkspaceProvider:
    """Bind runtime publications to the retained topology's parent inode."""

    __slots__ = ("_expected_parent_identity", "_provider", "_verify")

    def __init__(
        self,
        provider: LocalWorkspaceProvider,
        expected_parent_identity: tuple[int, ...],
        verifier: Callable[[], None],
    ) -> None:
        if type(provider) is not LocalWorkspaceProvider:
            raise TypeError("topology workspace provider requires a local provider")
        if (
            type(expected_parent_identity) is not tuple
            or len(expected_parent_identity) < 2
            or any(type(value) is not int for value in expected_parent_identity)
        ):
            raise TypeError("topology workspace parent identity is invalid")
        if not callable(verifier):
            raise TypeError("topology workspace verifier is invalid")
        self._provider = provider
        self._expected_parent_identity = tuple(expected_parent_identity)
        self._verify = verifier

    @property
    def allowed_root(self) -> Path:
        return self._provider.allowed_root

    def require_support(self) -> None:
        self._verify()
        self._provider.require_support()
        self._verify()

    def run_workspace(
        self,
        request,
        *,
        receipt_owner,
        operation,
        check_cancelled=None,
        _replacement_source=None,
    ):
        cleanup_actions = (
            (
                "local runtime topology exit validation also failed",
                self._verify,
            ),
        )
        with _run_context_with_cleanup_actions(cleanup_actions):
            self._verify()
            kwargs = {
                "receipt_owner": receipt_owner,
                "operation": operation,
                "_expected_parent_identity": self._expected_parent_identity,
            }
            if check_cancelled is not None:
                kwargs["check_cancelled"] = check_cancelled
            if _replacement_source is not None:
                kwargs["_replacement_source"] = _replacement_source
            return self._provider.run_workspace(request, **kwargs)


def _repository_languages(bundle: RepoBundle) -> list[str]:
    raw_languages = tuple(getattr(bundle.manifest, "languages", ()) or ())
    if not raw_languages and bundle.entry.language:
        raw_languages = (bundle.entry.language,)
    if not 1 <= len(raw_languages) <= _MAX_TARGET_LANGUAGES:
        raise LocalIndexServiceError(
            "local index repository requires bounded chunker languages"
        )
    languages: list[str] = []
    for raw_language in raw_languages:
        if type(raw_language) is not str:
            raise LocalIndexServiceError(
                "local index repository language must be exact text"
            )
        language = normalize_chunker_language(raw_language)
        if language is None:
            raise LocalIndexServiceError(
                "local index repository has an unsupported chunker language"
            )
        if language not in languages:
            languages.append(language)
    if not languages:
        raise LocalIndexServiceError(
            "local index repository has no supported chunker language"
        )
    return languages


def _repository_selection(bundle: RepoBundle) -> RepositorySourceSelection:
    selection = getattr(bundle.manifest, "source_selection", None)
    if type(selection) is not RepositorySourceSelection:
        return RepositorySourceSelection()
    return RepositorySourceSelection(selection.exclude_subtrees)


def _repository_head(repository_root: Path) -> str:
    commit = (checkout_commit(repository_root) or "").strip().lower()
    if _FULL_GIT_COMMIT_RE.fullmatch(commit) is None:
        raise LocalIndexServiceError(
            "local index repositories require a full resolved Git HEAD"
        )
    return commit


@dataclass(frozen=True, slots=True)
class _ConfiguredRepository:
    storage: LocalIndexStorageRepository
    repository_root: Path
    display_commit: str
    languages: tuple[str, ...]
    source_selection: RepositorySourceSelection


def _configured_repositories(
    config: LocalIndexStorageConfig,
    registry: RepoRegistry,
) -> tuple[_ConfiguredRepository, ...]:
    if registry.configured_index_types() != ("bm25",):
        raise LocalIndexServiceError(
            "local index runtime currently requires sparse Web mode"
        )
    with registry.pin_all() as active_bundles:
        by_repo: dict[str, RepoBundle] = {}
        for bundle in active_bundles:
            if type(bundle) is not RepoBundle:
                raise LocalIndexServiceError(
                    "repository registry returned an invalid bundle"
                )
            repo_id = bundle.entry.instance_id
            if type(repo_id) is not str or repo_id in by_repo:
                raise LocalIndexServiceError(
                    "repository registry contains invalid local bindings"
                )
            by_repo[repo_id] = bundle
        selected: list[_ConfiguredRepository] = []
        for binding in config.repositories:
            bundle = by_repo.get(binding.repo_id)
            if bundle is None:
                raise LocalIndexServiceError(
                    "configured local index repository is not loaded"
                )
            if bundle.entry.repo != binding.repository_key:
                raise LocalIndexServiceError(
                    "configured local index repository identity differs from its "
                    "loaded Web bundle"
                )
            repository_root = Path(bundle.entry.repo_dir)
            selected.append(
                _ConfiguredRepository(
                    storage=binding,
                    repository_root=repository_root,
                    display_commit=_repository_head(repository_root),
                    languages=tuple(_repository_languages(bundle)),
                    source_selection=_repository_selection(bundle),
                )
            )
        return tuple(selected)


def _shared_topology_guard(topology: LocalIndexStorageTopology) -> None:
    try:
        topology.verify_shared_storage()
    except LocalIndexServiceError as exc:
        raise StorageIntegrityError("local Web index shared storage changed") from exc


def _repository_topology_guard(
    topology: LocalIndexStorageTopology,
    repo_id: str,
) -> None:
    try:
        topology.verify_repository(repo_id)
    except (KeyError, LocalIndexServiceError) as exc:
        raise StorageIntegrityError(
            "local Web index repository topology changed"
        ) from exc


def _verify_catalog_bindings(
    catalog_factory,
    selected: tuple[_ConfiguredRepository, ...],
) -> None:
    """Open the existing catalog and require every configured repository ID."""

    with catalog_factory() as catalog:
        for selected_repo in selected:
            binding = selected_repo.storage
            catalog.read_ref_generation(binding.repository_id, binding.ref_name)


def _safe_runtime_failure(failure: IndexJobActivationError) -> None:
    logger.warning(
        "Durable index runtime reconciliation will retry: %s",
        type(failure).__name__,
    )


class LocalIndexRuntimeService:
    """Own the production local worker and guarded BM25 runtime composition."""

    __slots__ = (
        "_attempt_pool",
        "_attempt_pool_lease_owner",
        "_background",
        "_background_owner",
        "_capabilities",
        "_object_store_owner",
        "_reader",
        "_topology",
        "_topology_owner",
        "_writer",
    )

    def __init__(
        self,
        token: object,
        *,
        topology: LocalIndexStorageTopology,
        topology_owner: LocalIndexStorageTopologyOwner,
        attempt_pool: _LocalBM25AttemptPoolReaper,
        attempt_pool_lease_owner: _LocalAttemptPoolLeaseOwner,
        object_store_owner: _LocalRuntimeResourceOwner,
        background: IndexJobBackgroundService,
        background_owner: _LocalRuntimeResourceOwner,
        reader: CatalogIndexJobReader,
        writer: CatalogIndexJobWriter,
        capabilities: Mapping[str, Mapping[str, IndexUpdateCapability]],
    ) -> None:
        if token is not _LOCAL_RUNTIME_TOKEN:
            raise TypeError("local index runtime service requires acquisition")
        self._topology = topology
        self._topology_owner = topology_owner
        self._attempt_pool = attempt_pool
        self._attempt_pool_lease_owner = attempt_pool_lease_owner
        self._object_store_owner = object_store_owner
        self._background = background
        self._background_owner = background_owner
        self._reader = reader
        self._writer = writer
        self._capabilities = MappingProxyType(
            {
                repo_id: MappingProxyType(dict(repo_capabilities))
                for repo_id, repo_capabilities in capabilities.items()
            }
        )

    @classmethod
    def acquire(
        cls,
        config: LocalIndexStorageConfig,
        registry: RepoRegistry,
    ) -> "LocalIndexRuntimeService":
        """Compose existing storage and loaded repositories without starting."""

        if type(config) is not LocalIndexStorageConfig:
            raise TypeError("local index runtime requires the exact storage config")
        if type(registry) is not RepoRegistry:
            raise TypeError("local index runtime requires an exact RepoRegistry")

        selected = _configured_repositories(config, registry)
        repository_roots = {
            selected_repo.storage.repo_id: selected_repo.repository_root
            for selected_repo in selected
        }
        topology_owner = LocalIndexStorageTopologyOwner()
        attempt_pool_lease_owner = _LocalAttemptPoolLeaseOwner()
        attempt_pool = _LocalBM25AttemptPoolReaper(attempt_pool_lease_owner)
        object_store_owner = _LocalRuntimeResourceOwner()
        background_owner = _LocalRuntimeResourceOwner()
        topology: LocalIndexStorageTopology | None = None
        object_store: LocalCAS | None = None
        background: IndexJobBackgroundService | None = None

        def background_closed() -> bool:
            return (
                background_owner.closed
                if background is None
                else background.state == "closed"
            )

        def close_attempt_pool() -> None:
            if not background_closed():
                raise StorageIntegrityError(
                    "local index background must settle before attempt cleanup"
                )
            attempt_pool.close()

        def close_attempt_pool_lease() -> None:
            if not background_closed() or not attempt_pool.closed:
                raise StorageIntegrityError(
                    "local index attempt cleanup must settle before lease release"
                )
            attempt_pool_lease_owner.close()

        def close_object_store() -> None:
            if not background_closed():
                raise StorageIntegrityError(
                    "local index background must settle before CAS release"
                )
            object_store_owner.close()

        def close_topology() -> None:
            if (
                not background_closed()
                or not attempt_pool.closed
                or not attempt_pool_lease_owner.closed
                or not object_store_owner.closed
            ):
                raise StorageIntegrityError(
                    "local index resources must settle before topology release"
                )
            topology_owner.close()

        cleanup_actions = (
            _OrderedAction(
                label="local index background cleanup also failed",
                action=background_owner.close,
                complete=lambda: (
                    background_owner.closed
                    if background is None
                    else background.state == "closed"
                ),
                retry_incomplete="cancellation",
                incomplete_owner=background_owner,
            ),
            _OrderedAction(
                label="local index attempt-pool cleanup also failed",
                action=close_attempt_pool,
                complete=lambda: attempt_pool.closed,
                retry_incomplete="cancellation",
                incomplete_owner=attempt_pool,
            ),
            _OrderedAction(
                label="local index attempt-pool lease cleanup also failed",
                action=close_attempt_pool_lease,
                complete=lambda: attempt_pool_lease_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=attempt_pool_lease_owner,
            ),
            _OrderedAction(
                label="local index object store cleanup also failed",
                action=close_object_store,
                complete=lambda: object_store_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=object_store_owner,
            ),
            _OrderedAction(
                label="local index topology cleanup also failed",
                action=close_topology,
                complete=lambda: (
                    topology_owner.closed if topology is None else topology.closed
                ),
                retry_incomplete="cancellation",
                incomplete_owner=topology_owner,
            ),
        )
        with _run_context_with_cleanup_actions(
            cleanup_actions,
            cleanup_on_success=False,
        ):
            topology = LocalIndexStorageTopology.acquire(
                config,
                repository_roots,
                owner=topology_owner,
            )
            catalog_factory = topology.catalog_factory
            _verify_catalog_bindings(catalog_factory, selected)
            topology.verify()
            shared_topology_verifier = partial(_shared_topology_guard, topology)
            attempt_pool_lease_owner.acquire(
                config.worker_workspace_root,
                wait_timeout_ms=config.catalog_busy_timeout_ms,
                topology_verifier=shared_topology_verifier,
            )
            topology.verify()
            object_store = object_store_owner.acquire(
                lambda: LocalCAS(
                    config.cas_root,
                    require_preprovisioned=True,
                )
            )
            topology.verify()
            worker_provider = LocalWorkspaceProvider(config.worker_workspace_root)
            runtime_provider = LocalWorkspaceProvider(config.runtime_workspace_root)
            worker_provider.require_support()
            runtime_provider.require_support()
            web_bindings: list[IndexJobRepoBinding] = []
            worker_targets: list[LocalBM25SourceJobTarget] = []
            runtime_targets: list[LocalRetainedBm25RuntimeTarget] = []
            capabilities: dict[str, Mapping[str, IndexUpdateCapability]] = {}
            environment = dict(os.environ)
            for selected_repo in selected:
                storage_binding = selected_repo.storage
                repository_root = selected_repo.repository_root
                web_binding = IndexJobRepoBinding(
                    storage_binding.repo_id,
                    storage_binding.repository_id,
                    storage_binding.ref_name,
                )
                repository_topology_verifier = partial(
                    _repository_topology_guard,
                    topology,
                    storage_binding.repo_id,
                )
                runtime_workspace = _TopologyBoundWorkspaceProvider(
                    runtime_provider,
                    topology.runtime_workspace_identity,
                    repository_topology_verifier,
                )
                worker_target = LocalBM25SourceJobTarget(
                    repository_root=repository_root,
                    workspace_provider=worker_provider,
                    repository_key=storage_binding.repository_key,
                    display_commit=selected_repo.display_commit,
                    builder=BM25IndexBuilder(
                        languages=list(selected_repo.languages),
                        source_selection=selected_repo.source_selection,
                    ),
                    namespace_name=storage_binding.namespace_name,
                    environ=environment,
                    repository_root_authority=topology.repository_authority(
                        storage_binding.repo_id
                    ),
                    display_commit_resolver=lambda root=repository_root: (
                        _repository_head(root)
                    ),
                    workspace_parent_identity=topology.worker_workspace_identity,
                    topology_verifier=repository_topology_verifier,
                    attempt_orphan_sink=attempt_pool.accept,
                )
                web_bindings.append(web_binding)
                worker_targets.append(worker_target)
                runtime_targets.append(
                    LocalRetainedBm25RuntimeTarget(
                        binding=web_binding,
                        repository_key=storage_binding.repository_key,
                        repository_root=repository_root,
                        workspace_root=config.runtime_workspace_root,
                        workspace_provider=runtime_workspace,
                        namespace_name=storage_binding.namespace_name,
                        environ=environment,
                    )
                )
                capabilities[storage_binding.repo_id] = {
                    "bm25": IndexUpdateCapability(
                        mode="rebuild",
                        enabled=True,
                        reason="",
                    )
                }

            bindings = tuple(web_bindings)
            targets = tuple(worker_targets)
            attempt_pool.install(targets[0])
            attempt_pool.reclaim_stale()
            resources = LocalBM25SourceJobResourceFactory(targets)
            topology_repo_ids = {
                selected_repo.storage.repository_id: selected_repo.storage.repo_id
                for selected_repo in selected
            }

            def accepts_candidate(candidate) -> bool:
                if not resources.accepts_candidate(candidate):
                    return False
                repo_id = topology_repo_ids.get(candidate.repository_id)
                if repo_id is None:  # pragma: no cover - target-map invariant
                    raise StorageIntegrityError(
                        "local BM25 candidate has no repository topology"
                    )
                _repository_topology_guard(topology, repo_id)
                return True

            worker = IndexJobWorker(
                catalog_factory=catalog_factory,
                object_store=object_store,
                resolver=BM25SourceJobResolver(
                    resource_factory=resources,
                    object_store=object_store,
                ),
                lease_duration_ms=config.worker.lease_duration_ms,
                heartbeat_interval_ms=config.worker.heartbeat_interval_ms,
                scan_limit=config.worker.scan_limit,
                candidate_filter=accepts_candidate,
            )
            loader = LocalRetainedBm25SnapshotLoader(
                catalog_factory,
                object_store,
                tuple(runtime_targets),
            )
            publisher = RepoRegistryIndexJobRuntimePublisher(
                registry,
                catalog_factory,
                loader,
            )
            reconciler = CatalogIndexJobRuntimeReconciler(
                catalog_factory,
                bindings,
                publisher,
            )

            def on_worker_result(result: IndexJobWorkerRunResult) -> None:
                attempt_pool.reclaim_pending()
                try:
                    reconciler.on_worker_result(result)
                except IndexJobActivationError as failure:
                    _safe_runtime_failure(failure)

            scheduler = IndexJobWorkerScheduler(
                worker=worker,
                initial_idle_delay_ms=config.worker.initial_idle_delay_ms,
                max_idle_delay_ms=config.worker.max_idle_delay_ms,
                on_result=on_worker_result,
            )
            runtime = IndexJobRuntimeReconciliationLoop(
                reconciler,
                poll_interval_ms=config.runtime.poll_interval_ms,
                on_failure=_safe_runtime_failure,
            )
            background = background_owner.acquire(
                lambda: IndexJobBackgroundService(scheduler, runtime)
            )
            planner = LocalBM25SourceJobPlanner(
                catalog_factory,
                targets,
                max_attempts=config.worker.max_attempts,
            )
            reader = CatalogIndexJobReader(catalog_factory, bindings)
            writer = CatalogIndexJobWriter(catalog_factory, bindings, planner)
            topology.verify()
            return cls(
                _LOCAL_RUNTIME_TOKEN,
                topology=topology,
                topology_owner=topology_owner,
                attempt_pool=attempt_pool,
                attempt_pool_lease_owner=attempt_pool_lease_owner,
                object_store_owner=object_store_owner,
                background=background,
                background_owner=background_owner,
                reader=reader,
                writer=writer,
                capabilities=capabilities,
            )

    @property
    def reader(self) -> IndexJobReader:
        return self._reader

    @property
    def writer(self) -> IndexJobWriter:
        return self._writer

    @property
    def state(self) -> str:
        return self._background.state

    @property
    def healthy(self) -> bool:
        return self._background.healthy

    @property
    def closed(self) -> bool:
        return (
            self._background.state == "closed"
            and self._attempt_pool.closed
            and self._attempt_pool_lease_owner.closed
            and self._object_store_owner.closed
            and self._topology.closed
        )

    def capabilities(self, repo_id: str) -> dict[str, IndexUpdateCapability]:
        """Return one detached repository-scoped writer capability snapshot."""

        if type(repo_id) is not str:
            raise TypeError("local index capability repository ID must be exact text")
        capabilities = self._capabilities.get(repo_id)
        if capabilities is None:
            raise KeyError(repo_id)
        return dict(capabilities)

    def start(self) -> None:
        """Start both loops or settle the whole local service before failing."""

        cleanup_actions = self._cleanup_actions()
        with _run_context_with_cleanup_actions(
            cleanup_actions,
            cleanup_on_success=False,
        ):
            self._topology.verify()
            self._background.start()
            self._topology.verify()

    def _cleanup_actions(self) -> tuple[_OrderedAction, ...]:
        def background_closed() -> bool:
            return self._background.state == "closed"

        def close_attempt_pool() -> None:
            if not background_closed():
                raise StorageIntegrityError(
                    "local index background must settle before attempt cleanup"
                )
            self._attempt_pool.close()

        def close_attempt_pool_lease() -> None:
            if not background_closed() or not self._attempt_pool.closed:
                raise StorageIntegrityError(
                    "local index attempt cleanup must settle before lease release"
                )
            self._attempt_pool_lease_owner.close()

        def close_object_store() -> None:
            if not background_closed():
                raise StorageIntegrityError(
                    "local index background must settle before CAS release"
                )
            self._object_store_owner.close()

        def close_topology() -> None:
            if (
                not background_closed()
                or not self._attempt_pool.closed
                or not self._attempt_pool_lease_owner.closed
                or not self._object_store_owner.closed
            ):
                raise StorageIntegrityError(
                    "local index resources must settle before topology release"
                )
            self._topology_owner.close()

        return (
            _OrderedAction(
                label="local index background cleanup also failed",
                action=self._background_owner.close,
                complete=lambda: self._background.state == "closed",
                retry_incomplete="cancellation",
                incomplete_owner=self._background_owner,
            ),
            _OrderedAction(
                label="local index attempt-pool cleanup also failed",
                action=close_attempt_pool,
                complete=lambda: self._attempt_pool.closed,
                retry_incomplete="cancellation",
                incomplete_owner=self._attempt_pool,
            ),
            _OrderedAction(
                label="local index attempt-pool lease cleanup also failed",
                action=close_attempt_pool_lease,
                complete=lambda: self._attempt_pool_lease_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=self._attempt_pool_lease_owner,
            ),
            _OrderedAction(
                label="local index object store cleanup also failed",
                action=close_object_store,
                complete=lambda: self._object_store_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=self._object_store_owner,
            ),
            _OrderedAction(
                label="local index topology cleanup also failed",
                action=close_topology,
                complete=lambda: self._topology.closed,
                retry_incomplete="cancellation",
                incomplete_owner=self._topology_owner,
            ),
        )

    def close(self) -> None:
        """Join loops before releasing the object store and path authorities."""

        with _run_context_with_cleanup_actions(self._cleanup_actions()):
            pass


@contextmanager
def open_local_index_runtime_service(
    config: LocalIndexStorageConfig,
    registry: RepoRegistry,
) -> Iterator[LocalIndexRuntimeService]:
    """Acquire, start, and always settle one production local service."""

    service_owner = _LocalRuntimeResourceOwner()
    service: LocalIndexRuntimeService | None = None
    cleanup_actions = (
        _OrderedAction(
            label="local index runtime service cleanup also failed",
            action=service_owner.close,
            complete=lambda: (
                service_owner.closed if service is None else service.closed
            ),
            retry_incomplete="cancellation",
            incomplete_owner=service_owner,
        ),
    )
    with _run_context_with_cleanup_actions(cleanup_actions):
        service = service_owner.acquire(
            lambda: LocalIndexRuntimeService.acquire(config, registry)
        )
        service.start()
        yield service


__all__ = [
    "LocalIndexRuntimeService",
    "open_local_index_runtime_service",
]
