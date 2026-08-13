# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Descriptor-bound reads and writes for already-captured directory trees."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import logging
import os
import secrets
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Iterator, Mapping, TypeVar

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows fails closed when requested
    _fcntl = None  # type: ignore[assignment]

from . import _windows_fs_authority as _windows_fs
from ._atomic_directory import (
    _MAX_OWNERSHIP_COMPONENT_BYTES,
    DirectoryOrphan,
    PublicationDirectoryReader,
    TreeFileRecord,
    _annotate_secondary_error,
    _open_publication_authority,
    _PosixResourceOwner,
    _PublicationAuthority,
    _PublicationAuthorityOwner,
    _publish_staged_directory_with_authority,
    _rename_noreplace_at,
    _require_matching_ownership,
    _validate_ownership_inventory_budget,
    directory_ownership_entry_identities,
    directory_ownership_file_records,
    directory_ownership_inventory,
    directory_ownership_root_identity,
    directory_ownership_root_version_identity,
    discard_owned_directory,
    lexical_directory_path,
    publication_parent_identity,
    publish_staged_directory,
)
from ._owned_file_publication import _CancellationSafeRLock, _DescriptorOwner
from ._owned_file_publication import _file_identity as _owned_file_identity

logger = logging.getLogger(__name__)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_MAX_RELATIVE_PATH_BYTES = 4_096
_MAX_COMPONENTS = 256
_COPY_BYTES = 1024 * 1024
_MAX_IN_MEMORY_SNAPSHOT_BYTES = 512 * 1024 * 1024
_MAX_SNAPSHOT_CONSUMER_READ_BYTES = 8 * 1024 * 1024
_MFD_CLOEXEC = 0x0001
_MFD_ALLOW_SEALING = 0x0002
_F_ADD_SEALS = 1033
_F_GET_SEALS = 1034
_F_SEAL_SEAL = 0x0001
_F_SEAL_SHRINK = 0x0002
_F_SEAL_GROW = 0x0004
_F_SEAL_WRITE = 0x0008
_REQUIRED_SNAPSHOT_SEALS = _F_SEAL_SEAL | _F_SEAL_SHRINK | _F_SEAL_GROW | _F_SEAL_WRITE
_SNAPSHOT_UNAVAILABLE_ERRNOS = {
    value
    for value in (
        getattr(errno, "EACCES", None),
        getattr(errno, "EINVAL", None),
        getattr(errno, "ENOSYS", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "EPERM", None),
    )
    if value is not None
}
_UNSET_DESTINATION_OWNERSHIP = object()
_MAX_WORKSPACE_FILE_BYTES = 64 * 1024 * 1024 * 1024
_MAX_WORKSPACE_TOTAL_BYTES = 64 * 1024 * 1024 * 1024
_WORKSPACE_PLAN_DOMAIN = b"codenib-owned-workspace-plan-v1"
_WORKSPACE_RECEIPT_EMPTY = object()
_WORKSPACE_RECEIPT_CLOSED = object()
_WORKSPACE_RECEIPT_CLOSE = object()
_WORKSPACE_OWNER_RECOVERY_LIMIT = 64
_WorkspaceResult = TypeVar("_WorkspaceResult")


class _SnapshotUnavailable(RuntimeError):
    """The host cannot provide a sealed anonymous descriptor."""


def _create_sealable_memfd() -> int:
    """Create one anonymous Linux file or fail closed on unsupported hosts."""

    create = getattr(os, "memfd_create", None)
    if callable(create) and _fcntl is not None:
        try:
            return create(
                "codenib-authenticated-snapshot",
                _MFD_CLOEXEC | _MFD_ALLOW_SEALING,
            )
        except OSError as exc:
            if exc.errno not in _SNAPSHOT_UNAVAILABLE_ERRNOS:
                raise
            raise _SnapshotUnavailable(
                "sealed authenticated snapshots are unavailable on this host"
            ) from exc
    if os.name != "posix" or _fcntl is None:
        raise _SnapshotUnavailable(
            "sealed authenticated snapshots are unavailable on this host"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    create = getattr(libc, "memfd_create", None)
    if create is None:
        raise _SnapshotUnavailable(
            "sealed authenticated snapshots are unavailable on this host"
        )
    create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    create.restype = ctypes.c_int
    descriptor = create(
        b"codenib-authenticated-snapshot",
        _MFD_CLOEXEC | _MFD_ALLOW_SEALING,
    )
    if descriptor < 0:
        error = ctypes.get_errno()
        failure = OSError(error, os.strerror(error))
        if error not in _SNAPSHOT_UNAVAILABLE_ERRNOS:
            raise failure
        raise _SnapshotUnavailable(
            "sealed authenticated snapshots are unavailable on this host"
        ) from failure
    return descriptor


def _write_all(descriptor: int, block: bytes) -> None:
    view = memoryview(block)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("could not write authenticated snapshot")
        view = view[written:]


def _seal_snapshot_descriptor(descriptor: int) -> None:
    """Seal one fully written anonymous snapshot against later mutation."""

    os.fsync(descriptor)
    if _fcntl is None:
        raise _SnapshotUnavailable(
            "sealed authenticated snapshots are unavailable on this host"
        )
    try:
        _fcntl.fcntl(descriptor, _F_ADD_SEALS, _REQUIRED_SNAPSHOT_SEALS)
        observed = _fcntl.fcntl(descriptor, _F_GET_SEALS)
    except OSError as exc:
        if exc.errno not in _SNAPSHOT_UNAVAILABLE_ERRNOS:
            raise
        raise _SnapshotUnavailable(
            "sealed authenticated snapshots are unavailable on this host"
        ) from exc
    if observed & _REQUIRED_SNAPSHOT_SEALS != _REQUIRED_SNAPSHOT_SEALS:
        raise _SnapshotUnavailable("authenticated snapshot could not be sealed")
    os.lseek(descriptor, 0, os.SEEK_SET)


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
        getattr(metadata, "st_file_attributes", 0),
    )


def _opened_file_identity(metadata: object) -> tuple[object, ...]:
    if isinstance(metadata, _windows_fs.WindowsHandleMetadata):
        return _windows_fs.windows_metadata_identity(metadata)
    return _file_identity(metadata)  # type: ignore[arg-type]


def _use_windows_handles() -> bool:
    return sys.platform == "win32"


class _WindowsCleanupSlot:
    """Retain retryable HANDLE owners before native acquisition starts."""

    def __init__(self) -> None:
        self.resources: list[object] = []

    @property
    def closed(self) -> bool:
        return not self.resources

    def own(self, resource: object) -> None:
        if not any(candidate is resource for candidate in self.resources):
            self.resources.append(resource)

    def forget(self, resource: object) -> None:
        for index, candidate in enumerate(self.resources):
            if candidate is resource:
                self.resources.pop(index)
                return

    def close(self) -> None:
        primary: BaseException | None = None
        for resource in reversed(tuple(self.resources)):
            try:
                resource.close()  # type: ignore[attr-defined]
            except BaseException as exc:  # noqa: B036 - visit every owner
                if primary is None:
                    primary = exc
            try:
                closed = bool(resource.closed)  # type: ignore[attr-defined]
            except BaseException as exc:  # noqa: B036 - retain uncertain owner
                if primary is None:
                    primary = exc
                closed = False
            if closed:
                self.forget(resource)
        if primary is not None:
            raise primary


class _WindowsHandleOwner:
    """Own a small set of read HANDLEs with retryable close reconciliation."""

    def __init__(
        self,
        api: _windows_fs.WindowsKernelApi,
        cleanup_slot: _WindowsCleanupSlot,
    ) -> None:
        self.api = api
        self.cleanup_slot = cleanup_slot
        self.handles: list[int] = []
        self.identities: dict[int, tuple[object, ...]] = {}
        cleanup_slot.own(self)

    @property
    def closed(self) -> bool:
        return not self.handles

    def acquire(self, operation: Callable[[], int]) -> int:
        handle = 0
        try:
            handle = operation()
            self.handles.append(handle)
            return handle
        except BaseException:  # noqa: B036 - commit returned HANDLE ownership
            if handle and handle not in self.handles:
                try:
                    self.handles.append(handle)
                except BaseException:  # noqa: B036 - preserve acquisition failure
                    pass
            raise

    def bind(self, handle: int, metadata: _windows_fs.WindowsHandleMetadata) -> None:
        self.identities[handle] = _windows_fs.windows_handle_ownership_identity(
            metadata
        )

    def close(self) -> None:
        failure = _windows_fs._close_windows_handles(  # type: ignore[attr-defined]
            self.api,
            self.handles,
            self.identities,
        )
        if failure is not None:
            raise failure
        if self.closed:
            self.cleanup_slot.forget(self)


def _attach_cleanup_owner(failure: BaseException, owner: object) -> None:
    try:
        failure.captured_directory_cleanup_owner = owner  # type: ignore[attr-defined]
    except BaseException:  # noqa: B036 - local ownership remains authoritative
        pass


def _root_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0),
    )


def _captured_version_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0,
        getattr(metadata, "st_file_attributes", 0),
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _relative_path(value: str | Path | PurePosixPath) -> PurePosixPath:
    raw = value.as_posix() if isinstance(value, (Path, PurePosixPath)) else value
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise ValueError("captured directory path must be relative POSIX")
    relative = PurePosixPath(raw)
    if (
        relative.is_absolute()
        or relative.as_posix() != raw
        or any(part in {"", ".", ".."} for part in relative.parts)
        or len(relative.parts) > _MAX_COMPONENTS
        or any(
            len(os.fsencode(part)) > _MAX_OWNERSHIP_COMPONENT_BYTES
            for part in relative.parts
        )
        or len(os.fsencode(raw)) > _MAX_RELATIVE_PATH_BYTES
    ):
        raise ValueError("captured directory path must be normalized and bounded")
    return relative


def _workspace_mode(value: int, *, directory: bool) -> int:
    label = "workspace directory mode" if directory else "workspace file mode"
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < 0 or value > 0o777:
        raise ValueError(f"{label} must contain only portable permission bits")
    if value & 0o022:
        raise ValueError(f"{label} must not be group/world writable")
    required = 0o700 if directory else 0o400
    if value & required != required:
        requirement = "owner rwx" if directory else "owner read"
        raise ValueError(f"{label} must grant {requirement} permission")
    return value


def _workspace_frame(digest: "hashlib._Hash", domain: bytes, payload: bytes) -> None:
    digest.update(len(domain).to_bytes(2, "big"))
    digest.update(domain)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


@dataclass(frozen=True, slots=True)
class WorkspaceDirectory:
    """One directory that a trusted provisioner must pre-open for a plan."""

    path: PurePosixPath
    mode: int = 0o700

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _relative_path(self.path))
        object.__setattr__(self, "mode", _workspace_mode(self.mode, directory=True))


@dataclass(frozen=True, slots=True)
class WorkspaceFile:
    """One required regular-file slot in an exact workspace plan."""

    path: PurePosixPath
    mode: int = 0o600
    max_bytes: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _relative_path(self.path))
        object.__setattr__(self, "mode", _workspace_mode(self.mode, directory=False))
        if isinstance(self.max_bytes, bool) or not isinstance(self.max_bytes, int):
            raise TypeError("workspace file size limit must be an integer")
        if self.max_bytes < 0 or self.max_bytes > _MAX_WORKSPACE_FILE_BYTES:
            raise ValueError("workspace file size limit is out of bounds")


@dataclass(frozen=True, slots=True)
class WorkspacePlan:
    """Immutable exact skeleton and required file slots for one workspace."""

    subject_digest: str
    directories: tuple[WorkspaceDirectory, ...] = ()
    files: tuple[WorkspaceFile, ...] = ()
    root_mode: int = 0o700
    digest: str = ""

    def __post_init__(self) -> None:
        subject = self.subject_digest
        if (
            not isinstance(subject, str)
            or len(subject) != 64
            or any(character not in "0123456789abcdef" for character in subject)
        ):
            raise ValueError("workspace plan subject digest must be lowercase sha256")
        directories = tuple(self.directories)
        files = tuple(self.files)
        if not all(
            isinstance(directory_item, WorkspaceDirectory)
            for directory_item in directories
        ):
            raise TypeError("workspace plan directories must be WorkspaceDirectory")
        if not all(isinstance(file_item, WorkspaceFile) for file_item in files):
            raise TypeError("workspace plan files must be WorkspaceFile")
        root_mode = _workspace_mode(self.root_mode, directory=True)

        directory_by_path: dict[str, WorkspaceDirectory] = {}
        file_by_path: dict[str, WorkspaceFile] = {}
        portable_paths: dict[str, str] = {}
        for directory_item in directories:
            path = directory_item.path.as_posix()
            if path in directory_by_path:
                raise ValueError(f"workspace plan repeats directory: {path}")
            portable = path.casefold()
            previous = portable_paths.get(portable)
            if previous is not None:
                raise ValueError(
                    "workspace plan has a portable path collision: "
                    f"{previous!r} and {path!r}"
                )
            portable_paths[portable] = path
            directory_by_path[path] = directory_item
        total_bytes = 0
        for file_item in files:
            path = file_item.path.as_posix()
            if path in file_by_path:
                raise ValueError(f"workspace plan repeats file: {path}")
            if path in directory_by_path:
                raise ValueError(
                    f"workspace plan path is both file and directory: {path}"
                )
            portable = path.casefold()
            previous = portable_paths.get(portable)
            if previous is not None:
                raise ValueError(
                    "workspace plan has a portable path collision: "
                    f"{previous!r} and {path!r}"
                )
            portable_paths[portable] = path
            file_by_path[path] = file_item
            total_bytes += file_item.max_bytes
            if total_bytes > _MAX_WORKSPACE_TOTAL_BYTES:
                raise ValueError("workspace plan total byte limit is out of bounds")

        for path in tuple(directory_by_path) + tuple(file_by_path):
            relative = PurePosixPath(path)
            for depth in range(1, len(relative.parts)):
                ancestor = "/".join(relative.parts[:depth])
                if ancestor not in directory_by_path:
                    raise ValueError(
                        f"workspace plan is missing directory ancestor: {ancestor}"
                    )

        canonical_directories = tuple(
            sorted(directories, key=lambda item: item.path.as_posix())
        )
        canonical_files = tuple(sorted(files, key=lambda item: item.path.as_posix()))
        try:
            _validate_ownership_inventory_budget(
                os.fsencode(item.path.as_posix())
                for item in (*canonical_directories, *canonical_files)
            )
        except RuntimeError as exc:
            raise ValueError(
                "workspace plan exceeds the ownership scanner budget"
            ) from exc
        plan_digest = hashlib.sha256()
        _workspace_frame(plan_digest, b"domain", _WORKSPACE_PLAN_DOMAIN)
        _workspace_frame(plan_digest, b"subject", subject.encode("ascii"))
        _workspace_frame(plan_digest, b"root-mode", root_mode.to_bytes(2, "big"))
        for directory_item in canonical_directories:
            _workspace_frame(
                plan_digest,
                b"directory-path",
                os.fsencode(directory_item.path.as_posix()),
            )
            _workspace_frame(
                plan_digest,
                b"directory-mode",
                directory_item.mode.to_bytes(2, "big"),
            )
        for file_item in canonical_files:
            _workspace_frame(
                plan_digest,
                b"file-path",
                os.fsencode(file_item.path.as_posix()),
            )
            _workspace_frame(
                plan_digest,
                b"file-mode",
                file_item.mode.to_bytes(2, "big"),
            )
            _workspace_frame(
                plan_digest,
                b"file-max-bytes",
                file_item.max_bytes.to_bytes(8, "big"),
            )
        _workspace_frame(
            plan_digest,
            b"entry-count",
            (len(canonical_directories) + len(canonical_files)).to_bytes(8, "big"),
        )
        object.__setattr__(self, "directories", canonical_directories)
        object.__setattr__(self, "files", canonical_files)
        object.__setattr__(self, "root_mode", root_mode)
        object.__setattr__(self, "digest", plan_digest.hexdigest())


