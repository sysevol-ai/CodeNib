# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Repository-bound local BM25 attempt-pool shards and lease capabilities."""

from __future__ import annotations

import os
import posixpath
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, final

from .._atomic_directory import (
    _linux_mount_points,
    _mountinfo_path,
    _path_is_mount_point,
    _run_quiescent_directory_resource_scope,
    lexical_directory_path,
    publication_parent_identity,
)
from ..source_fingerprint import RepositorySourceRootAuthority
from ..storage.models import StorageIntegrityError, StorageValidationError
from ._directory_lease import (
    DirectoryLeaseMode,
    PrivateDirectoryLeaseOwner,
    PrivateDirectoryLeaseRoute,
    _create_private_directory_descriptor_owner,
    _PrivateDirectoryDescriptorOwner,
    acquire_private_directory_lease,
    require_private_directory_lease_support,
)

_BM25_ATTEMPT_POOL_LAYOUT = "codenib-bm25-attempt-pool-v1"
_BM25_ATTEMPT_POOL_SHARD_PREFIX = f".{_BM25_ATTEMPT_POOL_LAYOUT}-"
_REPOSITORY_ID = re.compile(r"repo_[0-9a-f]{64}\Z")
_MAX_BM25_MOUNTINFO_ENTRIES = 65_536
_MAX_BM25_MOUNTINFO_LINE_BYTES = 64 * 1024


def _require_identity(
    value: object,
    *,
    length: int,
    label: str,
) -> tuple[int, ...]:
    if (
        type(value) is not tuple
        or len(value) != length
        or any(type(item) is not int for item in value)
    ):
        raise TypeError(f"{label} must be an exact {length}-integer tuple")
    return value


def _require_private_directory(
    metadata: os.stat_result,
    *,
    label: str,
    parent_device: int | None = None,
) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or mode != 0o700
        or (parent_device is not None and metadata.st_dev != parent_device)
    ):
        raise PermissionError(f"{label} must be an owner-controlled 0700 directory")


