# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Existing-only local storage ownership for the Web index service."""

from __future__ import annotations

import os
import posixpath
import stat
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterator

from .._atomic_directory import (
    _mountinfo_path,
    _open_publication_authority,
    _OrderedAction,
    _PublicationAuthorityOwner,
    _run_context_with_cleanup_actions,
    publication_parent_identity,
)
from .._owned_file_publication import _CancellationSafeRLock
from ..artifacts.runtime import SourceBindingCleanupOwner
from ..source_fingerprint import (
    RepositorySourceRootAuthority,
    lexical_repository_path,
    pin_repository_source_root,
)
from ..storage import SQLiteCatalog, StorageError
from .config import LocalIndexStorageConfig

_MAX_BUSY_TIMEOUT_MS = 86_400_000
_MAX_MOUNTINFO_ENTRIES = 65_536
_MAX_MOUNTINFO_LINE_BYTES = 64 * 1024
_MISSING_CATALOG = object()
_LOCAL_TOPOLOGY_TOKEN = object()


class LocalIndexServiceError(StorageError):
    """The explicitly configured local index service is unavailable."""


def _canonical_catalog_path(value: Path) -> Path:
    if type(value) is not type(Path()):
        raise TypeError("local index catalog path must be an exact Path")
    if (
        not value.is_absolute()
        or value == value.parent
        or Path(os.path.abspath(os.fspath(value))) != value
    ):
        raise ValueError(
            "local index catalog path must be canonical, absolute, and non-root"
        )
    return value


def _observe_catalog_identity(path: Path) -> tuple[int, int, int]:
    """Attest one non-aliased, single-linked existing catalog file."""

    try:
        resolved_before = path.resolve(strict=True)
        metadata = path.lstat()
        resolved_after = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LocalIndexServiceError(
            "local index catalog cannot be inspected safely"
        ) from exc
    identity = (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_nlink),
    )
    if (
        resolved_before != path
        or resolved_after != path
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or identity[0] < 1
        or identity[1] < 1
        or identity[2] != 1
    ):
        raise LocalIndexServiceError(
            "local index catalog must be one real single-linked file"
        )
    return identity


def _observe_real_directory(
    path: Path,
    *,
    label: str,
    private: bool = False,
) -> tuple[int, ...]:
    try:
        resolved_before = path.resolve(strict=True)
        metadata = path.lstat()
        resolved_after = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LocalIndexServiceError(f"{label} cannot be inspected safely") from exc
    effective_uid = getattr(os, "geteuid", None)
    wrong_owner = (
        private and callable(effective_uid) and metadata.st_uid != effective_uid()
    )
    if (
        resolved_before != path
        or resolved_after != path
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_dev < 1
        or metadata.st_ino < 1
        or wrong_owner
        or (private and stat.S_IMODE(metadata.st_mode) != 0o700)
    ):
        qualifier = "private owner-only " if private else ""
        raise LocalIndexServiceError(f"{label} must be one real {qualifier}directory")
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        stat.S_IFMT(metadata.st_mode),
        int(getattr(metadata, "st_file_attributes", 0)),
    )


def _directory_ancestry(
    path: Path,
    *,
    label: str,
) -> tuple[tuple[Path, tuple[int, int]], ...]:
    ancestry: list[tuple[Path, tuple[int, int]]] = []
    current = path
    while True:
        identity = _observe_real_directory(current, label=label)
        ancestry.append((current, (identity[0], identity[1])))
        if current == current.parent:
            return tuple(ancestry)
        current = current.parent


@dataclass(frozen=True, slots=True)
class _LinuxMountMapping:
    device: str
    root: PurePosixPath
    mount_point: Path


@dataclass(frozen=True, slots=True)
class _LinuxPhysicalPath:
    device: str
    path: PurePosixPath


