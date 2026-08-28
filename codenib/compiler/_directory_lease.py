# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Retained POSIX ``flock`` leases bound to private directory inodes.

The route is deliberately authority-free: callers must retain and supply the
exact four-field directory identity themselves.  Acquisition only proves that
the visible path and opened directory still name that identity; it never
silently substitutes a newly observed inode.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

try:  # pragma: no cover - selected by the platform support gate
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from .. import _workspace_owner as _native_workspace_owner
from .._atomic_directory import (
    _OrderedAction,
    _publication_cleanup_owner_carrier,
    _publication_exception_prior_link,
    _run_context_with_cleanup_actions,
)
from . import cache_lock as _cache_lock

_DIRECTORY_LEASE_RETRY_SECONDS = 0.05
_FORK_CLEANUP_FAILURE_EXIT_CODE = 70
_HOST_PATH_TYPE = type(Path())
_OWNER_CONSTRUCTION_TOKEN = object()
_DESCRIPTOR_CONSTRUCTION_TOKEN = object()
_DIRECTORY_LEASE_THREAD_CLAIMS = threading.local()
_ACTIVE_DIRECTORY_LEASE_OWNERS: set[PrivateDirectoryLeaseOwner] = set()
_ACTIVE_PRIVATE_DIRECTORY_DESCRIPTOR_OWNERS: set[_PrivateDirectoryDescriptorOwner] = (
    set()
)

DirectoryInodeIdentity = tuple[int, int, int, int]
_ThreadDirectoryLeaseClaim = tuple[
    int,
    DirectoryInodeIdentity,
    object,
    dict[DirectoryInodeIdentity, object],
]


class DirectoryLeaseMode(Enum):
    """The exact advisory-lock capability requested for one directory."""

    SHARED = "shared"
    EXCLUSIVE = "exclusive"


def _require_exact_identity(value: object) -> DirectoryInodeIdentity:
    if (
        type(value) is not tuple
        or len(value) != 4
        or any(type(member) is not int for member in value)
    ):
        raise TypeError("private directory lease identity must be an exact 4-tuple")
    identity = value
    if identity[0] <= 0 or identity[1] <= 0:
        raise ValueError("private directory lease identity must use stable inode IDs")
    if identity[2] != stat.S_IFDIR:
        raise ValueError("private directory lease identity must name a directory")
    if identity[3] < 0:
        raise ValueError("private directory lease identity attributes are invalid")
    return identity


@dataclass(frozen=True, slots=True)
class PrivateDirectoryLeaseRoute:
    """One exact, process-bound path-to-directory-inode route."""

    path: Path
    identity: DirectoryInodeIdentity
    owner_pid: int

    def __post_init__(self) -> None:
        if type(self) is not PrivateDirectoryLeaseRoute:
            raise TypeError("private directory lease route must use the exact type")
        if type(self.path) is not _HOST_PATH_TYPE:
            raise TypeError("private directory lease path must be an exact Path")
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise ValueError(
                "private directory lease path must be absolute and lexical"
            )
        _require_exact_identity(self.identity)
        if type(self.owner_pid) is not int:
            raise TypeError("private directory lease owner PID must be an exact int")
        if self.owner_pid != os.getpid():
            raise ValueError("private directory lease route crossed a PID boundary")


def require_private_directory_lease_support() -> None:
    """Fail before shard creation when the required POSIX surface is absent."""

    supported = (
        sys.platform.startswith("linux")
        and os.name == "posix"
        and fcntl is not None
        and hasattr(fcntl, "flock")
        and hasattr(fcntl, "LOCK_SH")
        and hasattr(fcntl, "LOCK_EX")
        and hasattr(fcntl, "LOCK_NB")
        and hasattr(fcntl, "LOCK_UN")
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_CLOEXEC")
        and hasattr(os, "register_at_fork")
        and hasattr(os, "geteuid")
    )
    if not supported:
        raise RuntimeError(
            "private directory leases require Linux directory-inode flock support"
        )
    _native_workspace_owner._require_directory_fd_owner_support()


def _directory_inode_identity(metadata: os.stat_result) -> DirectoryInodeIdentity:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0),
    )


