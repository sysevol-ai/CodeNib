# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Race-resistant reads of repository files, including contained symlinks."""

from __future__ import annotations

import errno
import ntpath
import os
import secrets
import stat
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, Sequence

from ._windows_fs_authority import (
    FILE_ATTRIBUTE_DIRECTORY as _WINDOWS_FILE_ATTRIBUTE_DIRECTORY,
)
from ._windows_fs_authority import (
    FILE_ATTRIBUTE_REPARSE_POINT as _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT,
)
from ._windows_fs_authority import FILE_LIST_DIRECTORY as _WINDOWS_FILE_LIST_DIRECTORY
from ._windows_fs_authority import FILE_READ_ATTRIBUTES as _WINDOWS_FILE_READ_ATTRIBUTES
from ._windows_fs_authority import FILE_READ_DATA as _WINDOWS_FILE_READ_DATA
from ._windows_fs_authority import (
    IO_REPARSE_TAG_SYMLINK as _WINDOWS_IO_REPARSE_TAG_SYMLINK,
)
from ._windows_fs_authority import (
    SYMLINK_FLAG_RELATIVE as _WINDOWS_SYMLINK_FLAG_RELATIVE,
)
from ._windows_fs_authority import SYNCHRONIZE as _WINDOWS_SYNCHRONIZE
from ._windows_fs_authority import (
    WindowsDirectoryAuthority as _WindowsDirectoryAuthority,
)
from ._windows_fs_authority import WindowsDirectoryEntry as _WindowsDirectoryEntry
from ._windows_fs_authority import WindowsHandleMetadata as _WindowsHandleMetadata
from ._windows_fs_authority import WindowsKernelApi as _WindowsKernelApi
from ._windows_fs_authority import WindowsReparsePoint as _WindowsReparsePoint
from ._windows_fs_authority import _close_windows_handles, _WindowsHandleCleanup
from ._windows_fs_authority import (
    open_lexical_directory_authority as _open_lexical_windows_authority,
)
from ._windows_fs_authority import (
    windows_file_id_is_reliable as _windows_file_id_is_reliable,
)
from ._windows_fs_authority import windows_kernel_api as _shared_windows_kernel_api
from ._windows_fs_authority import (
    windows_require_source_api as _shared_windows_require_source_api,
)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_MAX_COMPONENTS = 256
_MAX_RELATIVE_PATH_BYTES = 4_096
_MAX_SYMLINKS = 40
_MAX_SYMLINK_TARGET_BYTES = 4_096
_READ_CHUNK_BYTES = 1024 * 1024
_CLOSE_COOKIE_FLOOR = 1 << 20
_CLOSE_COOKIE_SPAN = 1 << 30
_CLOSE_COOKIE_LOCK = threading.Lock()
_CLOSE_COOKIE_COUNTS: dict[int, int] = {}


def _reset_close_cookie_lock_after_fork() -> None:
    global _CLOSE_COOKIE_LOCK
    _CLOSE_COOKIE_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_close_cookie_lock_after_fork)
SECURE_CONTAINED_SYMLINKS = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in getattr(os, "supports_dir_fd", ())
    and os.stat in getattr(os, "supports_dir_fd", ())
    and os.stat in getattr(os, "supports_follow_symlinks", ())
    and os.readlink in getattr(os, "supports_dir_fd", ())
)


class ContainedSourceMissingError(ValueError):
    """A lexically present source link has no resolvable regular target."""


class _SourceCleanupSlot:
    """Stable, retryable owner across raw-resource and binding handoffs."""

    __slots__ = ("_resource",)

    def __init__(self) -> None:
        self._resource: object | None = None

    def own(self, resource: object) -> None:
        if resource is None:
            raise TypeError("source cleanup resource must not be null")
        self._resource = resource

    @property
    def pending_sources(self) -> tuple[object, ...]:
        resource = self._resource
        if resource is None or self.closed:
            return ()
        nested = getattr(resource, "pending_sources", None)
        if nested is not None:
            return tuple(nested)
        return (resource,)

    @property
    def closed(self) -> bool:
        resource = self._resource
        if resource is None:
            return True
        try:
            return bool(resource.closed)  # type: ignore[attr-defined]
        except BaseException:  # noqa: B036 - uncertain ownership stays retained
            return False

    def close(self) -> None:
        resource = self._resource
        if resource is None:
            return
        close = getattr(resource, "close", None)
        if not callable(close):
            raise TypeError("source cleanup resource has no close method")
        close()
        if self.closed:
            self._resource = None


class _PosixDescriptorCleanup:
    """Retryable owner for descriptors acquired during contained resolution."""

    __slots__ = ("descriptors", "expected_identities", "close_cookies")

    def __init__(self) -> None:
        self.descriptors: list[int] = []
        self.expected_identities: dict[int, tuple[int, ...]] = {}
        self.close_cookies: dict[int, int] = {}

    @property
    def closed(self) -> bool:
        return not self.descriptors

    def retain(
        self,
        descriptor: int,
        metadata: os.stat_result | None = None,
    ) -> int:
        if (
            isinstance(descriptor, bool)
            or not isinstance(descriptor, int)
            or descriptor < 0
        ):
            raise ValueError("source cleanup descriptor is invalid")
        if descriptor not in self.descriptors:
            self.descriptors.append(descriptor)
        if descriptor not in self.expected_identities:
            opened = os.fstat(descriptor) if metadata is None else metadata
            self.expected_identities[descriptor] = _descriptor_ownership_identity(
                opened
            )
        return descriptor

    def remember_identity(self, descriptor: int, metadata: os.stat_result) -> None:
        self.expected_identities[descriptor] = _descriptor_ownership_identity(metadata)

    def open(self, path: str | Path, flags: int, *, dir_fd: int | None = None) -> int:
        descriptor = -1
        try:
            if dir_fd is None:
                descriptor = os.open(path, flags)
            else:
                descriptor = os.open(path, flags, dir_fd=dir_fd)
            return self.retain(descriptor)
        except BaseException:  # noqa: B036 - commit native return ownership
            if descriptor >= 0:
                try:
                    self.retain(descriptor)
                except BaseException:  # noqa: B036 - preserve acquisition failure
                    pass
            raise

    def dup(self, descriptor: int) -> int:
        duplicate = -1
        try:
            duplicate = os.dup(descriptor)
            return self.retain(duplicate)
        except BaseException:  # noqa: B036 - commit native return ownership
            if duplicate >= 0:
                try:
                    self.retain(duplicate)
                except BaseException:  # noqa: B036 - preserve acquisition failure
                    pass
            raise

    def _close_selected(self, selected: tuple[int, ...]) -> BaseException | None:
        deferred: BaseException | None = None
        for descriptor in reversed(selected):
            if descriptor not in self.descriptors:
                continue
            closed = False
            try:
                before = os.fstat(descriptor)
                expected = self.expected_identities.get(descriptor)
                close_cookie = self.close_cookies.get(descriptor)
                if (
                    expected is not None
                    and _descriptor_ownership_identity(before) != expected
                ):
                    deferred = deferred or RuntimeError(
                        "source descriptor ownership changed"
                    )
                    closed = True
                elif close_cookie is not None and not _descriptor_has_close_cookie(
                    descriptor,
                    close_cookie,
                ):
                    deferred = deferred or RuntimeError(
                        "source descriptor ownership changed"
                    )
                    closed = True
                else:
                    try:
                        if close_cookie is None:
                            close_cookie = _arm_or_reuse_descriptor_close_cookie(
                                descriptor,
                                self.close_cookies.values(),
                            )
                            if close_cookie is not None:
                                self.close_cookies[descriptor] = close_cookie
                        os.close(descriptor)
                        closed = True
                    except BaseException as exc:  # noqa: B036 - probe close result
                        deferred = deferred or exc
                        try:
                            visible = os.fstat(descriptor)
                        except OSError as probe:
                            if probe.errno == errno.EBADF:
                                closed = True
                            else:
                                deferred = deferred or probe
                        except BaseException as probe:  # noqa: B036
                            deferred = deferred or probe
                        else:
                            if _descriptor_ownership_identity(
                                visible
                            ) != _descriptor_ownership_identity(before) or (
                                close_cookie is not None
                                and not _descriptor_has_close_cookie(
                                    descriptor,
                                    close_cookie,
                                )
                            ):
                                closed = True
            except OSError as exc:
                if exc.errno == errno.EBADF:
                    closed = True
                else:
                    deferred = deferred or exc
            except BaseException as exc:  # noqa: B036 - visit remaining fds
                deferred = deferred or exc
            if closed:
                while descriptor in self.descriptors:
                    self.descriptors.remove(descriptor)
                self.expected_identities.pop(descriptor, None)
                _discard_descriptor_close_cookie(self.close_cookies, descriptor)
        return deferred

    def close_descriptor(self, descriptor: int) -> None:
        failure = self._close_selected((descriptor,))
        if failure is not None:
            raise failure

    def close(self) -> None:
        failure = self._close_selected(tuple(self.descriptors))
        if failure is not None:
            raise failure