def _linux_mount_mappings() -> tuple[_LinuxMountMapping, ...]:
    if not sys.platform.startswith("linux"):
        return ()
    mappings: list[_LinuxMountMapping] = []
    try:
        with open("/proc/self/mountinfo", "rb") as mountinfo:
            for index, line in enumerate(mountinfo):
                if index >= _MAX_MOUNTINFO_ENTRIES:
                    raise LocalIndexServiceError(
                        "Linux mount table exceeds its safe entry limit"
                    )
                if len(line) > _MAX_MOUNTINFO_LINE_BYTES:
                    raise LocalIndexServiceError(
                        "Linux mount table contains an oversized entry"
                    )
                fields = line.split()
                try:
                    separator = fields.index(b"-")
                except ValueError as exc:
                    raise LocalIndexServiceError(
                        "Linux mount table contains a malformed entry"
                    ) from exc
                if separator < 6 or len(fields) < separator + 4:
                    raise LocalIndexServiceError(
                        "Linux mount table contains a malformed entry"
                    )
                try:
                    mount_id = int(fields[0])
                except ValueError as exc:
                    raise LocalIndexServiceError(
                        "Linux mount table contains an invalid mount ID"
                    ) from exc
                device = os.fsdecode(fields[2])
                device_parts = device.split(":", 1)
                if (
                    mount_id < 1
                    or len(device_parts) != 2
                    or any(not part.isdigit() for part in device_parts)
                ):
                    raise LocalIndexServiceError(
                        "Linux mount table contains an invalid identity"
                    )
                root = PurePosixPath(
                    posixpath.normpath(_mountinfo_path(os.fsdecode(fields[3])))
                )
                mount_point = Path(
                    os.path.normpath(_mountinfo_path(os.fsdecode(fields[4])))
                )
                if not root.is_absolute():
                    continue
                if not mount_point.is_absolute():
                    raise LocalIndexServiceError(
                        "Linux mount table contains a relative path"
                    )
                mappings.append(
                    _LinuxMountMapping(
                        device=device,
                        root=root,
                        mount_point=mount_point,
                    )
                )
    except LocalIndexServiceError:
        raise
    except (OSError, UnicodeError) as exc:
        raise LocalIndexServiceError(
            "Linux mount table cannot be inspected safely"
        ) from exc
    if not mappings:
        raise LocalIndexServiceError("Linux mount table is empty")
    return tuple(mappings)


def _linux_physical_path(
    path: Path,
    mappings: tuple[_LinuxMountMapping, ...],
) -> _LinuxPhysicalPath | None:
    candidates = tuple(
        mapping
        for mapping in mappings
        if path == mapping.mount_point or mapping.mount_point in path.parents
    )
    if not candidates:
        return None
    deepest = max(len(mapping.mount_point.parts) for mapping in candidates)
    selected = tuple(
        mapping for mapping in candidates if len(mapping.mount_point.parts) == deepest
    )
    if len(selected) != 1:
        raise LocalIndexServiceError(
            "Linux mount table contains an ambiguous stacked mapping"
        )
    mapping = selected[0]
    try:
        relative = path.relative_to(mapping.mount_point)
    except ValueError as exc:  # pragma: no cover - candidates prove containment
        raise LocalIndexServiceError("Linux mount mapping changed") from exc
    internal = PurePosixPath(
        posixpath.normpath(posixpath.join(mapping.root.as_posix(), relative.as_posix()))
    )
    if not internal.is_absolute():  # pragma: no cover - absolute root proves this
        raise LocalIndexServiceError(
            "Linux mount mapping produced a relative physical path"
        )
    return _LinuxPhysicalPath(mapping.device, internal)


@dataclass(frozen=True, slots=True)
class _TopologyPath:
    label: str
    path: Path
    device: int
    ancestry_root: Path


