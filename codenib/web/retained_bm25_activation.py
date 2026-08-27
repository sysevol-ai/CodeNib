# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Guarded local retained-BM25 publication for durable Web activations."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import stat
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol, runtime_checkable

from .._atomic_directory import lexical_directory_path
from .._workspace_provider import StrictWorkspaceProvider
from ..compiler.manifest_import import _snapshot_environment
from ..compiler.snapshot_store import normalize_repo
from ..mcp.retained_context import (
    RetainedServerContextOwner,
    RetainedServerContextResult,
    load_retained_server_context_snapshot,
)
from ..source_fingerprint import lexical_repository_path, pin_repository_source_root
from ..storage import (
    DEFAULT_NAMESPACE_NAME,
    JobResultCatalog,
    NamespaceIdentity,
    ReceiptRetainingObjectStore,
    RepositoryIdentity,
    RetainedSnapshotCatalog,
    StorageError,
    StorageValidationError,
)
from .index_job_activation import (
    IndexJobActivationError,
    IndexJobRuntimeActivation,
    _attest_current_result,
)
from .index_jobs import IndexJobRepoBinding
from .repo_registry import RepoRegistry

_RUNTIME_NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
_MAX_LOCAL_TARGETS = 4_096


def _runtime_nonce() -> str:
    return secrets.token_hex(16)


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _require_disjoint_runtime_paths(
    repository_root: Path,
    workspace_root: Path,
) -> None:
    """Reject lexical, resolved, or symlink-traversing runtime layouts."""

    resolved_repository = repository_root.resolve(strict=False)
    resolved_workspace = workspace_root.resolve(strict=False)
    if _paths_overlap(repository_root, workspace_root) or _paths_overlap(
        resolved_repository,
        resolved_workspace,
    ):
        raise ValueError("retained BM25 workspace must not overlap the repository")
    if resolved_workspace != workspace_root:
        raise ValueError("retained BM25 workspace must not traverse symbolic links")


def _require_private_runtime_root(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError("retained BM25 runtime root is unavailable") from exc
    effective_uid = getattr(os, "geteuid", None)
    wrong_owner = callable(effective_uid) and metadata.st_uid != effective_uid()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or wrong_owner
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or stat.S_IMODE(metadata.st_mode) & 0o700 != 0o700
    ):
        raise RuntimeError(
            "retained BM25 runtime root must be a private owner-only directory"
        )


@dataclass(frozen=True, slots=True)
class LocalRetainedBm25RuntimeTarget:
    """One explicit Web binding and its local materialization authority."""

    binding: IndexJobRepoBinding
    repository_key: str
    repository_root: Path
    workspace_root: Path
    workspace_provider: StrictWorkspaceProvider
    namespace_name: str = DEFAULT_NAMESPACE_NAME
    environ: Mapping[str, str] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if type(self) is not LocalRetainedBm25RuntimeTarget:
            raise TypeError("retained BM25 runtime target must use the exact type")
        if type(self.binding) is not IndexJobRepoBinding:
            raise TypeError("retained BM25 runtime target binding must be exact")
        if not isinstance(self.repository_root, Path):
            raise TypeError("retained BM25 repository root must be a Path")
        if not isinstance(self.workspace_root, Path):
            raise TypeError("retained BM25 workspace root must be a Path")
        if type(self.repository_key) is not str or type(self.namespace_name) is not str:
            raise TypeError(
                "retained BM25 namespace and repository key must be exact text"
            )
        if not callable(getattr(self.workspace_provider, "require_support", None)) or (
            not callable(getattr(self.workspace_provider, "run_workspace", None))
        ):
            raise TypeError("retained BM25 workspace provider is invalid")

        repository_root = lexical_repository_path(self.repository_root)
        workspace_root = lexical_directory_path(self.workspace_root)
        if repository_root == repository_root.parent:
            raise ValueError("retained BM25 repository cannot be a filesystem root")
        if workspace_root == workspace_root.parent:
            raise ValueError("retained BM25 workspace cannot be a filesystem root")
        _require_private_runtime_root(workspace_root)
        _require_disjoint_runtime_paths(repository_root, workspace_root)

        provider_root = getattr(self.workspace_provider, "allowed_root", None)
        if provider_root is not None and (
            not isinstance(provider_root, Path)
            or lexical_directory_path(provider_root) != workspace_root
        ):
            raise ValueError(
                "retained BM25 workspace differs from its provider authority"
            )

        namespace = NamespaceIdentity(self.namespace_name)
        repository = RepositoryIdentity(
            namespace_id=namespace.namespace_id,
            repository_key=self.repository_key,
        )
        try:
            normalized_repository = normalize_repo(repository.repository_key)
        except ValueError as exc:
            raise StorageValidationError(
                "retained BM25 repository key is not canonical"
            ) from exc
        if (
            namespace.name != self.namespace_name
            or repository.repository_key != self.repository_key
            or normalized_repository != repository.repository_key
            or repository.repository_id != self.binding.repository_id
        ):
            raise StorageValidationError(
                "retained BM25 target storage identity is inconsistent"
            )

        object.__setattr__(self, "repository_root", repository_root)
        object.__setattr__(self, "workspace_root", workspace_root)
        object.__setattr__(self, "repository_key", repository.repository_key)
        object.__setattr__(self, "namespace_name", namespace.name)
        object.__setattr__(self, "environ", _snapshot_environment(self.environ))