def _snapshot_workspace_plan(plan: object) -> WorkspacePlan:
    """Detach one exact plan from caller-visible frozen dataclasses."""

    if type(plan) is not WorkspacePlan:
        raise TypeError("owned workspace plan must be an exact WorkspacePlan")
    subject_digest = plan.subject_digest
    root_mode = plan.root_mode
    source_directories = plan.directories
    source_files = plan.files
    source_digest = plan.digest
    if (
        type(subject_digest) is not str
        or type(root_mode) is not int
        or type(source_directories) is not tuple
        or type(source_files) is not tuple
        or type(source_digest) is not str
    ):
        raise TypeError("owned workspace plan fields must use exact types")

    directory_fields: list[tuple[str, int]] = []
    file_fields: list[tuple[str, int, int]] = []

    def snapshot_paths() -> Iterator[bytes]:
        for directory in source_directories:
            if type(directory) is not WorkspaceDirectory:
                raise TypeError("owned workspace directories must use exact types")
            path = directory.path
            mode = directory.mode
            if type(path) is not PurePosixPath or type(mode) is not int:
                raise TypeError("owned workspace directory fields must use exact types")
            path_text = path.as_posix()
            directory_fields.append((path_text, mode))
            yield os.fsencode(path_text)
        for file in source_files:
            if type(file) is not WorkspaceFile:
                raise TypeError("owned workspace files must use exact types")
            path = file.path
            mode = file.mode
            max_bytes = file.max_bytes
            if (
                type(path) is not PurePosixPath
                or type(mode) is not int
                or type(max_bytes) is not int
            ):
                raise TypeError("owned workspace file fields must use exact types")
            path_text = path.as_posix()
            file_fields.append((path_text, mode, max_bytes))
            yield os.fsencode(path_text)

    try:
        _validate_ownership_inventory_budget(snapshot_paths())
    except RuntimeError as exc:
        raise ValueError(
            "owned workspace plan exceeds the ownership scanner budget"
        ) from exc

    directories = tuple(
        WorkspaceDirectory(
            PurePosixPath(path),
            mode=mode,
        )
        for path, mode in directory_fields
    )
    files = tuple(
        WorkspaceFile(
            PurePosixPath(path),
            mode=mode,
            max_bytes=max_bytes,
        )
        for path, mode, max_bytes in file_fields
    )
    detached = WorkspacePlan(
        subject_digest=subject_digest,
        directories=directories,
        files=files,
        root_mode=root_mode,
    )
    if detached.digest != source_digest:
        raise ValueError("owned workspace plan digest is inconsistent")
    return detached


class UnsupportedWorkspaceCreation(RuntimeError):
    """Strict workspace creation is unavailable without a native creator."""


@dataclass(slots=True)
class _WorkspaceFileOwner:
    owner: _DescriptorOwner
    identity: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class _WorkspacePublicationTransfer:
    """Shared aggregate owner installed before any publication mutation."""

    workspace: "OwnedWorkspaceAuthority"


@dataclass(frozen=True, slots=True)
class _WorkspaceReservation:
    transfer: _WorkspacePublicationTransfer


@dataclass(slots=True)
class _WorkspaceCleanup:
    transfer: _WorkspacePublicationTransfer
    attempted: bool = False


class PublishedWorkspaceReceipt:
    """Borrowed exact-generation receipt controlled by its caller-owned slot."""

    __slots__ = (
        "path",
        "_plan",
        "sealed_ownership",
        "ownership",
        "parent_identity",
        "orphan",
        "durable",
        "_transfer",
        "_owner_pid",
    )

    def __init__(
        self,
        *,
        transfer: _WorkspacePublicationTransfer,
        path: Path,
        plan: WorkspacePlan,
        sealed_ownership: object,
        published_ownership: object,
        parent_identity: tuple[int, ...],
        orphan: DirectoryOrphan | None,
    ) -> None:
        # Store the shared aggregate first.  A constructor interruption can
        # therefore be reconciled against the same transfer held by the owner.
        self._transfer = transfer
        self.path = path
        self._plan = _snapshot_workspace_plan(plan)
        self.sealed_ownership = sealed_ownership
        self.ownership = published_ownership
        self.parent_identity = parent_identity
        self.orphan = orphan
        self.durable = True
        self._owner_pid = os.getpid()

    @property
    def plan_digest(self) -> str:
        return self._plan.digest

    @property
    def plan(self) -> WorkspacePlan:
        """Return a detached diagnostic copy of the publication plan."""

        return _snapshot_workspace_plan(self._plan)

    @property
    def closed(self) -> bool:
        return self._transfer.workspace._publication_transfer_closed(self._transfer)

    def close(self) -> None:
        raise RuntimeError("close the PublishedWorkspaceReceiptOwner, not its receipt")

    def _consume_from_owner(
        self,
        close_authority: object,
        callback: Callable[
            ["PublishedWorkspaceReceipt", PublicationDirectoryReader],
            _WorkspaceResult,
        ],
    ) -> _WorkspaceResult:
        if close_authority is not _WORKSPACE_RECEIPT_CLOSE:
            raise RuntimeError("published workspace receipt authority is invalid")
        if os.getpid() != self._owner_pid:
            raise RuntimeError(
                "published workspace receipt cannot cross a PID boundary"
            )
        return self._transfer.workspace._consume_published_workspace(
            self,
            callback,
        )

    def _close_from_owner(self, close_authority: object) -> None:
        if close_authority is not _WORKSPACE_RECEIPT_CLOSE:
            raise RuntimeError("published workspace receipt close authority is invalid")
        self._transfer.workspace._close_publication_transfer(self._transfer)