def _validate_private_directory_metadata(
    metadata: os.stat_result,
    route: PrivateDirectoryLeaseRoute,
    *,
    opened: bool,
) -> None:
    path = route.path
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (not opened and stat.S_ISLNK(metadata.st_mode))
        or getattr(metadata, "st_file_attributes", 0)
    ):
        raise RuntimeError(f"private directory lease path is not real: {path}")
    if metadata.st_uid != os.geteuid():
        raise RuntimeError(f"private directory lease path has another owner: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RuntimeError(f"private directory lease path is not mode 0700: {path}")
    if _directory_inode_identity(metadata) != route.identity:
        raise RuntimeError(f"private directory lease path changed: {path}")


def _validate_visible_directory(route: PrivateDirectoryLeaseRoute) -> None:
    try:
        metadata = route.path.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"could not inspect private directory lease path: {route.path}"
        ) from exc
    _validate_private_directory_metadata(metadata, route, opened=False)


def _validate_opened_directory(
    descriptor: int,
    route: PrivateDirectoryLeaseRoute,
) -> None:
    try:
        metadata = os.fstat(descriptor)
    except OSError as exc:
        raise RuntimeError(
            f"could not inspect opened private directory lease: {route.path}"
        ) from exc
    _validate_private_directory_metadata(metadata, route, opened=True)


def _validate_identity_sandwich(
    descriptor: int,
    route: PrivateDirectoryLeaseRoute,
) -> None:
    _validate_opened_directory(descriptor, route)
    _validate_visible_directory(route)
    _validate_opened_directory(descriptor, route)


def _require_route_process(route: PrivateDirectoryLeaseRoute) -> None:
    if route.owner_pid != os.getpid():
        raise RuntimeError("private directory lease route crossed a PID boundary")


def _open_registered_directory(owner: PrivateDirectoryLeaseOwner) -> int:
    route = owner._route
    path_bytes = os.fsencode(route.path)
    with _cache_lock._PosixLifecycleGuardLease() as acquire_interruption:
        if acquire_interruption is not None:
            raise acquire_interruption
        _require_route_process(route)
        if owner._generation != _cache_lock._POSIX_FORK_GENERATION:
            raise RuntimeError("private directory lease fork generation changed")
        if not owner._acquisition_pending:
            raise RuntimeError("private directory lease was closed before acquisition")
        _validate_visible_directory(route)
        _require_route_process(route)
        # Publish the already-installed Python owner to the fork callback and
        # mark the native acquisition attempt before entering C.  The native
        # owner stores a successful open before returning, closing the raw
        # integer-fd return-to-first-STORE seam that Python cannot own.
        _ACTIVE_DIRECTORY_LEASE_OWNERS.add(owner)
        owner._native_open_started = True
        _native_workspace_owner._open_directory_fd(
            owner._native_descriptor_owner,
            path_bytes,
        )
        descriptor = _native_workspace_owner._borrow_directory_fd(
            owner._native_descriptor_owner
        )
        owner._descriptor_owner.append(descriptor)
        _validate_identity_sandwich(descriptor, route)
        _require_route_process(route)
        if owner._generation != _cache_lock._POSIX_FORK_GENERATION:
            raise RuntimeError("private directory lease fork generation changed")
        return descriptor
    raise AssertionError("unreachable private directory lease lifecycle guard")


def _claim_thread_directory_lease(owner: PrivateDirectoryLeaseOwner) -> None:
    identity = owner.identity
    generation = _cache_lock._POSIX_FORK_GENERATION
    state = getattr(_DIRECTORY_LEASE_THREAD_CLAIMS, "state", None)
    if state is None or state[0] != generation:
        tokens: dict[DirectoryInodeIdentity, object] = {}
        _DIRECTORY_LEASE_THREAD_CLAIMS.state = (generation, tokens)
    else:
        tokens = state[1]
        if type(tokens) is not dict:
            raise RuntimeError("private directory lease thread claims are invalid")
    if identity in tokens:
        raise RuntimeError("private directory lease is already held by this thread")
    token = object()
    claim = (generation, identity, token, tokens)
    # Publish cleanup authority before the token mutation.  If cancellation
    # lands after dict insertion but before its return, the acquisition's
    # prebuilt cleanup owner can still reconcile the exact token.
    owner._claim_owner.append(claim)
    tokens[identity] = token


