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
from types import MappingProxyType
from typing import Callable, Iterator, Mapping

from .._atomic_directory import (
    DirectoryOrphan,
    _OrderedAction,
    _run_context_with_cleanup_actions,
)
from .._local_workspace_provider import LocalWorkspaceProvider
from .._owned_file_publication import _CancellationSafeRLock
from ..compiler.bm25_attempt_pool import (
    LocalBM25AttemptPoolBinding,
    bootstrap_local_bm25_attempt_pool,
)
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
from ..source_fingerprint import lexical_repository_path
from ..storage import (
    IndexJobRecord,
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


class _LocalBM25AttemptPoolRegistration:
    """Retain one target's paired routes, receipts, and retryable cleanup."""

    __slots__ = (
        "binding",
        "cleanup_owners",
        "closed",
        "orphans",
        "repository_id",
        "target",
    )

    def __init__(
        self,
        repository_id: str,
        target: LocalBM25SourceJobTarget,
        binding: LocalBM25AttemptPoolBinding,
    ) -> None:
        self.repository_id = repository_id
        self.target = target
        self.binding = binding
        self.orphans: list[DirectoryOrphan] = []
        self.cleanup_owners: list[object] = []
        self.closed = False


class _LocalBM25AttemptPoolReaper:
    """Route exact attempt receipts to independently leased repository shards."""

    __slots__ = ("_by_parent", "_by_repository", "_lock", "_state")

    def __init__(self) -> None:
        self._by_parent: dict[
            tuple[Path, tuple[int, ...]], _LocalBM25AttemptPoolRegistration
        ] = {}
        self._by_repository: dict[str, _LocalBM25AttemptPoolRegistration] = {}
        self._lock = _CancellationSafeRLock()
        self._state = "open"

    @property
    def closed(self) -> bool:
        return self._lock.run(lambda: self._state == "closed")

    def install(
        self,
        repository_id: str,
        target: LocalBM25SourceJobTarget,
        binding: LocalBM25AttemptPoolBinding,
    ) -> None:
        if type(repository_id) is not str or not repository_id:
            raise TypeError("local attempt-pool repository ID is invalid")
        if type(target) is not LocalBM25SourceJobTarget:
            raise TypeError("local attempt-pool reaper target is invalid")
        if type(binding) is not LocalBM25AttemptPoolBinding:
            raise TypeError("local attempt-pool reaper binding is invalid")
        if target.attempt_pool_writer_route is not binding.writer_route:
            raise StorageIntegrityError(
                "local attempt-pool target uses another writer route"
            )
        parent = target.attempt_pool_root
        identity = target.attempt_pool_identity
        if (
            type(parent) is not type(Path())
            or type(identity) is not tuple
            or len(identity) != 4
            or any(type(value) is not int for value in identity)
        ):
            raise StorageIntegrityError(
                "local attempt-pool target has no routed parent authority"
            )
        key = (parent, identity)
        registration = _LocalBM25AttemptPoolRegistration(
            repository_id,
            target,
            binding,
        )

        def commit() -> None:
            if self._state != "open":
                raise RuntimeError("local attempt-pool reaper is not configurable")
            if repository_id in self._by_repository or key in self._by_parent:
                raise RuntimeError("local attempt-pool route is already configured")
            self._by_repository[repository_id] = registration
            self._by_parent[key] = registration

        self._lock.run(commit)

    def accept(self, orphan: DirectoryOrphan) -> None:
        """Accept an authenticated receipt at least once without reentrancy."""

        if type(orphan) is not DirectoryOrphan:
            raise TypeError("local attempt-pool orphan receipt is invalid")
        key = (orphan.path.parent, orphan.parent_identity)

        def commit() -> None:
            registration = self._by_parent.get(key)
            if self._state != "open" or registration is None or registration.closed:
                raise StorageIntegrityError("local attempt-pool reaper is unavailable")
            if any(candidate == orphan for candidate in registration.orphans):
                return
            pending = sum(
                len(candidate.orphans) for candidate in self._by_repository.values()
            )
            if pending >= _MAX_PENDING_ATTEMPT_ORPHANS:
                raise StorageIntegrityError(
                    "local attempt-pool receipt queue exceeds its bound"
                )
            registration.orphans.append(orphan)

        self._lock.run(commit)

    def _registration(
        self,
        repository_id: str,
    ) -> _LocalBM25AttemptPoolRegistration:
        if type(repository_id) is not str:
            raise TypeError("local attempt-pool repository ID must be exact text")

        def read() -> _LocalBM25AttemptPoolRegistration:
            registration = self._by_repository.get(repository_id)
            if (
                self._state not in {"open", "closing"}
                or registration is None
                or registration.closed
            ):
                raise StorageIntegrityError("local attempt-pool reaper is unavailable")
            return registration

        return self._lock.run(read)

    def _forget(
        self,
        registration: _LocalBM25AttemptPoolRegistration,
        reclaimed: tuple[DirectoryOrphan, ...],
    ) -> None:
        if not reclaimed:
            return

        def commit() -> None:
            registration.orphans[:] = [
                candidate
                for candidate in registration.orphans
                if not any(candidate == receipt for receipt in reclaimed)
            ]

        self._lock.run(commit)

    def _retain_cleanup_owners(
        self,
        registration: _LocalBM25AttemptPoolRegistration,
        error: BaseException,
    ) -> None:
        owners = getattr(error, "publication_cleanup_owners", ())
        if type(owners) is not tuple:
            return

        def commit() -> None:
            for owner in owners:
                if not callable(getattr(owner, "close", None)):
                    continue
                if any(candidate is owner for candidate in registration.cleanup_owners):
                    continue
                registration.cleanup_owners.append(owner)

        self._lock.run(commit)

    def _settle_cleanup_owners(
        self,
        registration: _LocalBM25AttemptPoolRegistration,
    ) -> None:
        owners = self._lock.run(lambda: tuple(registration.cleanup_owners))
        if not owners:
            return
        actions = tuple(
            _OrderedAction(
                label="local attempt-pool retained route cleanup also failed",
                action=owner.close,  # type: ignore[attr-defined]
                complete=lambda owner=owner: bool(getattr(owner, "closed", False)),
                retry_incomplete="cancellation",
                incomplete_owner=owner,
            )
            for owner in owners
            if not bool(getattr(owner, "closed", False))
        )
        with _run_context_with_cleanup_actions(actions):
            pass

        def forget_closed() -> None:
            registration.cleanup_owners[:] = [
                owner
                for owner in registration.cleanup_owners
                if not bool(getattr(owner, "closed", False))
            ]

        self._lock.run(forget_closed)

    def _sweep(self, registration: _LocalBM25AttemptPoolRegistration) -> bool:
        self._settle_cleanup_owners(registration)
        try:
            result = LocalBM25AttemptPoolCoordinator(
                registration.target,
                reaper_route=registration.binding.reaper_route,
            ).reclaim(
                caller_asserts_quiescence=True,
                blocking=False,
            )
        except BaseException as error:  # noqa: B036 - retain exclusive route owner
            self._retain_cleanup_owners(registration, error)
            raise
        if result is None:
            logger.debug(
                "Deferred busy local attempt-pool sweep: repository_id=%s",
                registration.repository_id,
            )
            return False
        return True

    def _reclaim_pending_one(self, repository_id: str) -> bool:
        registration = self._registration(repository_id)
        receipts = self._lock.run(lambda: tuple(registration.orphans))
        if not receipts:
            return False
        # The routed coordinator holds LOCK_EX while it classifies and removes
        # the complete bounded shard. Keep the exact receipts queued until that
        # sweep returns, so a failed or interrupted route cleanup remains
        # retryable without falling back to path authority.
        if not self._sweep(registration):
            return True
        self._forget(registration, receipts)
        return True

    def reclaim_pending(self, repository_id: str) -> None:
        """Reclaim receipts only through the completed job's repository route."""

        self._reclaim_pending_one(repository_id)

    def _reclaim_stale_one(self, repository_id: str) -> None:
        self._sweep(self._registration(repository_id))

    def reclaim_stale(self, repository_id: str | None = None) -> None:
        """Sweep one route, or every configured route, under independent leases."""

        if repository_id is not None:
            self._reclaim_stale_one(repository_id)
            return
        repository_ids = self._lock.run(lambda: tuple(sorted(self._by_repository)))
        completed: set[str] = set()

        def reclaim(candidate_id: str) -> None:
            self._reclaim_stale_one(candidate_id)
            completed.add(candidate_id)

        actions = tuple(
            _OrderedAction(
                label=f"local attempt-pool {candidate_id} sweep also failed",
                action=lambda candidate_id=candidate_id: reclaim(candidate_id),
                complete=lambda candidate_id=candidate_id: candidate_id in completed,
                retry_incomplete="cancellation",
                incomplete_owner=self,
            )
            for candidate_id in repository_ids
        )
        with _run_context_with_cleanup_actions(actions):
            pass

    def _close_repository(self, repository_id: str) -> None:
        registration = self._registration(repository_id)
        if not self._reclaim_pending_one(repository_id):
            self._sweep(registration)

        def finish() -> None:
            registration.closed = True

        self._lock.run(finish)

    def close(self) -> None:
        def begin() -> tuple[str, ...]:
            if self._state == "closed":
                return ()
            if self._state == "open":
                self._state = "closing"
            return tuple(
                repository_id
                for repository_id, registration in sorted(self._by_repository.items())
                if not registration.closed
            )

        repository_ids = self._lock.run(begin)
        if not repository_ids:
            self._lock.run(lambda: setattr(self, "_state", "closed"))
            return
        actions = tuple(
            _OrderedAction(
                label=f"local attempt-pool {repository_id} cleanup also failed",
                action=lambda repository_id=repository_id: self._close_repository(
                    repository_id
                ),
                complete=lambda repository_id=repository_id: bool(
                    self._by_repository[repository_id].closed
                ),
                retry_incomplete="cancellation",
                incomplete_owner=self,
            )
            for repository_id in repository_ids
        )
        with _run_context_with_cleanup_actions(actions):
            pass

        def finish() -> None:
            if not all(
                registration.closed for registration in self._by_repository.values()
            ):
                raise RuntimeError("local attempt-pool cleanup did not settle")
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
        "_background",
        "_background_owner",
        "_capabilities",
        "_object_store_owner",
        "_reader",
        "_repositories",
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
        object_store_owner: _LocalRuntimeResourceOwner,
        background: IndexJobBackgroundService,
        background_owner: _LocalRuntimeResourceOwner,
        reader: CatalogIndexJobReader,
        writer: CatalogIndexJobWriter,
        capabilities: Mapping[str, Mapping[str, IndexUpdateCapability]],
        repositories: tuple[_ConfiguredRepository, ...],
    ) -> None:
        if token is not _LOCAL_RUNTIME_TOKEN:
            raise TypeError("local index runtime service requires acquisition")
        self._topology = topology
        self._topology_owner = topology_owner
        self._attempt_pool = attempt_pool
        self._object_store_owner = object_store_owner
        self._background = background
        self._background_owner = background_owner
        self._reader = reader
        self._writer = writer
        self._repositories = MappingProxyType(
            {repository.storage.repo_id: repository for repository in repositories}
        )
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
        attempt_pool = _LocalBM25AttemptPoolReaper()
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
                repository_authority = topology.repository_authority(
                    storage_binding.repo_id
                )
                attempt_pool_binding = bootstrap_local_bm25_attempt_pool(
                    workspace_root=config.worker_workspace_root,
                    workspace_identity=topology.worker_workspace_identity,
                    repository_id=storage_binding.repository_id,
                    repository_authority=repository_authority,
                    topology_verifier=repository_topology_verifier,
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
                    repository_root_authority=repository_authority,
                    display_commit_resolver=lambda root=repository_root: (
                        _repository_head(root)
                    ),
                    workspace_parent_identity=topology.worker_workspace_identity,
                    topology_verifier=repository_topology_verifier,
                    attempt_orphan_sink=attempt_pool.accept,
                    attempt_pool_writer_route=attempt_pool_binding.writer_route,
                )
                attempt_pool.install(
                    storage_binding.repository_id,
                    worker_target,
                    attempt_pool_binding,
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
                if result.job_id is not None:
                    with catalog_factory() as catalog:
                        completed_job = catalog.get_job(result.job_id)
                    if type(completed_job) is not IndexJobRecord:
                        raise StorageIntegrityError(
                            "local BM25 worker result has an invalid job record"
                        )
                    if completed_job.repository_id not in topology_repo_ids:
                        raise StorageIntegrityError(
                            "local BM25 worker result has no repository route"
                        )
                    attempt_pool.reclaim_pending(completed_job.repository_id)
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
                object_store_owner=object_store_owner,
                background=background,
                background_owner=background_owner,
                reader=reader,
                writer=writer,
                capabilities=capabilities,
                repositories=selected,
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

    def accepts_repository(self, repo_id: str, bundle: RepoBundle) -> bool:
        """Return whether a live Web bundle matches this runtime's frozen inputs."""

        if type(repo_id) is not str or type(bundle) is not RepoBundle:
            return False
        configured = self._repositories.get(repo_id)
        if configured is None:
            return False
        try:
            return (
                bundle.entry.instance_id == repo_id
                and bundle.entry.repo == configured.storage.repository_key
                and lexical_repository_path(bundle.entry.repo_dir)
                == configured.repository_root
                and tuple(_repository_languages(bundle)) == configured.languages
                and _repository_selection(bundle) == configured.source_selection
            )
        except (AttributeError, LocalIndexServiceError, OSError, TypeError, ValueError):
            return False

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