class _SourceCleanupGroup:
    """Retryable cleanup-all owner for independently pending authorities."""

    __slots__ = ("owners",)

    def __init__(self, *owners: object) -> None:
        self.owners: list[object] = []
        for owner in owners:
            self.add(owner)

    def add(self, owner: object) -> None:
        if owner is self:
            return
        if isinstance(owner, _SourceCleanupGroup):
            for nested in owner.owners:
                self.add(nested)
            return
        if not any(candidate is owner for candidate in self.owners):
            self.owners.append(owner)

    @property
    def closed(self) -> bool:
        for owner in self.owners:
            try:
                if not bool(owner.closed):  # type: ignore[attr-defined]
                    return False
            except BaseException:  # noqa: B036 - uncertain ownership is pending
                return False
        return True

    @property
    def pending_sources(self) -> tuple[object, ...]:
        pending: list[object] = []
        for owner in self.owners:
            try:
                if bool(owner.closed):  # type: ignore[attr-defined]
                    continue
            except BaseException:  # noqa: B036 - retain uncertain owner
                pass
            nested = getattr(owner, "pending_sources", None)
            if nested is None:
                pending.append(owner)
            else:
                pending.extend(nested)
        return tuple(pending)

    def close(self) -> None:
        first_failure: BaseException | None = None
        for owner in tuple(self.owners):
            try:
                owner.close()  # type: ignore[attr-defined]
            except BaseException as exc:  # noqa: B036 - cleanup every owner
                first_failure = first_failure or exc
        if first_failure is not None:
            raise first_failure


def _attach_source_cleanup_owner(failure: BaseException, owner: object) -> None:
    """Attach all still-live cleanup owners to the propagated exception."""

    try:
        if bool(owner.closed):  # type: ignore[attr-defined]
            return
    except BaseException:  # noqa: B036 - uncertain ownership must stay reachable
        pass
    existing = getattr(failure, "source_cleanup_owner", None)
    if existing is owner:
        return
    if existing is None:
        attached = owner
    elif isinstance(existing, _SourceCleanupGroup):
        existing.add(owner)
        attached = existing
    else:
        attached = _SourceCleanupGroup(existing, owner)
    try:
        failure.source_cleanup_owner = attached  # type: ignore[attr-defined]
    except BaseException:  # noqa: B036 - the current frame still retains *owner*
        pass


def _inherit_source_cleanup_owner(
    failure: BaseException,
    prior: BaseException,
) -> None:
    """Keep a nested retry owner reachable from a translated primary error."""

    owner = getattr(prior, "source_cleanup_owner", None)
    if owner is not None:
        _attach_source_cleanup_owner(failure, owner)


def _raise_with_cleanup(
    primary: BaseException,
    owner: object,
) -> None:
    """Prefer *primary* while retaining any failed cleanup for explicit retry."""

    cleanup_failure: BaseException | None = None
    try:
        owner.close()  # type: ignore[attr-defined]
    except BaseException as exc:  # noqa: B036 - primary remains authoritative
        cleanup_failure = exc
    _attach_source_cleanup_owner(primary, owner)
    if cleanup_failure is not None:
        raise primary from cleanup_failure
    raise primary


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _binding_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0,
        getattr(metadata, "st_file_attributes", 0),
    )


def _descriptor_ownership_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Stable identity for deciding whether an fd number is still ours."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT,
    )


def _arm_descriptor_close_cookie(descriptor: int) -> int | None:
    """Bind a retryable close to this exact open-file description."""

    cookie = _CLOSE_COOKIE_FLOOR + secrets.randbelow(_CLOSE_COOKIE_SPAN)
    try:
        observed = os.lseek(descriptor, cookie, os.SEEK_SET)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ESPIPE, getattr(errno, "ENOTSUP", -1)}:
            return None
        raise
    if observed != cookie or os.lseek(descriptor, 0, os.SEEK_CUR) != cookie:
        raise RuntimeError("could not arm source descriptor close ownership")
    return cookie


def _arm_or_reuse_descriptor_close_cookie(
    descriptor: int,
    active_cookies: Iterable[int],
) -> int | None:
    """Reuse a process-wide cookie shared by dup aliases, or arm one."""

    with _CLOSE_COOKIE_LOCK:
        try:
            position = os.lseek(descriptor, 0, os.SEEK_CUR)
        except OSError as exc:
            if exc.errno in {
                errno.EINVAL,
                errno.ESPIPE,
                getattr(errno, "ENOTSUP", -1),
            }:
                return None
            raise
        candidates = set(active_cookies).union(_CLOSE_COOKIE_COUNTS)
        for cookie in candidates:
            if type(cookie) is int and position == cookie:
                _CLOSE_COOKIE_COUNTS[cookie] = _CLOSE_COOKIE_COUNTS.get(cookie, 0) + 1
                return cookie
        cookie = _arm_descriptor_close_cookie(descriptor)
        if cookie is not None:
            _CLOSE_COOKIE_COUNTS[cookie] = _CLOSE_COOKIE_COUNTS.get(cookie, 0) + 1
        return cookie


def _release_descriptor_close_cookie(cookie: int) -> None:
    with _CLOSE_COOKIE_LOCK:
        count = _CLOSE_COOKIE_COUNTS.get(cookie, 0)
        if count <= 1:
            _CLOSE_COOKIE_COUNTS.pop(cookie, None)
        else:
            _CLOSE_COOKIE_COUNTS[cookie] = count - 1


def _discard_descriptor_close_cookie(
    close_cookies: dict[int, int],
    descriptor: int,
) -> None:
    cookie = close_cookies.pop(descriptor, None)
    if cookie is not None:
        _release_descriptor_close_cookie(cookie)


def _descriptor_has_close_cookie(descriptor: int, cookie: int) -> bool:
    try:
        return os.lseek(descriptor, 0, os.SEEK_CUR) == cookie
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return False
        raise


def _version_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        *_binding_identity(metadata),
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _identity_is_reliable(metadata: os.stat_result) -> bool:
    return bool(metadata.st_dev) and bool(metadata.st_ino)


def _relative_parts(value: str) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not value
        or (os.name == "nt" and "\\" in value)
        or "\x00" in value
    ):
        raise ValueError("source path must be a repository-relative POSIX path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
        or len(relative.parts) > _MAX_COMPONENTS
        or len(os.fsencode(value)) > _MAX_RELATIVE_PATH_BYTES
    ):
        raise ValueError("source path must be a repository-relative POSIX path")
    return relative.parts


def _normalize_link_target(
    root: Path,
    prefix: tuple[str, ...],
    target: str,
    *,
    allow_absolute: bool,
) -> tuple[str, ...]:
    if not target or "\x00" in target:
        raise ValueError("source symlink target must be non-empty")
    if len(os.fsencode(target)) > _MAX_SYMLINK_TARGET_BYTES:
        raise ValueError("source symlink target exceeds its byte limit")
    if os.path.isabs(target):
        if not allow_absolute:
            raise ValueError("source symlink target must be relative")
        normalized_target = Path(os.path.normpath(target))
        try:
            target = normalized_target.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError("source symlink resolves outside the repository") from exc
        resolved: list[str] = []
    else:
        resolved = list(prefix)
    for part in target.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise ValueError("source symlink resolves outside the repository")
            resolved.pop()
            continue
        resolved.append(part)
        if len(resolved) > _MAX_COMPONENTS:
            raise ValueError("resolved source path exceeds its component limit")
    return tuple(resolved)


@dataclass(slots=True)
class _PathObservation:
    parent_descriptor: int
    name: str
    identity: tuple[int, ...]
    link_target: str | None = None