def _release_claim_for_cleanup(owner: PrivateDirectoryLeaseOwner) -> None:
    if not _descriptor_cleanup_complete(owner):
        raise RuntimeError(
            "private directory lease claim cannot outlive an open descriptor"
        )
    deferred: BaseException | None = None
    while owner._claim_owner:
        claim = owner._claim_owner[-1]
        generation, identity, token, tokens = claim
        try:
            if generation == owner._generation and tokens.get(identity) is token:
                tokens.pop(identity, None)
        except BaseException as error:  # noqa: B036 - reconcile cancellation
            if isinstance(error, Exception):
                raise
            deferred = _cache_lock._remember_cleanup_interruption(deferred, error)
            continue
        try:
            owner._claim_owner.pop()
        except BaseException as error:  # noqa: B036 - recheck ambiguous pop
            if isinstance(error, Exception):
                raise
            deferred = _cache_lock._remember_cleanup_interruption(deferred, error)
            continue
    if deferred is not None:
        raise deferred


def _close_descriptor_for_cleanup(owner: PrivateDirectoryLeaseOwner) -> None:
    if _descriptor_cleanup_complete(owner):
        return
    if owner._generation != _cache_lock._POSIX_FORK_GENERATION:
        raise RuntimeError("private directory lease fork generation changed")
    deferred: BaseException | None = None
    with _cache_lock._PosixLifecycleGuardLease() as acquire_interruption:
        deferred = acquire_interruption
        native_closed = _native_workspace_owner._directory_fd_owner_closed(
            owner._native_descriptor_owner
        )
        if not native_closed:
            try:
                # Closing both authenticated descriptors releases the flock.
                # Do not unlock first: a nonterminal native-close failure must
                # retain the cooperative exclusion capability for cleanup retry.
                _native_workspace_owner._close_directory_fd_owner(
                    owner._native_descriptor_owner
                )
            except BaseException as error:  # noqa: B036 - native close is terminal
                deferred = _cache_lock._remember_cleanup_interruption(
                    deferred,
                    error,
                )
        native_closed = _native_workspace_owner._directory_fd_owner_closed(
            owner._native_descriptor_owner
        )
        if native_closed:
            owner._locked = False
            owner._acquisition_pending = False
            deferred = _cache_lock._retry_cleanup_interruption(
                owner._descriptor_owner.clear,
                deferred,
            )
            deferred = _cache_lock._retry_cleanup_interruption(
                _ACTIVE_DIRECTORY_LEASE_OWNERS.discard,
                deferred,
                owner,
            )
    if deferred is not None:
        raise deferred


def _descriptor_cleanup_complete(owner: PrivateDirectoryLeaseOwner) -> bool:
    if owner._acquisition_pending:
        return False
    native_closed = _native_workspace_owner._directory_fd_owner_closed(
        owner._native_descriptor_owner
    )
    return bool(
        native_closed
        and not owner._descriptor_owner
        and owner not in _ACTIVE_DIRECTORY_LEASE_OWNERS
    )


def _private_descriptor_cleanup_complete(
    owner: _PrivateDirectoryDescriptorOwner,
) -> bool:
    native_closed = (
        not owner._native_open_started
        or _native_workspace_owner._directory_fd_owner_closed(
            owner._native_descriptor_owner
        )
    )
    return bool(
        native_closed
        and not owner._descriptor_owner
        and owner not in _ACTIVE_PRIVATE_DIRECTORY_DESCRIPTOR_OWNERS
    )