class PublishedWorkspaceReceiptOwner:
    """Caller-precreated one-shot owner for a published workspace generation."""

    __slots__ = ("_slot", "_lock", "_owner_pid", "_process_locks")

    def __init__(self) -> None:
        self._slot: object = _WORKSPACE_RECEIPT_EMPTY
        self._lock = _CancellationSafeRLock()
        self._owner_pid = os.getpid()
        self._process_locks = {self._owner_pid: self._lock}

    @property
    def state(self) -> str:
        self._require_owner_pid()

        def observe() -> str:
            self._normalize_closed_locked()
            return self._state_locked()

        return self._lock.run(observe)

    @property
    def active(self) -> bool:
        return self.state == "active"

    @property
    def closed(self) -> bool:
        return self.state == "closed"

    @property
    def receipt(self) -> PublishedWorkspaceReceipt:
        self._require_owner_pid()

        def borrow() -> PublishedWorkspaceReceipt:
            self._normalize_closed_locked()
            if not isinstance(self._slot, PublishedWorkspaceReceipt):
                raise RuntimeError(
                    "published workspace receipt owner is "
                    f"{self._state_locked()}, expected active"
                )
            return self._slot

        return self._lock.run(borrow)

    def consume(
        self,
        callback: Callable[
            [PublishedWorkspaceReceipt, PublicationDirectoryReader],
            _WorkspaceResult,
        ],
    ) -> _WorkspaceResult:
        """Synchronously borrow one exact authenticated publication reader."""

        if not callable(callback):
            raise TypeError("published workspace receipt consumer must be callable")
        self._require_owner_pid()
        if self._lock.held_by_current_thread():
            raise RuntimeError("published workspace receipt consume is reentrant")

        def consume_locked() -> _WorkspaceResult:
            self._normalize_closed_locked()
            if not isinstance(self._slot, PublishedWorkspaceReceipt):
                raise RuntimeError(
                    "published workspace receipt owner is "
                    f"{self._state_locked()}, expected active"
                )
            return self._slot._consume_from_owner(
                _WORKSPACE_RECEIPT_CLOSE,
                callback,
            )

        return self._lock.run(consume_locked)

    def close(self) -> None:
        current_pid = os.getpid()
        owner_changed = current_pid != self._owner_pid
        if owner_changed:
            lifecycle_lock = self._process_locks.setdefault(
                current_pid,
                _CancellationSafeRLock(),
            )
        else:
            if self._lock.held_by_current_thread():
                raise RuntimeError("published workspace receipt close is reentrant")
            lifecycle_lock = self._lock

        close_error: BaseException | None = None
        try:
            lifecycle_lock.run(self._close_locked)
        except BaseException as exc:  # noqa: B036 - reconcile before PID report
            close_error = exc

        if owner_changed:
            boundary_error = RuntimeError(
                "published workspace receipt owner cannot cross a PID boundary"
            )
            if close_error is not None:
                raise boundary_error from close_error
            raise boundary_error
        if close_error is not None:
            raise close_error

    def _close_locked(self) -> None:
        self._normalize_closed_locked()
        if self._slot is _WORKSPACE_RECEIPT_CLOSED:
            return
        if self._slot is _WORKSPACE_RECEIPT_EMPTY:
            self._slot = _WORKSPACE_RECEIPT_CLOSED
            return
        if isinstance(self._slot, _WorkspaceReservation):
            if os.getpid() == self._owner_pid:
                raise RuntimeError(
                    "published workspace receipt publication is in progress"
                )
            # The child owns only its inherited descriptor references.  It
            # cannot wait for or affect the parent's in-flight publication,
            # so revoke the child copy and run the transfer's raw inherited-fd
            # cleanup before reporting the PID boundary.
            cleanup = _WorkspaceCleanup(self._slot.transfer, attempted=True)
            self._slot = cleanup
        if isinstance(self._slot, PublishedWorkspaceReceipt):
            receipt = self._slot
            cleanup = _WorkspaceCleanup(receipt._transfer, attempted=True)
            self._slot = cleanup
        elif isinstance(self._slot, _WorkspaceCleanup):
            cleanup = self._slot
            cleanup.attempted = True
        else:  # pragma: no cover - all private slot states are exhaustive
            raise RuntimeError("published workspace receipt owner state is invalid")

        primary_error: BaseException | None = None
        try:
            cleanup.transfer.workspace._close_publication_transfer(cleanup.transfer)
        except BaseException as close_error:  # noqa: B036 - settle aggregate
            primary_error = close_error
        try:
            if cleanup.transfer.workspace._publication_transfer_closed(
                cleanup.transfer
            ):
                self._slot = _WORKSPACE_RECEIPT_CLOSED
        except BaseException as observation_error:  # noqa: B036
            if primary_error is None:
                primary_error = observation_error
            else:
                _annotate_secondary_error(
                    primary_error,
                    "workspace receipt close-state observation also failed",
                    observation_error,
                )
        if primary_error is not None:
            raise primary_error

    def close_after_error(self, primary_error: BaseException) -> None:
        try:
            self.close()
        except BaseException as cleanup_error:  # noqa: B036 - keep primary
            _annotate_secondary_error(
                primary_error,
                "published workspace receipt cleanup also failed",
                cleanup_error,
            )

    def _reserve(self, reservation: _WorkspaceReservation) -> None:
        self._require_owner_pid()

        def reserve() -> None:
            if self._slot is not _WORKSPACE_RECEIPT_EMPTY:
                raise RuntimeError(
                    "published workspace receipt owner is "
                    f"{self._state_locked()}, expected empty"
                )
            self._slot = reservation

        self._lock.run(reserve)

    def _install(
        self,
        reservation: _WorkspaceReservation,
        receipt: PublishedWorkspaceReceipt,
    ) -> None:
        self._require_owner_pid()

        def install() -> None:
            if self._slot is not reservation:
                raise RuntimeError("published workspace receipt reservation changed")
            if receipt._transfer is not reservation.transfer:
                raise RuntimeError("published workspace receipt transfer changed")
            self._slot = receipt

        self._lock.run(install)

    def _owns_transfer(self, transfer: _WorkspacePublicationTransfer) -> bool:
        def owns() -> bool:
            return self._slot_transfer_locked() is transfer

        return self._lock.run(owns)

    def _has_active_transfer(self, transfer: _WorkspacePublicationTransfer) -> bool:
        def active() -> bool:
            return (
                isinstance(self._slot, PublishedWorkspaceReceipt)
                and self._slot._transfer is transfer
            )

        return self._lock.run(active)

    def _transfer_state(self, transfer: _WorkspacePublicationTransfer) -> str:
        def observe() -> str:
            if isinstance(self._slot, PublishedWorkspaceReceipt):
                return "active" if self._slot._transfer is transfer else "absent"
            if isinstance(self._slot, _WorkspaceReservation):
                return "reserved" if self._slot.transfer is transfer else "absent"
            if isinstance(self._slot, _WorkspaceCleanup):
                return "cleanup" if self._slot.transfer is transfer else "absent"
            return "absent"

        return self._lock.run(observe)

    def _cancel_reservation(self, reservation: _WorkspaceReservation) -> None:
        deferred: BaseException | None = None
        for _attempt in range(_WORKSPACE_OWNER_RECOVERY_LIMIT):
            try:
                terminal = self._lock.run(
                    lambda: self._cancel_reservation_locked(reservation)
                )
            except BaseException as cleanup_error:  # noqa: B036 - converge slot
                if deferred is None:
                    deferred = cleanup_error
                else:
                    _annotate_secondary_error(
                        deferred,
                        "workspace reservation cancellation also failed",
                        cleanup_error,
                    )
                continue
            if terminal:
                if deferred is not None:
                    raise deferred
                return
        if deferred is not None:
            raise deferred
        raise RuntimeError("workspace receipt reservation did not converge")

    def _cancel_reservation_locked(
        self,
        reservation: _WorkspaceReservation,
    ) -> bool:
        if self._slot is reservation:
            self._slot = _WorkspaceCleanup(reservation.transfer)
        return self._slot is not reservation

    def _normalize_closed_locked(self) -> None:
        transfer = self._slot_transfer_locked()
        if (
            transfer is not None
            and not isinstance(self._slot, _WorkspaceReservation)
            and transfer.workspace._publication_transfer_closed(transfer)
        ):
            self._slot = _WORKSPACE_RECEIPT_CLOSED

    def _slot_transfer_locked(self) -> _WorkspacePublicationTransfer | None:
        if isinstance(self._slot, _WorkspaceReservation):
            return self._slot.transfer
        if isinstance(self._slot, _WorkspaceCleanup):
            return self._slot.transfer
        if isinstance(self._slot, PublishedWorkspaceReceipt):
            return self._slot._transfer
        return None

    def _state_locked(self) -> str:
        if self._slot is _WORKSPACE_RECEIPT_EMPTY:
            return "empty"
        if self._slot is _WORKSPACE_RECEIPT_CLOSED:
            return "closed"
        if isinstance(self._slot, _WorkspaceReservation):
            return "reserved"
        if isinstance(self._slot, PublishedWorkspaceReceipt):
            return "active"
        if isinstance(self._slot, _WorkspaceCleanup):
            return "close-failed" if self._slot.attempted else "cleanup"
        raise RuntimeError("published workspace receipt owner state is invalid")

    def _require_owner_pid(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError(
                "published workspace receipt owner cannot cross a PID boundary"
            )

    def __enter__(self) -> "PublishedWorkspaceReceiptOwner":
        self._require_owner_pid()
        return self

    def __exit__(
        self,
        _exc_type: object,
        exc: BaseException | None,
        _traceback: object,
    ) -> None:
        if exc is None:
            self.close()
        else:
            self.close_after_error(exc)


class AuthenticatedSnapshot:
    """Read-only immutable bytes produced by one successful authentication."""

    __slots__ = (
        "record",
        "_descriptor",
        "_chunks",
        "_chunk_index",
        "_chunk_offset",
        "_consumed",
        "_closed",
    )

    def __init__(
        self,
        record: TreeFileRecord,
        *,
        descriptor: int = -1,
        chunks: tuple[bytes, ...] | None = None,
    ) -> None:
        if (descriptor >= 0) == (chunks is not None):
            raise ValueError("authenticated snapshot needs exactly one backing")
        self.record = record
        self._descriptor = descriptor
        self._chunks = () if chunks is None else chunks
        self._chunk_index = 0
        self._chunk_offset = 0
        self._consumed = 0
        self._closed = False

    @property
    def descriptor(self) -> int:
        if self._closed or self._descriptor < 0:
            raise RuntimeError("authenticated snapshot has no sealed descriptor")
        return self._descriptor

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError("authenticated snapshot is closed")
        if self._descriptor >= 0:
            requested = (
                size
                if size is not None and size >= 0
                else self.record.size - self._consumed
            )
            block = os.read(self._descriptor, requested)
            self._consumed += len(block)
            return block
        remaining = self.record.size - self._consumed
        requested = remaining if size is None or size < 0 else min(size, remaining)
        if requested <= 0:
            return b""
        output = bytearray()
        while requested and self._chunk_index < len(self._chunks):
            chunk = self._chunks[self._chunk_index]
            available = len(chunk) - self._chunk_offset
            consumed = min(requested, available)
            output.extend(chunk[self._chunk_offset : self._chunk_offset + consumed])
            requested -= consumed
            self._consumed += consumed
            self._chunk_offset += consumed
            if self._chunk_offset == len(chunk):
                self._chunk_index += 1
                self._chunk_offset = 0
        return bytes(output)

    def readline(self, size: int = -1) -> bytes:
        if isinstance(size, bool) or not isinstance(size, int):
            raise TypeError("authenticated snapshot readline size must be an integer")
        limit = self.record.size if size < 0 else size
        output = bytearray()
        while len(output) < limit:
            block = self.read(min(_COPY_BYTES, limit - len(output)))
            if not block:
                break
            newline = block.find(b"\n")
            if newline < 0:
                output.extend(block)
                continue
            output.extend(block[: newline + 1])
            # Pickle only uses readline during sequential consumption. A line
            # with trailing bytes is rare; preserve them by rewinding the
            # sealed fd or the immutable chunk cursor.
            trailing = len(block) - newline - 1
            if trailing:
                self._rewind(trailing)
            break
        return bytes(output)

    def _rewind(self, count: int) -> None:
        if self._descriptor >= 0:
            os.lseek(self._descriptor, -count, os.SEEK_CUR)
            self._consumed -= count
            return
        self._consumed -= count
        while count:
            if self._chunk_offset:
                moved = min(count, self._chunk_offset)
                self._chunk_offset -= moved
                count -= moved
            else:
                self._chunk_index -= 1
                self._chunk_offset = len(self._chunks[self._chunk_index])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._descriptor >= 0:
            try:
                os.close(self._descriptor)
            except OSError:
                pass
            self._descriptor = -1
        self._chunks = ()

    def __enter__(self) -> AuthenticatedSnapshot:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class AuthenticatedSnapshotReader:
    """Bound each parser read while exposing only authenticated immutable bytes."""

    __slots__ = ("record", "_snapshot", "_remaining")

    def __init__(self, snapshot: AuthenticatedSnapshot) -> None:
        self.record = snapshot.record
        self._snapshot = snapshot
        self._remaining = snapshot.record.size

    def read(self, size: int = -1) -> bytes:
        if isinstance(size, bool) or not isinstance(size, int):
            raise TypeError("authenticated snapshot read size must be an integer")
        if self._remaining <= 0 or size == 0:
            return b""
        requested = (
            _MAX_SNAPSHOT_CONSUMER_READ_BYTES
            if size < 0
            else min(size, _MAX_SNAPSHOT_CONSUMER_READ_BYTES)
        )
        block = self._snapshot.read(min(requested, self._remaining))
        self._remaining -= len(block)
        return block

    def readline(self, size: int = -1) -> bytes:
        if isinstance(size, bool) or not isinstance(size, int):
            raise TypeError("authenticated snapshot readline size must be an integer")
        if self._remaining <= 0 or size == 0:
            return b""
        requested = (
            _MAX_SNAPSHOT_CONSUMER_READ_BYTES
            if size < 0
            else min(size, _MAX_SNAPSHOT_CONSUMER_READ_BYTES)
        )
        block = self._snapshot.readline(min(requested, self._remaining))
        self._remaining -= len(block)
        return block


class AuthenticatedFile:
    """One opened fd or HANDLE whose bytes match the initial tree record."""

    def __init__(
        self,
        descriptor: int,
        opened: object,
        record: TreeFileRecord,
        *,
        read_callback: Callable[[int], bytes] | None = None,
        metadata_callback: Callable[[], object] | None = None,
        rewind_callback: Callable[[], None] | None = None,
        verify_callback: Callable[[], None] | None = None,
        close_callback: Callable[[], None] | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.opened = opened
        self.record = record
        self._hasher = hashlib.sha256()
        self._consumed = 0
        self._authenticated = False
        self._closed = False
        self._read_callback = read_callback or (
            lambda size: os.read(self.descriptor, size)
        )
        self._metadata_callback = metadata_callback or (
            lambda: os.fstat(self.descriptor)
        )
        self._rewind_callback = rewind_callback or (
            lambda: os.lseek(self.descriptor, 0, os.SEEK_SET)
        )
        self._verify_callback = verify_callback or (lambda: None)
        self._close_callback = close_callback

    @property
    def size(self) -> int:
        return self.record.size

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError("authenticated file is closed")
        remaining = self.record.size - self._consumed
        if size is None or size < 0:
            requested = remaining + 1
        else:
            requested = min(size, remaining + 1)
        if requested == 0:
            return b""
        try:
            block = self._read_callback(requested)
        except OSError as exc:
            raise ValueError(
                f"captured file is not readable: {self.record.path}"
            ) from exc
        if not isinstance(block, bytes):
            raise RuntimeError("captured file backend returned non-bytes data")
        if self._consumed + len(block) > self.record.size:
            raise ValueError(f"captured file grew while reading: {self.record.path}")
        self._hasher.update(block)
        self._consumed += len(block)
        return block

    def readline(self, size: int = -1) -> bytes:
        """Provide the file protocol required by trusted-local Unpickler."""

        if isinstance(size, bool) or not isinstance(size, int):
            raise TypeError("authenticated readline size must be an integer")
        limit = self.record.size - self._consumed if size < 0 else size
        payload = bytearray()
        while len(payload) < limit:
            block = self.read(1)
            if not block:
                break
            payload.extend(block)
            if block == b"\n":
                break
        return bytes(payload)

    def authenticate(self) -> None:
        if self._authenticated:
            return
        while self._consumed < self.record.size:
            block = self.read(min(_COPY_BYTES, self.record.size - self._consumed))
            if not block:
                raise ValueError(f"captured file was truncated: {self.record.path}")
        try:
            if self._read_callback(1):
                raise ValueError(
                    f"captured file grew while reading: {self.record.path}"
                )
            after = self._metadata_callback()
            self._verify_callback()
        except OSError as exc:
            raise ValueError(
                f"captured file changed while reading: {self.record.path}"
            ) from exc
        if (
            _opened_file_identity(after) != _opened_file_identity(self.opened)
            or self._hasher.hexdigest() != self.record.sha256
        ):
            raise ValueError(
                f"captured file differs from its initial record: {self.record.path}"
            )
        self._authenticated = True

    def _copy_authenticated_from_start(
        self,
        sink: Callable[[bytes], None],
    ) -> None:
        """Copy and independently authenticate every byte from offset zero."""

        self.authenticate()
        try:
            self._rewind_callback()
        except OSError as exc:
            raise ValueError(
                f"captured file could not be copied immutably: {self.record.path}"
            ) from exc

        remaining = self.record.size
        digest = hashlib.sha256()
        try:
            while remaining:
                block = self._read_callback(min(_COPY_BYTES, remaining))
                if not isinstance(block, bytes):
                    raise RuntimeError("captured file backend returned non-bytes data")
                if not block:
                    raise ValueError(f"captured file was truncated: {self.record.path}")
                if len(block) > remaining:
                    raise ValueError(
                        f"captured file grew while reading: {self.record.path}"
                    )
                sink(block)
                digest.update(block)
                remaining -= len(block)
            if self._read_callback(1):
                raise ValueError(
                    f"captured file grew while reading: {self.record.path}"
                )
            after = self._metadata_callback()
            self._verify_callback()
        except OSError as exc:
            raise ValueError(
                f"captured file could not be copied immutably: {self.record.path}"
            ) from exc
        if (
            _opened_file_identity(after) != _opened_file_identity(self.opened)
            or digest.hexdigest() != self.record.sha256
        ):
            raise ValueError(
                f"captured file differs from its initial record: {self.record.path}"
            )

    def sealed_snapshot(self) -> int:
        """Return an immutable private copy authenticated against the record."""

        snapshot = -1
        try:
            snapshot = _create_sealable_memfd()
            self._copy_authenticated_from_start(
                lambda block: _write_all(snapshot, block)
            )
            _seal_snapshot_descriptor(snapshot)
            owned = snapshot
            snapshot = -1
            return owned
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                f"captured file could not be copied immutably: {self.record.path}"
            ) from exc
        finally:
            if snapshot >= 0:
                os.close(snapshot)

    def immutable_snapshot(self) -> AuthenticatedSnapshot:
        """Authenticate into sealed Linux storage or bounded immutable bytes."""

        try:
            descriptor = self.sealed_snapshot()
        except ValueError as exc:
            if not isinstance(exc.__cause__, _SnapshotUnavailable):
                raise
            if self.record.size > _MAX_IN_MEMORY_SNAPSHOT_BYTES:
                raise ValueError(
                    "captured file needs a sealed snapshot above the "
                    f"{_MAX_IN_MEMORY_SNAPSHOT_BYTES}-byte fallback limit: "
                    f"{self.record.path}"
                ) from exc
            chunks: list[bytes] = []
            self._copy_authenticated_from_start(chunks.append)
            return AuthenticatedSnapshot(self.record, chunks=tuple(chunks))
        return AuthenticatedSnapshot(self.record, descriptor=descriptor)

    def verify_unchanged(self) -> None:
        self.authenticate()
        try:
            after = self._metadata_callback()
            self._verify_callback()
        except OSError as exc:
            raise ValueError(
                f"captured file changed after authentication: {self.record.path}"
            ) from exc
        if _opened_file_identity(after) != _opened_file_identity(self.opened):
            raise ValueError(
                f"captured file changed after authentication: {self.record.path}"
            )

    def close(self) -> None:
        if self._closed:
            return
        if self._close_callback is None:
            self._closed = True
            try:
                os.close(self.descriptor)
            except OSError:
                pass
            return
        owner = getattr(self._close_callback, "__self__", None)
        try:
            self._close_callback()
        except BaseException as close_error:  # noqa: B036 - retain retry owner
            try:
                self._closed = bool(getattr(owner, "closed", False))
            except BaseException:  # noqa: B036 - uncertain ownership stays live
                self._closed = False
            if not self._closed:
                _attach_cleanup_owner(close_error, self)
            raise
        self._closed = bool(getattr(owner, "closed", False))
        if not self._closed:
            failure = RuntimeError("captured file HANDLE cleanup is incomplete")
            _attach_cleanup_owner(failure, self)
            raise failure

    def __enter__(self) -> AuthenticatedFile:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            try:
                self.authenticate()
            except BaseException as primary:  # noqa: B036 - preserve auth failure
                try:
                    self.close()
                except BaseException as cleanup:  # noqa: B036
                    _attach_cleanup_owner(primary, self)
                    raise primary from cleanup
                raise
            self.close()
            return
        try:
            self.close()
        except BaseException:  # noqa: B036 - never replace the body failure
            if exc is not None:
                _attach_cleanup_owner(exc, self)


class CapturedDirectoryReader:
    """Read a fixed ownership token through one pinned no-follow authority."""

    def __init__(self, root: Path, ownership: object) -> None:
        if not _use_windows_handles() and (
            not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW")
        ):
            raise RuntimeError(
                "captured directory reads require no-follow directory descriptors"
            )
        self.root = lexical_directory_path(root)
        self.ownership = ownership
        self._descriptor = -1
        self._windows_slot: _WindowsCleanupSlot | None = None
        self._windows_authority: _windows_fs.WindowsDirectoryAuthority | None = None
        self._windows_api: _windows_fs.WindowsKernelApi | None = None
        self._records = {
            record.path: record
            for record in directory_ownership_file_records(ownership)  # type: ignore[arg-type]
        }
        self._entry_identities = {
            path: (kind, identity)
            for path, kind, identity in directory_ownership_entry_identities(
                ownership  # type: ignore[arg-type]
            )
        }
        try:
            if _use_windows_handles():
                self._open_windows_root()
                return
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
            self._descriptor = os.open(self.root, flags)
            opened = os.fstat(self._descriptor)
            if _root_identity(opened) != directory_ownership_root_identity(
                ownership
            ) or _captured_version_identity(
                opened
            ) != directory_ownership_root_version_identity(
                ownership
            ):
                raise RuntimeError("captured directory root changed after capture")
            self._opened_root = opened
        except BaseException as primary:  # noqa: B036 - preserve open failure
            try:
                self.close()
            except BaseException as cleanup:  # noqa: B036
                _attach_cleanup_owner(primary, self)
                raise primary from cleanup
            raise

    def _open_windows_root(self) -> None:
        slot = _WindowsCleanupSlot()
        self._windows_slot = slot
        authority = _windows_fs.open_lexical_directory_authority(
            self.root,
            cleanup_slot=slot,
        )
        self._windows_authority = authority
        self._windows_api = authority.api
        opened = authority.api.metadata(authority.handle)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or opened.delete_pending
            or not _windows_fs.windows_file_id_is_reliable(opened.file_id_128)
            or _root_identity(opened)
            != directory_ownership_root_identity(self.ownership)  # type: ignore[arg-type]
            or _captured_version_identity(opened)
            != directory_ownership_root_version_identity(
                self.ownership  # type: ignore[arg-type]
            )
        ):
            raise RuntimeError("captured directory root changed after capture")
        self._opened_root = opened
        authority.verify()

    def close(self) -> None:
        if self._windows_slot is not None:
            try:
                self._windows_slot.close()
            except BaseException as close_error:  # noqa: B036 - retryable owner
                _attach_cleanup_owner(close_error, self)
                raise
            if self._windows_slot.closed:
                self._windows_slot = None
                self._windows_authority = None
                self._windows_api = None
            return
        if self._descriptor >= 0:
            try:
                os.close(self._descriptor)
            except OSError:
                pass
            self._descriptor = -1

    def __enter__(self) -> CapturedDirectoryReader:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.close()
        except BaseException:  # noqa: B036 - preserve body failure
            if exc is None:
                raise
            _attach_cleanup_owner(exc, self)

    @property
    def file_records(self) -> tuple[TreeFileRecord, ...]:
        return tuple(sorted(self._records.values(), key=lambda record: record.path))

    def record(self, relative: str | Path | PurePosixPath) -> TreeFileRecord:
        normalized = _relative_path(relative).as_posix()
        try:
            return self._records[normalized]
        except KeyError as exc:
            raise ValueError(
                f"captured directory has no initial file record: {normalized}"
            ) from exc

    def verify_root(self) -> None:
        if self._windows_authority is not None and self._windows_api is not None:
            self._windows_authority.verify()
            opened = self._windows_api.metadata(self._windows_authority.handle)
            if _windows_fs.windows_metadata_identity(
                opened
            ) != _windows_fs.windows_metadata_identity(
                self._opened_root
            ) or _captured_version_identity(
                opened
            ) != directory_ownership_root_version_identity(
                self.ownership  # type: ignore[arg-type]
            ):
                raise RuntimeError("captured directory root changed")
            return
        try:
            opened = os.fstat(self._descriptor)
            path_metadata = self.root.lstat()
        except OSError as exc:
            raise RuntimeError("captured directory root changed") from exc
        if _file_identity(opened) != _file_identity(
            self._opened_root
        ) or _root_identity(path_metadata) != _root_identity(opened):
            raise RuntimeError("captured directory root changed")

    def _windows_find_child(
        self,
        api: _windows_fs.WindowsKernelApi,
        parent_handle: int,
        name: str,
    ) -> _windows_fs.WindowsDirectoryEntry | None:
        iterator = getattr(api, "iter_directory", None)
        entries = (
            iterator(parent_handle)
            if callable(iterator)
            else api.enumerate_directory(parent_handle)
        )
        folded = name.casefold()
        match: _windows_fs.WindowsDirectoryEntry | None = None
        entry_limit = len(self._entry_identities)
        for index, entry in enumerate(entries):
            if index >= entry_limit:
                raise RuntimeError(
                    "captured Windows directory exceeds its ownership entry limit"
                )
            if entry.name.casefold() != folded:
                continue
            if match is not None:
                raise RuntimeError(
                    "captured Windows directory has ambiguous child names"
                )
            match = entry
        return match

    def _windows_open_child(
        self,
        owner: _WindowsHandleOwner,
        parent_handle: int,
        name: str,
        *,
        expected_directory: bool,
    ) -> tuple[
        int, _windows_fs.WindowsDirectoryEntry, _windows_fs.WindowsHandleMetadata
    ]:
        assert self._windows_api is not None
        api = self._windows_api
        entry = self._windows_find_child(api, parent_handle, name)
        if entry is None:
            raise FileNotFoundError(name)
        is_directory = bool(entry.attributes & _windows_fs.FILE_ATTRIBUTE_DIRECTORY)
        if (
            is_directory != expected_directory
            or entry.attributes & _windows_fs.FILE_ATTRIBUTE_REPARSE_POINT
            or not _windows_fs.windows_file_id_is_reliable(entry.file_id_128)
        ):
            raise ValueError("captured path is not a private real entry")
        access = _windows_fs.FILE_READ_ATTRIBUTES | _windows_fs.SYNCHRONIZE
        access |= (
            _windows_fs.FILE_LIST_DIRECTORY
            if expected_directory
            else _windows_fs.FILE_READ_DATA
        )
        handle = owner.acquire(
            lambda: api.open_relative(
                parent_handle,
                entry.name,
                desired_access=access,
                is_directory=expected_directory,
                allow_reparse=False,
            )
        )
        opened = api.metadata(handle)
        owner.bind(handle, opened)
        rebound = self._windows_find_child(api, parent_handle, entry.name)
        if (
            opened.file_id_128 != entry.file_id_128
            or not _windows_fs.windows_file_id_is_reliable(opened.file_id_128)
            or opened.st_file_attributes & _windows_fs.FILE_ATTRIBUTE_REPARSE_POINT
            or opened.delete_pending
            or bool(opened.st_file_attributes & _windows_fs.FILE_ATTRIBUTE_DIRECTORY)
            != expected_directory
            or rebound is None
            or rebound.file_id_128 != entry.file_id_128
        ):
            raise RuntimeError("captured Windows entry changed while opening")
        return handle, entry, opened

    def _open_windows(
        self,
        relative: PurePosixPath,
    ) -> AuthenticatedFile:
        record = self.record(relative)
        if self._windows_authority is None or self._windows_api is None:
            raise RuntimeError("captured directory reader is closed")
        if self._windows_slot is None:
            raise RuntimeError("captured directory reader has no cleanup authority")
        self.verify_root()
        api = self._windows_api
        owner = _WindowsHandleOwner(api, self._windows_slot)
        parent_handle = self._windows_authority.handle
        bindings: list[tuple[int, str, bytes, int, tuple[int, ...]]] = []
        prefix: list[str] = []
        try:
            for part in relative.parts[:-1]:
                prefix.append(part)
                prefix_text = "/".join(prefix)
                expected_kind, expected_identity = self._entry_identities.get(
                    prefix_text,
                    (None, None),
                )
                handle, entry, opened = self._windows_open_child(
                    owner,
                    parent_handle,
                    part,
                    expected_directory=True,
                )
                if (
                    expected_kind != "directory"
                    or expected_identity is None
                    or _captured_version_identity(opened) != expected_identity
                ):
                    raise ValueError(
                        f"captured path is not a real directory: {relative}"
                    )
                bindings.append(
                    (
                        parent_handle,
                        entry.name,
                        entry.file_id_128,
                        handle,
                        expected_identity,
                    )
                )
                parent_handle = handle

            expected_kind, expected_identity = self._entry_identities.get(
                relative.as_posix(),
                (None, None),
            )
            handle, entry, opened = self._windows_open_child(
                owner,
                parent_handle,
                relative.name,
                expected_directory=False,
            )
            if (
                expected_kind != "file"
                or expected_identity is None
                or _captured_version_identity(opened) != expected_identity
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != record.mode
                or opened.st_size != record.size
            ):
                raise ValueError(f"captured path is not a private file: {relative}")

            def verify() -> None:
                current = api.metadata(handle)
                rebound_file = self._windows_find_child(
                    api,
                    parent_handle,
                    entry.name,
                )
                if (
                    _captured_version_identity(current) != expected_identity
                    or _windows_fs.windows_metadata_identity(current)
                    != _windows_fs.windows_metadata_identity(opened)
                    or rebound_file is None
                    or rebound_file.file_id_128 != entry.file_id_128
                ):
                    raise ValueError(f"captured file changed while reading: {relative}")
                for (
                    bound_parent,
                    name,
                    file_id,
                    directory_handle,
                    identity,
                ) in reversed(bindings):
                    rebound = self._windows_find_child(api, bound_parent, name)
                    if (
                        rebound is None
                        or rebound.file_id_128 != file_id
                        or _captured_version_identity(api.metadata(directory_handle))
                        != identity
                    ):
                        raise ValueError(
                            f"captured directory changed while reading: {relative}"
                        )
                self.verify_root()

            def rewind() -> None:
                raise OSError(
                    "captured authenticated descriptors are unavailable on Windows"
                )

            return AuthenticatedFile(
                handle,
                opened,
                record,
                read_callback=lambda size: api.read(handle, size),
                metadata_callback=lambda: api.metadata(handle),
                rewind_callback=rewind,
                verify_callback=verify,
                close_callback=owner.close,
            )
        except BaseException as primary:  # noqa: B036 - preserve open failure
            try:
                owner.close()
            except BaseException as cleanup:  # noqa: B036
                _attach_cleanup_owner(primary, owner)
                raise primary from cleanup
            raise

    def _open(self, relative: PurePosixPath) -> tuple[int, os.stat_result]:
        record = self.record(relative)
        directory_descriptor = os.dup(self._descriptor)
        source_descriptor = -1
        try:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(
                os, "O_NONBLOCK", 0
            )
            prefix: list[str] = []
            for part in relative.parts[:-1]:
                prefix.append(part)
                prefix_text = "/".join(prefix)
                expected_kind, expected_identity = self._entry_identities.get(
                    prefix_text,
                    (None, None),
                )
                before = os.stat(
                    part,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if (
                    expected_kind != "directory"
                    or expected_identity is None
                    or _captured_version_identity(before) != expected_identity
                    or not stat.S_ISDIR(before.st_mode)
                    or bool(
                        getattr(before, "st_file_attributes", 0)
                        & _FILE_ATTRIBUTE_REPARSE_POINT
                    )
                ):
                    raise ValueError(
                        f"captured path is not a real directory: {relative}"
                    )
                child = -1
                try:
                    child = os.open(
                        part,
                        directory_flags,
                        dir_fd=directory_descriptor,
                    )
                    opened = os.fstat(child)
                    if _root_identity(opened) != _root_identity(before):
                        raise ValueError(
                            f"captured directory changed while opening: {relative}"
                        )
                except BaseException:
                    if child >= 0:
                        os.close(child)
                    raise
                os.close(directory_descriptor)
                directory_descriptor = child

            name = relative.parts[-1]
            expected_kind, expected_identity = self._entry_identities.get(
                relative.as_posix(),
                (None, None),
            )
            before = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or expected_kind != "file"
                or expected_identity is None
                or _captured_version_identity(before) != expected_identity
                or before.st_nlink != 1
                or bool(
                    getattr(before, "st_file_attributes", 0)
                    & _FILE_ATTRIBUTE_REPARSE_POINT
                )
            ):
                raise ValueError(f"captured path is not a private file: {relative}")
            flags = os.O_RDONLY | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
            source_descriptor = os.open(name, flags, dir_fd=directory_descriptor)
            opened = os.fstat(source_descriptor)
            if (
                _file_identity(opened) != _file_identity(before)
                or stat.S_IMODE(opened.st_mode) != record.mode
                or opened.st_size != record.size
            ):
                raise ValueError(f"captured file changed while opening: {relative}")
            owned = source_descriptor
            source_descriptor = -1
            return owned, opened
        except OSError as exc:
            raise ValueError(
                f"captured file is not safely readable: {relative}"
            ) from exc
        finally:
            if source_descriptor >= 0:
                os.close(source_descriptor)
            os.close(directory_descriptor)

    def open_file(
        self,
        relative: str | Path | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ) -> AuthenticatedFile:
        normalized = _relative_path(relative)
        record = self.record(normalized)
        if max_bytes is not None and record.size > max_bytes:
            raise ValueError(
                f"captured file exceeds its {max_bytes}-byte limit: {normalized}"
            )
        if self._windows_authority is not None:
            return self._open_windows(normalized)
        descriptor, opened = self._open(normalized)
        return AuthenticatedFile(descriptor, opened, record)

    def read_bytes(
        self,
        relative: str | Path | PurePosixPath,
        *,
        max_bytes: int,
    ) -> bytes:
        with self.open_file(relative, max_bytes=max_bytes) as source:
            payload = bytearray()
            while block := source.read(_COPY_BYTES):
                payload.extend(block)
            return bytes(payload)

    def authenticate(
        self,
        relative: str | Path | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ) -> TreeFileRecord:
        with self.open_file(relative, max_bytes=max_bytes) as source:
            source.authenticate()
            return source.record

    @contextmanager
    def authenticated_snapshot(
        self,
        relative: str | Path | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ) -> Iterator[tuple[AuthenticatedSnapshot, TreeFileRecord]]:
        source = self.open_file(relative, max_bytes=max_bytes)
        snapshot: AuthenticatedSnapshot | None = None
        try:
            snapshot = source.immutable_snapshot()
            yield snapshot, source.record
            source.verify_unchanged()
        finally:
            if snapshot is not None:
                snapshot.close()
            source.close()

    @contextmanager
    def authenticated_descriptor(
        self,
        relative: str | Path | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ) -> Iterator[tuple[int, TreeFileRecord]]:
        """Yield a sealed Linux fd; portable consumers should use snapshot()."""

        if self._windows_authority is not None:
            raise RuntimeError(
                "captured authenticated descriptors are available only on POSIX"
            )
        with self.authenticated_snapshot(relative, max_bytes=max_bytes) as (
            snapshot,
            record,
        ):
            yield snapshot.descriptor, record

    def copy_chunks(
        self,
        relative: str | Path | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ) -> Iterator[bytes]:
        source = self.open_file(relative, max_bytes=max_bytes)
        try:
            while block := source.read(_COPY_BYTES):
                yield block
            source.authenticate()
        except BaseException as primary:  # noqa: B036 - preserve consumer failure
            try:
                source.close()
            except BaseException as cleanup:  # noqa: B036 - attach retry owner
                _attach_cleanup_owner(primary, source)
                raise primary from cleanup
            raise
        else:
            source.close()