def _require_disjoint_topology(paths: tuple[_TopologyPath, ...]) -> None:
    mappings = _linux_mount_mappings()
    for subject in paths:
        if subject.ancestry_root != subject.path and any(
            mapping.mount_point == subject.path for mapping in mappings
        ):
            raise LocalIndexServiceError(
                f"{subject.label} must not be a Linux mount point"
            )
    physical: list[tuple[_TopologyPath, _LinuxPhysicalPath | None]] = []
    lexical_stack: list[_TopologyPath] = []
    ancestry_identities: dict[tuple[int, int], tuple[Path, str]] = {}
    for subject in sorted(paths, key=lambda item: item.path.parts):
        while lexical_stack and not (
            lexical_stack[-1].path == subject.path
            or lexical_stack[-1].path in subject.path.parents
        ):
            lexical_stack.pop()
        if lexical_stack:
            raise LocalIndexServiceError(
                f"{lexical_stack[-1].label} must not overlap {subject.label}"
            )
        lexical_stack.append(subject)
        for ancestor, identity in _directory_ancestry(
            subject.ancestry_root,
            label=f"{subject.label} ancestry",
        ):
            previous = ancestry_identities.get(identity)
            if previous is not None and previous[0] != ancestor:
                raise LocalIndexServiceError(
                    f"{previous[1]} physically aliases {subject.label}"
                )
            ancestry_identities.setdefault(identity, (ancestor, subject.label))

    for subject in paths:
        mapped = _linux_physical_path(subject.path, mappings)
        if mapped is not None:
            expected_device = f"{os.major(subject.device)}:{os.minor(subject.device)}"
            if mapped.device != expected_device:
                raise LocalIndexServiceError(
                    f"{subject.label} mount mapping differs from its inode"
                )
        physical.append((subject, mapped))

    physical_stack: list[tuple[_TopologyPath, _LinuxPhysicalPath]] = []
    concrete = sorted(
        ((subject, mapped) for subject, mapped in physical if mapped is not None),
        key=lambda item: (item[1].device, item[1].path.parts),
    )
    active_device: str | None = None
    for subject, mapped in concrete:
        if mapped.device != active_device:
            physical_stack.clear()
            active_device = mapped.device
        while physical_stack and not (
            physical_stack[-1][1].path == mapped.path
            or physical_stack[-1][1].path in mapped.path.parents
        ):
            physical_stack.pop()
        if physical_stack:
            raise LocalIndexServiceError(
                f"{physical_stack[-1][0].label} traverses a physical alias of "
                f"{subject.label}"
            )
        physical_stack.append((subject, mapped))


@dataclass(frozen=True, slots=True)
class _RetainedRootBinding:
    label: str
    path: Path
    identity: tuple[int, ...]
    owner: _PublicationAuthorityOwner = field(repr=False, compare=False)

    def verify(self) -> None:
        authority = self.owner.authority
        if authority is None:
            raise LocalIndexServiceError(f"{self.label} authority is closed")
        try:
            authority.verify_path_binding()
            observed = publication_parent_identity(authority.resource)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise LocalIndexServiceError(f"{self.label} binding changed") from exc
        if observed != self.identity:
            raise LocalIndexServiceError(f"{self.label} identity changed")


def _open_root_binding(
    path: Path,
    *,
    label: str,
    owner: _PublicationAuthorityOwner,
) -> _RetainedRootBinding:
    authority = _open_publication_authority(
        path,
        parent_resource=None,
        expected_parent_identity=None,
        authority_owner=owner,
    )
    return _RetainedRootBinding(
        label=label,
        path=path,
        identity=publication_parent_identity(authority.resource),
        owner=owner,
    )


class _CatalogSessionOwner:
    """Retain an opened catalog until cancellation-safe cleanup completes."""

    __slots__ = ("_catalog",)

    def __init__(self) -> None:
        self._catalog: object = _MISSING_CATALOG

    @property
    def closed(self) -> bool:
        return self._catalog is _MISSING_CATALOG

    def acquire(self, factory) -> SQLiteCatalog:
        if self._catalog is not _MISSING_CATALOG:
            raise RuntimeError("local index catalog session is already open")
        # The cleanup owner is reachable before the factory runs. A direct
        # attribute store leaves only the native-return-to-STORE_ATTR edge that
        # Python cannot protect without a constructor-owned handoff protocol.
        self._catalog = factory()
        return self._catalog  # type: ignore[return-value]

    def close(self) -> None:
        catalog = self._catalog
        if catalog is _MISSING_CATALOG:
            return
        catalog.close()  # type: ignore[attr-defined]
        self._catalog = _MISSING_CATALOG