def _publication_parent_identity_from_metadata(
    metadata: os.stat_result,
) -> tuple[int, ...]:
    """Project one already-no-follow directory stat into the binding tuple."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0),
    )


def _require_unmounted_attempt_pool_shard(path: Path) -> None:
    """Reject a nested mount before it can become attempt authority."""

    mount_points = _linux_mount_points()
    if not mount_points:
        raise StorageIntegrityError(
            "BM25 attempt-pool mount topology could not be authenticated"
        )
    if _path_is_mount_point(path, mount_points=mount_points):
        raise StorageIntegrityError("BM25 attempt-pool shard must not be a mount point")


def _require_symlink_free_lexical_path(path: Path, *, label: str) -> None:
    """Reject a lexical authority whose ancestry resolves through a symlink."""

    try:
        resolved = Path(os.path.realpath(path))
    except (OSError, RuntimeError, ValueError) as exc:
        raise StorageIntegrityError(f"{label} could not be resolved safely") from exc
    if resolved != path:
        raise StorageIntegrityError(f"{label} must not traverse a symbolic link")


def _require_opened_workspace_lexical_path(
    descriptor: int,
    *,
    workspace_root: Path,
) -> None:
    try:
        observed = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
    except (OSError, RuntimeError, ValueError) as exc:
        raise StorageIntegrityError(
            "BM25 attempt-pool opened workspace path could not be authenticated"
        ) from exc
    if observed != workspace_root:
        raise StorageIntegrityError(
            "BM25 attempt-pool workspace ancestry changed while opening"
        )


@dataclass(frozen=True, slots=True)
class _BM25LinuxMountMapping:
    mount_id: int
    device: tuple[int, int]
    root: PurePosixPath
    mount_point: Path


@dataclass(frozen=True, slots=True)
class _BM25LinuxPhysicalPath:
    mount_id: int
    device: tuple[int, int]
    path: PurePosixPath


def _bm25_linux_mount_mappings() -> tuple[_BM25LinuxMountMapping, ...]:
    """Read one bounded mount snapshot for repository/workspace alias checks."""

    mountinfo_path = Path("/proc/self/mountinfo")
    if os.name != "posix" or not mountinfo_path.is_file():
        raise StorageIntegrityError(
            "BM25 attempt-pool mount topology could not be authenticated"
        )
    mappings: list[_BM25LinuxMountMapping] = []
    try:
        with open(mountinfo_path, "rb") as mountinfo:
            for index, line in enumerate(mountinfo):
                if index >= _MAX_BM25_MOUNTINFO_ENTRIES:
                    raise StorageIntegrityError(
                        "BM25 attempt-pool mount table exceeds its safe entry limit"
                    )
                if len(line) > _MAX_BM25_MOUNTINFO_LINE_BYTES:
                    raise StorageIntegrityError(
                        "BM25 attempt-pool mount table contains an oversized entry"
                    )
                fields = line.split()
                try:
                    separator = fields.index(b"-")
                except ValueError as exc:
                    raise StorageIntegrityError(
                        "BM25 attempt-pool mount table contains a malformed entry"
                    ) from exc
                if separator < 6 or len(fields) < separator + 4:
                    raise StorageIntegrityError(
                        "BM25 attempt-pool mount table contains a malformed entry"
                    )
                try:
                    mount_id = int(fields[0])
                    raw_device = os.fsdecode(fields[2])
                    device_parts = raw_device.split(":", 1)
                    device = tuple(int(part) for part in device_parts)
                except (TypeError, ValueError) as exc:
                    raise StorageIntegrityError(
                        "BM25 attempt-pool mount table contains an invalid identity"
                    ) from exc
                if (
                    mount_id < 1
                    or len(device_parts) != 2
                    or len(device) != 2
                    or any(part < 0 for part in device)
                ):
                    raise StorageIntegrityError(
                        "BM25 attempt-pool mount table contains an invalid identity"
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
                    raise StorageIntegrityError(
                        "BM25 attempt-pool mount table contains a relative path"
                    )
                mappings.append(
                    _BM25LinuxMountMapping(
                        mount_id=mount_id,
                        device=(device[0], device[1]),
                        root=root,
                        mount_point=mount_point,
                    )
                )
    except StorageIntegrityError:
        raise
    except (OSError, UnicodeError) as exc:
        raise StorageIntegrityError(
            "BM25 attempt-pool mount topology could not be authenticated"
        ) from exc
    if not mappings:
        raise StorageIntegrityError("BM25 attempt-pool mount table is empty")
    return tuple(mappings)


def _bm25_linux_physical_path(
    path: Path,
    *,
    expected_device: int,
    mappings: tuple[_BM25LinuxMountMapping, ...],
) -> _BM25LinuxPhysicalPath:
    candidates = tuple(
        mapping
        for mapping in mappings
        if path == mapping.mount_point or mapping.mount_point in path.parents
    )
    if not candidates:
        raise StorageIntegrityError(
            "BM25 attempt-pool path has no authenticated mount mapping"
        )
    deepest = max(len(mapping.mount_point.parts) for mapping in candidates)
    selected_candidates = tuple(
        mapping for mapping in candidates if len(mapping.mount_point.parts) == deepest
    )
    if len(selected_candidates) != 1:
        raise StorageIntegrityError(
            "BM25 attempt-pool path has an ambiguous stacked mount mapping"
        )
    selected = selected_candidates[0]
    expected = (os.major(expected_device), os.minor(expected_device))
    if selected.device != expected:
        raise StorageIntegrityError("BM25 attempt-pool mount mapping device changed")
    try:
        relative = path.relative_to(selected.mount_point)
    except ValueError as exc:  # pragma: no cover - candidates prove containment
        raise StorageIntegrityError("BM25 attempt-pool mount mapping changed") from exc
    physical = PurePosixPath(
        posixpath.normpath(
            posixpath.join(selected.root.as_posix(), relative.as_posix())
        )
    )
    if not physical.is_absolute():  # pragma: no cover - normalized absolute root
        raise StorageIntegrityError(
            "BM25 attempt-pool mount mapping produced a relative path"
        )
    return _BM25LinuxPhysicalPath(selected.mount_id, selected.device, physical)


def _bm25_physical_paths_overlap(
    first: _BM25LinuxPhysicalPath,
    second: _BM25LinuxPhysicalPath,
) -> bool:
    return first.device == second.device and (
        first.path == second.path
        or first.path in second.path.parents
        or second.path in first.path.parents
    )


def _require_repository_workspace_disjoint(
    *,
    repository_root: Path,
    repository_root_identity: tuple[int, ...],
    workspace_root: Path,
    workspace_identity: tuple[int, ...],
) -> tuple[_BM25LinuxPhysicalPath, _BM25LinuxPhysicalPath]:
    if repository_root_identity[:2] == workspace_identity[:2]:
        raise StorageIntegrityError(
            "BM25 attempt-pool workspace physically aliases the repository"
        )
    mappings = _bm25_linux_mount_mappings()
    if any(repository_root in mapping.mount_point.parents for mapping in mappings):
        # A workspace backing directory can otherwise be bind-mounted below
        # the repository while its own lexical root remains disjoint.  Any
        # bootstrap mutation would then be visible inside source.  Source
        # capture rejects nested mounts too, but bootstrap runs first and must
        # enforce the same boundary before creating the permanent shard.
        raise StorageIntegrityError(
            "BM25 attempt-pool repository contains a nested mount"
        )
    repository_physical = _bm25_linux_physical_path(
        repository_root,
        expected_device=repository_root_identity[0],
        mappings=mappings,
    )
    workspace_physical = _bm25_linux_physical_path(
        workspace_root,
        expected_device=workspace_identity[0],
        mappings=mappings,
    )
    if repository_physical.mount_id != workspace_physical.mount_id:
        # Mountinfo root/device mapping proves aliases inside one mounted
        # namespace, including ordinary bind mounts.  It cannot authenticate
        # external backing paths for a distinct overlay, FUSE, network, or
        # custom filesystem.  Phase A therefore stays deliberately narrow:
        # repository and workspace must be disjoint paths in one selected
        # mount, rather than guessing about two independently mounted views.
        raise StorageIntegrityError(
            "BM25 attempt-pool repository and workspace must share one mount"
        )
    if _bm25_physical_paths_overlap(repository_physical, workspace_physical):
        raise StorageIntegrityError(
            "BM25 attempt-pool workspace physically overlaps the repository"
        )
    return repository_physical, workspace_physical


@dataclass(frozen=True, slots=True)
class _BM25AttemptPoolRouteState:
    """Immutable identity shared by least-authority writer/reaper routes."""

    repository_id: str
    repository_root: Path
    repository_root_identity: tuple[int, ...]
    workspace_root: Path
    workspace_identity: tuple[int, ...]
    repository_physical: _BM25LinuxPhysicalPath
    workspace_physical: _BM25LinuxPhysicalPath
    shard_path: Path
    shard_identity: tuple[int, ...]
    directory_lease_route: PrivateDirectoryLeaseRoute = field(repr=False)
    repository_authority: RepositorySourceRootAuthority = field(
        repr=False,
        compare=False,
    )
    topology_verifier: Callable[[], None] = field(repr=False, compare=False)
    token: object = field(repr=False, compare=False)
    owner_pid: int = field(repr=False, compare=False)

    def verify(self) -> None:
        if os.getpid() != self.owner_pid:
            raise RuntimeError("BM25 attempt-pool route cannot cross a PID boundary")
        expected_shard = self.workspace_root / (
            _BM25_ATTEMPT_POOL_SHARD_PREFIX + self.repository_id
        )
        if (
            self.shard_path != expected_shard
            or self.directory_lease_route.path != self.shard_path
            or self.directory_lease_route.identity != self.shard_identity
            or self.directory_lease_route.owner_pid != self.owner_pid
        ):
            raise StorageIntegrityError("BM25 attempt-pool route binding changed")
        self.directory_lease_route.__post_init__()
        self.topology_verifier()
        self.repository_authority.verify()
        if self.repository_authority.root != self.repository_root:
            raise StorageIntegrityError("BM25 attempt-pool repository root changed")
        if tuple(self.repository_authority.root_identity) != (
            self.repository_root_identity
        ):
            raise StorageIntegrityError("BM25 attempt-pool repository identity changed")
        _require_symlink_free_lexical_path(
            self.repository_root,
            label="BM25 attempt-pool repository root",
        )
        _require_symlink_free_lexical_path(
            self.workspace_root,
            label="BM25 attempt-pool workspace root",
        )
        try:
            workspace_metadata = self.workspace_root.lstat()
        except OSError as exc:
            raise StorageIntegrityError(
                "BM25 attempt-pool workspace is no longer visible"
            ) from exc
        _require_private_directory(
            workspace_metadata,
            label="BM25 attempt-pool workspace",
        )
        if (
            _publication_parent_identity_from_metadata(workspace_metadata)
            != self.workspace_identity
        ):
            raise StorageIntegrityError("BM25 attempt-pool workspace identity changed")
        observed_physical = _require_repository_workspace_disjoint(
            repository_root=self.repository_root,
            repository_root_identity=self.repository_root_identity,
            workspace_root=self.workspace_root,
            workspace_identity=self.workspace_identity,
        )
        if observed_physical != (
            self.repository_physical,
            self.workspace_physical,
        ):
            raise StorageIntegrityError("BM25 attempt-pool physical topology changed")
        try:
            shard_metadata = self.shard_path.lstat()
        except OSError as exc:
            raise StorageIntegrityError(
                "BM25 attempt-pool shard is no longer visible"
            ) from exc
        _require_private_directory(
            shard_metadata,
            label="BM25 attempt-pool shard",
            parent_device=self.workspace_identity[0],
        )
        if (
            _publication_parent_identity_from_metadata(shard_metadata)
            != self.shard_identity
        ):
            raise StorageIntegrityError("BM25 attempt-pool shard identity changed")
        _require_unmounted_attempt_pool_shard(self.shard_path)
        try:
            shard_metadata = self.shard_path.lstat()
        except OSError as exc:
            raise StorageIntegrityError(
                "BM25 attempt-pool shard is no longer visible"
            ) from exc
        _require_private_directory(
            shard_metadata,
            label="BM25 attempt-pool shard",
            parent_device=self.workspace_identity[0],
        )
        if (
            _publication_parent_identity_from_metadata(shard_metadata)
            != self.shard_identity
        ):
            raise StorageIntegrityError("BM25 attempt-pool shard identity changed")
        _require_unmounted_attempt_pool_shard(self.shard_path)
        self.topology_verifier()


@final
class _LocalBM25AttemptPoolWriterRoute:
    """Opaque capability that admits only shared attempt writers."""

    __slots__ = ("_state",)

    def __init__(self, state: _BM25AttemptPoolRouteState) -> None:
        if type(state) is not _BM25AttemptPoolRouteState:
            raise TypeError("BM25 attempt-pool writer route state is invalid")
        self._state = state

    @property
    def _route_token(self) -> object:
        return self._state.token

    @property
    def _repository_id(self) -> str:
        return self._state.repository_id

    @property
    def _repository_root(self) -> Path:
        return self._state.repository_root

    @property
    def _repository_root_identity(self) -> tuple[int, ...]:
        return self._state.repository_root_identity

    @property
    def _workspace_root(self) -> Path:
        return self._state.workspace_root

    @property
    def _workspace_identity(self) -> tuple[int, ...]:
        return self._state.workspace_identity

    @property
    def _shard_path(self) -> Path:
        return self._state.shard_path

    @property
    def _shard_identity(self) -> tuple[int, ...]:
        return self._state.shard_identity

    def _verify(self) -> None:
        self._state.verify()

    def _acquire(
        self,
        *,
        check_cancelled: Callable[[], None] | None,
        construction_owner: Callable[[PrivateDirectoryLeaseOwner], None],
    ) -> PrivateDirectoryLeaseOwner:
        if not callable(construction_owner):
            raise TypeError("BM25 attempt-pool lease construction owner is invalid")
        self._state.verify()
        owner = acquire_private_directory_lease(
            self._state.directory_lease_route,
            mode=DirectoryLeaseMode.SHARED,
            blocking=True,
            check_cancelled=check_cancelled,
            _construction_owner=construction_owner,
        )
        try:
            self._state.verify()
        except BaseException as primary:  # noqa: B036 - settle admitted lease
            try:
                owner.close()
            except BaseException as cleanup:  # noqa: B036 - preserve exact primary
                raise primary from cleanup
            raise
        return owner


@final
class _LocalBM25AttemptPoolReaperRoute:
    """Opaque capability that admits only exclusive shard reapers."""

    __slots__ = ("_state",)

    def __init__(self, state: _BM25AttemptPoolRouteState) -> None:
        if type(state) is not _BM25AttemptPoolRouteState:
            raise TypeError("BM25 attempt-pool reaper route state is invalid")
        self._state = state

    @property
    def _route_token(self) -> object:
        return self._state.token

    @property
    def _repository_id(self) -> str:
        return self._state.repository_id

    @property
    def _shard_path(self) -> Path:
        return self._state.shard_path

    @property
    def _shard_identity(self) -> tuple[int, ...]:
        return self._state.shard_identity

    def _verify(self) -> None:
        self._state.verify()

    def _acquire(
        self,
        *,
        blocking: bool,
        check_cancelled: Callable[[], None] | None,
        construction_owner: Callable[[PrivateDirectoryLeaseOwner], None],
    ) -> PrivateDirectoryLeaseOwner:
        if not callable(construction_owner):
            raise TypeError("BM25 attempt-pool lease construction owner is invalid")
        self._state.verify()
        owner = acquire_private_directory_lease(
            self._state.directory_lease_route,
            mode=DirectoryLeaseMode.EXCLUSIVE,
            blocking=blocking,
            check_cancelled=check_cancelled,
            _construction_owner=construction_owner,
        )
        try:
            self._state.verify()
        except BaseException as primary:  # noqa: B036 - settle admitted lease
            try:
                owner.close()
            except BaseException as cleanup:  # noqa: B036 - preserve exact primary
                raise primary from cleanup
            raise
        return owner


@dataclass(frozen=True, slots=True)
class LocalBM25AttemptPoolBinding:
    """Paired least-authority routes for one permanent repository shard."""

    _writer_route: _LocalBM25AttemptPoolWriterRoute = field(repr=False)
    _reaper_route: _LocalBM25AttemptPoolReaperRoute = field(repr=False)

    def __post_init__(self) -> None:
        if type(self) is not LocalBM25AttemptPoolBinding:
            raise TypeError("local BM25 attempt-pool binding must use the exact type")
        if (
            type(self._writer_route) is not _LocalBM25AttemptPoolWriterRoute
            or type(self._reaper_route) is not _LocalBM25AttemptPoolReaperRoute
        ):
            raise TypeError("local BM25 attempt-pool routes are invalid")
        if self._writer_route._state is not self._reaper_route._state:
            raise ValueError("local BM25 attempt-pool routes have different authority")

    @property
    def writer_route(self) -> _LocalBM25AttemptPoolWriterRoute:
        return self._writer_route

    @property
    def reaper_route(self) -> _LocalBM25AttemptPoolReaperRoute:
        return self._reaper_route


@final
class _BM25AttemptPoolBootstrapResources:
    """Retain native root/shard fds across every Python return handoff."""

    __slots__ = ("_owners",)

    def __init__(self) -> None:
        self._owners: list[_PrivateDirectoryDescriptorOwner] = []

    def _open(
        self,
        path: Path,
        identity: tuple[int, ...],
    ) -> tuple[_PrivateDirectoryDescriptorOwner, int]:
        route = PrivateDirectoryLeaseRoute(
            path=path,
            identity=identity,
            owner_pid=os.getpid(),
        )
        owner = _create_private_directory_descriptor_owner(route)
        self._owners.append(owner)
        return owner, owner._open()

    @property
    def closed(self) -> bool:
        return all(owner.closed for owner in self._owners)

    def close(self) -> None:
        for owner in reversed(self._owners):
            if not owner.closed:
                owner.close()
        if not self.closed:
            raise RuntimeError("BM25 attempt-pool bootstrap cleanup did not settle")


def _bootstrap_bm25_attempt_pool_shard(
    resources: _BM25AttemptPoolBootstrapResources,
    *,
    repository_root: Path,
    repository_root_identity: tuple[int, ...],
    repository_authority: RepositorySourceRootAuthority,
    workspace_root: Path,
    workspace_identity: tuple[int, ...],
    physical_topology: tuple[_BM25LinuxPhysicalPath, _BM25LinuxPhysicalPath],
    shard_name: str,
    topology_verifier: Callable[[], None],
) -> tuple[Path, tuple[int, ...]]:
    try:
        visible_root = workspace_root.lstat()
    except OSError as exc:
        raise StorageIntegrityError(
            "BM25 attempt-pool workspace could not be inspected"
        ) from exc
    _require_private_directory(visible_root, label="BM25 attempt-pool workspace")
    if _publication_parent_identity_from_metadata(visible_root) != workspace_identity:
        raise StorageIntegrityError("BM25 attempt-pool workspace identity changed")
    root_owner, root_descriptor = resources._open(
        workspace_root,
        workspace_identity,
    )
    _require_opened_workspace_lexical_path(
        root_descriptor,
        workspace_root=workspace_root,
    )
    root_metadata = os.fstat(root_descriptor)
    _require_private_directory(root_metadata, label="BM25 attempt-pool workspace")
    if publication_parent_identity(root_descriptor) != workspace_identity:
        raise StorageIntegrityError("BM25 attempt-pool workspace identity changed")

    shard_path = workspace_root / shard_name

    def pre_mutation_check() -> None:
        """Reattest every retained topology binding in the native mkdir frame."""

        topology_verifier()
        repository_authority.verify()
        _require_symlink_free_lexical_path(
            repository_root,
            label="BM25 attempt-pool repository root",
        )
        _require_symlink_free_lexical_path(
            workspace_root,
            label="BM25 attempt-pool workspace root",
        )
        _require_opened_workspace_lexical_path(
            root_descriptor,
            workspace_root=workspace_root,
        )
        visible = workspace_root.lstat()
        _require_private_directory(visible, label="BM25 attempt-pool workspace")
        if _publication_parent_identity_from_metadata(visible) != workspace_identity:
            raise StorageIntegrityError(
                "BM25 attempt-pool workspace identity changed before mutation"
            )
        if (
            _require_repository_workspace_disjoint(
                repository_root=repository_root,
                repository_root_identity=repository_root_identity,
                workspace_root=workspace_root,
                workspace_identity=workspace_identity,
            )
            != physical_topology
        ):
            raise StorageIntegrityError(
                "BM25 attempt-pool physical topology changed before mutation"
            )

    try:
        before = os.stat(shard_name, dir_fd=root_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        root_owner._mkdir_child(os.fsencode(shard_name), pre_mutation_check)
        before = os.stat(shard_name, dir_fd=root_descriptor, follow_symlinks=False)
    _require_private_directory(
        before,
        label="BM25 attempt-pool shard",
        parent_device=root_metadata.st_dev,
    )
    _require_unmounted_attempt_pool_shard(shard_path)
    shard_identity = _publication_parent_identity_from_metadata(before)
    _shard_owner, shard_descriptor = resources._open(
        shard_path,
        shard_identity,
    )
    opened = os.fstat(shard_descriptor)
    _require_private_directory(
        opened,
        label="BM25 attempt-pool shard",
        parent_device=root_metadata.st_dev,
    )
    if publication_parent_identity(shard_descriptor) != shard_identity:
        raise StorageIntegrityError("BM25 attempt-pool shard changed while opening")
    _require_unmounted_attempt_pool_shard(shard_path)
    os.fsync(shard_descriptor)
    os.fsync(root_descriptor)
    after = os.stat(shard_name, dir_fd=root_descriptor, follow_symlinks=False)
    if _publication_parent_identity_from_metadata(after) != shard_identity:
        raise StorageIntegrityError(
            "BM25 attempt-pool shard changed after synchronization"
        )
    _require_unmounted_attempt_pool_shard(shard_path)
    if publication_parent_identity(root_descriptor) != workspace_identity:
        raise StorageIntegrityError(
            "BM25 attempt-pool workspace changed after bootstrap"
        )
    return shard_path, shard_identity


def bootstrap_local_bm25_attempt_pool(
    *,
    workspace_root: Path,
    workspace_identity: tuple[int, ...],
    repository_id: str,
    repository_authority: RepositorySourceRootAuthority,
    topology_verifier: Callable[[], None],
) -> LocalBM25AttemptPoolBinding:
    """Create/reopen one shard under caller-quiescent path and mount topology."""

    require_private_directory_lease_support()
    if type(workspace_root) is not type(Path()):
        raise TypeError("BM25 attempt-pool workspace root must be an exact Path")
    workspace_root = lexical_directory_path(workspace_root)
    workspace_identity = _require_identity(
        workspace_identity,
        length=4,
        label="BM25 attempt-pool workspace identity",
    )
    if (
        type(repository_id) is not str
        or _REPOSITORY_ID.fullmatch(repository_id) is None
    ):
        raise StorageValidationError("BM25 attempt-pool repository ID is invalid")
    if type(repository_authority) is not RepositorySourceRootAuthority:
        raise TypeError("BM25 attempt-pool repository authority is invalid")
    if not callable(topology_verifier):
        raise TypeError("BM25 attempt-pool topology verifier must be callable")
    repository_authority.verify()
    repository_root = repository_authority.root
    repository_root_identity = _require_identity(
        repository_authority.root_identity,
        length=2,
        label="BM25 attempt-pool repository identity",
    )
    _require_symlink_free_lexical_path(
        repository_root,
        label="BM25 attempt-pool repository root",
    )
    _require_symlink_free_lexical_path(
        workspace_root,
        label="BM25 attempt-pool workspace root",
    )
    topology_verifier()
    physical_topology = _require_repository_workspace_disjoint(
        repository_root=repository_root,
        repository_root_identity=repository_root_identity,
        workspace_root=workspace_root,
        workspace_identity=workspace_identity,
    )
    shard_name = _BM25_ATTEMPT_POOL_SHARD_PREFIX + repository_id
    if len(os.fsencode(shard_name)) > 255:
        raise StorageValidationError("BM25 attempt-pool shard name is too long")
    if not _linux_mount_points():
        raise StorageIntegrityError(
            "BM25 attempt-pool mount topology could not be authenticated"
        )
    resources = _BM25AttemptPoolBootstrapResources()
    shard_path, shard_identity = _run_quiescent_directory_resource_scope(
        resources,
        lambda: _bootstrap_bm25_attempt_pool_shard(
            resources,
            repository_root=repository_root,
            repository_root_identity=repository_root_identity,
            repository_authority=repository_authority,
            workspace_root=workspace_root,
            workspace_identity=workspace_identity,
            physical_topology=physical_topology,
            shard_name=shard_name,
            topology_verifier=topology_verifier,
        ),
        label="BM25 attempt-pool bootstrap cleanup also failed",
    )
    topology_verifier()
    repository_authority.verify()
    if (
        _require_repository_workspace_disjoint(
            repository_root=repository_root,
            repository_root_identity=repository_root_identity,
            workspace_root=workspace_root,
            workspace_identity=workspace_identity,
        )
        != physical_topology
    ):
        raise StorageIntegrityError(
            "BM25 attempt-pool physical topology changed during bootstrap"
        )
    owner_pid = os.getpid()
    directory_route = PrivateDirectoryLeaseRoute(
        path=shard_path,
        identity=shard_identity,
        owner_pid=owner_pid,
    )
    state = _BM25AttemptPoolRouteState(
        repository_id=repository_id,
        repository_root=repository_root,
        repository_root_identity=repository_root_identity,
        workspace_root=workspace_root,
        workspace_identity=workspace_identity,
        repository_physical=physical_topology[0],
        workspace_physical=physical_topology[1],
        shard_path=shard_path,
        shard_identity=shard_identity,
        directory_lease_route=directory_route,
        repository_authority=repository_authority,
        topology_verifier=topology_verifier,
        token=object(),
        owner_pid=owner_pid,
    )
    state.verify()
    return LocalBM25AttemptPoolBinding(
        _LocalBM25AttemptPoolWriterRoute(state),
        _LocalBM25AttemptPoolReaperRoute(state),
    )


__all__ = ["LocalBM25AttemptPoolBinding", "bootstrap_local_bm25_attempt_pool"]
