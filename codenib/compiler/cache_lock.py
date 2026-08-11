# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Cooperative serialization for a private compiler cache."""

from __future__ import annotations

import errno
import os
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:  # pragma: no cover - selected by the platform gate
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - selected by the platform gate
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

COMPILER_CACHE_LOCK_FILENAME = ".index-compiler.lock"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_LOCK_OPEN_RETRIES = 8
_WINDOWS_LOCK_RETRY_SECONDS = 0.05
_ACTIVE_POSIX_LOCK_DESCRIPTORS: set[int] = set()
_POSIX_FORK_GENERATION = 0
_POSIX_LOCK_LIFECYCLE_DEPTH = threading.local()


def _posix_lifecycle_guard_depth() -> int:
    return int(getattr(_POSIX_LOCK_LIFECYCLE_DEPTH, "value", 0))


def _set_posix_lifecycle_guard_depth(depth: int) -> None:
    _POSIX_LOCK_LIFECYCLE_DEPTH.value = depth


class _PosixLifecycleRLock:
    """RLock with a CPython-owner depth token visible through cancellation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def _native_is_owned(self) -> bool:
        runtime: Any = self._lock
        return bool(runtime._is_owned())

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        depth = _posix_lifecycle_guard_depth()
        if depth:
            try:
                _set_posix_lifecycle_guard_depth(depth + 1)
                return True
            except BaseException:
                # The caller compares the visible depth with its baseline.
                raise
        try:
            if timeout == -1:
                acquired = self._lock.acquire(blocking)
            else:
                acquired = self._lock.acquire(blocking, timeout)
            if acquired:
                _set_posix_lifecycle_guard_depth(1)
            return acquired
        except BaseException:
            # A signal/trace exception can arrive after the C RLock acquired
            # but before Python observes its return.  Publish that ownership.
            if self._native_is_owned():
                _set_posix_lifecycle_guard_depth(1)
            raise

    def release(self) -> None:
        depth = _posix_lifecycle_guard_depth()
        if depth < 1 or not self._native_is_owned():
            raise RuntimeError("cannot release un-acquired lifecycle guard")
        if depth > 1:
            try:
                _set_posix_lifecycle_guard_depth(depth - 1)
                return
            except BaseException:
                raise
        try:
            self._lock.release()
            _set_posix_lifecycle_guard_depth(0)
        except BaseException:
            if not self._native_is_owned():
                _set_posix_lifecycle_guard_depth(0)
            raise

    def _is_owned(self) -> bool:
        return bool(_posix_lifecycle_guard_depth() and self._native_is_owned())


_POSIX_LOCK_LIFECYCLE_GUARD = _PosixLifecycleRLock()


def _remember_cleanup_interruption(
    deferred: BaseException | None,
    interruption: BaseException,
) -> BaseException:
    return deferred if deferred is not None else interruption


def _annotate_secondary_cleanup_error(
    primary_error: BaseException,
    label: str,
    cleanup_error: BaseException,
) -> None:
    """Keep cleanup diagnostics reachable without replacing the first fault."""

    message = f"{label}: {cleanup_error!r}"
    try:
        add_note = getattr(primary_error, "add_note", None)
        if callable(add_note):
            add_note(message)
            return
    except BaseException:  # noqa: B036 - annotation must not mask primary
        pass
    try:
        notes = list(getattr(primary_error, "_codenib_cleanup_notes", ()))
        notes.append(message)
        primary_error._codenib_cleanup_notes = tuple(notes)  # type: ignore[attr-defined]
    except BaseException:  # noqa: B036 - primary still remains authoritative
        pass
    try:
        if primary_error.__cause__ is None:
            primary_error.__cause__ = cleanup_error
    except BaseException:  # noqa: B036 - primary still remains authoritative
        pass


def _finish_cleanup_preserving_primary(
    primary_error: BaseException | None,
    label: str,
    cleanup: Callable[[], BaseException | None],
) -> None:
    """Finish cleanup, propagating it only when no earlier failure exists."""

    try:
        deferred = cleanup()
        if deferred is not None:
            raise deferred
    except BaseException as cleanup_error:  # noqa: B036 - preserve first fault
        if primary_error is None:
            raise
        _annotate_secondary_cleanup_error(
            primary_error,
            label,
            cleanup_error,
        )


def _posix_lifecycle_guard_is_owned() -> bool:
    """Return whether the current CPython thread owns the lifecycle RLock."""

    ownership_check = getattr(_POSIX_LOCK_LIFECYCLE_GUARD, "_is_owned", None)
    if ownership_check is None:
        raise RuntimeError("compiler cache lifecycle guard lacks owner tracking")
    return bool(ownership_check())


def _retry_cleanup_interruption(
    operation: Callable[..., Any],
    deferred: BaseException | None,
    *args: Any,
) -> BaseException | None:
    """Finish one non-failing cleanup operation before propagating cancellation."""

    while True:
        try:
            operation(*args)
        except BaseException as exc:  # noqa: B036 - defer cancellation
            # Built-in close/set/list operations may be interrupted by
            # cancellation, but ordinary operational errors must retain their
            # normal contract instead of turning into an unbounded retry.
            if isinstance(exc, Exception):
                raise
            deferred = _remember_cleanup_interruption(deferred, exc)
            continue
        return deferred


def _close_descriptor_for_cleanup(
    descriptor: int,
    deferred: BaseException | None,
) -> BaseException | None:
    """Close an fd despite cancellation, confirming ambiguous close results."""

    while True:
        try:
            expected_identity = _inode_identity(os.fstat(descriptor))
        except OSError as exc:
            if exc.errno == errno.EBADF:
                return deferred
            raise
        except BaseException as exc:  # noqa: B036 - defer cancellation
            if isinstance(exc, Exception):
                raise
            deferred = _remember_cleanup_interruption(deferred, exc)
            continue

        try:
            os.close(descriptor)
        except BaseException as exc:  # noqa: B036 - confirm before retry
            deferred = _remember_cleanup_interruption(deferred, exc)
            while True:
                try:
                    visible = os.fstat(descriptor)
                except OSError as probe_error:
                    if probe_error.errno == errno.EBADF:
                        return deferred
                    raise
                except BaseException as probe_error:  # noqa: B036
                    if isinstance(probe_error, Exception):
                        raise
                    deferred = _remember_cleanup_interruption(
                        deferred,
                        probe_error,
                    )
                    continue
                if _inode_identity(visible) != expected_identity:
                    # The owned fd closed and its number was reused.  Never
                    # close the replacement descriptor.
                    return deferred
                break
            continue
        return deferred


def _close_owned_descriptor_for_cleanup(
    descriptor: int,
    descriptor_owner: list[int],
    deferred: BaseException | None,
    *,
    active_registry: set[int] | None = None,
) -> BaseException | None:
    """Close and unregister a descriptor before relinquishing its ownership."""

    deferred = _close_descriptor_for_cleanup(descriptor, deferred)
    if active_registry is not None:
        deferred = _retry_cleanup_interruption(
            active_registry.discard,
            deferred,
            descriptor,
        )
    deferred = _remove_owned_descriptor_for_cleanup(
        descriptor_owner,
        descriptor,
        deferred,
    )
    return deferred


def _remove_owned_descriptor_for_cleanup(
    descriptor_owner: list[int],
    descriptor: int,
    deferred: BaseException | None,
) -> BaseException | None:
    """Commit owner removal once, rechecking after an interrupted pop."""

    while True:
        if not descriptor_owner:
            return deferred
        if descriptor_owner[-1] != descriptor:
            raise RuntimeError("compiler cache descriptor ownership changed")
        try:
            descriptor_owner.pop()
            return deferred
        except BaseException as exc:  # noqa: B036 - recheck idempotently
            if isinstance(exc, Exception):
                raise
            deferred = _remember_cleanup_interruption(deferred, exc)


def _acquire_posix_lifecycle_guard(
    deferred: BaseException | None = None,
) -> BaseException | None:
    """Acquire exactly one guard level while deferring cancellation."""

    while True:
        try:
            baseline_depth = _posix_lifecycle_guard_depth()
            break
        except BaseException as exc:  # noqa: B036 - at-fork cannot escape
            deferred = _remember_cleanup_interruption(deferred, exc)
    while True:
        try:
            if _POSIX_LOCK_LIFECYCLE_GUARD.acquire():
                return deferred
        except BaseException as exc:  # noqa: B036 - defer until descriptor close
            deferred = _remember_cleanup_interruption(deferred, exc)
            try:
                if (
                    _posix_lifecycle_guard_depth() > baseline_depth
                    and _posix_lifecycle_guard_is_owned()
                ):
                    return deferred
            except BaseException as ownership_error:  # noqa: B036
                deferred = _remember_cleanup_interruption(
                    deferred,
                    ownership_error,
                )


def _relinquish_posix_lifecycle_guard(
    deferred: BaseException | None = None,
) -> BaseException | None:
    """Release exactly one guard level, idempotently across cancellation."""

    while True:
        try:
            baseline_depth = _posix_lifecycle_guard_depth()
            if baseline_depth < 1 or not _posix_lifecycle_guard_is_owned():
                return deferred
            break
        except BaseException as exc:  # noqa: B036 - at-fork cannot escape
            deferred = _remember_cleanup_interruption(deferred, exc)
    while True:
        try:
            _POSIX_LOCK_LIFECYCLE_GUARD.release()
            return deferred
        except BaseException as exc:  # noqa: B036 - confirm before retry
            deferred = _remember_cleanup_interruption(deferred, exc)
            if _posix_lifecycle_guard_depth() < baseline_depth:
                return deferred


class _PosixLifecycleGuardLease:
    """Pair one cancellation-safe lifecycle acquisition with one release."""

    def __init__(self) -> None:
        self._baseline_depth = _posix_lifecycle_guard_depth()

    def __enter__(self) -> BaseException | None:
        deferred: BaseException | None = None
        while True:
            try:
                deferred = _acquire_posix_lifecycle_guard(deferred)
                return deferred
            except BaseException as exc:  # noqa: B036 - enter must finish
                deferred = _remember_cleanup_interruption(deferred, exc)
                continue

    def __exit__(
        self,
        _exc_type: object,
        _exc_value: object,
        _traceback: object,
    ) -> None:
        deferred: BaseException | None = None
        while True:
            try:
                if _posix_lifecycle_guard_depth() <= self._baseline_depth:
                    break
                deferred = _relinquish_posix_lifecycle_guard(deferred)
            except BaseException as exc:  # noqa: B036 - exit must finish
                deferred = _remember_cleanup_interruption(deferred, exc)
        if deferred is not None:
            raise deferred


def _close_inherited_posix_locks() -> None:
    """Close parent lock descriptions while the at-fork guard is held."""

    global _POSIX_FORK_GENERATION
    try:
        for descriptor in tuple(_ACTIVE_POSIX_LOCK_DESCRIPTORS):
            while True:
                try:
                    os.close(descriptor)
                except OSError:
                    break
                except BaseException:  # noqa: B036 - at-fork must not escape
                    # Exceptions raised from at-fork callbacks are ignored by
                    # CPython.  Retry cancellation here so an inherited open
                    # file description cannot silently pin the parent's lock.
                    continue
                break
        _ACTIVE_POSIX_LOCK_DESCRIPTORS.clear()
        _POSIX_FORK_GENERATION += 1
    finally:
        # ``before`` acquired this exact lock in the surviving fork thread.
        # Acquiring it for the first time in the child could deadlock forever
        # when the owning parent thread no longer exists.
        _finish_posix_fork()


def _prepare_posix_fork() -> None:
    # CPython reports and ignores at-fork callback exceptions, then continues
    # with fork.  This helper never propagates cancellation before one complete
    # recursion level is visible in the owner-depth token.
    deferred: BaseException | None = None
    while True:
        try:
            baseline_depth = _posix_lifecycle_guard_depth()
            break
        except BaseException as exc:  # noqa: B036 - at-fork cannot escape
            deferred = _remember_cleanup_interruption(deferred, exc)
    while True:
        try:
            if (
                _posix_lifecycle_guard_depth() > baseline_depth
                and _posix_lifecycle_guard_is_owned()
            ):
                return
            deferred = _acquire_posix_lifecycle_guard(deferred)
        except BaseException as exc:  # noqa: B036 - at-fork cannot escape
            deferred = _remember_cleanup_interruption(deferred, exc)


def _finish_posix_fork() -> None:
    # The release helper confirms the depth transition before returning; any
    # deferred callback cancellation is intentionally swallowed here.
    while True:
        try:
            target_depth = max(_posix_lifecycle_guard_depth() - 1, 0)
            break
        except BaseException:  # noqa: B036 - at-fork cannot escape
            continue
    while True:
        try:
            if _posix_lifecycle_guard_depth() <= target_depth:
                return
            _relinquish_posix_lifecycle_guard()
        except BaseException:  # noqa: B036 - at-fork cannot escape
            continue


if hasattr(os, "register_at_fork"):  # pragma: no branch - true on supported POSIX
    os.register_at_fork(
        before=_prepare_posix_fork,
        after_in_parent=_finish_posix_fork,
        after_in_child=_close_inherited_posix_locks,
    )


def _cache_path(cache_dir: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(cache_dir))))


def _is_reparse_point(metadata: os.stat_result) -> bool:
    return bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _inode_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0),
        getattr(metadata, "st_reparse_tag", 0),
    )


def _validate_lock_metadata(metadata: os.stat_result, lock_path: Path) -> None:
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
        raise RuntimeError(f"compiler cache lock is a link: {lock_path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"compiler cache lock is not a regular file: {lock_path}")
    if metadata.st_nlink != 1:
        raise RuntimeError(
            f"compiler cache lock must have exactly one link: {lock_path}"
        )


def _directory_open_flags() -> int:
    common = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    if sys.platform.startswith("linux"):
        if not hasattr(os, "O_PATH"):
            raise RuntimeError("compiler cache locking requires Linux O_PATH support")
        return common | os.O_PATH
    if hasattr(os, "O_SEARCH"):
        return common | os.O_SEARCH
    # Some POSIX Python builds expose neither O_PATH nor O_SEARCH.  Read-only
    # directory descriptors preserve dir_fd binding, but require read access in
    # addition to search access; callers on those systems get that explicit
    # compatibility fallback rather than Linux's execute-only behavior.
    return common | os.O_RDONLY


def _require_posix_primitives() -> None:
    supported = (
        os.name == "posix"
        and fcntl is not None
        and hasattr(fcntl, "flock")
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_CLOEXEC")
        and hasattr(os, "register_at_fork")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    )
    if not supported:
        raise RuntimeError(
            "compiler cache locking requires POSIX dir_fd and flock support"
        )


@contextmanager
def _open_posix_cache_directory(cache_dir: str | Path) -> Iterator[tuple[Path, int]]:
    _require_posix_primitives()
    cache = _cache_path(cache_dir)
    try:
        os.makedirs(cache, mode=0o700, exist_ok=True)
        descriptor = os.open(cache, _directory_open_flags())
    except OSError as exc:
        raise RuntimeError(f"could not open compiler cache directory: {cache}") from exc
    primary_error: BaseException | None = None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or _is_reparse_point(metadata):
            raise RuntimeError(f"compiler cache path is not a real directory: {cache}")
        os.set_inheritable(descriptor, False)
        yield cache, descriptor
    except BaseException as exc:  # noqa: B036 - cleanup preserves first fault
        primary_error = exc
        raise
    finally:
        _finish_cleanup_preserving_primary(
            primary_error,
            "compiler cache directory cleanup also failed",
            lambda: _close_descriptor_for_cleanup(descriptor, None),
        )


def _posix_lock_flags(*, create: bool) -> int:
    # Linux may emulate flock with whole-file byte-range locks over NFS.  An
    # exclusive lock therefore needs a writable descriptor even though this
    # helper never writes or truncates the coordination file.
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    return flags


def _stat_posix_lock(directory_descriptor: int, lock_path: Path) -> os.stat_result:
    try:
        metadata = os.stat(
            COMPILER_CACHE_LOCK_FILENAME,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise RuntimeError(
            f"could not inspect compiler cache lock: {lock_path}"
        ) from exc
    _validate_lock_metadata(metadata, lock_path)
    return metadata


def _validate_posix_lock_entry(
    descriptor: int,
    directory_descriptor: int,
    lock_path: Path,
    *,
    expected_identity: tuple[int, ...] | None = None,
) -> tuple[int, ...]:
    opened = os.fstat(descriptor)
    _validate_lock_metadata(opened, lock_path)
    visible = _stat_posix_lock(directory_descriptor, lock_path)
    opened_identity = _inode_identity(opened)
    if _inode_identity(visible) != opened_identity:
        raise RuntimeError(f"compiler cache lock changed: {lock_path}")
    if expected_identity is not None and opened_identity != expected_identity:
        raise RuntimeError(f"compiler cache lock changed: {lock_path}")
    return opened_identity


def _open_posix_lock_file(
    cache: Path,
    directory_descriptor: int,
    descriptor_owner: list[int],
) -> tuple[int, tuple[int, ...]]:
    lock_path = cache / COMPILER_CACHE_LOCK_FILENAME
    with _PosixLifecycleGuardLease() as acquire_interruption:
        if acquire_interruption is not None:
            raise acquire_interruption
        for _attempt in range(_LOCK_OPEN_RETRIES):
            expected_identity: tuple[int, ...] | None
            try:
                before = os.stat(
                    COMPILER_CACHE_LOCK_FILENAME,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                expected_identity = None
                create = True
            except OSError as exc:
                raise RuntimeError(
                    f"could not inspect compiler cache lock: {lock_path}"
                ) from exc
            else:
                _validate_lock_metadata(before, lock_path)
                expected_identity = _inode_identity(before)
                create = False

            descriptor: int | None = None
            try:
                descriptor = os.open(
                    COMPILER_CACHE_LOCK_FILENAME,
                    _posix_lock_flags(create=create),
                    0o600,
                    dir_fd=directory_descriptor,
                )
                # Register immediately after open so a same-thread fork from
                # any later validation hook closes this open description in
                # the child.  Validation failure removes it before returning.
                _ACTIVE_POSIX_LOCK_DESCRIPTORS.add(descriptor)
                descriptor_owner.append(descriptor)
                os.set_inheritable(descriptor, False)
                identity = _validate_posix_lock_entry(
                    descriptor,
                    directory_descriptor,
                    lock_path,
                    expected_identity=expected_identity,
                )
            except (FileExistsError, FileNotFoundError):
                if descriptor is not None:
                    deferred = _close_owned_descriptor_for_cleanup(
                        descriptor,
                        descriptor_owner,
                        None,
                        active_registry=_ACTIVE_POSIX_LOCK_DESCRIPTORS,
                    )
                    if deferred is not None:
                        raise deferred
                continue
            except OSError as exc:
                if descriptor is None:
                    raise RuntimeError(
                        f"could not open compiler cache lock: {lock_path}"
                    ) from exc
                _finish_cleanup_preserving_primary(
                    exc,
                    "compiler cache lock acquisition cleanup also failed",
                    lambda descriptor=descriptor: _close_owned_descriptor_for_cleanup(
                        descriptor,
                        descriptor_owner,
                        None,
                        active_registry=_ACTIVE_POSIX_LOCK_DESCRIPTORS,
                    ),
                )
                raise
            except BaseException as exc:
                if descriptor is not None:
                    _finish_cleanup_preserving_primary(
                        exc,
                        "compiler cache lock acquisition cleanup also failed",
                        lambda descriptor=descriptor: _close_owned_descriptor_for_cleanup(
                            descriptor,
                            descriptor_owner,
                            None,
                            active_registry=_ACTIVE_POSIX_LOCK_DESCRIPTORS,
                        ),
                    )
                raise
            return descriptor, identity
        raise RuntimeError(f"compiler cache lock changed repeatedly: {lock_path}")
    raise AssertionError("unreachable compiler cache lifecycle lease")


def _close_posix_lock_file(
    descriptor_owner: list[int],
    *,
    locked: bool,
) -> None:
    """Unregister and close a lock without exposing a fork inheritance gap."""

    with _PosixLifecycleGuardLease() as deferred:
        descriptor = descriptor_owner[-1]
        try:
            if locked and fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except BaseException as exc:  # noqa: B036 - re-raised after close
            # Closing the descriptor also releases flock.  Preserve the
            # explicit-unlock error, but never let it skip close/unregister.
            deferred = _remember_cleanup_interruption(deferred, exc)
        deferred = _close_owned_descriptor_for_cleanup(
            descriptor,
            descriptor_owner,
            deferred,
            active_registry=_ACTIVE_POSIX_LOCK_DESCRIPTORS,
        )
    if deferred is not None:
        raise deferred


@contextmanager
def _posix_compiler_cache_lock(cache_dir: str | Path) -> Iterator[None]:
    with _open_posix_cache_directory(cache_dir) as (cache, directory_descriptor):
        owner_generation = _POSIX_FORK_GENERATION
        lock_path = cache / COMPILER_CACHE_LOCK_FILENAME
        descriptor_owner: list[int] = []
        locked = False
        primary_error: BaseException | None = None
        try:
            descriptor, identity = _open_posix_lock_file(
                cache,
                directory_descriptor,
                descriptor_owner,
            )
            assert fcntl is not None  # narrowed by _require_posix_primitives
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
            _validate_posix_lock_entry(
                descriptor,
                directory_descriptor,
                lock_path,
                expected_identity=identity,
            )
            yield
        except BaseException as exc:  # noqa: B036 - cleanup preserves first fault
            primary_error = exc
            raise
        finally:
            # The child-side at-fork callback already closed its inherited
            # descriptor.  The generation guard prevents closing a reused fd.
            if descriptor_owner and _POSIX_FORK_GENERATION == owner_generation:
                _finish_cleanup_preserving_primary(
                    primary_error,
                    "compiler cache lock cleanup also failed",
                    lambda: _close_posix_lock_file(
                        descriptor_owner,
                        locked=locked,
                    ),
                )


def _windows_identity(metadata: os.stat_result, path: Path) -> tuple[int, ...]:
    if metadata.st_ino == 0:
        raise RuntimeError(
            f"compiler cache locking requires stable Windows file IDs: {path}"
        )
    return _inode_identity(metadata)


def _validate_windows_cache(cache: Path) -> tuple[int, ...]:
    try:
        metadata = cache.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"could not inspect compiler cache directory: {cache}"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
    ):
        raise RuntimeError(f"compiler cache path is not a real directory: {cache}")
    return _windows_identity(metadata, cache)


def _validate_windows_lock_entry(
    descriptor: int,
    lock_path: Path,
    *,
    expected_identity: tuple[int, ...] | None = None,
) -> tuple[int, ...]:
    opened = os.fstat(descriptor)
    _validate_lock_metadata(opened, lock_path)
    try:
        visible = lock_path.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"could not inspect compiler cache lock: {lock_path}"
        ) from exc
    _validate_lock_metadata(visible, lock_path)
    opened_identity = _windows_identity(opened, lock_path)
    if _windows_identity(visible, lock_path) != opened_identity:
        raise RuntimeError(f"compiler cache lock changed: {lock_path}")
    if expected_identity is not None and opened_identity != expected_identity:
        raise RuntimeError(f"compiler cache lock changed: {lock_path}")
    return opened_identity


def _open_windows_lock_file(
    cache: Path,
    descriptor_owner: list[int] | None = None,
) -> tuple[int, tuple[int, ...]]:
    lock_path = cache / COMPILER_CACHE_LOCK_FILENAME
    owner = descriptor_owner if descriptor_owner is not None else []
    common_flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOINHERIT", 0)
    )
    for _attempt in range(_LOCK_OPEN_RETRIES):
        try:
            before = lock_path.lstat()
        except FileNotFoundError:
            expected_identity = None
            flags = common_flags | os.O_EXCL
        except OSError as exc:
            raise RuntimeError(
                f"could not inspect compiler cache lock: {lock_path}"
            ) from exc
        else:
            _validate_lock_metadata(before, lock_path)
            expected_identity = _windows_identity(before, lock_path)
            flags = common_flags
        descriptor: int | None = None
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            os.set_inheritable(descriptor, False)
            identity = _validate_windows_lock_entry(
                descriptor,
                lock_path,
                expected_identity=expected_identity,
            )
            owner.append(descriptor)
        except (FileExistsError, FileNotFoundError):
            if descriptor is not None:
                deferred = _close_owned_descriptor_for_cleanup(
                    descriptor,
                    owner,
                    None,
                )
                if deferred is not None:
                    raise deferred
            continue
        except OSError as exc:
            if descriptor is None:
                raise RuntimeError(
                    f"could not open compiler cache lock: {lock_path}"
                ) from exc
            _finish_cleanup_preserving_primary(
                exc,
                "compiler cache lock acquisition cleanup also failed",
                lambda descriptor=descriptor: _close_owned_descriptor_for_cleanup(
                    descriptor,
                    owner,
                    None,
                ),
            )
            raise
        except BaseException as exc:
            if descriptor is not None:
                _finish_cleanup_preserving_primary(
                    exc,
                    "compiler cache lock acquisition cleanup also failed",
                    lambda descriptor=descriptor: _close_owned_descriptor_for_cleanup(
                        descriptor,
                        owner,
                        None,
                    ),
                )
            raise
        return descriptor, identity
    raise RuntimeError(f"compiler cache lock changed repeatedly: {lock_path}")


def _acquire_windows_lock(descriptor: int) -> None:
    assert msvcrt is not None
    runtime: Any = msvcrt
    contention = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
    while True:
        # The CRT permits a locked range to extend beyond EOF, so an empty
        # coordination inode needs no initialization write.
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            runtime.locking(descriptor, runtime.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in contention:
                raise
            time.sleep(_WINDOWS_LOCK_RETRY_SECONDS)


def _release_windows_lock(descriptor: int) -> None:
    assert msvcrt is not None
    runtime: Any = msvcrt
    os.lseek(descriptor, 0, os.SEEK_SET)
    runtime.locking(descriptor, runtime.LK_UNLCK, 1)


def _close_windows_lock_file(
    descriptor_owner: list[int],
    *,
    locked: bool,
) -> None:
    """Release and close an owned CRT lock before dropping ownership."""

    descriptor = descriptor_owner[-1]
    deferred: BaseException | None = None
    if locked:
        try:
            _release_windows_lock(descriptor)
        except BaseException as exc:  # noqa: B036 - re-raised after close
            # A successful close also releases the CRT byte-range lock.
            deferred = _remember_cleanup_interruption(deferred, exc)
    deferred = _close_owned_descriptor_for_cleanup(
        descriptor,
        descriptor_owner,
        deferred,
    )
    if deferred is not None:
        raise deferred


@contextmanager
def _windows_compiler_cache_lock(cache_dir: str | Path) -> Iterator[None]:
    if msvcrt is None:
        raise RuntimeError("compiler cache locking requires Windows msvcrt support")
    cache = _cache_path(cache_dir)
    try:
        os.makedirs(cache, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"could not create compiler cache directory: {cache}"
        ) from exc
    cache_identity = _validate_windows_cache(cache)
    descriptor_owner: list[int] = []
    locked = False
    primary_error: BaseException | None = None
    try:
        descriptor, lock_identity = _open_windows_lock_file(
            cache,
            descriptor_owner,
        )
        _acquire_windows_lock(descriptor)
        locked = True
        if _validate_windows_cache(cache) != cache_identity:
            raise RuntimeError(f"compiler cache path changed: {cache}")
        _validate_windows_lock_entry(
            descriptor,
            cache / COMPILER_CACHE_LOCK_FILENAME,
            expected_identity=lock_identity,
        )
        yield
    except BaseException as exc:  # noqa: B036 - cleanup preserves first fault
        primary_error = exc
        raise
    finally:
        if descriptor_owner:
            _finish_cleanup_preserving_primary(
                primary_error,
                "compiler cache lock cleanup also failed",
                lambda: _close_windows_lock_file(
                    descriptor_owner,
                    locked=locked,
                ),
            )


@contextmanager
def compiler_cache_lock(cache_dir: str | Path) -> Iterator[None]:
    """Serialize cooperating users of one private compiler cache.

    The fixed lock entry is opened read/write without following links or
    truncating data on POSIX, and must remain a single-link regular inode
    through the pre-entry identity sandwich.  POSIX uses ``flock``; Windows
    uses the equivalent cooperative ``msvcrt`` byte lock after rejecting an
    existing reparse-point cache or lock entry.

    This helper protects participating compiler/importer operations from each
    other.  It does not validate or defend the manifest and view tree against
    a user who actively replaces paths while the lock is held.
    """

    if os.name == "nt":
        with _windows_compiler_cache_lock(cache_dir):
            yield
        return
    with _posix_compiler_cache_lock(cache_dir):
        yield


__all__ = ["COMPILER_CACHE_LOCK_FILENAME", "compiler_cache_lock"]