@runtime_checkable
class RetainedBm25SnapshotLoader(Protocol):
    """Load one exact snapshot into a caller-owned one-shot runtime owner."""

    def load(
        self,
        binding: IndexJobRepoBinding,
        activation: IndexJobRuntimeActivation,
        runtime_owner: RetainedServerContextOwner,
    ) -> RetainedServerContextResult: ...


class LocalRetainedBm25SnapshotLoader:
    """Materialize one exact successful snapshot into a fresh private output."""

    def __init__(
        self,
        catalog_factory: Callable[
            [],
            AbstractContextManager[RetainedSnapshotCatalog],
        ],
        object_store: ReceiptRetainingObjectStore,
        targets: tuple[LocalRetainedBm25RuntimeTarget, ...],
        *,
        nonce_factory: Callable[[], str] = _runtime_nonce,
    ) -> None:
        if not callable(catalog_factory):
            raise TypeError("retained BM25 catalog factory must be callable")
        if not isinstance(object_store, ReceiptRetainingObjectStore):
            raise TypeError("retained BM25 object store lacks receipt retention")
        if (
            type(targets) is not tuple
            or not targets
            or len(targets) > _MAX_LOCAL_TARGETS
        ):
            raise ValueError("retained BM25 loader requires 1 to 4096 local targets")
        if any(
            type(target) is not LocalRetainedBm25RuntimeTarget for target in targets
        ):
            raise TypeError("retained BM25 loader targets must use exact values")
        if not callable(nonce_factory):
            raise TypeError("retained BM25 runtime nonce factory must be callable")

        by_storage: dict[
            tuple[str, str],
            LocalRetainedBm25RuntimeTarget,
        ] = {}
        for target in targets:
            key = (target.binding.repository_id, target.binding.ref_name)
            if key in by_storage:
                raise ValueError("retained BM25 loader targets must be unique")
            by_storage[key] = target
        self._catalog_factory = catalog_factory
        self._object_store = object_store
        self._by_storage = by_storage
        self._nonce_factory = nonce_factory

    @staticmethod
    def _require_catalog(value: object) -> RetainedSnapshotCatalog:
        if not isinstance(value, RetainedSnapshotCatalog):
            raise TypeError("catalog does not implement retained snapshot reads")
        return value

    def load(
        self,
        binding: IndexJobRepoBinding,
        activation: IndexJobRuntimeActivation,
        runtime_owner: RetainedServerContextOwner,
    ) -> RetainedServerContextResult:
        if type(binding) is not IndexJobRepoBinding:
            raise TypeError("retained BM25 loader binding must use the exact type")
        if type(activation) is not IndexJobRuntimeActivation:
            raise TypeError("retained BM25 loader activation must use the exact type")
        if type(runtime_owner) is not RetainedServerContextOwner:
            raise TypeError("retained BM25 loader owner must use the exact type")
        if runtime_owner.state != "empty":
            raise RuntimeError("retained BM25 loader owner must be empty")
        if (
            activation.repo_id != binding.repo_id
            or activation.repository_id != binding.repository_id
            or activation.ref_name != binding.ref_name
        ):
            raise ValueError("retained BM25 loader activation binding differs")

        target = self._by_storage.get((binding.repository_id, binding.ref_name))
        if target is None or target.binding != binding:
            raise ValueError("retained BM25 loader has no exact local target")
        _require_private_runtime_root(target.workspace_root)
        _require_disjoint_runtime_paths(
            target.repository_root,
            target.workspace_root,
        )
        target.workspace_provider.require_support()

        nonce = self._nonce_factory()
        if type(nonce) is not str or _RUNTIME_NONCE_RE.fullmatch(nonce) is None:
            raise RuntimeError("retained BM25 runtime nonce is invalid")
        job_token = hashlib.sha256(activation.job_id.encode("utf-8")).hexdigest()[:16]
        destination = target.workspace_root / (
            ".codenib-bm25-runtime-" f"{activation.ref_generation}-{job_token}-{nonce}"
        )

        with pin_repository_source_root(target.repository_root) as root_authority:
            with self._catalog_factory() as value:
                catalog = self._require_catalog(value)
                return load_retained_server_context_snapshot(
                    target.repository_key,
                    activation.snapshot_id,
                    destination,
                    catalog=catalog,
                    object_store=self._object_store,
                    workspace_provider=target.workspace_provider,
                    runtime_owner=runtime_owner,
                    namespace_name=target.namespace_name,
                    repo_path=target.repository_root,
                    expected_root_authority=root_authority,
                    environ=target.environ,
                )