@dataclass(frozen=True, slots=True)
class ExistingLocalIndexCatalogFactory:
    """Open short existing-only sessions bound to one catalog inode."""

    catalog_path: Path
    busy_timeout_ms: int = 5_000
    _catalog_identity: tuple[int, int, int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self) is not ExistingLocalIndexCatalogFactory:
            raise TypeError("local index catalog factory must use the exact type")
        path = _canonical_catalog_path(self.catalog_path)
        timeout = self.busy_timeout_ms
        if type(timeout) is not int or not 0 <= timeout <= _MAX_BUSY_TIMEOUT_MS:
            raise ValueError("local index catalog busy timeout is invalid")
        object.__setattr__(self, "catalog_path", path)
        object.__setattr__(self, "_catalog_identity", _observe_catalog_identity(path))

    @property
    def catalog_identity(self) -> tuple[int, int, int]:
        """Return the immutable existing-file identity for topology checks."""

        return self._catalog_identity

    def verify(self) -> None:
        """Fail if the configured catalog path no longer names its inode."""

        if _observe_catalog_identity(self.catalog_path) != self._catalog_identity:
            raise LocalIndexServiceError("local index catalog binding changed")

    @contextmanager
    def __call__(self) -> Iterator[SQLiteCatalog]:
        """Open one thread-confined catalog and revalidate its exit binding."""

        owner = _CatalogSessionOwner()
        cleanup_actions = (
            (
                "local index catalog exit binding validation also failed",
                self.verify,
            ),
            _OrderedAction(
                label="local index catalog session cleanup also failed",
                action=owner.close,
                complete=lambda: owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=owner,
            ),
        )
        with _run_context_with_cleanup_actions(cleanup_actions):
            self.verify()
            catalog = owner.acquire(
                lambda: SQLiteCatalog(
                    self.catalog_path,
                    create=False,
                    expected_file_identity=self._catalog_identity,
                    busy_timeout_ms=self.busy_timeout_ms,
                )
            )
            self.verify()
            yield catalog