class OwnedWorkspaceAuthority:
    """Caller-owned strict writer for one pre-opened exact directory skeleton.

    POSIX cannot atomically create a directory and return its descriptor.  This
    authority therefore never provisions directories.  A trusted/quiescent
    caller creates and opens the complete skeleton first, constructs this empty
    owner, and then calls :meth:`adopt`.  Every later file create is relative to
    a retained planned-parent descriptor; no runtime path is promoted into an
    ownership boundary.

    The object is deliberately pre-created before descriptor acquisition.  Its
    resource owners remain reachable if ``KeyboardInterrupt`` or ``SystemExit``
    lands at a Python return/store boundary.  ``close`` is retryable after a
    persistent descriptor-close failure and never removes a path.
    """

    def __init__(self) -> None:
        self._lock = _CancellationSafeRLock()
        self._owner_pid = os.getpid()
        self._process_locks = {self._owner_pid: self._lock}
        self._state = "empty"
        self._parent_owner = _PublicationAuthorityOwner()
        self._resources = _PosixResourceOwner()
        self._destination: Path | None = None
        self._stage_path: Path | None = None
        self._plan: WorkspacePlan | None = None
        self._expected_destination: object | None = None
        self._root_descriptor = -1
        self._root_identity: tuple[int, ...] | None = None
        self._directory_descriptors: dict[str, int] = {}
        self._directory_identities: dict[str, tuple[int, ...]] = {}
        self._file_owners: list[_WorkspaceFileOwner] = []
        self._file_specs: dict[str, WorkspaceFile] = {}
        self._written_files: dict[
            str,
            tuple[tuple[int, ...], int, int, str],
        ] = {}
        self._sealed_ownership: object | None = None
        self._publication_transfer: _WorkspacePublicationTransfer | None = None
        self._resources_transferred = False

    @property
    def state(self) -> str:
        self._require_owner_pid()
        return self._lock.run(lambda: self._state)

    @property
    def plan(self) -> WorkspacePlan:
        self._require_owner_pid()

        def get_plan() -> WorkspacePlan:
            if self._plan is None:
                raise RuntimeError("owned workspace authority has not been adopted")
            return _snapshot_workspace_plan(self._plan)

        return self._lock.run(get_plan)

    @classmethod
    def create(cls, *_args: object, **_kwargs: object) -> OwnedWorkspaceAuthority:
        """Fail before mutation until a native create-and-open backend exists."""

        raise UnsupportedWorkspaceCreation(
            "strict workspace creation requires a trusted pre-opened skeleton"
        )

    def adopt(
        self,
        *,
        destination: Path,
        stage_name: str,
        parent_descriptor: int,
        root_descriptor: int,
        directory_descriptors: Mapping[
            str | Path | PurePosixPath,
            int,
        ],
        plan: WorkspacePlan,
        expected_destination: object | None,
    ) -> None:
        """Adopt a pre-opened sibling root and exact empty-file skeleton."""

        self._require_owner_pid()
        if not sys.platform.startswith("linux"):
            raise UnsupportedWorkspaceCreation(
                "strict pre-opened workspace publication requires Linux"
            )
        self._reject_reentrant("adopt")
        self._lock.run(
            lambda: self._adopt_locked(
                destination=destination,
                stage_name=stage_name,
                parent_descriptor=parent_descriptor,
                root_descriptor=root_descriptor,
                directory_descriptors=directory_descriptors,
                plan=plan,
                expected_destination=expected_destination,
            )
        )

    def _adopt_locked(
        self,
        *,
        destination: Path,
        stage_name: str,
        parent_descriptor: int,
        root_descriptor: int,
        directory_descriptors: Mapping[
            str | Path | PurePosixPath,
            int,
        ],
        plan: WorkspacePlan,
        expected_destination: object | None,
    ) -> None:
        self._require_owner_pid()
        if self._state != "empty":
            raise RuntimeError("owned workspace authority is not empty")
        plan = _snapshot_workspace_plan(plan)
        if (
            isinstance(parent_descriptor, bool)
            or not isinstance(parent_descriptor, int)
            or parent_descriptor < 0
            or isinstance(root_descriptor, bool)
            or not isinstance(root_descriptor, int)
            or root_descriptor < 0
        ):
            raise ValueError("workspace descriptors must be open integer descriptors")
        stage_relative = _relative_path(stage_name)
        if len(stage_relative.parts) != 1:
            raise ValueError("workspace stage name must be one bounded file name")
        destination_path = lexical_directory_path(destination)
        stage_path = destination_path.parent / stage_relative.name
        if stage_path == destination_path:
            raise ValueError("workspace stage and destination must differ")

        expected_directory_paths = {item.path.as_posix() for item in plan.directories}
        normalized_descriptors: dict[str, int] = {}
        if not isinstance(directory_descriptors, Mapping):
            raise TypeError("workspace directory descriptors must be a mapping")
        for raw_path, descriptor in directory_descriptors.items():
            path = _relative_path(raw_path).as_posix()
            if path in normalized_descriptors:
                raise ValueError(f"workspace repeats directory descriptor: {path}")
            if (
                isinstance(descriptor, bool)
                or not isinstance(descriptor, int)
                or descriptor < 0
            ):
                raise ValueError(
                    "workspace directory descriptors must be open integers"
                )
            normalized_descriptors[path] = descriptor
        if set(normalized_descriptors) != expected_directory_paths:
            raise ValueError(
                "workspace directory descriptors must exactly match the plan"
            )

        if expected_destination is not None:
            # Validate the private token before acquiring any authority.
            directory_ownership_root_identity(expected_destination)

        self._state = "adopting"
        self._destination = destination_path
        self._stage_path = stage_path
        self._plan = plan
        self._expected_destination = expected_destination
        self._file_specs = {item.path.as_posix(): item for item in plan.files}
        try:
            _open_publication_authority(
                destination_path.parent,
                parent_resource=parent_descriptor,
                expected_parent_identity=publication_parent_identity(parent_descriptor),
                authority_owner=self._parent_owner,
            )
            authority = self._require_parent_authority()
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(
                os,
                "O_NONBLOCK",
                0,
            )
            self._root_descriptor = self._resources.open(
                ".",
                flags,
                dir_fd=root_descriptor,
            )
            root_metadata = os.fstat(self._root_descriptor)
            if not stat.S_ISDIR(root_metadata.st_mode):
                raise ValueError("workspace root descriptor is not a directory")
            if stat.S_IMODE(root_metadata.st_mode) != plan.root_mode:
                raise ValueError("workspace root mode differs from its plan")
            self._root_identity = _root_identity(root_metadata)
            if self._root_identity[0] != authority.identity[0]:
                raise ValueError(
                    "workspace root and destination parent differ by device"
                )

            stage_metadata = authority.child_metadata(
                stage_relative.name,
                path=stage_path,
                label="owned workspace stage",
            )
            if stage_metadata is None:
                raise ValueError("owned workspace stage does not exist")
            if _root_identity(stage_metadata) != self._root_identity:
                raise RuntimeError("workspace stage name differs from its root handle")

            expected_directory_modes = {
                item.path.as_posix(): item.mode for item in plan.directories
            }

            def skeleton_policy(path: str, kind: str, mode: int, _size: int) -> None:
                if kind != "directory":
                    raise ValueError("workspace skeleton must contain no files")
                expected_mode = expected_directory_modes.get(path)
                if expected_mode is None or mode != expected_mode:
                    raise ValueError(
                        f"workspace skeleton directory differs from plan: {path}"
                    )

            skeleton = authority.capture_child(
                stage_relative.name,
                path=stage_path,
                label="owned workspace stage",
                allow_empty_root=True,
                entry_policy=skeleton_policy,
            )
            expected_inventory = tuple(
                (item.path.as_posix(), "directory") for item in plan.directories
            )
            if directory_ownership_inventory(skeleton) != expected_inventory:
                raise ValueError("workspace skeleton differs from its exact plan")
            if directory_ownership_root_identity(skeleton) != self._root_identity:
                raise RuntimeError("workspace root changed while it was adopted")

            captured_identities = {
                path: (
                    identity[0],
                    identity[1],
                    stat.S_IFMT(identity[2]),
                    identity[4],
                )
                for path, kind, identity in directory_ownership_entry_identities(
                    skeleton
                )
                if kind == "directory"
            }
            self._directory_descriptors = {"": self._root_descriptor}
            self._directory_identities = {"": self._root_identity}
            for item in plan.directories:
                path = item.path.as_posix()
                descriptor = self._resources.open(
                    ".",
                    flags,
                    dir_fd=normalized_descriptors[path],
                )
                metadata = os.fstat(descriptor)
                identity = _root_identity(metadata)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != item.mode
                    or identity != captured_identities.get(path)
                    or identity[0] != self._root_identity[0]
                ):
                    raise RuntimeError(
                        f"workspace directory handle differs from plan: {path}"
                    )
                parent_path = item.path.parent.as_posix()
                if parent_path == ".":
                    parent_path = ""
                parent = self._directory_descriptors.get(parent_path)
                if parent is None:
                    raise RuntimeError(
                        f"workspace directory parent was not retained: {path}"
                    )
                observed = os.stat(
                    item.path.name,
                    dir_fd=parent,
                    follow_symlinks=False,
                )
                if _root_identity(observed) != identity:
                    raise RuntimeError(
                        f"workspace directory binding changed during adopt: {path}"
                    )
                self._directory_descriptors[path] = descriptor
                self._directory_identities[path] = identity

            destination_metadata = authority.child_metadata(
                destination_path.name,
                path=destination_path,
                label="owned workspace destination",
            )
            if expected_destination is None:
                if destination_metadata is not None:
                    raise RuntimeError(
                        "owned workspace destination was expected missing"
                    )
            else:
                if destination_metadata is None:
                    raise RuntimeError("owned workspace destination disappeared")
                observed_destination = authority.capture_child(
                    destination_path.name,
                    path=destination_path,
                    label="owned workspace destination",
                    allow_empty_root=True,
                )
                if observed_destination != expected_destination:
                    raise RuntimeError("owned workspace destination changed")
            authority.verify_path_binding()
            self._refresh_locked(require_complete=False)
            self._state = "adopted"
        except BaseException as refresh_error:
            self._state = "failed"
            self._close_resources_after_error_locked(refresh_error)
            raise

    def write_file(
        self,
        relative: str | Path | PurePosixPath,
        chunks: Iterable[bytes],
    ) -> None:
        self._require_owner_pid()
        self._reject_reentrant("write")
        self._lock.run(lambda: self._write_file_locked(relative, chunks))

    def _write_file_locked(
        self,
        relative: str | Path | PurePosixPath,
        chunks: Iterable[bytes],
    ) -> None:
        self._require_owner_pid()
        if self._state not in {"adopted", "writing"}:
            raise RuntimeError(f"owned workspace cannot write while {self._state}")
        normalized = _relative_path(relative)
        path = normalized.as_posix()
        spec = self._file_specs.get(path)
        if spec is None:
            raise ValueError(f"workspace file is absent from its plan: {path}")
        if path in self._written_files:
            raise ValueError(f"workspace file was already written: {path}")

        descriptor = -1
        descriptor_record = _WorkspaceFileOwner(_DescriptorOwner())
        self._file_owners.append(descriptor_record)
        iterator = None
        primary_error: BaseException | None = None
        record: tuple[tuple[int, ...], int, int, str] | None = None
        try:
            self._refresh_locked(require_complete=False)
            iterator = iter(chunks)
            parent_path = normalized.parent.as_posix()
            if parent_path == ".":
                parent_path = ""
            parent = self._directory_descriptors[parent_path]
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            descriptor = descriptor_record.owner.open(
                normalized.name,
                flags,
                0o600,
                dir_fd=parent,
            )
            byte_count = 0
            digest = hashlib.sha256()
            for chunk in iterator:
                if not isinstance(chunk, bytes):
                    raise TypeError("owned workspace file chunks must be bytes")
                byte_count += len(chunk)
                if byte_count > spec.max_bytes:
                    raise ValueError(
                        f"workspace file exceeds its {spec.max_bytes}-byte limit: "
                        f"{path}"
                    )
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("could not write owned workspace file")
                    view = view[written:]
            os.fchmod(descriptor, spec.mode)
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
            published = os.stat(
                normalized.name,
                dir_fd=parent,
                follow_symlinks=False,
            )
            identity = _root_identity(opened)
            descriptor_record.identity = _owned_file_identity(opened)
            descriptor_record.owner.bind_identity(opened)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != spec.mode
                or opened.st_size != byte_count
                or identity != _root_identity(published)
                or self._root_identity is None
                or identity[0] != self._root_identity[0]
            ):
                raise RuntimeError(f"owned workspace file changed: {path}")
            os.fsync(parent)
            record = (identity, spec.mode, byte_count, digest.hexdigest())
        except BaseException as exc:  # noqa: B036 - cleanup before re-raise
            primary_error = exc
        finally:
            try:
                close_iterator = getattr(iterator, "close", None)
            except BaseException as close_error:  # noqa: B036
                if primary_error is None:
                    primary_error = close_error
                else:
                    _annotate_secondary_error(
                        primary_error,
                        "workspace producer cleanup lookup also failed",
                        close_error,
                    )
            else:
                if callable(close_iterator):
                    try:
                        close_iterator()
                    except BaseException as close_error:  # noqa: B036
                        if primary_error is None:
                            primary_error = close_error
                        else:
                            _annotate_secondary_error(
                                primary_error,
                                "workspace producer cleanup also failed",
                                close_error,
                            )
            if descriptor >= 0:
                try:
                    if descriptor_record.identity is None:
                        metadata = os.fstat(descriptor)
                        descriptor_record.identity = _owned_file_identity(metadata)
                        descriptor_record.owner.bind_identity(metadata)
                    assert descriptor_record.identity is not None
                    descriptor_record.owner.close(
                        expected_identity=descriptor_record.identity,
                        retryable=True,
                    )
                except BaseException as close_error:  # noqa: B036
                    if primary_error is None:
                        primary_error = close_error
                    else:
                        _annotate_secondary_error(
                            primary_error,
                            "workspace file descriptor cleanup also failed",
                            close_error,
                        )

        if primary_error is not None:
            self._state = "failed"
            self._close_resources_after_error_locked(primary_error)
            raise primary_error.with_traceback(primary_error.__traceback__)
        assert record is not None
        try:
            self._written_files[path] = record
            self._refresh_locked(require_complete=False)
            self._state = "writing"
        except BaseException as transition_error:
            self._state = "failed"
            self._close_resources_after_error_locked(transition_error)
            raise

    def seal(self) -> object:
        self._require_owner_pid()
        self._reject_reentrant("seal")

        def seal_locked() -> object:
            self._require_owner_pid()
            if self._state == "sealed" and self._sealed_ownership is not None:
                return self._sealed_ownership
            if self._state not in {"adopted", "writing"}:
                raise RuntimeError(f"owned workspace cannot seal while {self._state}")
            try:
                ownership = self._refresh_locked(require_complete=True)
                self._fsync_directories_locked()
                ownership = self._refresh_locked(require_complete=True)
            except BaseException as primary_error:
                self._state = "failed"
                self._close_resources_after_error_locked(primary_error)
                raise
            self._sealed_ownership = ownership
            self._state = "sealed"
            return ownership

        return self._lock.run(seal_locked)

    def _fsync_directories_locked(self) -> None:
        """Persist the complete pre-opened skeleton from leaves to its root."""

        ordered_paths = sorted(
            self._directory_descriptors,
            key=lambda path: (path.count("/") + bool(path), path),
            reverse=True,
        )
        for path in ordered_paths:
            os.fsync(self._directory_descriptors[path])

    def publish_into(self, receipt_owner: PublishedWorkspaceReceiptOwner) -> None:
        """Durably publish and install ownership in a pre-created caller slot."""

        self._require_owner_pid()
        self._reject_reentrant("publish")
        if not isinstance(receipt_owner, PublishedWorkspaceReceiptOwner):
            raise TypeError("receipt_owner must be a PublishedWorkspaceReceiptOwner")
        transfer = _WorkspacePublicationTransfer(self)
        reservation = _WorkspaceReservation(transfer)
        try:
            self._lock.run(
                lambda: self._publish_into_locked(
                    receipt_owner,
                    transfer,
                    reservation,
                )
            )
        except BaseException as primary_error:  # noqa: B036 - reconcile owners
            self._reconcile_publish_failure_outside_lock(
                receipt_owner,
                transfer,
                reservation,
                primary_error,
            )
            raise

    def _publish_into_locked(
        self,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        transfer: _WorkspacePublicationTransfer,
        reservation: _WorkspaceReservation,
    ) -> None:
        self._require_owner_pid()
        if self._state != "sealed" or self._sealed_ownership is None:
            raise RuntimeError(f"owned workspace cannot publish while {self._state}")
        if self._destination is None or self._stage_path is None or self._plan is None:
            raise RuntimeError("owned workspace publication state is incomplete")

        # This first ownership store and every later publication operation are
        # covered by publish_into's outer exception reconciliation.  Keeping
        # that reconciliation outside the workspace lock avoids an ABBA with
        # receipt consume/close, whose lifecycle order is owner then workspace.
        self._store_publication_transfer_locked(transfer)
        self._resources_transferred = True
        self._state = "reserving-publication"
        receipt_owner._reserve(reservation)
        self._state = "publishing"
        sealed_ownership = self._sealed_ownership
        destination = self._destination
        plan = self._plan
        authority = self._require_parent_authority()

        def install_receipt(
            committed_sealed: object,
            published_ownership: object,
            previous_orphan: DirectoryOrphan | None,
        ) -> None:
            if committed_sealed != sealed_ownership:
                raise RuntimeError("published workspace sealed token changed")
            _require_matching_ownership(
                published_ownership,
                sealed_ownership,
                label="published workspace",
                allow_root_rename=True,
            )
            receipt = PublishedWorkspaceReceipt(
                transfer=transfer,
                path=destination,
                plan=_snapshot_workspace_plan(plan),
                sealed_ownership=sealed_ownership,
                published_ownership=published_ownership,
                parent_identity=authority.identity,
                orphan=previous_orphan,
            )
            # State is written before install.  If install is interrupted
            # before its slot store, the outer reconciliation demotes this to
            # failed; after the store no later state write is required.
            self._state = "published"
            receipt_owner._install(reservation, receipt)

        _publish_staged_directory_with_authority(
            authority,
            self._stage_path,
            self._destination,
            expected_stage_root_ownership=sealed_ownership,
            expected_destination_ownership=self._expected_destination,
            commit_callback=install_receipt,
        )

    def _store_publication_transfer_locked(
        self,
        transfer: _WorkspacePublicationTransfer,
    ) -> None:
        """Install the first recoverable publication ownership marker."""

        self._publication_transfer = transfer

    def _reconcile_publish_failure_outside_lock(
        self,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        transfer: _WorkspacePublicationTransfer,
        reservation: _WorkspaceReservation,
        primary_error: BaseException,
    ) -> None:
        """Settle a failed publication without ever nesting owner/workspace locks."""

        deferred: BaseException | None = None
        for _attempt in range(_WORKSPACE_OWNER_RECOVERY_LIMIT):
            try:
                terminal = self._reconcile_publish_failure_once(
                    receipt_owner,
                    transfer,
                    reservation,
                    primary_error,
                )
            except BaseException as reconciliation_error:  # noqa: B036
                if deferred is None:
                    deferred = reconciliation_error
                else:
                    _annotate_secondary_error(
                        deferred,
                        "workspace publication reconciliation also failed",
                        reconciliation_error,
                    )
                continue
            if terminal:
                if deferred is not None:
                    _annotate_secondary_error(
                        primary_error,
                        "workspace publication reconciliation was interrupted",
                        deferred,
                    )
                return
        recovery_error = RuntimeError(
            "workspace publication reconciliation did not converge"
        )
        if deferred is not None:
            _annotate_secondary_error(
                recovery_error,
                "workspace publication reconciliation recovery also failed",
                deferred,
            )
        _annotate_secondary_error(
            primary_error,
            "workspace publication ownership is unknown",
            recovery_error,
        )

    def _reconcile_publish_failure_once(
        self,
        receipt_owner: PublishedWorkspaceReceiptOwner,
        transfer: _WorkspacePublicationTransfer,
        reservation: _WorkspaceReservation,
        primary_error: BaseException,
    ) -> bool:
        publication_started = self._lock.run(
            lambda: self._publication_transfer is transfer
        )
        if not publication_started:
            return True

        transfer_state = self._receipt_transfer_state_after_error(
            receipt_owner,
            transfer,
            primary_error,
        )
        if transfer_state == "unknown":
            raise RuntimeError("workspace receipt ownership is unknown")
        if transfer_state == "reserved":
            receipt_owner._cancel_reservation(reservation)
            transfer_state = self._receipt_transfer_state_after_error(
                receipt_owner,
                transfer,
                primary_error,
            )
            if transfer_state in {"reserved", "unknown"}:
                raise RuntimeError(
                    "workspace receipt reservation cancellation did not settle"
                )

        def settle_workspace() -> None:
            # The receipt owner lock is not held here.  A concurrent close may
            # have completed after the owner snapshot; identity is therefore
            # checked again before touching workspace state.
            if self._publication_transfer is not transfer:
                return
            if transfer_state == "active":
                self._state = "published"
                return
            if transfer_state == "cleanup":
                self._state = "failed"
                return
            if transfer_state != "absent":
                raise RuntimeError(
                    "workspace receipt ownership state is invalid: " f"{transfer_state}"
                )
            # The owner never received the transfer.  Make public workspace
            # close retryable before attempting aggregate cleanup, and clear
            # the transfer marker last so an interrupted settle can resume.
            self._resources_transferred = False
            self._state = "failed"
            self._close_resources_after_error_locked(primary_error)
            self._publication_transfer = None

        self._lock.run(settle_workspace)
        return True

    @staticmethod
    def _receipt_transfer_state_after_error(
        receipt_owner: PublishedWorkspaceReceiptOwner,
        transfer: _WorkspacePublicationTransfer,
        primary_error: BaseException,
    ) -> str:
        deferred: BaseException | None = None
        for _attempt in range(_WORKSPACE_OWNER_RECOVERY_LIMIT):
            try:
                state = receipt_owner._transfer_state(transfer)
            except BaseException as state_error:  # noqa: B036 - bounded recovery
                if deferred is None:
                    deferred = state_error
                else:
                    _annotate_secondary_error(
                        deferred,
                        "workspace receipt ownership observation also failed",
                        state_error,
                    )
                continue
            if deferred is not None:
                _annotate_secondary_error(
                    primary_error,
                    "workspace receipt ownership observation was interrupted",
                    deferred,
                )
            return state
        recovery_error = RuntimeError(
            "workspace receipt ownership observation did not converge"
        )
        if deferred is not None:
            _annotate_secondary_error(
                recovery_error,
                "workspace receipt ownership recovery also failed",
                deferred,
            )
        _annotate_secondary_error(
            primary_error,
            "workspace receipt ownership is unknown",
            recovery_error,
        )
        # Unknown ownership must never trigger a competing resource close.
        return "unknown"

    def _consume_published_workspace(
        self,
        receipt: PublishedWorkspaceReceipt,
        callback: Callable[
            [PublishedWorkspaceReceipt, PublicationDirectoryReader],
            _WorkspaceResult,
        ],
    ) -> _WorkspaceResult:
        self._require_owner_pid()
        self._reject_reentrant("consume")

        def consume_locked() -> _WorkspaceResult:
            if (
                self._state != "published"
                or self._publication_transfer is not receipt._transfer
                or self._destination != receipt.path
                or self._plan != receipt._plan
            ):
                raise RuntimeError("published workspace receipt is not active")
            authority = self._require_parent_authority()
            if authority.identity != receipt.parent_identity:
                raise RuntimeError("published workspace parent authority changed")
            root_before = os.fstat(self._root_descriptor)
            if _root_identity(root_before) != directory_ownership_root_identity(
                receipt.ownership
            ) or _captured_version_identity(
                root_before
            ) != directory_ownership_root_version_identity(
                receipt.ownership
            ):
                raise RuntimeError("published workspace root handle changed")
            authority.verify_path_binding()

            def exact_reader(
                reader: PublicationDirectoryReader,
            ) -> _WorkspaceResult:
                before = reader.capture_ownership(allow_empty_root=True)
                if before != receipt.ownership:
                    raise RuntimeError("published workspace generation changed")
                result = callback(receipt, reader)
                after = reader.capture_ownership(allow_empty_root=True)
                if after != before:
                    raise RuntimeError(
                        "published workspace changed during receipt consumption"
                    )
                return result

            result = authority.read_child(
                receipt.path.name,
                path=receipt.path,
                label="published workspace",
                expected_ownership=receipt.ownership,
                callback=exact_reader,
            )
            authority.verify_path_binding()
            root_after = os.fstat(self._root_descriptor)
            if _captured_version_identity(root_after) != _captured_version_identity(
                root_before
            ):
                raise RuntimeError(
                    "published workspace root changed during receipt consumption"
                )
            return result

        return self._lock.run(consume_locked)

    def _close_publication_transfer(
        self,
        transfer: _WorkspacePublicationTransfer,
    ) -> None:
        current_pid = os.getpid()
        if current_pid != self._owner_pid:
            child_lock = self._process_locks.setdefault(
                current_pid,
                _CancellationSafeRLock(),
            )
            inherited_close_error: BaseException | None = None

            def close_inherited() -> None:
                self._require_publication_transfer_locked(transfer)
                self._close_inherited_resources_locked()
                if self._state == "closed":
                    self._finish_publication_transfer_closed_locked(transfer)

            try:
                child_lock.run(close_inherited)
            except BaseException as exc:  # noqa: B036 - report PID boundary
                inherited_close_error = exc
            boundary_error = RuntimeError(
                "published workspace transfer cannot cross a PID boundary"
            )
            if inherited_close_error is not None:
                raise boundary_error from inherited_close_error
            raise boundary_error

        self._reject_reentrant("receipt close")

        def close_locked() -> None:
            self._require_publication_transfer_locked(transfer)
            primary_error: BaseException | None = None
            try:
                self._close_resources_locked()
            except BaseException as close_error:  # noqa: B036 - reconcile transfer
                primary_error = close_error
            try:
                if self._state == "closed":
                    self._finish_publication_transfer_closed_locked(transfer)
            except BaseException as transition_error:  # noqa: B036
                if primary_error is None:
                    primary_error = transition_error
                else:
                    _annotate_secondary_error(
                        primary_error,
                        "workspace transfer close transition also failed",
                        transition_error,
                    )
            if primary_error is not None:
                raise primary_error

        self._lock.run(close_locked)

    def _publication_transfer_closed(
        self,
        transfer: _WorkspacePublicationTransfer,
    ) -> bool:
        def observe() -> bool:
            return self._state == "closed" and (
                self._publication_transfer is None
                or self._publication_transfer is transfer
            )

        if os.getpid() != self._owner_pid:
            return observe()
        return self._lock.run(observe)

    def _require_publication_transfer_locked(
        self,
        transfer: _WorkspacePublicationTransfer,
    ) -> None:
        if (
            transfer.workspace is not self
            or self._publication_transfer is not transfer
            or not self._resources_transferred
        ):
            raise RuntimeError("published workspace transfer authority changed")

    def _finish_publication_transfer_closed_locked(
        self,
        transfer: _WorkspacePublicationTransfer,
    ) -> None:
        self._require_publication_transfer_locked(transfer)
        self._resources_transferred = False
        self._publication_transfer = None

    def _refresh_locked(self, *, require_complete: bool) -> object:
        authority = self._require_parent_authority()
        if (
            self._stage_path is None
            or self._root_identity is None
            or self._plan is None
        ):
            raise RuntimeError("owned workspace authority is incomplete")
        authority.verify_path_binding()
        root_metadata = authority.child_metadata(
            self._stage_path.name,
            path=self._stage_path,
            label="owned workspace stage",
        )
        if (
            root_metadata is None
            or _root_identity(root_metadata) != self._root_identity
        ):
            raise RuntimeError("owned workspace root changed")
        if _root_identity(os.fstat(self._root_descriptor)) != self._root_identity:
            raise RuntimeError("owned workspace root handle changed")

        directory_specs = {
            item.path.as_posix(): item for item in self._plan.directories
        }
        for path, descriptor in self._directory_descriptors.items():
            metadata = os.fstat(descriptor)
            expected_identity = self._directory_identities[path]
            expected_mode = (
                self._plan.root_mode if not path else directory_specs[path].mode
            )
            if (
                _root_identity(metadata) != expected_identity
                or stat.S_IMODE(metadata.st_mode) != expected_mode
            ):
                raise RuntimeError(
                    "owned workspace directory handle changed"
                    + (f": {path}" if path else "")
                )
            if path:
                relative = PurePosixPath(path)
                parent_path = relative.parent.as_posix()
                if parent_path == ".":
                    parent_path = ""
                observed = os.stat(
                    relative.name,
                    dir_fd=self._directory_descriptors[parent_path],
                    follow_symlinks=False,
                )
                if _root_identity(observed) != expected_identity:
                    raise RuntimeError(
                        f"owned workspace directory binding changed: {path}"
                    )

        allowed_files = self._written_files

        def entry_policy(path: str, kind: str, mode: int, size: int) -> None:
            if kind == "directory":
                spec = directory_specs.get(path)
                if spec is None or mode != spec.mode:
                    raise RuntimeError(
                        f"owned workspace contains an unplanned directory: {path}"
                    )
                return
            record = allowed_files.get(path)
            if record is None or (mode, size) != (record[1], record[2]):
                raise RuntimeError(
                    f"owned workspace contains an unplanned file: {path}"
                )

        observed = authority.capture_child(
            self._stage_path.name,
            path=self._stage_path,
            label="owned workspace stage",
            allow_empty_root=True,
            entry_policy=entry_policy,
        )
        expected_inventory = tuple(
            sorted(
                [(path, "directory") for path in directory_specs]
                + [(path, "file") for path in allowed_files]
            )
        )
        if directory_ownership_inventory(observed) != expected_inventory:
            raise RuntimeError("owned workspace inventory differs from its plan")
        if directory_ownership_root_identity(observed) != self._root_identity:
            raise RuntimeError("owned workspace root changed during verification")
        identities = {
            path: identity
            for path, _kind, identity in directory_ownership_entry_identities(observed)
        }
        records = {
            record.path: record for record in directory_ownership_file_records(observed)
        }
        for path, identity in self._directory_identities.items():
            if not path:
                continue
            observed_identity = identities.get(path)
            if (
                observed_identity is None
                or (
                    observed_identity[0],
                    observed_identity[1],
                    stat.S_IFMT(observed_identity[2]),
                    observed_identity[4],
                )
                != identity
            ):
                raise RuntimeError(f"owned workspace directory changed: {path}")
        for path, (identity, mode, size, digest) in allowed_files.items():
            observed_identity = identities.get(path)
            record = records.get(path)
            if (
                observed_identity is None
                or (
                    observed_identity[0],
                    observed_identity[1],
                    stat.S_IFMT(observed_identity[2]),
                    observed_identity[4],
                )
                != identity
                or observed_identity[7] != 1
                or record is None
                or (record.mode, record.size, record.sha256) != (mode, size, digest)
            ):
                raise RuntimeError(f"owned workspace file changed: {path}")
        if require_complete and set(allowed_files) != set(self._file_specs):
            missing = sorted(set(self._file_specs) - set(allowed_files))
            raise RuntimeError(
                "owned workspace is missing required files: " + ", ".join(missing)
            )
        authority.verify_path_binding()
        return observed

    def close(self) -> None:
        current_pid = os.getpid()
        if current_pid != self._owner_pid:
            if self._resources_transferred:
                raise RuntimeError(
                    "close the PublishedWorkspaceReceiptOwner for this transfer"
                )
            # An RLock held by another thread at fork can never be released in
            # the child.  CPython's single dict.setdefault call installs one
            # process-local lock before its return can be interrupted; every
            # later child thread and cleanup retry therefore serializes on the
            # same lock.  Close inherited descriptors without an OFD-offset
            # cookie; the parent keeps independent object and fd state.
            child_lock = self._process_locks.setdefault(
                current_pid,
                _CancellationSafeRLock(),
            )
            inherited_close_error: BaseException | None = None
            try:
                child_lock.run(self._close_inherited_resources_locked)
            except BaseException as exc:  # noqa: B036 - report PID boundary
                inherited_close_error = exc
            boundary_error = RuntimeError(
                "owned workspace authority cannot cross a PID boundary"
            )
            if inherited_close_error is not None:
                raise boundary_error from inherited_close_error
            raise boundary_error
        self._reject_reentrant("close")

        def close_locked() -> None:
            if self._resources_transferred:
                raise RuntimeError(
                    "close the PublishedWorkspaceReceiptOwner for this transfer"
                )
            self._close_resources_locked()

        close_error: BaseException | None = None
        try:
            self._lock.run(close_locked)
        except BaseException as exc:  # noqa: B036 - report PID after cleanup
            close_error = exc
        if close_error is not None:
            raise close_error

    def _close_inherited_resources_locked(self) -> None:
        """Close only this fork child's inherited descriptor references."""

        if self._state == "closed":
            return
        primary_error: BaseException | None = None
        for record in reversed(tuple(self._file_owners)):
            descriptor = record.owner.descriptor
            if descriptor < 0:
                continue
            try:
                close_error = self._close_inherited_file_descriptor(record)
            except BaseException as unexpected_close_error:  # noqa: B036
                close_error = unexpected_close_error
            if close_error is not None:
                if primary_error is None:
                    primary_error = close_error
                else:
                    _annotate_secondary_error(
                        primary_error,
                        "additional inherited workspace file cleanup failed",
                        close_error,
                    )
        try:
            self._resources.close_all()
        except BaseException as close_error:  # noqa: B036 - cleanup all owners
            if primary_error is None:
                primary_error = close_error
            else:
                _annotate_secondary_error(
                    primary_error,
                    "inherited workspace directory cleanup also failed",
                    close_error,
                )
        try:
            self._parent_owner.close()
        except BaseException as close_error:  # noqa: B036 - cleanup all owners
            if primary_error is None:
                primary_error = close_error
            else:
                _annotate_secondary_error(
                    primary_error,
                    "inherited workspace parent cleanup also failed",
                    close_error,
                )
        try:
            close_complete = (
                all(record.owner.descriptor < 0 for record in self._file_owners)
                and self._resources.closed
                and self._parent_owner.authority is None
            )
        except BaseException as reconciliation_error:  # noqa: B036
            if primary_error is None:
                primary_error = reconciliation_error
            else:
                _annotate_secondary_error(
                    primary_error,
                    "inherited workspace close-state reconciliation also failed",
                    reconciliation_error,
                )
        else:
            if close_complete:
                self._state = "closed"
                self._root_descriptor = -1
                self._directory_descriptors.clear()
        if primary_error is not None:
            raise primary_error

    @staticmethod
    def _close_inherited_file_descriptor(
        record: _WorkspaceFileOwner,
    ) -> BaseException | None:
        descriptor = record.owner.descriptor
        if descriptor < 0:
            return None
        try:
            metadata = os.fstat(descriptor)
        except OSError as probe_error:
            if probe_error.errno == errno.EBADF:
                OwnedWorkspaceAuthority._invalidate_inherited_file_owner(record)
                return None
            return probe_error
        except BaseException as probe_error:  # noqa: B036 - retain owner
            return probe_error
        observed_identity = _owned_file_identity(metadata)
        if record.identity is None:
            record.identity = observed_identity
            record.owner.bind_identity(metadata)
        elif observed_identity != record.identity:
            OwnedWorkspaceAuthority._invalidate_inherited_file_owner(record)
            return RuntimeError("inherited workspace file descriptor changed")
        try:
            os.close(descriptor)
        except BaseException as close_error:  # noqa: B036 - reconcile close
            try:
                rebound = os.fstat(descriptor)
            except OSError as probe_error:
                if probe_error.errno == errno.EBADF:
                    OwnedWorkspaceAuthority._invalidate_inherited_file_owner(record)
                else:
                    _annotate_secondary_error(
                        close_error,
                        "inherited file close reconciliation also failed",
                        probe_error,
                    )
            except BaseException as probe_error:  # noqa: B036 - keep close error
                _annotate_secondary_error(
                    close_error,
                    "inherited file close reconciliation also failed",
                    probe_error,
                )
            else:
                if _owned_file_identity(rebound) != record.identity:
                    OwnedWorkspaceAuthority._invalidate_inherited_file_owner(record)
            return close_error
        OwnedWorkspaceAuthority._invalidate_inherited_file_owner(record)
        return None

    @staticmethod
    def _invalidate_inherited_file_owner(record: _WorkspaceFileOwner) -> None:
        # This mutates only the post-fork child copy.  It intentionally avoids
        # _DescriptorOwner.close(), whose lseek cookie would alter the parent's
        # shared open-file-description offset.
        record.owner._descriptor = -1
        record.owner._identity = None
        record.owner._close_cookie = None

    def _close_resources_locked(self) -> None:
        if self._state == "closed":
            return
        primary_error: BaseException | None = None
        for record in reversed(tuple(self._file_owners)):
            descriptor = record.owner.descriptor
            if descriptor < 0:
                continue
            try:
                if record.identity is None:
                    metadata = os.fstat(descriptor)
                    record.identity = _owned_file_identity(metadata)
                    record.owner.bind_identity(metadata)
                assert record.identity is not None
                record.owner.close(
                    expected_identity=record.identity,
                    retryable=True,
                )
            except BaseException as close_error:  # noqa: B036 - cleanup all
                if primary_error is None:
                    primary_error = close_error
                else:
                    _annotate_secondary_error(
                        primary_error,
                        "additional workspace file cleanup also failed",
                        close_error,
                    )
        try:
            self._resources.close_all()
        except BaseException as close_error:  # noqa: B036 - cleanup all owners
            if primary_error is None:
                primary_error = close_error
            else:
                _annotate_secondary_error(
                    primary_error,
                    "workspace directory cleanup also failed",
                    close_error,
                )
        try:
            self._parent_owner.close()
        except BaseException as close_error:  # noqa: B036 - cleanup all owners
            if primary_error is None:
                primary_error = close_error
            else:
                _annotate_secondary_error(
                    primary_error,
                    "workspace parent authority cleanup also failed",
                    close_error,
                )
        try:
            close_complete = (
                all(record.owner.descriptor < 0 for record in self._file_owners)
                and self._resources.closed
                and self._parent_owner.authority is None
            )
        except BaseException as reconciliation_error:  # noqa: B036
            if primary_error is None:
                primary_error = reconciliation_error
            else:
                _annotate_secondary_error(
                    primary_error,
                    "workspace close-state reconciliation also failed",
                    reconciliation_error,
                )
        else:
            if close_complete:
                self._state = "closed"
                self._root_descriptor = -1
                self._directory_descriptors.clear()
        if primary_error is not None:
            raise primary_error

    def _close_resources_after_error_locked(
        self,
        primary_error: BaseException,
    ) -> None:
        try:
            self._close_resources_locked()
        except BaseException as close_error:  # noqa: B036 - keep primary
            _annotate_secondary_error(
                primary_error,
                "owned workspace cleanup also failed",
                close_error,
            )

    def _require_parent_authority(self) -> _PublicationAuthority:
        authority = self._parent_owner.authority
        if authority is None:
            raise RuntimeError("owned workspace parent authority is closed")
        return authority

    def _require_owner_pid(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError("owned workspace authority cannot cross a PID boundary")

    def _reject_reentrant(self, operation: str) -> None:
        if self._lock.held_by_current_thread():
            raise RuntimeError(f"owned workspace {operation} is reentrant")

    def __enter__(self) -> OwnedWorkspaceAuthority:
        self._require_owner_pid()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: object,
    ) -> None:
        if exc is None:
            self.close()
        else:
            try:
                self.close()
            except BaseException as close_error:  # noqa: B036 - keep primary
                _annotate_secondary_error(
                    exc,
                    "owned workspace context cleanup also failed",
                    close_error,
                )