def _close_private_descriptor_for_cleanup(
    owner: _PrivateDirectoryDescriptorOwner,
) -> None:
    if _private_descriptor_cleanup_complete(owner):
        return
    if owner._generation != _cache_lock._POSIX_FORK_GENERATION:
        raise RuntimeError("private directory descriptor fork generation changed")
    deferred: BaseException | None = None
    with _cache_lock._PosixLifecycleGuardLease() as acquire_interruption:
        deferred = acquire_interruption
        native_closed = (
            not owner._native_open_started
            or _native_workspace_owner._directory_fd_owner_closed(
                owner._native_descriptor_owner
            )
        )
        if not native_closed:
            try:
                _native_workspace_owner._close_directory_fd_owner(
                    owner._native_descriptor_owner
                )
            except BaseException as error:  # noqa: B036 - native close is terminal
                deferred = _cache_lock._remember_cleanup_interruption(
                    deferred,
                    error,
                )
        native_closed = (
            not owner._native_open_started
            or _native_workspace_owner._directory_fd_owner_closed(
                owner._native_descriptor_owner
            )
        )
        if native_closed:
            deferred = _cache_lock._retry_cleanup_interruption(
                owner._descriptor_owner.clear,
                deferred,
            )
            deferred = _cache_lock._retry_cleanup_interruption(
                _ACTIVE_PRIVATE_DIRECTORY_DESCRIPTOR_OWNERS.discard,
                deferred,
                owner,
            )
    if deferred is not None:
        raise deferred


class _PrivateDirectoryDescriptorOwner:
    """Retain one exact native directory fd without acquiring a lock."""

    __slots__ = (
        "_descriptor_owner",
        "_generation",
        "_native_descriptor_owner",
        "_native_open_started",
        "_route",
    )

    def __init__(
        self,
        route: PrivateDirectoryLeaseRoute,
        *,
        _token: object,
    ) -> None:
        if type(self) is not _PrivateDirectoryDescriptorOwner or (
            _token is not _DESCRIPTOR_CONSTRUCTION_TOKEN
        ):
            raise TypeError("private directory descriptor owners cannot be forged")
        if type(route) is not PrivateDirectoryLeaseRoute:
            raise TypeError("private directory descriptor route is invalid")
        route.__post_init__()
        self._route = route
        self._generation = _cache_lock._POSIX_FORK_GENERATION
        self._native_descriptor_owner = (
            _native_workspace_owner._create_directory_fd_owner()
        )
        self._native_open_started = False
        self._descriptor_owner: list[int] = []

    def _require_process(self) -> None:
        if self._route.owner_pid != os.getpid():
            raise RuntimeError(
                "private directory descriptor owner crossed a PID boundary"
            )

    def _open(self) -> int:
        self._require_process()
        if self._native_open_started:
            raise RuntimeError("private directory descriptor owner is already open")
        route = self._route
        path_bytes = os.fsencode(route.path)
        with _cache_lock._PosixLifecycleGuardLease() as acquire_interruption:
            if acquire_interruption is not None:
                raise acquire_interruption
            _require_route_process(route)
            if self._generation != _cache_lock._POSIX_FORK_GENERATION:
                raise RuntimeError(
                    "private directory descriptor fork generation changed"
                )
            _validate_visible_directory(route)
            _ACTIVE_PRIVATE_DIRECTORY_DESCRIPTOR_OWNERS.add(self)
            self._native_open_started = True
            _native_workspace_owner._open_directory_fd(
                self._native_descriptor_owner,
                path_bytes,
            )
            descriptor = _native_workspace_owner._borrow_directory_fd(
                self._native_descriptor_owner
            )
            self._descriptor_owner.append(descriptor)
            _validate_identity_sandwich(descriptor, route)
            _require_route_process(route)
            if self._generation != _cache_lock._POSIX_FORK_GENERATION:
                raise RuntimeError(
                    "private directory descriptor fork generation changed"
                )
            return descriptor
        raise AssertionError("unreachable private directory descriptor lifecycle guard")

    def _mkdir_child(
        self,
        name: bytes,
        pre_mutation_check: Callable[[], None],
    ) -> bool:
        """Create one child after a native-bound final policy callback."""

        self._require_process()
        if not self._native_open_started or self.closed:
            raise RuntimeError("private directory descriptor owner is not open")
        return _native_workspace_owner._mkdir_directory_fd_child(
            self._native_descriptor_owner,
            name,
            pre_mutation_check,
        )

    @property
    def closed(self) -> bool:
        return _private_descriptor_cleanup_complete(self)

    def close(self) -> None:
        self._require_process()
        if self.closed:
            return
        cleanup = _OrderedAction(
            label="private directory descriptor cleanup also failed",
            action=lambda: _close_private_descriptor_for_cleanup(self),
            complete=lambda: _private_descriptor_cleanup_complete(self),
            retry_incomplete="cancellation",
            incomplete_owner=self,
        )
        with _run_context_with_cleanup_actions((cleanup,)):
            pass
        if not self.closed:
            raise RuntimeError("private directory descriptor cleanup did not complete")