class LocalIndexStorageTopology:
    """Retain exact catalog, storage-root, and repository authorities."""

    __slots__ = (
        "_catalog_factory",
        "_cas",
        "_config",
        "_lifecycle_lock",
        "_repositories",
        "_repository_paths",
        "_runtime_workspace",
        "_source_owner",
        "_worker_workspace",
    )

    def __init__(
        self,
        token: object,
        *,
        config: LocalIndexStorageConfig,
        catalog_factory: ExistingLocalIndexCatalogFactory,
        cas: _RetainedRootBinding,
        worker_workspace: _RetainedRootBinding,
        runtime_workspace: _RetainedRootBinding,
        repository_paths: tuple[tuple[str, Path], ...],
        repositories: tuple[
            tuple[str, RepositorySourceRootAuthority],
            ...,
        ],
        source_owner: SourceBindingCleanupOwner,
    ) -> None:
        if token is not _LOCAL_TOPOLOGY_TOKEN:
            raise TypeError("local index storage topology requires acquisition")
        self._config = config
        self._catalog_factory = catalog_factory
        self._cas = cas
        self._worker_workspace = worker_workspace
        self._runtime_workspace = runtime_workspace
        self._repository_paths = repository_paths
        self._repositories = repositories
        self._source_owner = source_owner
        self._lifecycle_lock = _CancellationSafeRLock()

    @classmethod
    def acquire(
        cls,
        config: LocalIndexStorageConfig,
        repository_roots: Mapping[str, Path],
    ) -> "LocalIndexStorageTopology":
        """Open every configured authority without creating storage paths."""

        if type(config) is not LocalIndexStorageConfig:
            raise TypeError("local index topology requires the exact storage config")
        if not isinstance(repository_roots, Mapping) or any(
            type(repo_id) is not str for repo_id in repository_roots
        ):
            raise TypeError("local index repository roots must be a mapping")
        roots = dict(repository_roots)
        configured_ids = tuple(binding.repo_id for binding in config.repositories)
        if set(roots) != set(configured_ids):
            raise ValueError(
                "local index repository roots must match configured bindings"
            )
        repository_paths: list[tuple[str, Path]] = []
        repository_identities: list[tuple[int, ...]] = []
        for repo_id in configured_ids:
            path = roots[repo_id]
            if type(path) is not type(Path()):
                raise TypeError("local index repository roots must be exact Paths")
            canonical = lexical_repository_path(path)
            if canonical != path or canonical == canonical.parent:
                raise ValueError(
                    "local index repository root must be canonical and non-root"
                )
            identity = _observe_real_directory(
                canonical,
                label=f"repository {repo_id!r}",
            )
            repository_paths.append((repo_id, canonical))
            repository_identities.append(identity)

        catalog_factory = ExistingLocalIndexCatalogFactory(
            config.catalog_path,
            busy_timeout_ms=config.catalog_busy_timeout_ms,
        )
        root_specs = (
            (config.cas_root, "local CAS root", False),
            (config.worker_workspace_root, "worker workspace root", True),
            (config.runtime_workspace_root, "runtime workspace root", True),
        )
        root_identities = tuple(
            _observe_real_directory(path, label=label, private=private)
            for path, label, private in root_specs
        )
        catalog_factory.verify()
        topology_paths = (
            _TopologyPath(
                "local index catalog",
                config.catalog_path,
                catalog_factory.catalog_identity[0],
                config.catalog_path.parent,
            ),
            *(
                _TopologyPath(label, path, identity[0], path)
                for (path, label, _private), identity in zip(
                    root_specs,
                    root_identities,
                    strict=True,
                )
            ),
            *(
                _TopologyPath(
                    f"repository {repo_id!r}",
                    path,
                    identity[0],
                    path,
                )
                for (repo_id, path), identity in zip(
                    repository_paths,
                    repository_identities,
                    strict=True,
                )
            ),
        )
        _require_disjoint_topology(topology_paths)

        directory_owners = tuple(_PublicationAuthorityOwner() for _index in range(3))
        source_owner = SourceBindingCleanupOwner()
        cleanup_actions = (
            _OrderedAction(
                label="local index repository authority cleanup also failed",
                action=source_owner.close,
                complete=lambda: source_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=source_owner,
            ),
            *(
                _OrderedAction(
                    label="local index root authority cleanup also failed",
                    action=owner.close,
                    complete=lambda owner=owner: owner.closed,
                    retry_incomplete="cancellation",
                    incomplete_owner=owner,
                )
                for owner in reversed(directory_owners)
            ),
        )
        with _run_context_with_cleanup_actions(
            cleanup_actions,
            cleanup_on_success=False,
        ):
            repositories = tuple(
                (
                    repo_id,
                    pin_repository_source_root(
                        path,
                        _source_owner=source_owner.retain,
                    ),
                )
                for repo_id, path in repository_paths
            )
            cas = _open_root_binding(
                config.cas_root,
                label="local CAS root",
                owner=directory_owners[0],
            )
            worker_workspace = _open_root_binding(
                config.worker_workspace_root,
                label="worker workspace root",
                owner=directory_owners[1],
            )
            runtime_workspace = _open_root_binding(
                config.runtime_workspace_root,
                label="runtime workspace root",
                owner=directory_owners[2],
            )
            topology = cls(
                _LOCAL_TOPOLOGY_TOKEN,
                config=config,
                catalog_factory=catalog_factory,
                cas=cas,
                worker_workspace=worker_workspace,
                runtime_workspace=runtime_workspace,
                repository_paths=tuple(repository_paths),
                repositories=repositories,
                source_owner=source_owner,
            )
            topology.verify()
            return topology

    @property
    def catalog_factory(self) -> ExistingLocalIndexCatalogFactory:
        return self._catalog_factory

    @property
    def worker_workspace_identity(self) -> tuple[int, ...]:
        return self._worker_workspace.identity

    @property
    def runtime_workspace_identity(self) -> tuple[int, ...]:
        return self._runtime_workspace.identity

    @property
    def closed(self) -> bool:
        return self._lifecycle_lock.run(self._closed_unlocked)

    def _closed_unlocked(self) -> bool:
        return self._source_owner.closed and all(
            binding.owner.closed
            for binding in (
                self._cas,
                self._worker_workspace,
                self._runtime_workspace,
            )
        )

    def repository_authority(
        self,
        repo_id: str,
    ) -> RepositorySourceRootAuthority:
        """Return one non-owning configured source authority."""

        def read() -> RepositorySourceRootAuthority:
            if self._closed_unlocked():
                raise LocalIndexServiceError("local index topology is closed")
            for candidate_id, authority in self._repositories:
                if candidate_id == repo_id:
                    authority.verify()
                    return authority
            raise KeyError(repo_id)

        return self._lifecycle_lock.run(read)

    def _verify_unlocked(self) -> None:
        if self._closed_unlocked():
            raise LocalIndexServiceError("local index topology is closed")
        self._catalog_factory.verify()
        for binding, private in (
            (self._cas, False),
            (self._worker_workspace, True),
            (self._runtime_workspace, True),
        ):
            binding.verify()
            observed = _observe_real_directory(
                binding.path,
                label=binding.label,
                private=private,
            )
            if observed != binding.identity:
                raise LocalIndexServiceError(f"{binding.label} identity changed")
        for (repo_id, path), (authority_id, authority) in zip(
            self._repository_paths,
            self._repositories,
            strict=True,
        ):
            if authority_id != repo_id:
                raise LocalIndexServiceError(
                    "local index repository authority ordering changed"
                )
            authority.verify()
            if authority.root != path:
                raise LocalIndexServiceError(
                    f"repository {repo_id!r} authority path changed"
                )
            observed = _observe_real_directory(
                path,
                label=f"repository {repo_id!r}",
            )
            if tuple(observed[:2]) != tuple(authority.root_identity[:2]):
                raise LocalIndexServiceError(f"repository {repo_id!r} identity changed")
        topology_paths = (
            _TopologyPath(
                "local index catalog",
                self._config.catalog_path,
                self._catalog_factory.catalog_identity[0],
                self._config.catalog_path.parent,
            ),
            _TopologyPath(
                self._cas.label,
                self._cas.path,
                self._cas.identity[0],
                self._cas.path,
            ),
            _TopologyPath(
                self._worker_workspace.label,
                self._worker_workspace.path,
                self._worker_workspace.identity[0],
                self._worker_workspace.path,
            ),
            _TopologyPath(
                self._runtime_workspace.label,
                self._runtime_workspace.path,
                self._runtime_workspace.identity[0],
                self._runtime_workspace.path,
            ),
            *(
                _TopologyPath(
                    f"repository {repo_id!r}",
                    path,
                    authority.root_identity[0],
                    path,
                )
                for (repo_id, path), (_authority_id, authority) in zip(
                    self._repository_paths,
                    self._repositories,
                    strict=True,
                )
            ),
        )
        _require_disjoint_topology(topology_paths)

    def verify(self) -> None:
        """Revalidate every retained path and physical-separation invariant."""

        self._lifecycle_lock.run(self._verify_unlocked)

    def _close_unlocked(self) -> None:
        cleanup_actions = (
            _OrderedAction(
                label="local index repository authority cleanup also failed",
                action=self._source_owner.close,
                complete=lambda: self._source_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=self._source_owner,
            ),
            *(
                _OrderedAction(
                    label="local index root authority cleanup also failed",
                    action=binding.owner.close,
                    complete=lambda binding=binding: binding.owner.closed,
                    retry_incomplete="cancellation",
                    incomplete_owner=binding.owner,
                )
                for binding in (
                    self._runtime_workspace,
                    self._worker_workspace,
                    self._cas,
                )
            ),
        )
        with _run_context_with_cleanup_actions(cleanup_actions):
            pass

    def close(self) -> None:
        """Release every source and directory authority, with retryable cleanup."""

        self._lifecycle_lock.run(self._close_unlocked)


__all__ = [
    "ExistingLocalIndexCatalogFactory",
    "LocalIndexServiceError",
    "LocalIndexStorageTopology",
]