class OwnedDirectoryStage:
    """Build one new private sibling tree without path-based file mutation."""

    def __init__(
        self,
        destination: Path,
        *,
        _parent_authority: _PublicationAuthority | None = None,
        _expected_destination_ownership: object = _UNSET_DESTINATION_OWNERSHIP,
    ) -> None:
        self.destination = lexical_directory_path(destination)
        self.path = self.destination.parent / (
            f".{self.destination.name}.normalize-{secrets.token_hex(12)}"
        )
        self._stage_parent_fd = -1
        self._descriptor = -1
        self._parent_authority = _parent_authority
        self._has_expected_destination_ownership = (
            _expected_destination_ownership is not _UNSET_DESTINATION_OWNERSHIP
        )
        self._expected_destination_ownership = _expected_destination_ownership
        self._published = False
        self._owned_directories: dict[str, tuple[int, ...]] = {}
        self._owned_files: dict[
            str,
            tuple[tuple[int, ...], int, int, str],
        ] = {}
        self._cleanup_ownership = None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        created = False
        try:
            if self._parent_authority is None:
                self._parent_authority = _open_publication_authority(
                    self.destination.parent,
                    parent_resource=None,
                    expected_parent_identity=None,
                )
            self._stage_parent_fd = os.dup(self._parent_authority.resource)
            self._parent_identity = self._parent_authority.identity
            os.mkdir(
                self.path.name,
                mode=0o700,
                dir_fd=self._stage_parent_fd,
            )
            created = True
            created_metadata = os.stat(
                self.path.name,
                dir_fd=self._stage_parent_fd,
                follow_symlinks=False,
            )
            try:
                self._initial = self._parent_authority.capture_child(
                    self.path.name,
                    path=self.path,
                    label="owned stage",
                    allow_empty_root=True,
                )
            except RuntimeError as exc:
                raise RuntimeError(
                    "owned stage root changed during initialization"
                ) from exc
            self._descriptor = os.open(
                self.path.name,
                flags,
                dir_fd=self._stage_parent_fd,
            )
            opened = os.fstat(self._descriptor)
            if (
                _root_identity(created_metadata) != _root_identity(opened)
                or _root_identity(opened)
                != directory_ownership_root_identity(self._initial)
                or _captured_version_identity(opened)
                != directory_ownership_root_version_identity(self._initial)
            ):
                raise RuntimeError("owned stage root changed during initialization")
            self._stage_identity = _root_identity(opened)
            self._cleanup_ownership = self._initial
            self._verify_parent_path()
        except BaseException:
            self.close()
            # A created but unverified stage is intentionally left as an
            # orphan; deleting by name could remove a raced foreign object.
            if not created:
                self.path = self.destination.parent / self.path.name
            raise

    @classmethod
    def prepare(
        cls,
        destination: Path,
        *,
        required_destination_file: str | None = None,
        allow_empty_destination: bool = False,
        create_parent: bool = True,
    ) -> OwnedDirectoryStage:
        """Capture the destination and build under one retained authority."""

        lexical = lexical_directory_path(destination)
        authority = _open_publication_authority(
            lexical.parent,
            parent_resource=None,
            expected_parent_identity=None,
            create_missing=create_parent,
        )
        try:
            metadata = authority.child_metadata(
                lexical.name,
                path=lexical,
                label="owned stage destination",
            )
            if metadata is None:
                expected_ownership = None
            else:
                expected_ownership = authority.capture_child(
                    lexical.name,
                    path=lexical,
                    label="owned stage destination",
                    required_root_file=required_destination_file,
                    allow_empty_root=allow_empty_destination,
                )
            authority.verify_path_binding()
            return cls(
                lexical,
                _parent_authority=authority,
                _expected_destination_ownership=expected_ownership,
            )
        except BaseException:
            authority.close()
            raise

    @property
    def expected_destination_ownership(self) -> object | None:
        """Return the destination token captured by :meth:`prepare`."""

        if not self._has_expected_destination_ownership:
            raise RuntimeError("owned stage did not capture its destination")
        return self._expected_destination_ownership

    def close(self) -> None:
        if self._descriptor >= 0:
            try:
                os.close(self._descriptor)
            except OSError:
                pass
            self._descriptor = -1
        if self._stage_parent_fd >= 0:
            try:
                os.close(self._stage_parent_fd)
            except OSError:
                pass
            self._stage_parent_fd = -1
        if self._parent_authority is not None:
            self._parent_authority.close()
            self._parent_authority = None

    @staticmethod
    def _record_orphan(orphan: DirectoryOrphan | None, *, operation: str) -> None:
        if orphan is None:
            return
        logger.warning(
            "Owned directory %s retained an orphan for quiescent GC: "
            "path=%s digest=%s entries=%d bytes=%d verified=%s",
            operation,
            orphan.path,
            orphan.ownership_digest,
            orphan.entries,
            orphan.byte_count,
            orphan.verified_at_isolation,
        )

    def discard(self) -> DirectoryOrphan | None:
        ownership = self._cleanup_ownership
        self.close()
        if not self._published and ownership is not None:
            orphan = discard_owned_directory(self.path, ownership)
            self._record_orphan(orphan, operation="discard")
            return orphan
        return None

    def _verify_parent_path(self) -> None:
        if self._stage_parent_fd < 0 or self._parent_authority is None:
            raise RuntimeError("owned stage parent is closed")
        try:
            opened = os.fstat(self._stage_parent_fd)
            self._parent_authority.verify_path_binding()
        except OSError as exc:
            raise RuntimeError("owned stage parent changed") from exc
        if _root_identity(opened) != self._parent_identity:
            raise RuntimeError("owned stage parent changed")

    def _verify_root_descriptor(self) -> None:
        if self._descriptor < 0:
            raise RuntimeError("owned stage is closed")
        try:
            opened = os.fstat(self._descriptor)
            path_metadata = os.stat(
                self.path.name,
                dir_fd=self._stage_parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise RuntimeError("owned stage root changed") from exc
        if (
            _root_identity(opened) != self._stage_identity
            or _root_identity(path_metadata) != self._stage_identity
        ):
            raise RuntimeError("owned stage root changed")

    def _parent_descriptor(self, relative: PurePosixPath) -> int:
        self._verify_parent_path()
        self._verify_root_descriptor()
        descriptor = os.dup(self._descriptor)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        prefix: list[str] = []
        try:
            for part in relative.parts[:-1]:
                prefix.append(part)
                tracked_path = "/".join(prefix)
                created = False
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError as exc:
                    if tracked_path not in self._owned_directories:
                        raise ValueError(
                            f"owned stage component was not created here: {relative}"
                        ) from exc
                else:
                    created = True
                child = -1
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                    opened = os.fstat(child)
                    identity = _root_identity(opened)
                    if not stat.S_ISDIR(opened.st_mode) or (
                        not created
                        and self._owned_directories.get(tracked_path) != identity
                    ):
                        raise ValueError(f"owned stage component changed: {relative}")
                    path_metadata = os.stat(
                        part,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                    if _root_identity(path_metadata) != identity:
                        raise ValueError(f"owned stage component changed: {relative}")
                except BaseException:
                    if child >= 0:
                        os.close(child)
                    raise
                if created:
                    self._owned_directories[tracked_path] = identity
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def write_file(
        self,
        relative: str | Path | PurePosixPath,
        chunks: Iterable[bytes],
        *,
        mode: int = 0o600,
        max_bytes: int | None = None,
    ) -> None:
        normalized = _relative_path(relative)
        temporary = f".{normalized.name}.tmp-{secrets.token_hex(12)}"
        parent = -1
        descriptor = -1
        byte_count = 0
        digest = hashlib.sha256()
        iterator = None
        try:
            parent = self._parent_descriptor(normalized)
            iterator = iter(chunks)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(temporary, flags, mode, dir_fd=parent)
            for chunk in iterator:
                if not isinstance(chunk, bytes):
                    raise TypeError("owned stage file chunks must be bytes")
                byte_count += len(chunk)
                if max_bytes is not None and byte_count > max_bytes:
                    raise ValueError(
                        f"owned stage file exceeds its {max_bytes}-byte limit: "
                        f"{normalized}"
                    )
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("could not write owned stage file")
                    view = view[written:]
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
            try:
                _rename_noreplace_at(
                    temporary,
                    normalized.name,
                    parent,
                    parent,
                )
            except FileExistsError as exc:
                raise ValueError(
                    f"owned stage file already exists: {normalized}"
                ) from exc
            opened = os.fstat(descriptor)
            published = os.stat(
                normalized.name,
                dir_fd=parent,
                follow_symlinks=False,
            )
            if _root_identity(opened) != _root_identity(published):
                raise RuntimeError(f"owned stage file changed: {normalized}")
            self._owned_files[normalized.as_posix()] = (
                _root_identity(opened),
                stat.S_IMODE(opened.st_mode),
                byte_count,
                digest.hexdigest(),
            )
            os.fsync(parent)
            self._refresh_cleanup_ownership()
        finally:
            try:
                close_iterator = getattr(iterator, "close", None)
                if callable(close_iterator):
                    close_iterator()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                # A failed temporary is left in the private stage. Name-based
                # unlink cannot prove it is still the owned inode at deletion.
                if parent >= 0:
                    os.close(parent)

    def _refresh_cleanup_ownership(self) -> None:
        self._verify_parent_path()
        self._verify_root_descriptor()
        assert self._parent_authority is not None
        observed = self._parent_authority.capture_child(
            self.path.name,
            path=self.path,
            label="owned stage",
            allow_empty_root=True,
        )
        expected_inventory = tuple(
            sorted(
                [(path, "directory") for path in self._owned_directories]
                + [(path, "file") for path in self._owned_files]
            )
        )
        if directory_ownership_inventory(observed) != expected_inventory:
            raise RuntimeError("owned stage contains an untracked entry")
        identities = {
            path: identity
            for path, _kind, identity in directory_ownership_entry_identities(observed)
        }
        records = {
            record.path: record for record in directory_ownership_file_records(observed)
        }
        for path, identity in self._owned_directories.items():
            observed_identity = identities.get(path)
            if (
                observed_identity is None
                or (
                    observed_identity[0],
                    observed_identity[1],
                    stat.S_IFMT(observed_identity[2]),
                    observed_identity[4],
                )
                != identity
            ):
                raise RuntimeError(f"owned stage directory changed: {path}")
        for path, (identity, mode, size, digest) in self._owned_files.items():
            observed_identity = identities.get(path)
            record = records.get(path)
            if (
                observed_identity is None
                or (
                    observed_identity[0],
                    observed_identity[1],
                    stat.S_IFMT(observed_identity[2]),
                    observed_identity[4],
                )
                != identity
                or record is None
                or (record.mode, record.size, record.sha256) != (mode, size, digest)
            ):
                raise RuntimeError(f"owned stage file changed: {path}")
        if directory_ownership_root_identity(observed) != self._stage_identity:
            raise RuntimeError("owned stage root changed")
        self._cleanup_ownership = observed

    def capture_ownership(self) -> object:
        """Return the exact current token for this owned stage."""

        self._refresh_cleanup_ownership()
        return self._cleanup_ownership

    def publish(
        self,
        *,
        expected_destination_ownership: object = _UNSET_DESTINATION_OWNERSHIP,
        validate_staged_directory=None,
        validate_published_destination=None,
    ) -> DirectoryOrphan | None:
        if expected_destination_ownership is _UNSET_DESTINATION_OWNERSHIP:
            if not self._has_expected_destination_ownership:
                raise TypeError(
                    "expected_destination_ownership is required for an "
                    "unprepared owned stage"
                )
            expected_destination_ownership = self._expected_destination_ownership
        self._refresh_cleanup_ownership()
        orphan = publish_staged_directory(
            self.path,
            self.destination,
            expected_stage_root_ownership=self._cleanup_ownership,
            expected_destination_ownership=expected_destination_ownership,
            validate_staged_directory=validate_staged_directory,
            validate_published_destination=validate_published_destination,
            parent_descriptor=self._stage_parent_fd,
            expected_parent_identity=self._parent_identity,
        )
        self._record_orphan(orphan, operation="publication")
        self._published = True
        self.close()
        return orphan


__all__ = [
    "AuthenticatedFile",
    "AuthenticatedSnapshotReader",
    "CapturedDirectoryReader",
    "OwnedDirectoryStage",
    "OwnedWorkspaceAuthority",
    "PublishedWorkspaceReceipt",
    "PublishedWorkspaceReceiptOwner",
    "UnsupportedWorkspaceCreation",
    "WorkspaceDirectory",
    "WorkspaceFile",
    "WorkspacePlan",
]