def _create_private_directory_descriptor_owner(
    route: PrivateDirectoryLeaseRoute,
) -> _PrivateDirectoryDescriptorOwner:
    """Create an unopened retained owner for one exact directory route."""

    require_private_directory_lease_support()
    return _PrivateDirectoryDescriptorOwner(
        route,
        _token=_DESCRIPTOR_CONSTRUCTION_TOKEN,
    )


class PrivateDirectoryLeaseOwner:
    """The exact retained owner of one directory-inode advisory lock."""

    __slots__ = (
        "_acquisition_pending",
        "_claim_owner",
        "_descriptor_owner",
        "_generation",
        "_locked",
        "_mode",
        "_native_descriptor_owner",
        "_native_open_started",
        "_route",
    )

    def __init__(
        self,
        route: PrivateDirectoryLeaseRoute,
        mode: DirectoryLeaseMode,
        *,
        _token: object,
    ) -> None:
        if type(self) is not PrivateDirectoryLeaseOwner or (
            _token is not _OWNER_CONSTRUCTION_TOKEN
        ):
            raise TypeError("private directory lease owners cannot be forged")
        self._route = route
        self._mode = mode
        self._generation = _cache_lock._POSIX_FORK_GENERATION
        self._native_descriptor_owner = (
            _native_workspace_owner._create_directory_fd_owner()
        )
        self._native_open_started = False
        self._acquisition_pending = True
        self._descriptor_owner: list[int] = []
        self._claim_owner: list[_ThreadDirectoryLeaseClaim] = []
        self._locked = False

    def _require_process(self) -> None:
        if self._route.owner_pid != os.getpid():
            raise RuntimeError("private directory lease owner crossed a PID boundary")

    @property
    def path(self) -> Path:
        return self._route.path

    @property
    def identity(self) -> DirectoryInodeIdentity:
        return self._route.identity

    @property
    def mode(self) -> DirectoryLeaseMode:
        return self._mode

    @property
    def closed(self) -> bool:
        return _descriptor_cleanup_complete(self) and not self._claim_owner

    def __enter__(self) -> PrivateDirectoryLeaseOwner:
        self._require_process()
        if self.closed:
            raise RuntimeError("private directory lease owner is closed")
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: object,
    ) -> None:
        if exc is None:
            self.close()
            return
        prior_link, _ = _publication_exception_prior_link(exc)
        try:
            self.close()
        except BaseException as cleanup_error:  # noqa: B036 - keep body primary
            carrier = _publication_cleanup_owner_carrier(
                (self,),
                cleanup_error,
                prior_link,
                forbidden=exc,
                label="private directory lease context cleanup recovery",
            )
            raise exc from carrier

    def close(self) -> None:
        self._require_process()
        if self.closed:
            return
        actions = (
            _OrderedAction(
                label="private directory lease descriptor cleanup also failed",
                action=lambda: _close_descriptor_for_cleanup(self),
                complete=lambda: _descriptor_cleanup_complete(self),
                retry_incomplete="cancellation",
                incomplete_owner=self,
            ),
            _OrderedAction(
                label="private directory lease thread claim cleanup also failed",
                action=lambda: _release_claim_for_cleanup(self),
                complete=lambda: not self._claim_owner,
                retry_incomplete="cancellation",
                incomplete_owner=self,
            ),
        )
        with _run_context_with_cleanup_actions(actions):
            pass
        if not self.closed:
            raise RuntimeError("private directory lease cleanup did not complete")