@dataclass(slots=True)
class _BoundRepositoryFile:
    descriptor: int
    opened_identity: tuple[int, ...]
    root: Path
    root_descriptor: int
    root_identity: tuple[int, ...]
    observations: list[_PathObservation]
    cleanup: _PosixDescriptorCleanup
    is_regular = True
    is_directory = False

    @property
    def closed(self) -> bool:
        return self.cleanup.closed

    def update_hash(self, hasher: object) -> None:
        update = getattr(hasher, "update", None)
        if not callable(update):
            raise TypeError("hasher must provide an update method")
        opened = os.fstat(self.descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _version_identity(opened) != self.opened_identity
            or not _identity_is_reliable(opened)
        ):
            raise ValueError("source file is not a stable regular file")
        remaining = opened.st_size
        while remaining:
            block = os.read(self.descriptor, min(remaining, _READ_CHUNK_BYTES))
            if not block:
                raise ValueError("source file was truncated while hashing")
            update(block)
            remaining -= len(block)
        if os.read(self.descriptor, 1):
            raise ValueError("source file grew while hashing")
        self.verify()

    def read_bytes(self, *, max_bytes: int) -> bytes:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("source byte limit must be positive")
        opened = os.fstat(self.descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _version_identity(opened) != self.opened_identity
            or opened.st_size < 0
            or opened.st_size > max_bytes
        ):
            raise ValueError("source file is not a stable bounded regular file")
        payload = bytearray()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(self.descriptor, min(remaining, _READ_CHUNK_BYTES))
            if not chunk:
                raise ValueError("source file was truncated while being read")
            payload.extend(chunk)
            remaining -= len(chunk)
        if os.read(self.descriptor, 1):
            raise ValueError("source file grew while being read")
        self.verify()
        return bytes(payload)

    def verify(self) -> None:
        _verify_resolution_state(
            self.root,
            self.root_descriptor,
            self.root_identity,
            self.observations,
        )
        if _version_identity(os.fstat(self.descriptor)) != self.opened_identity:
            raise ValueError("source file changed while it was being read")

    def close(self) -> None:
        try:
            self.cleanup.close()
        finally:
            pending = set(self.cleanup.descriptors)
            if self.descriptor not in pending:
                self.descriptor = -1
            if self.root_descriptor not in pending:
                self.root_descriptor = -1
            for item in self.observations:
                if item.parent_descriptor not in pending:
                    item.parent_descriptor = -1


@dataclass(slots=True)
class _StableUnresolvedRepositoryFile:
    root: Path
    root_descriptor: int
    root_identity: tuple[int, ...]
    observations: list[_PathObservation]
    parent_descriptor: int
    parent_identity: tuple[int, ...]
    name: str
    expected: tuple[int, ...] | None
    cleanup: _PosixDescriptorCleanup
    is_regular = False
    is_directory = False

    @property
    def closed(self) -> bool:
        return self.cleanup.closed

    def verify(self) -> None:
        _verify_stable_unresolved(
            root=self.root,
            root_descriptor=self.root_descriptor,
            root_identity=self.root_identity,
            observations=self.observations,
            parent_descriptor=self.parent_descriptor,
            parent_identity=self.parent_identity,
            name=self.name,
            expected=self.expected,
        )

    def close(self) -> None:
        try:
            self.cleanup.close()
        finally:
            pending = set(self.cleanup.descriptors)
            if self.root_descriptor not in pending:
                self.root_descriptor = -1
            if self.parent_descriptor not in pending:
                self.parent_descriptor = -1
            for item in self.observations:
                if item.parent_descriptor not in pending:
                    item.parent_descriptor = -1


@dataclass(slots=True)
class _BoundRepositoryDirectory:
    """Pinned directory outcome for a lexical repository symlink."""

    descriptor: int
    opened_identity: tuple[int, ...]
    root: Path
    root_descriptor: int
    root_identity: tuple[int, ...]
    observations: list[_PathObservation]
    cleanup: _PosixDescriptorCleanup
    is_regular = False
    is_directory = True

    @property
    def closed(self) -> bool:
        return self.cleanup.closed

    def verify(self) -> None:
        _verify_resolution_state(
            self.root,
            self.root_descriptor,
            self.root_identity,
            self.observations,
        )
        opened = os.fstat(self.descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _version_identity(opened) != self.opened_identity
        ):
            raise ValueError("source directory changed during contained resolution")

    def close(self) -> None:
        try:
            self.cleanup.close()
        finally:
            pending = set(self.cleanup.descriptors)
            if self.descriptor not in pending:
                self.descriptor = -1
            if self.root_descriptor not in pending:
                self.root_descriptor = -1
            for item in self.observations:
                if item.parent_descriptor not in pending:
                    item.parent_descriptor = -1


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _regular_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )


def _verify_resolution_state(
    root: Path,
    root_descriptor: int,
    root_identity: tuple[int, ...],
    observations: list[_PathObservation],
) -> None:
    root_opened = os.fstat(root_descriptor)
    if _version_identity(root_opened) != root_identity:
        raise ValueError("repository root changed during source resolution")
    root_after = root.lstat()
    if _binding_identity(root_after) != _binding_identity(root_opened):
        raise ValueError("repository root changed during source resolution")
    for observation in observations:
        current = os.stat(
            observation.name,
            dir_fd=observation.parent_descriptor,
            follow_symlinks=False,
        )
        if _version_identity(current) != observation.identity:
            raise ValueError("source path changed during contained resolution")
        if observation.link_target is not None and (
            not stat.S_ISLNK(current.st_mode)
            or os.readlink(
                observation.name,
                dir_fd=observation.parent_descriptor,
            )
            != observation.link_target
        ):
            raise ValueError("source symlink changed during contained resolution")


def _verify_stable_unresolved(
    *,
    root: Path,
    root_descriptor: int,
    root_identity: tuple[int, ...],
    observations: list[_PathObservation],
    parent_descriptor: int,
    parent_identity: tuple[int, ...],
    name: str,
    expected: tuple[int, ...] | None,
) -> None:
    """Prove a missing or nonregular terminal outcome without opening it."""

    _verify_resolution_state(root, root_descriptor, root_identity, observations)
    if _version_identity(os.fstat(parent_descriptor)) != parent_identity:
        raise ValueError("source parent changed during contained resolution")
    try:
        observed = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        observed_identity = None
    else:
        observed_identity = _version_identity(observed)
    if observed_identity != expected:
        raise ValueError("source terminal changed during contained resolution")
    _verify_resolution_state(root, root_descriptor, root_identity, observations)
    if _version_identity(os.fstat(parent_descriptor)) != parent_identity:
        raise ValueError("source parent changed during contained resolution")