class RepoRegistryIndexJobRuntimePublisher:
    """Guard and transfer one retained snapshot through the registry RCU."""

    def __init__(
        self,
        registry: RepoRegistry,
        catalog_factory: Callable[[], AbstractContextManager[JobResultCatalog]],
        loader: RetainedBm25SnapshotLoader,
    ) -> None:
        if type(registry) is not RepoRegistry:
            raise TypeError("runtime publisher requires an exact RepoRegistry")
        if not callable(catalog_factory):
            raise TypeError("runtime publisher catalog factory must be callable")
        if not isinstance(loader, RetainedBm25SnapshotLoader):
            raise TypeError("runtime publisher requires a retained BM25 loader")
        self._registry = registry
        self._catalog_factory = catalog_factory
        self._loader = loader

    @staticmethod
    def _require_binding(
        binding: object,
        activation: object,
    ) -> tuple[IndexJobRepoBinding, IndexJobRuntimeActivation]:
        if type(binding) is not IndexJobRepoBinding:
            raise TypeError("runtime publisher binding must use the exact type")
        if type(activation) is not IndexJobRuntimeActivation:
            raise TypeError("runtime publisher activation must use the exact type")
        if (
            activation.repo_id != binding.repo_id
            or activation.repository_id != binding.repository_id
            or activation.ref_name != binding.ref_name
        ):
            raise ValueError("runtime publisher activation binding differs")
        return binding, activation

    def _require_current(
        self,
        binding: IndexJobRepoBinding,
        expected: IndexJobRuntimeActivation,
    ) -> None:
        try:
            with self._catalog_factory() as value:
                if not isinstance(value, JobResultCatalog):
                    raise IndexJobActivationError(
                        "catalog does not implement durable current-result reads"
                    )
                current = value.find_current_successful_job(
                    binding.repository_id,
                    binding.ref_name,
                )
                actual = (
                    None
                    if current is None
                    else _attest_current_result(binding, current)
                )
        except IndexJobActivationError:
            raise
        except (OSError, sqlite3.Error, StorageError) as exc:
            raise IndexJobActivationError(
                "durable current-result publication check is unavailable"
            ) from exc
        except Exception as exc:
            raise IndexJobActivationError(
                "durable current-result publication check failed"
            ) from exc
        if actual != expected:
            raise IndexJobActivationError(
                "durable current result changed during runtime publication"
            )

    def publish(
        self,
        binding: IndexJobRepoBinding,
        activation: IndexJobRuntimeActivation,
        *,
        transfer_if_current: Callable[[Callable[[], None]], None],
    ) -> None:
        binding, activation = self._require_binding(binding, activation)
        if not callable(transfer_if_current):
            raise TypeError("runtime publisher guarded transfer is not callable")

        self._require_current(binding, activation)
        if self._registry.attest_retained_bm25_snapshot_if_equivalent(
            binding,
            activation,
            transfer_if_current=transfer_if_current,
        ):
            return

        def load(
            runtime_owner: RetainedServerContextOwner,
        ) -> RetainedServerContextResult:
            self._require_current(binding, activation)
            result = self._loader.load(binding, activation, runtime_owner)
            self._require_current(binding, activation)
            return result

        self._registry.load_and_replace_retained_bm25_snapshot(
            binding,
            activation,
            loader=load,
            transfer_if_current=transfer_if_current,
        )


__all__ = [
    "LocalRetainedBm25RuntimeTarget",
    "LocalRetainedBm25SnapshotLoader",
    "RepoRegistryIndexJobRuntimePublisher",
    "RetainedBm25SnapshotLoader",
]