def _acquire_flock(
    owner: PrivateDirectoryLeaseOwner,
    *,
    blocking: bool,
    check_cancelled: Callable[[], None] | None,
) -> None:
    descriptor = _native_workspace_owner._borrow_directory_fd(
        owner._native_descriptor_owner
    )
    assert fcntl is not None  # narrowed by support preflight
    lock_mode = (
        fcntl.LOCK_SH if owner._mode is DirectoryLeaseMode.SHARED else fcntl.LOCK_EX
    )
    if blocking and check_cancelled is None:
        fcntl.flock(descriptor, lock_mode)
    else:
        while True:
            if check_cancelled is not None:
                check_cancelled()
            try:
                fcntl.flock(descriptor, lock_mode | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if not blocking:
                    raise BlockingIOError(
                        errno.EWOULDBLOCK,
                        f"private directory lease is unavailable: {owner.path}",
                    ) from None
                time.sleep(_DIRECTORY_LEASE_RETRY_SECONDS)
                continue
            break
    owner._locked = True
    if check_cancelled is not None:
        check_cancelled()


def _acquire_into_owner(
    owner: PrivateDirectoryLeaseOwner,
    *,
    blocking: bool,
    check_cancelled: Callable[[], None] | None,
) -> None:
    descriptor = _open_registered_directory(owner)
    _claim_thread_directory_lease(owner)
    _validate_identity_sandwich(descriptor, owner._route)
    _acquire_flock(
        owner,
        blocking=blocking,
        check_cancelled=check_cancelled,
    )
    _validate_identity_sandwich(descriptor, owner._route)
    _require_route_process(owner._route)
    if owner._generation != _cache_lock._POSIX_FORK_GENERATION:
        raise RuntimeError("private directory lease fork generation changed")


def _complete_owner_acquisition(owner: PrivateDirectoryLeaseOwner) -> None:
    with _cache_lock._PosixLifecycleGuardLease() as acquire_interruption:
        if acquire_interruption is not None:
            raise acquire_interruption
        if not owner._acquisition_pending:
            raise RuntimeError("private directory lease was closed during acquisition")
        if (
            not owner._native_open_started
            or not owner._descriptor_owner
            or not owner._claim_owner
            or not owner._locked
            or _native_workspace_owner._directory_fd_owner_closed(
                owner._native_descriptor_owner
            )
        ):
            raise RuntimeError("private directory lease acquisition is incomplete")
        owner._acquisition_pending = False


def acquire_private_directory_lease(
    route: PrivateDirectoryLeaseRoute,
    *,
    mode: DirectoryLeaseMode,
    blocking: bool,
    check_cancelled: Callable[[], None] | None = None,
    _construction_owner: Callable[[PrivateDirectoryLeaseOwner], None] | None = None,
) -> PrivateDirectoryLeaseOwner:
    """Acquire a retained shared or exclusive lease for one exact route."""

    require_private_directory_lease_support()
    if type(route) is not PrivateDirectoryLeaseRoute:
        raise TypeError("private directory lease requires an exact route")
    # Revalidate the frozen record because hostile code can use
    # ``object.__setattr__`` to bypass dataclass construction.
    route.__post_init__()
    if type(mode) is not DirectoryLeaseMode:
        raise TypeError("private directory lease mode must use the exact enum")
    if type(blocking) is not bool:
        raise TypeError("private directory lease blocking policy must be exact bool")
    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("private directory lease cancellation check must be callable")
    if not callable(_construction_owner):
        raise TypeError("private directory lease requires a construction owner")

    owner = PrivateDirectoryLeaseOwner(
        route,
        mode,
        _token=_OWNER_CONSTRUCTION_TOKEN,
    )
    cleanup = _OrderedAction(
        label="private directory lease acquisition cleanup also failed",
        action=owner.close,
        complete=lambda: owner.closed,
        retry_incomplete="cancellation",
        incomplete_owner=owner,
    )
    with _run_context_with_cleanup_actions((cleanup,), cleanup_on_success=False):
        _construction_owner(owner)
        _acquire_into_owner(
            owner,
            blocking=blocking,
            check_cancelled=check_cancelled,
        )
        _complete_owner_acquisition(owner)
    if type(owner) is not PrivateDirectoryLeaseOwner or owner.closed:
        raise RuntimeError("private directory lease acquisition did not retain owner")
    return owner