def _open_secure(
    root: Path,
    parts: tuple[str, ...],
    *,
    expected_final_identity: tuple[int, ...] | None = None,
    expected_final_link_target: str | None = None,
    allow_stable_unresolved: bool = False,
    allow_absolute_symlinks: bool = False,
    pinned_root_descriptor: int | None = None,
    expected_root_identity: tuple[int, ...] | None = None,
    cleanup_slot: _SourceCleanupSlot | None = None,
) -> _BoundRepositoryFile | _BoundRepositoryDirectory | _StableUnresolvedRepositoryFile:
    cleanup = _PosixDescriptorCleanup()
    if cleanup_slot is not None:
        cleanup_slot.own(cleanup)
    if pinned_root_descriptor is None:
        try:
            root_before = root.lstat()
        except FileNotFoundError as exc:
            raise ContainedSourceMissingError("repository root does not exist") from exc
        try:
            root_descriptor = cleanup.open(root, _directory_flags())
        except BaseException as primary:  # noqa: B036 - retain partial open
            _raise_with_cleanup(primary, cleanup_slot or cleanup)
    else:
        if (
            isinstance(pinned_root_descriptor, bool)
            or not isinstance(pinned_root_descriptor, int)
            or pinned_root_descriptor < 0
        ):
            raise ValueError("pinned repository root descriptor is invalid")
        try:
            root_descriptor = cleanup.dup(pinned_root_descriptor)
        except BaseException as primary:  # noqa: B036 - retain partial dup
            _raise_with_cleanup(primary, cleanup_slot or cleanup)
        try:
            root_before = os.fstat(root_descriptor)
            path_before = root.lstat()
        except BaseException as primary:  # noqa: B036 - preserve acquisition
            _raise_with_cleanup(primary, cleanup_slot or cleanup)
        if _binding_identity(path_before) != _binding_identity(root_before):
            _raise_with_cleanup(
                ValueError("repository root changed during source resolution"),
                cleanup_slot or cleanup,
            )
    if _is_link_or_reparse(root_before) or not stat.S_ISDIR(root_before.st_mode):
        _raise_with_cleanup(
            ValueError("repository root must be a real directory"),
            cleanup_slot or cleanup,
        )
    if not _identity_is_reliable(root_before):
        _raise_with_cleanup(
            ValueError("repository root has no reliable filesystem identity"),
            cleanup_slot or cleanup,
        )
    if (
        expected_root_identity is not None
        and _version_identity(root_before) != expected_root_identity
    ):
        _raise_with_cleanup(
            ValueError("repository root changed during source resolution"),
            cleanup_slot or cleanup,
        )
    current_descriptor = -1
    source_descriptor = -1
    observations: list[_PathObservation] = []
    seen_links: set[tuple[int, int]] = set()
    try:
        root_opened = os.fstat(root_descriptor)
        if _binding_identity(root_opened) != _binding_identity(root_before):
            raise ValueError("repository root changed during source resolution")

        def retain_unresolved(
            *,
            parent_descriptor: int,
            parent_identity: tuple[int, ...],
            name: str,
            expected: tuple[int, ...] | None,
        ) -> _StableUnresolvedRepositoryFile:
            nonlocal root_descriptor, observations
            _verify_stable_unresolved(
                root=root,
                root_descriptor=root_descriptor,
                root_identity=_version_identity(root_opened),
                observations=observations,
                parent_descriptor=parent_descriptor,
                parent_identity=parent_identity,
                name=name,
                expected=expected,
            )
            retained = _StableUnresolvedRepositoryFile(
                root=root,
                root_descriptor=root_descriptor,
                root_identity=_version_identity(root_opened),
                observations=observations,
                parent_descriptor=cleanup.dup(parent_descriptor),
                parent_identity=parent_identity,
                name=name,
                expected=expected,
                cleanup=cleanup,
            )
            root_descriptor = -1
            observations = []
            if cleanup_slot is not None:
                cleanup_slot.own(retained)
            return retained

        def retain_directory(
            directory_descriptor: int,
            *,
            expected_binding: tuple[int, ...] | None = None,
        ) -> _BoundRepositoryDirectory:
            nonlocal root_descriptor, observations
            try:
                opened = os.fstat(directory_descriptor)
                if (
                    expected_binding is not None
                    and _binding_identity(opened) != expected_binding
                ):
                    raise ValueError("source directory changed while opening")
                binding = _BoundRepositoryDirectory(
                    descriptor=directory_descriptor,
                    opened_identity=_version_identity(opened),
                    root=root,
                    root_descriptor=root_descriptor,
                    root_identity=_version_identity(root_opened),
                    observations=observations,
                    cleanup=cleanup,
                )
            except BaseException as primary:  # noqa: B036 - preserve construction
                _raise_with_cleanup(primary, cleanup_slot or cleanup)
            root_descriptor = -1
            observations = []
            try:
                binding.verify()
            except BaseException as primary:  # noqa: B036 - preserve validation
                _raise_with_cleanup(primary, cleanup_slot or binding)
            if cleanup_slot is not None:
                cleanup_slot.own(binding)
            return binding

        current_descriptor = cleanup.dup(root_descriptor)
        pending = [(name, index == len(parts) - 1) for index, name in enumerate(parts)]
        resolved_prefix: tuple[str, ...] = ()
        symlink_count = 0
        while pending:
            name, is_lexical_final = pending.pop(0)
            parent_identity = _version_identity(os.fstat(current_descriptor))
            try:
                before = os.stat(
                    name,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                expected_is_link = expected_final_identity is not None and stat.S_ISLNK(
                    expected_final_identity[2]
                )
                if not allow_stable_unresolved or not expected_is_link:
                    raise
                return retain_unresolved(
                    parent_descriptor=current_descriptor,
                    parent_identity=parent_identity,
                    name=name,
                    expected=None,
                )
            if not _identity_is_reliable(before):
                raise ValueError("source path has no reliable filesystem identity")
            if (
                is_lexical_final
                and expected_final_identity is not None
                and _version_identity(before) != expected_final_identity
            ):
                raise ValueError("source path differs from its initial observation")
            if bool(
                getattr(before, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError("source path contains a reparse point")
            if stat.S_ISLNK(before.st_mode):
                symlink_count += 1
                if symlink_count > _MAX_SYMLINKS:
                    raise ValueError("source symlink resolution exceeds its limit")
                link_identity = (before.st_dev, before.st_ino)
                target = os.readlink(name, dir_fd=current_descriptor)
                if (
                    is_lexical_final
                    and expected_final_link_target is not None
                    and target != expected_final_link_target
                ):
                    raise ValueError(
                        "source symlink target differs from its initial observation"
                    )
                after = os.stat(
                    name,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
                if _version_identity(after) != _version_identity(before):
                    raise ValueError(
                        "source symlink changed during contained resolution"
                    )
                observations.append(
                    _PathObservation(
                        parent_descriptor=cleanup.dup(current_descriptor),
                        name=name,
                        identity=_version_identity(before),
                        link_target=target,
                    )
                )
                if link_identity in seen_links:
                    expected_is_link = (
                        expected_final_identity is not None
                        and stat.S_ISLNK(expected_final_identity[2])
                    )
                    if not allow_stable_unresolved or not expected_is_link:
                        raise ValueError("source symlink resolution contains a loop")
                    return retain_unresolved(
                        parent_descriptor=current_descriptor,
                        parent_identity=parent_identity,
                        name=name,
                        expected=_version_identity(before),
                    )
                seen_links.add(link_identity)
                target_parts = _normalize_link_target(
                    root,
                    resolved_prefix,
                    target,
                    allow_absolute=allow_absolute_symlinks,
                )
                if len(target_parts) + len(pending) > _MAX_COMPONENTS:
                    raise ValueError("resolved source path exceeds its component limit")
                if not target_parts and not pending:
                    expected_is_link = (
                        expected_final_identity is not None
                        and stat.S_ISLNK(expected_final_identity[2])
                    )
                    if not allow_stable_unresolved or not expected_is_link:
                        raise ValueError(
                            "source path resolves to the repository directory"
                        )
                    return retain_directory(
                        cleanup.dup(root_descriptor),
                        expected_binding=_binding_identity(root_opened),
                    )
                pending = [*((part, False) for part in target_parts), *pending]
                resolved_prefix = ()
                cleanup.close_descriptor(current_descriptor)
                current_descriptor = cleanup.dup(root_descriptor)
                continue

            observations.append(
                _PathObservation(
                    parent_descriptor=cleanup.dup(current_descriptor),
                    name=name,
                    identity=_version_identity(before),
                )
            )
            if pending:
                if not stat.S_ISDIR(before.st_mode):
                    raise ValueError("source path has a non-directory component")
                child_descriptor = -1
                try:
                    child_descriptor = cleanup.open(
                        name,
                        _directory_flags(),
                        dir_fd=current_descriptor,
                    )
                    opened = os.fstat(child_descriptor)
                    if _binding_identity(opened) != _binding_identity(before):
                        raise ValueError("source directory changed while opening")
                except BaseException as primary:  # noqa: B036 - preserve open error
                    _raise_with_cleanup(primary, cleanup_slot or cleanup)
                cleanup.close_descriptor(current_descriptor)
                current_descriptor = child_descriptor
                resolved_prefix = (*resolved_prefix, name)
                continue

            if not stat.S_ISREG(before.st_mode):
                expected_is_link = expected_final_identity is not None and stat.S_ISLNK(
                    expected_final_identity[2]
                )
                if (
                    allow_stable_unresolved
                    and expected_is_link
                    and stat.S_ISDIR(before.st_mode)
                ):
                    directory_descriptor = -1
                    try:
                        directory_descriptor = cleanup.open(
                            name,
                            _directory_flags(),
                            dir_fd=current_descriptor,
                        )
                        retained_descriptor = directory_descriptor
                        directory_descriptor = -1
                        return retain_directory(
                            retained_descriptor,
                            expected_binding=_binding_identity(before),
                        )
                    finally:
                        if directory_descriptor >= 0:
                            cleanup.close_descriptor(directory_descriptor)
                if (
                    allow_stable_unresolved
                    and expected_is_link
                    and not stat.S_ISDIR(before.st_mode)
                ):
                    return retain_unresolved(
                        parent_descriptor=current_descriptor,
                        parent_identity=parent_identity,
                        name=name,
                        expected=_version_identity(before),
                    )
                raise ValueError("source path is not a regular repository file")
            source_descriptor = cleanup.open(
                name,
                _regular_flags(),
                dir_fd=current_descriptor,
            )
            opened = os.fstat(source_descriptor)
            if _binding_identity(opened) != _binding_identity(before):
                raise ValueError("source file changed while opening")
            binding = _BoundRepositoryFile(
                descriptor=source_descriptor,
                opened_identity=_version_identity(opened),
                root=root,
                root_descriptor=root_descriptor,
                root_identity=_version_identity(root_opened),
                observations=observations,
                cleanup=cleanup,
            )
            source_descriptor = -1
            root_descriptor = -1
            try:
                binding.verify()
            except BaseException as primary:  # noqa: B036 - preserve validation
                _raise_with_cleanup(primary, cleanup_slot or binding)
            if cleanup_slot is not None:
                cleanup_slot.own(binding)
            return binding
        raise ValueError("source path does not resolve to a file")
    except FileNotFoundError as exc:
        raise ContainedSourceMissingError(
            "source path has no resolvable regular target"
        ) from exc
    except OSError as exc:
        raise ValueError("source path could not be resolved safely") from exc
    finally:
        active_failure = sys.exc_info()[1]
        cleanup_failure: BaseException | None = None
        try:
            if root_descriptor < 0 and source_descriptor < 0:
                if current_descriptor >= 0:
                    cleanup.close_descriptor(current_descriptor)
            else:
                cleanup.close()
        except BaseException as exc:  # noqa: B036 - preserve primary + owner
            cleanup_failure = exc
        owner = cleanup_slot or cleanup
        if active_failure is not None:
            _attach_source_cleanup_owner(active_failure, owner)
            if cleanup_failure is not None:
                raise active_failure from cleanup_failure
        elif cleanup_failure is not None:
            _attach_source_cleanup_owner(cleanup_failure, owner)
            raise cleanup_failure


def _windows_kernel_api() -> _WindowsKernelApi:
    return _shared_windows_kernel_api()


def _windows_lexical_repository_path(value: str | Path) -> Path:
    expanded = os.path.expanduser(os.fspath(value))
    return Path(ntpath.abspath(expanded))


def _windows_binding_identity(metadata: _WindowsHandleMetadata) -> tuple[object, ...]:
    return (
        metadata.st_dev,
        metadata.file_id_128,
        metadata.st_mode,
        metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0,
        metadata.st_file_attributes,
        metadata.reparse_tag,
    )


def _windows_version_identity(metadata: _WindowsHandleMetadata) -> tuple[object, ...]:
    return (
        *_windows_binding_identity(metadata),
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
        metadata.delete_pending,
    )


def _windows_identity_is_reliable(metadata: _WindowsHandleMetadata) -> bool:
    return (
        bool(metadata.st_dev)
        and _windows_file_id_is_reliable(metadata.file_id_128)
        and not metadata.delete_pending
    )


def _windows_entry_is_reparse(entry: _WindowsDirectoryEntry) -> bool:
    return bool(entry.attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT)


def _windows_metadata_is_reparse(metadata: _WindowsHandleMetadata) -> bool:
    return bool(metadata.st_file_attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT)


def _windows_simple_child_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or len(name.encode("utf-8", errors="strict")) > _MAX_RELATIVE_PATH_BYTES
    ):
        raise ValueError("Windows source entry has an invalid child name")


def _windows_find_child(
    api: _WindowsKernelApi,
    parent_handle: int,
    name: str,
) -> _WindowsDirectoryEntry | None:
    """Find one exact child without imposing unsafe case-folding semantics."""

    _windows_simple_child_name(name)
    matches = [
        entry for entry in api.enumerate_directory(parent_handle) if entry.name == name
    ]
    if len(matches) > 1:
        raise ValueError("Windows source directory has duplicate exact child names")
    return matches[0] if matches else None


def _windows_open_child(
    api: _WindowsKernelApi,
    parent_handle: int,
    entry: _WindowsDirectoryEntry,
    *,
    cleanup: _WindowsHandleCleanup | None = None,
) -> tuple[int, _WindowsHandleMetadata]:
    """Open the enumerated child no-follow and rebind its exact FILE_ID."""

    if not _windows_file_id_is_reliable(entry.file_id_128):
        raise ValueError("Windows source entry has no reliable 128-bit FILE_ID")
    is_reparse = _windows_entry_is_reparse(entry)
    is_directory = bool(entry.attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY)
    desired_access = _WINDOWS_FILE_READ_ATTRIBUTES | _WINDOWS_SYNCHRONIZE
    if not is_reparse:
        desired_access |= (
            _WINDOWS_FILE_LIST_DIRECTORY if is_directory else _WINDOWS_FILE_READ_DATA
        )
    selected_cleanup = cleanup or _WindowsHandleCleanup(api)
    handle = 0
    try:
        handle = api.open_relative(
            parent_handle,
            entry.name,
            desired_access=desired_access,
            is_directory=is_directory,
            allow_reparse=is_reparse,
        )
        selected_cleanup.retain(handle)
        opened = api.metadata(handle)
        selected_cleanup.retain(handle, opened)
        if (
            not _windows_identity_is_reliable(opened)
            or opened.file_id_128 != entry.file_id_128
            or opened.st_dev != api.metadata(parent_handle).st_dev
            or bool(opened.st_file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY)
            != is_directory
            or _windows_metadata_is_reparse(opened) != is_reparse
            or (is_reparse and opened.reparse_tag != entry.reparse_tag)
        ):
            raise ValueError("Windows source entry changed while it was opened")
        return handle, opened
    except BaseException as primary:  # noqa: B036 - preserve construction failure
        _raise_with_cleanup(primary, selected_cleanup)


def _normalize_windows_link_target(
    root: Path,
    prefix: tuple[str, ...],
    point: _WindowsReparsePoint,
) -> tuple[str, ...]:
    """Normalize one Windows SYMLINK payload into pinned-root components."""

    target = point.substitute_name
    if not isinstance(target, str) or not target or "\x00" in target:
        raise ValueError("Windows source symlink target must be non-empty")
    if len(target.encode("utf-8", errors="strict")) > _MAX_SYMLINK_TARGET_BYTES:
        raise ValueError("Windows source symlink target exceeds its byte limit")
    relative = bool(point.flags & _WINDOWS_SYMLINK_FLAG_RELATIVE)
    if point.flags & ~_WINDOWS_SYMLINK_FLAG_RELATIVE:
        raise ValueError("Windows source symlink has unsupported flags")

    if relative:
        if ntpath.isabs(target) or ntpath.splitdrive(target)[0]:
            raise ValueError("Windows relative symlink contains an absolute target")
        resolved: list[str] = list(prefix)
        target_parts = target.replace("\\", "/").split("/")
    else:
        raise ValueError("Windows source symlink target must be relative")

    for part in target_parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise ValueError(
                    "Windows source symlink resolves outside the repository"
                )
            resolved.pop()
            continue
        if ":" in part:
            raise ValueError("Windows source symlink contains a stream component")
        _windows_simple_child_name(part)
        resolved.append(part)
        if len(resolved) > _MAX_COMPONENTS:
            raise ValueError("resolved Windows source path exceeds its component limit")
    return tuple(resolved)


def _windows_link_target_text(point: _WindowsReparsePoint) -> str:
    target = point.print_name or point.substitute_name
    if not isinstance(target, str) or not target or "\x00" in target:
        raise ValueError("Windows source symlink has no canonical text target")
    if len(target.encode("utf-8", errors="strict")) > _MAX_SYMLINK_TARGET_BYTES:
        raise ValueError("Windows source symlink target exceeds its byte limit")
    return target


@dataclass(slots=True)
class _WindowsPathObservation:
    parent_handle: int
    parent_identity: tuple[object, ...]
    name: str
    file_id_128: bytes
    handle: int
    identity: tuple[object, ...]
    reparse_point: _WindowsReparsePoint | None = None


def _verify_windows_root_path(
    api: _WindowsKernelApi,
    authority: _WindowsDirectoryAuthority,
    root_handle: int,
    root_identity: tuple[object, ...],
) -> None:
    authority.verify()
    opened = api.metadata(root_handle)
    if _windows_version_identity(opened) != root_identity:
        raise ValueError("Windows repository root changed during source resolution")


def _verify_windows_resolution_state(
    api: _WindowsKernelApi,
    authority: _WindowsDirectoryAuthority,
    root_handle: int,
    root_identity: tuple[object, ...],
    observations: Sequence[_WindowsPathObservation],
) -> None:
    _verify_windows_root_path(api, authority, root_handle, root_identity)
    for observation in observations:
        if (
            _windows_version_identity(api.metadata(observation.parent_handle))
            != observation.parent_identity
        ):
            raise ValueError("Windows source parent changed during resolution")
        entry = _windows_find_child(api, observation.parent_handle, observation.name)
        if entry is None or entry.file_id_128 != observation.file_id_128:
            raise ValueError("Windows source path changed during resolution")
        if (
            _windows_version_identity(api.metadata(observation.handle))
            != observation.identity
        ):
            raise ValueError("Windows source object changed during resolution")
        if observation.reparse_point is not None and (
            api.query_reparse_point(observation.handle) != observation.reparse_point
        ):
            raise ValueError("Windows source symlink changed during resolution")
    _verify_windows_root_path(api, authority, root_handle, root_identity)


class _WindowsResolutionBinding:
    is_regular = False
    is_directory = False

    def __init__(
        self,
        *,
        api: _WindowsKernelApi,
        root: Path,
        root_authority: _WindowsDirectoryAuthority,
        owns_root_authority: bool,
        root_handle: int,
        root_identity: tuple[object, ...],
        observations: list[_WindowsPathObservation],
        handles: list[int],
        handle_identities: dict[int, tuple[object, ...]],
    ) -> None:
        self.api = api
        self.root = root
        self.root_authority = root_authority
        self.owns_root_authority = owns_root_authority
        self.root_handle = root_handle
        self.root_identity = root_identity
        self.observations = observations
        self.handles = handles
        self.handle_identities = handle_identities
        self.closed = False
        self._cleanup_started = False

    def verify(self) -> None:
        if self.closed or self._cleanup_started:
            raise ValueError("Windows source binding is closed")
        _verify_windows_resolution_state(
            self.api,
            self.root_authority,
            self.root_handle,
            self.root_identity,
            self.observations,
        )

    def close(self) -> None:
        if self.closed:
            return
        self._cleanup_started = True
        failure = _close_windows_handles(
            self.api,
            self.handles,
            self.handle_identities,
        )
        if self.root_handle not in self.handles:
            self.root_handle = 0
        if self.owns_root_authority:
            try:
                self.root_authority.close()
            except BaseException as exc:  # noqa: B036 - finish all cleanup
                if failure is None:
                    failure = exc
        self.closed = not self.handles and (
            not self.owns_root_authority or self.root_authority.closed
        )
        if failure is not None:
            raise failure


class _WindowsResolutionConstructionCleanup:
    """Owner spanning partial resolution HANDLEs and an optional root authority."""

    __slots__ = ("handles", "root_authority", "owns_root_authority")

    def __init__(
        self,
        api: _WindowsKernelApi,
        root_authority: _WindowsDirectoryAuthority,
        *,
        owns_root_authority: bool,
    ) -> None:
        self.handles = _WindowsHandleCleanup(api)
        self.root_authority = root_authority
        self.owns_root_authority = owns_root_authority

    @property
    def closed(self) -> bool:
        return self.handles.closed and (
            not self.owns_root_authority or self.root_authority.closed
        )

    def close(self) -> None:
        failure: BaseException | None = None
        try:
            self.handles.close()
        except BaseException as exc:  # noqa: B036 - cleanup all authorities
            failure = exc
        if self.owns_root_authority:
            try:
                self.root_authority.close()
            except BaseException as exc:  # noqa: B036 - preserve first failure
                failure = failure or exc
        if failure is not None:
            raise failure


class _WindowsBoundRepositoryFile(_WindowsResolutionBinding):
    is_regular = True

    def __init__(
        self,
        *,
        file_handle: int,
        opened_identity: tuple[object, ...],
        opened_size: int,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.file_handle = file_handle
        self.opened_identity = opened_identity
        self.opened_size = opened_size

    def _opened(self) -> _WindowsHandleMetadata:
        opened = self.api.metadata(self.file_handle)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _windows_metadata_is_reparse(opened)
            or not _windows_identity_is_reliable(opened)
            or _windows_version_identity(opened) != self.opened_identity
            or opened.st_size != self.opened_size
        ):
            raise ValueError("Windows source file is not a stable regular file")
        return opened

    def update_hash(self, hasher: object) -> None:
        update = getattr(hasher, "update", None)
        if not callable(update):
            raise TypeError("hasher must provide an update method")
        opened = self._opened()
        remaining = opened.st_size
        while remaining:
            block = self.api.read(self.file_handle, min(remaining, _READ_CHUNK_BYTES))
            if not block:
                raise ValueError("Windows source file was truncated while hashing")
            update(block)
            remaining -= len(block)
        if self.api.read(self.file_handle, 1):
            raise ValueError("Windows source file grew while hashing")
        self.verify()
        self._opened()

    def read_bytes(self, *, max_bytes: int) -> bytes:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("source byte limit must be positive")
        opened = self._opened()
        if opened.st_size < 0 or opened.st_size > max_bytes:
            raise ValueError("Windows source file exceeds its bounded read limit")
        payload = bytearray()
        remaining = opened.st_size
        while remaining:
            block = self.api.read(self.file_handle, min(remaining, _READ_CHUNK_BYTES))
            if not block:
                raise ValueError("Windows source file was truncated while being read")
            payload.extend(block)
            remaining -= len(block)
        if self.api.read(self.file_handle, 1):
            raise ValueError("Windows source file grew while being read")
        self.verify()
        self._opened()
        return bytes(payload)


class _WindowsBoundRepositoryDirectory(_WindowsResolutionBinding):
    is_directory = True

    def __init__(
        self,
        *,
        directory_handle: int,
        opened_identity: tuple[object, ...],
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.directory_handle = directory_handle
        self.opened_identity = opened_identity

    def verify(self) -> None:
        super().verify()
        opened = self.api.metadata(self.directory_handle)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _windows_metadata_is_reparse(opened)
            or _windows_version_identity(opened) != self.opened_identity
        ):
            raise ValueError("Windows source directory changed during resolution")


class _WindowsStableUnresolvedRepositoryFile(_WindowsResolutionBinding):
    def __init__(
        self,
        *,
        parent_handle: int,
        parent_identity: tuple[object, ...],
        name: str,
        expected_file_id: bytes | None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.parent_handle = parent_handle
        self.parent_identity = parent_identity
        self.name = name
        self.expected_file_id = expected_file_id

    def verify(self) -> None:
        super().verify()
        if (
            _windows_version_identity(self.api.metadata(self.parent_handle))
            != self.parent_identity
        ):
            raise ValueError("Windows unresolved source parent changed")
        entry = _windows_find_child(self.api, self.parent_handle, self.name)
        observed = None if entry is None else entry.file_id_128
        if observed != self.expected_file_id:
            raise ValueError("Windows unresolved source terminal changed")


def _open_windows_pinned_repository_root(
    root: Path,
    *,
    api: _WindowsKernelApi | None = None,
    cleanup_slot: _SourceCleanupSlot | None = None,
) -> tuple[
    _WindowsKernelApi,
    _WindowsDirectoryAuthority,
    tuple[object, ...],
]:
    selected = _windows_kernel_api() if api is None else api
    if api is None:
        _shared_windows_require_source_api(selected)
    authority = _open_lexical_windows_authority(
        root,
        api=selected,
        cleanup_slot=cleanup_slot,
    )
    try:
        opened = selected.metadata(authority.handle)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _windows_metadata_is_reparse(opened)
            or not _windows_identity_is_reliable(opened)
        ):
            raise ValueError(
                "Windows repository root must be a real identity-bound directory"
            )
        identity = _windows_version_identity(opened)
        _verify_windows_root_path(selected, authority, authority.handle, identity)
        return selected, authority, identity
    except BaseException as primary:  # noqa: B036 - preserve root failure
        _raise_with_cleanup(primary, cleanup_slot or authority)


def _verify_windows_pinned_repository_root(
    api: _WindowsKernelApi,
    authority: _WindowsDirectoryAuthority,
    expected_identity: tuple[object, ...],
) -> None:
    _verify_windows_root_path(
        api,
        authority,
        authority.handle,
        expected_identity,
    )


def _open_windows_resolution_at(
    root: Path,
    root_authority: _WindowsDirectoryAuthority,
    parts: tuple[str, ...],
    *,
    expected_root_identity: tuple[object, ...],
    expected_final_identity: tuple[object, ...] | None,
    expected_final_link_target: str | None = None,
    allow_stable_unresolved: bool,
    api: _WindowsKernelApi,
    owns_root_authority: bool = False,
    cleanup_slot: _SourceCleanupSlot | None = None,
) -> _WindowsResolutionBinding:
    construction = _WindowsResolutionConstructionCleanup(
        api,
        root_authority,
        owns_root_authority=owns_root_authority,
    )
    if cleanup_slot is not None:
        cleanup_slot.own(construction)
    cleanup = construction.handles
    owned_root = 0
    try:
        owned_root = api.duplicate_handle(root_authority.handle)
        cleanup.retain(owned_root)
    except BaseException as primary:  # noqa: B036 - retain partial duplicate
        _raise_with_cleanup(primary, cleanup_slot or construction)
    handles = cleanup.handles
    handle_identities = cleanup.expected_identities
    observations: list[_WindowsPathObservation] = []
    try:
        root_opened = api.metadata(owned_root)
        cleanup.retain(owned_root, root_opened)
        if (
            _windows_version_identity(root_opened) != expected_root_identity
            or not stat.S_ISDIR(root_opened.st_mode)
            or _windows_metadata_is_reparse(root_opened)
            or not _windows_identity_is_reliable(root_opened)
        ):
            raise ValueError("Windows repository root changed before resolution")
        current_handle = owned_root
        pending = [(part, index == len(parts) - 1) for index, part in enumerate(parts)]
        resolved_prefix: tuple[str, ...] = ()
        seen_links: set[bytes] = set()
        symlink_count = 0
        lexical_final_is_link = bool(
            expected_final_identity is not None
            and len(expected_final_identity) > 5
            and expected_final_identity[5] == _WINDOWS_IO_REPARSE_TAG_SYMLINK
        )

        def common_kwargs() -> dict[str, object]:
            return {
                "api": api,
                "root": root,
                "root_authority": root_authority,
                "owns_root_authority": owns_root_authority,
                "root_handle": owned_root,
                "root_identity": expected_root_identity,
                "observations": observations,
                "handles": handles,
                "handle_identities": handle_identities,
            }

        while pending:
            name, is_lexical_final = pending.pop(0)
            parent_identity = _windows_version_identity(api.metadata(current_handle))
            entry = _windows_find_child(api, current_handle, name)
            if entry is None:
                if not (allow_stable_unresolved and lexical_final_is_link):
                    raise FileNotFoundError(name)
                binding = _WindowsStableUnresolvedRepositoryFile(
                    parent_handle=current_handle,
                    parent_identity=parent_identity,
                    name=name,
                    expected_file_id=None,
                    **common_kwargs(),
                )
                binding.verify()
                if cleanup_slot is not None:
                    cleanup_slot.own(binding)
                return binding
            opened_handle, opened = _windows_open_child(
                api,
                current_handle,
                entry,
                cleanup=cleanup,
            )
            opened_identity = _windows_version_identity(opened)
            if (
                is_lexical_final
                and expected_final_identity is not None
                and opened_identity != expected_final_identity
            ):
                raise ValueError("Windows source differs from its initial observation")

            point: _WindowsReparsePoint | None = None
            if _windows_metadata_is_reparse(opened):
                if opened.reparse_tag != _WINDOWS_IO_REPARSE_TAG_SYMLINK:
                    raise ValueError(
                        "Windows source contains an unsupported reparse point"
                    )
                point = api.query_reparse_point(opened_handle)
                if point.tag != _WINDOWS_IO_REPARSE_TAG_SYMLINK:
                    raise ValueError("Windows source reparse tag changed while opening")
                if (
                    is_lexical_final
                    and expected_final_link_target is not None
                    and _windows_link_target_text(point) != expected_final_link_target
                ):
                    raise ValueError(
                        "Windows source symlink target differs from its initial observation"
                    )
            observation = _WindowsPathObservation(
                parent_handle=current_handle,
                parent_identity=parent_identity,
                name=name,
                file_id_128=entry.file_id_128,
                handle=opened_handle,
                identity=opened_identity,
                reparse_point=point,
            )
            observations.append(observation)

            if point is not None:
                symlink_count += 1
                if symlink_count > _MAX_SYMLINKS:
                    raise ValueError(
                        "Windows source symlink resolution exceeds its limit"
                    )
                if opened.file_id_128 in seen_links:
                    if not (allow_stable_unresolved and lexical_final_is_link):
                        raise ValueError(
                            "Windows source symlink resolution contains a loop"
                        )
                    binding = _WindowsStableUnresolvedRepositoryFile(
                        parent_handle=current_handle,
                        parent_identity=parent_identity,
                        name=name,
                        expected_file_id=entry.file_id_128,
                        **common_kwargs(),
                    )
                    binding.verify()
                    if cleanup_slot is not None:
                        cleanup_slot.own(binding)
                    return binding
                seen_links.add(opened.file_id_128)
                target_parts = _normalize_windows_link_target(
                    root,
                    resolved_prefix,
                    point,
                )
                if len(target_parts) + len(pending) > _MAX_COMPONENTS:
                    raise ValueError(
                        "resolved Windows source path exceeds its component limit"
                    )
                if not target_parts and not pending:
                    if not (allow_stable_unresolved and lexical_final_is_link):
                        raise ValueError(
                            "Windows source path resolves to the repository directory"
                        )
                    directory_handle = api.duplicate_handle(owned_root)
                    cleanup.retain(directory_handle)
                    directory_metadata = api.metadata(directory_handle)
                    cleanup.retain(directory_handle, directory_metadata)
                    binding = _WindowsBoundRepositoryDirectory(
                        directory_handle=directory_handle,
                        opened_identity=_windows_version_identity(directory_metadata),
                        **common_kwargs(),
                    )
                    binding.verify()
                    if cleanup_slot is not None:
                        cleanup_slot.own(binding)
                    return binding
                pending = [*((part, False) for part in target_parts), *pending]
                resolved_prefix = ()
                current_handle = owned_root
                continue

            if pending:
                if not stat.S_ISDIR(opened.st_mode):
                    raise ValueError("Windows source has a non-directory component")
                current_handle = opened_handle
                resolved_prefix = (*resolved_prefix, name)
                continue

            if stat.S_ISDIR(opened.st_mode):
                if not (allow_stable_unresolved and lexical_final_is_link):
                    raise ValueError("Windows source path is not a regular file")
                binding = _WindowsBoundRepositoryDirectory(
                    directory_handle=opened_handle,
                    opened_identity=opened_identity,
                    **common_kwargs(),
                )
                binding.verify()
                if cleanup_slot is not None:
                    cleanup_slot.own(binding)
                return binding
            if not stat.S_ISREG(opened.st_mode):
                if not (allow_stable_unresolved and lexical_final_is_link):
                    raise ValueError("Windows source path is not a regular file")
                binding = _WindowsStableUnresolvedRepositoryFile(
                    parent_handle=current_handle,
                    parent_identity=parent_identity,
                    name=name,
                    expected_file_id=entry.file_id_128,
                    **common_kwargs(),
                )
                binding.verify()
                if cleanup_slot is not None:
                    cleanup_slot.own(binding)
                return binding
            binding = _WindowsBoundRepositoryFile(
                file_handle=opened_handle,
                opened_identity=opened_identity,
                opened_size=opened.st_size,
                **common_kwargs(),
            )
            binding.verify()
            if cleanup_slot is not None:
                cleanup_slot.own(binding)
            return binding
        raise ValueError("Windows source path does not resolve to a file")
    except BaseException as primary:  # noqa: B036 - preserve resolution failure
        _raise_with_cleanup(primary, cleanup_slot or construction)


@contextmanager
def _resolved_windows_repository_file_at(
    root: str | Path,
    root_authority: _WindowsDirectoryAuthority,
    relative: str,
    *,
    expected_root_identity: tuple[object, ...],
    expected_final_identity: tuple[object, ...],
    expected_final_link_target: str | None = None,
    api: _WindowsKernelApi | None = None,
) -> Iterator[_WindowsResolutionBinding]:
    """Resolve one source entry through a caller-owned pinned Windows root."""

    selected = _windows_kernel_api() if api is None else api
    parts = _relative_parts(relative)
    cleanup_slot = _SourceCleanupSlot()
    binding = _open_windows_resolution_at(
        Path(root),
        root_authority,
        parts,
        expected_root_identity=expected_root_identity,
        expected_final_identity=expected_final_identity,
        expected_final_link_target=expected_final_link_target,
        allow_stable_unresolved=True,
        api=selected,
        cleanup_slot=cleanup_slot,
    )
    try:
        yield binding
        binding.verify()
    except BaseException as primary:  # noqa: B036 - preserve body failure
        _raise_with_cleanup(primary, cleanup_slot)
    else:
        try:
            cleanup_slot.close()
        except BaseException as cleanup_failure:  # noqa: B036
            _attach_source_cleanup_owner(cleanup_failure, cleanup_slot)
            raise


def _open_windows_repository_file(
    root: Path,
    parts: tuple[str, ...],
    *,
    expected_final_identity: tuple[object, ...] | None = None,
    expected_final_link_target: str | None = None,
    allow_stable_unresolved: bool = False,
) -> _WindowsResolutionBinding:
    cleanup_slot = _SourceCleanupSlot()
    api, root_authority, root_identity = _open_windows_pinned_repository_root(
        root,
        cleanup_slot=cleanup_slot,
    )
    return _open_windows_resolution_at(
        root,
        root_authority,
        parts,
        expected_root_identity=root_identity,
        expected_final_identity=expected_final_identity,
        expected_final_link_target=expected_final_link_target,
        allow_stable_unresolved=allow_stable_unresolved,
        api=api,
        owns_root_authority=True,
        cleanup_slot=cleanup_slot,
    )


def _static_components(
    root: Path,
    parts: tuple[str, ...],
) -> tuple[Path, tuple[tuple[int, ...], ...]]:
    current = root
    identities: list[tuple[int, ...]] = []
    root_metadata = root.lstat()
    if _is_link_or_reparse(root_metadata) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("repository root must be a real directory")
    identities.append(_version_identity(root_metadata))
    if not _identity_is_reliable(root_metadata):
        raise ValueError("repository root has no reliable filesystem identity")
    for index, part in enumerate(parts):
        current /= part
        metadata = current.lstat()
        if not _identity_is_reliable(metadata):
            raise ValueError("source path has no reliable filesystem identity")
        if _is_link_or_reparse(metadata):
            raise ValueError(
                "safe contained symlink resolution requires directory-fd support"
            )
        expected = stat.S_ISREG if index == len(parts) - 1 else stat.S_ISDIR
        if not expected(metadata.st_mode):
            raise ValueError("source path is not a regular repository file")
        identities.append(_version_identity(metadata))
    return current, tuple(identities)


def _read_static(root: Path, parts: tuple[str, ...], *, max_bytes: int) -> bytes:
    source, identities = _static_components(root, parts)
    descriptor = -1
    try:
        descriptor = os.open(source, _regular_flags())
        opened = os.fstat(descriptor)
        if _version_identity(opened) != identities[-1] or opened.st_size > max_bytes:
            raise ValueError("source file is not a stable bounded regular file")
        payload = bytearray()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
            if not chunk:
                raise ValueError("source file was truncated while being read")
            payload.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("source file grew while being read")
        _source, after_identities = _static_components(root, parts)
        if after_identities != identities or _version_identity(
            os.fstat(descriptor)
        ) != (identities[-1]):
            raise ValueError("source path changed while it was being read")
        return bytes(payload)
    except OSError as exc:
        raise ValueError("source path could not be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def validate_repository_file(root: str | Path, relative: str) -> None:
    """Require one stable regular source file resolving beneath *root*."""

    try:
        if sys.platform == "win32":
            repository = _windows_lexical_repository_path(root)
            binding = _open_windows_repository_file(
                repository,
                _relative_parts(relative),
            )
            try:
                if not binding.is_regular:
                    raise ValueError("source path is not a regular repository file")
                binding.verify()
            finally:
                binding.close()
            return
        repository = Path(root).expanduser().resolve()
        parts = _relative_parts(relative)
        if SECURE_CONTAINED_SYMLINKS:
            binding = _open_secure(repository, parts)
            if not isinstance(binding, _BoundRepositoryFile):  # pragma: no cover
                raise AssertionError("regular source resolver returned no binding")
            try:
                try:
                    binding.verify()
                except OSError as exc:
                    raise ValueError(
                        "source path could not be resolved safely"
                    ) from exc
            finally:
                binding.close()
            return
        _static_components(repository, parts)
    except FileNotFoundError as exc:
        raise ContainedSourceMissingError("source path does not exist") from exc


def read_repository_file(
    root: str | Path,
    relative: str,
    *,
    max_bytes: int,
) -> bytes:
    """Read a bounded source file through the contained-symlink contract."""

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("source byte limit must be positive")
    try:
        if sys.platform == "win32":
            repository = _windows_lexical_repository_path(root)
            binding = _open_windows_repository_file(
                repository,
                _relative_parts(relative),
            )
            try:
                if not isinstance(binding, _WindowsBoundRepositoryFile):
                    raise ValueError("source path is not a regular repository file")
                return binding.read_bytes(max_bytes=max_bytes)
            finally:
                binding.close()
        repository = Path(root).expanduser().resolve()
        parts = _relative_parts(relative)
        if SECURE_CONTAINED_SYMLINKS:
            binding = _open_secure(repository, parts)
            if not isinstance(binding, _BoundRepositoryFile):  # pragma: no cover
                raise AssertionError("regular source resolver returned no binding")
            try:
                return binding.read_bytes(max_bytes=max_bytes)
            except OSError as exc:
                raise ValueError("source path could not be read safely") from exc
            finally:
                binding.close()
        return _read_static(repository, parts, max_bytes=max_bytes)
    except FileNotFoundError as exc:
        raise ContainedSourceMissingError("source path does not exist") from exc


@contextmanager
def resolved_repository_file(
    root: str | Path,
    relative: str,
    *,
    expected_final_identity: tuple[int, ...],
) -> Iterator[
    _BoundRepositoryFile | _BoundRepositoryDirectory | _StableUnresolvedRepositoryFile
]:
    """Yield a pinned regular file or a retained stable link-only outcome."""

    if sys.platform == "win32":
        repository = _windows_lexical_repository_path(root)
        binding = _open_windows_repository_file(
            repository,
            _relative_parts(relative),
            expected_final_identity=expected_final_identity,
            allow_stable_unresolved=True,
        )
        try:
            yield binding
            binding.verify()
        finally:
            binding.close()
        return
    if not SECURE_CONTAINED_SYMLINKS:
        raise ValueError("stable link-only resolution requires directory-fd support")
    repository = Path(root).expanduser().resolve()
    parts = _relative_parts(relative)
    try:
        binding = _open_secure(
            repository,
            parts,
            expected_final_identity=expected_final_identity,
            allow_stable_unresolved=True,
            allow_absolute_symlinks=True,
        )
    except FileNotFoundError as exc:
        raise ContainedSourceMissingError("source path does not exist") from exc
    try:
        yield binding
        binding.verify()
    finally:
        binding.close()


@contextmanager
def _resolved_repository_file_at(
    root: str | Path,
    root_descriptor: int,
    relative: str,
    *,
    expected_root_identity: tuple[int, ...],
    expected_final_identity: tuple[int, ...],
    expected_final_link_target: str | None = None,
) -> Iterator[
    _BoundRepositoryFile | _BoundRepositoryDirectory | _StableUnresolvedRepositoryFile
]:
    """Resolve one entry through a caller-owned, already-pinned root fd."""

    if not SECURE_CONTAINED_SYMLINKS:
        raise ValueError("pinned source resolution requires directory-fd support")
    repository = Path(root)
    if not repository.is_absolute():
        raise ValueError("pinned repository root must be absolute")
    parts = _relative_parts(relative)
    cleanup_slot = _SourceCleanupSlot()
    binding = _open_secure(
        repository,
        parts,
        expected_final_identity=expected_final_identity,
        expected_final_link_target=expected_final_link_target,
        allow_stable_unresolved=True,
        pinned_root_descriptor=root_descriptor,
        expected_root_identity=expected_root_identity,
        cleanup_slot=cleanup_slot,
    )
    try:
        yield binding
        binding.verify()
    except BaseException as primary:  # noqa: B036 - preserve body failure
        _raise_with_cleanup(primary, cleanup_slot)
    else:
        try:
            cleanup_slot.close()
        except BaseException as cleanup_failure:  # noqa: B036
            _attach_source_cleanup_owner(cleanup_failure, cleanup_slot)
            raise


def update_repository_file_hash(
    root: str | Path,
    relative: str,
    hasher: object,
    *,
    expected_final_identity: tuple[int, ...] | None = None,
) -> None:
    """Stream one stable contained source file into a hashlib-compatible hash."""

    update = getattr(hasher, "update", None)
    if not callable(update):
        raise TypeError("hasher must provide an update method")
    if sys.platform == "win32":
        repository = _windows_lexical_repository_path(root)
        binding = _open_windows_repository_file(
            repository,
            _relative_parts(relative),
            expected_final_identity=expected_final_identity,
        )
        try:
            if not isinstance(binding, _WindowsBoundRepositoryFile):
                raise ValueError("source path is not a regular repository file")
            binding.update_hash(hasher)
        finally:
            binding.close()
        return
    repository = Path(root).expanduser().resolve()
    parts = _relative_parts(relative)
    if not SECURE_CONTAINED_SYMLINKS:
        try:
            source, identities = _static_components(repository, parts)
        except FileNotFoundError as exc:
            raise ContainedSourceMissingError("source path does not exist") from exc
        if (
            expected_final_identity is not None
            and identities[-1] != expected_final_identity
        ):
            raise ValueError("source path differs from its initial observation")
        descriptor = -1
        try:
            descriptor = os.open(source, _regular_flags())
            opened = os.fstat(descriptor)
            if _version_identity(opened) != identities[-1]:
                raise ValueError("source file changed while opening")
            remaining = opened.st_size
            while remaining:
                block = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
                if not block:
                    raise ValueError("source file was truncated while hashing")
                update(block)
                remaining -= len(block)
            if os.read(descriptor, 1):
                raise ValueError("source file grew while hashing")
            _source, after_identities = _static_components(repository, parts)
            if after_identities != identities:
                raise ValueError("source path changed while hashing")
            return
        except OSError as exc:
            raise ValueError("source path could not be hashed safely") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    try:
        binding = _open_secure(
            repository,
            parts,
            expected_final_identity=expected_final_identity,
        )
    except FileNotFoundError as exc:
        raise ContainedSourceMissingError("source path does not exist") from exc
    if not isinstance(binding, _BoundRepositoryFile):  # pragma: no cover
        raise AssertionError("regular source resolver returned no binding")
    try:
        binding.update_hash(hasher)
    except OSError as exc:
        raise ValueError("source path could not be hashed safely") from exc
    finally:
        binding.close()


__all__ = [
    "ContainedSourceMissingError",
    "SECURE_CONTAINED_SYMLINKS",
    "read_repository_file",
    "resolved_repository_file",
    "update_repository_file_hash",
    "validate_repository_file",
]
