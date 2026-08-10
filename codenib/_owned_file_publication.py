# SPDX-FileCopyrightText: 2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Descriptor-bound no-overwrite publication of one regular file.

This module deliberately leaves failed and redundant stages in place for a
later, quiescent garbage collector.  POSIX has no inode-conditional unlink, so
online cleanup cannot prove that a name still denotes the file this process
created.

Publication never returns a descriptor-owning receipt.  The caller creates a
``PublishedFileReceiptOwner`` first; publication installs a borrowed receipt in
that owner before returning, closing the return-to-caller cancellation window.

The receipt authenticates an exact retained descriptor at publication and on
each bounded read.  A normal filesystem writable by another process with the
same UID cannot guarantee byte immutability across time; that stronger property
requires a protected object store, fs-verity, or a service ownership boundary.

The descriptor guards cover asynchronous failure after an fd reaches its Python
owner and cover ordinary fd-number reuse.  One native promotion gate remains:
``os.open`` and ``os.dup`` return bare integers, so arbitrary opcode injection
between the C return and the first Python ownership store cannot be closed in
Python.  Full closure requires a native owning-fd object that installs itself in
the supplied owner before returning, matching the atomic-directory promotion
gate.

These guards are not a sandbox against arbitrary code already executing in this
process; such code can directly close, seek, or reassign any process descriptor.
In particular, a callback can ``dup2`` an alias of the same open-file
description onto a just-closed number.  That exact ABA is indistinguishable from
a close that did not happen.  A retryable receipt owner therefore retains that
exact alias after an interrupted close.  Its stored offset cookie rejects later
observable fd reuse before another close, but arbitrary same-process code can
continually recreate the exact alias and defeats any proof available in Python.
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, TypeVar

from . import _atomic_directory as _atomic

_COPY_BYTES = 1 << 20
_MAX_STAGE_ATTEMPTS = 128
_CLOSE_COOKIE_FLOOR = 1 << 20
_CLOSE_COOKIE_SPAN = 1 << 30
_OWNER_STATE_RECOVERY_LIMIT = 64
_TERMINAL_DESCRIPTOR_RECOVERY_LIMIT = 8
_REAL_OS_CLOSE = os.close
_T = TypeVar("_T")
_SAFE_POSIX_FILE_FDS = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_CLOEXEC")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
)


def require_owned_file_publication_support() -> None:
    """Fail before mutation when strict regular-file publication is unavailable."""

    # The shared Windows authority does not yet expose a RootDirectory-bound
    # regular-file create HANDLE.  Keep this gate ahead of every parent or stage
    # mutation so callers can safely select an unsupported-platform fallback.
    if sys.platform == "win32":
        raise RuntimeError("owned regular-file publication is unsupported on Windows")
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        raise RuntimeError("owned regular-file publication is unsupported on this host")
    if not _SAFE_POSIX_FILE_FDS:
        raise RuntimeError(
            "owned regular-file publication requires POSIX dir-fd support"
        )
    if not _atomic._SAFE_OWNERSHIP_DIRECTORY_FDS:
        raise RuntimeError(
            "owned regular-file publication requires anchored directory-fd support"
        )
    _atomic._require_rename_noreplace_platform()


class _DescriptorOwner:
    """One shared, invalidatable owner slot for a POSIX descriptor.

    Sharing the slot, rather than copying an fd number between locals and
    result objects, makes every alias observe cleanup performed after a
    constructor or return-path ``BaseException``.
    """

    __slots__ = ("_descriptor", "_identity", "_close_cookie")

    def __init__(self) -> None:
        self._descriptor = -1
        self._identity: tuple[int, ...] | None = None
        self._close_cookie: int | None = None

    @property
    def descriptor(self) -> int:
        return self._descriptor

    def open(
        self,
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if self._descriptor >= 0:
            raise RuntimeError("descriptor owner is already active")
        descriptor = -1
        try:
            if dir_fd is None:
                descriptor = os.open(path, flags, mode)
            else:
                descriptor = os.open(path, flags, mode, dir_fd=dir_fd)
            self._descriptor = descriptor
            self._close_cookie = None
            return descriptor
        except BaseException as primary_error:
            if descriptor >= 0:
                if self._descriptor == descriptor:
                    self.close_after_error(primary_error)
                else:
                    _cleanup_untransferred_descriptor(descriptor, primary_error)
            raise

    def duplicate(self, descriptor: int) -> int:
        if self._descriptor >= 0:
            raise RuntimeError("descriptor owner is already active")
        duplicate = -1
        try:
            duplicate = os.dup(descriptor)
            self._descriptor = duplicate
            self._close_cookie = None
            return duplicate
        except BaseException as primary_error:
            if duplicate >= 0:
                if self._descriptor == duplicate:
                    self.close_after_error(primary_error)
                else:
                    _cleanup_untransferred_descriptor(duplicate, primary_error)
            raise

    def bind_identity(self, metadata: os.stat_result) -> None:
        identity = _file_identity(metadata)
        if self._descriptor < 0:
            raise RuntimeError("cannot bind an inactive descriptor owner")
        self._identity = identity

    def close(
        self,
        *,
        expected_identity: tuple[int, ...],
        retryable: bool = False,
    ) -> None:
        descriptor = self._descriptor
        if descriptor < 0:
            return
        if retryable:
            self._close_retryable(
                descriptor,
                expected_identity=expected_identity,
            )
            return
        close_cookie: int | None = None
        try:
            close_cookie = _arm_descriptor_for_close(descriptor)
            # Clearing the shared slot is the close linearization point.  It is
            # deliberately before the syscall: if close succeeds and delivery
            # of an asynchronous exception follows, no alias retains a stale
            # descriptor number that it could close again after fd reuse.
            self._descriptor = -1
            self._identity = None
            self._close_cookie = None
            os.close(descriptor)
        except BaseException as close_error:
            if self._descriptor < 0:
                _finish_close_after_error(
                    descriptor,
                    expected_identity,
                    close_error,
                    close_cookie=close_cookie,
                )
            else:
                # Arming itself, or the tiny arm-to-owner-clear window, can be
                # interrupted.  This is still unquestionably our descriptor:
                # invalidate the shared owner and finish one terminal cleanup
                # attempt while keeping the interruption primary.
                self._descriptor = -1
                self._identity = None
                self._close_cookie = None
                _cleanup_descriptor_after_error(
                    descriptor,
                    expected_identity,
                    close_error,
                )
            raise

    def _close_retryable(
        self,
        descriptor: int,
        *,
        expected_identity: tuple[int, ...],
    ) -> None:
        """Attempt one close while retaining a provably open owner for retry."""

        previous_cookie = self._close_cookie
        if previous_cookie is not None:
            try:
                still_owned = _descriptor_still_exact(
                    descriptor,
                    expected_identity,
                    close_cookie=previous_cookie,
                )
            except BaseException as ownership_error:  # noqa: B036
                self._descriptor = -1
                self._identity = None
                self._close_cookie = None
                raise RuntimeError(
                    "published receipt descriptor ownership is unknown"
                ) from ownership_error
            if not still_owned:
                self._descriptor = -1
                self._identity = None
                self._close_cookie = None
                raise RuntimeError(
                    "published receipt descriptor changed before close retry"
                )

        close_cookie = _arm_descriptor_for_close(
            descriptor,
            cookie_owner=self,
        )
        try:
            os.close(descriptor)
            self._descriptor = -1
            self._identity = None
            self._close_cookie = None
        except BaseException as close_error:  # noqa: B036 - reconcile syscall
            if self._descriptor < 0:
                self._identity = None
                self._close_cookie = None
                raise
            try:
                still_exact = _descriptor_still_exact(
                    descriptor,
                    expected_identity,
                    close_cookie=close_cookie,
                )
            except BaseException as reconciliation_error:  # noqa: B036
                _annotate_error(
                    close_error,
                    "descriptor close reconciliation failed",
                    reconciliation_error,
                )
                # Unknown is not sufficient authority for another close.  A
                # retry is offered only when identity plus the OFD cookie were
                # positively re-observed after the failed syscall.
                self._descriptor = -1
                self._identity = None
                self._close_cookie = None
            else:
                if not still_exact:
                    self._descriptor = -1
                    self._identity = None
                    self._close_cookie = None
            raise

    def close_after_error(self, primary_error: BaseException) -> None:
        """Terminally clean while retaining ownership across cancellation."""

        deferred: BaseException | None = None
        for _attempt in range(_TERMINAL_DESCRIPTOR_RECOVERY_LIMIT):
            descriptor = self._descriptor
            if descriptor < 0:
                deferred = self._invalidate_converged(deferred)
                self._note_deferred_cleanup(primary_error, deferred)
                return

            identity = self._identity
            if identity is None:
                try:
                    metadata = os.fstat(descriptor)
                except OSError as error:
                    if error.errno == errno.EBADF:
                        deferred = self._invalidate_converged(deferred)
                        self._note_deferred_cleanup(primary_error, deferred)
                        return
                    deferred = _remember_first_cleanup_error(deferred, error)
                    continue
                except BaseException as error:  # noqa: B036 - bounded recovery
                    deferred = _remember_first_cleanup_error(deferred, error)
                    continue
                try:
                    self.bind_identity(metadata)
                except BaseException as error:  # noqa: B036 - observe next pass
                    deferred = _remember_first_cleanup_error(deferred, error)
                continue

            try:
                self._close_retryable(
                    descriptor,
                    expected_identity=identity,
                )
            except BaseException as error:  # noqa: B036 - bounded recovery
                deferred = _remember_first_cleanup_error(deferred, error)
                if self._descriptor < 0:
                    deferred = self._invalidate_converged(deferred)
                    self._note_deferred_cleanup(primary_error, deferred)
                    return
                continue

            deferred = self._invalidate_converged(deferred)
            self._note_deferred_cleanup(primary_error, deferred)
            return

        descriptor = self._descriptor
        identity = self._identity
        if descriptor < 0:
            deferred = self._invalidate_converged(deferred)
            self._note_deferred_cleanup(primary_error, deferred)
            return
        try:
            if identity is None:
                closed = _cleanup_untransferred_descriptor(
                    descriptor,
                    primary_error,
                )
            else:
                closed = _cleanup_descriptor_after_error(
                    descriptor,
                    identity,
                    primary_error,
                    cookie_owner=self,
                )
        except BaseException as error:  # noqa: B036 - observe terminal outcome
            deferred = _remember_first_cleanup_error(deferred, error)
            closed = self._descriptor < 0
            if not closed and self._close_cookie is not None and identity is not None:
                try:
                    closed = not _descriptor_still_exact(
                        descriptor,
                        identity,
                        close_cookie=self._close_cookie,
                    )
                except BaseException as reconciliation_error:  # noqa: B036
                    deferred = _remember_first_cleanup_error(
                        deferred,
                        reconciliation_error,
                    )
        if closed:
            deferred = self._invalidate_converged(deferred)
        self._note_deferred_cleanup(primary_error, deferred)

    def _invalidate_converged(
        self,
        deferred: BaseException | None,
    ) -> BaseException | None:
        for _attempt in range(_OWNER_STATE_RECOVERY_LIMIT):
            try:
                self._invalidate()
            except BaseException as error:  # noqa: B036 - converge owner state
                deferred = _remember_first_cleanup_error(deferred, error)
                continue
            if (
                self._descriptor < 0
                and self._identity is None
                and self._close_cookie is None
            ):
                return deferred
        return _remember_first_cleanup_error(
            deferred,
            RuntimeError("descriptor owner invalidation did not converge"),
        )

    def _invalidate(self) -> None:
        self._descriptor = -1
        self._identity = None
        self._close_cookie = None

    @staticmethod
    def _note_deferred_cleanup(
        primary_error: BaseException,
        deferred: BaseException | None,
    ) -> None:
        if deferred is not None:
            _annotate_error(
                primary_error,
                "descriptor terminal cleanup was interrupted",
                deferred,
            )


def _close_posix_resources(
    resources: _atomic._PosixResourceOwner,
    *,
    primary_error: BaseException | None = None,
) -> None:
    """Close an independent directory bridge while retaining its first error."""

    first_error = primary_error
    for _attempt in range(_OWNER_STATE_RECOVERY_LIMIT):
        if resources.closed:
            break
        try:
            resources.close_all()
        except BaseException as close_error:  # noqa: B036 - converge ownership
            if first_error is None:
                first_error = close_error
            else:
                _annotate_error(
                    first_error,
                    "borrowed parent bridge cleanup also failed",
                    close_error,
                )
    if not resources.closed:
        convergence_error = RuntimeError(
            "borrowed parent bridge cleanup did not converge"
        )
        if first_error is None:
            first_error = convergence_error
        else:
            _annotate_error(
                first_error,
                "borrowed parent bridge cleanup did not converge",
                convergence_error,
            )
    if primary_error is None and first_error is not None:
        raise first_error


class _PublicationAuthorityOwner:
    """Owner installed before the shared parent authority is acquired."""

    __slots__ = ("_authority",)

    def __init__(self) -> None:
        self._authority: _atomic._PublicationAuthority | None = None

    @property
    def authority(self) -> _atomic._PublicationAuthority | None:
        return self._authority

    def install(self, authority: _atomic._PublicationAuthority) -> None:
        """Accept authority ownership before the factory returns to Python."""

        if self._authority is not None:
            raise RuntimeError("publication authority owner is already active")
        self._authority = authority

    def acquire(self, path: Path, *, parent_resource: int | None = None) -> None:
        if self._authority is not None:
            raise RuntimeError("publication authority owner is already active")
        bridge_resources = _atomic._PosixResourceOwner()
        authority_parent: int | None = None
        if parent_resource is not None:
            if isinstance(parent_resource, bool) or not isinstance(
                parent_resource, int
            ):
                raise TypeError(
                    "borrowed publication parent resource must be an integer"
                )
            if parent_resource < 0:
                raise ValueError(
                    "borrowed publication parent resource must be non-negative"
                )
            expected_parent_identity = _atomic.publication_parent_identity(
                parent_resource
            )
        else:
            expected_parent_identity = None
        try:
            if parent_resource is not None:
                # ``dup(parent_resource)`` would share the caller's open-file
                # description and therefore its directory offset.  Open "."
                # relative to the borrowed descriptor to create an independent
                # OFD before atomic takes its own duplicate.  Close cookies can
                # then never perturb the caller's offset.
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                flags |= getattr(os, "O_NONBLOCK", 0)
                authority_parent = bridge_resources.open(
                    ".",
                    flags,
                    dir_fd=parent_resource,
                )
                if (
                    _atomic.publication_parent_identity(authority_parent)
                    != expected_parent_identity
                ):
                    raise RuntimeError(
                        "borrowed publication parent resource changed while opened"
                    )
            _atomic._open_publication_authority(
                path,
                parent_resource=authority_parent,
                expected_parent_identity=expected_parent_identity,
                create_missing=False,
                authority_owner=self,
            )
            if self._authority is None:
                raise RuntimeError("publication authority was not installed")
            if authority_parent is not None:
                _close_posix_resources(bridge_resources)
        except BaseException as primary_error:
            if self._authority is not None:
                self.close_after_error(primary_error)
            _close_posix_resources(
                bridge_resources,
                primary_error=primary_error,
            )
            raise

    def close(self) -> None:
        authority = self._authority
        if authority is None:
            return
        try:
            _close_publication_authority(authority)
        finally:
            if authority._closed:
                self._authority = None

    def close_after_error(self, primary_error: BaseException) -> None:
        try:
            self.close()
        except BaseException as cleanup_error:  # noqa: B036 - keep primary
            _annotate_error(
                primary_error,
                "parent authority cleanup also failed",
                cleanup_error,
            )


@dataclass(frozen=True, slots=True)
class PublishedFileRecord:
    """Canonical content and mode authenticated by publication."""

    size: int
    sha256: str
    mode: int


@dataclass(frozen=True, slots=True)
class OwnedFileOrphan:
    """A diagnostic locator for an owned stage that was not published.

    The path is not an authority and this type intentionally has no deletion
    operation.  Reclamation belongs to a separate cooperative-GC boundary.
    """

    path: Path
    parent_identity: tuple[int, ...]
    file_identity: tuple[int, ...]
    record: PublishedFileRecord


class OwnedFileConflictError(RuntimeError):
    """The destination is not the requested receipt-time-authenticated object."""

    def __init__(
        self,
        destination: Path,
        expected: PublishedFileRecord,
        observed: PublishedFileRecord | None,
        orphan: OwnedFileOrphan,
    ) -> None:
        super().__init__(destination, expected, observed, orphan)
        self.destination = destination
        self.expected = expected
        self.observed = observed
        self.orphan = orphan

    def __str__(self) -> str:
        return f"owned file publication conflicts at {self.destination}"


_RECEIPT_OWNER_CLOSE = object()


class PublishedFileReceipt:
    """A borrowed handle-bound view that re-authenticates each bounded read.

    Retaining the handle prevents path substitution; it does not make a normal
    same-UID writable filesystem immutable after publication.  Its caller owns
    the corresponding :class:`PublishedFileReceiptOwner`, which controls close.
    """

    __slots__ = (
        "path",
        "record",
        "file_identity",
        "parent_identity",
        "reused",
        "orphan",
        "_descriptor_owner",
        "_owner_pid",
        "_version_identity",
    )

    def __init__(
        self,
        *,
        path: Path,
        record: PublishedFileRecord,
        file_identity: tuple[int, ...],
        version_identity: tuple[int, ...],
        parent_identity: tuple[int, ...],
        reused: bool,
        orphan: OwnedFileOrphan | None,
        descriptor_owner: _DescriptorOwner,
    ) -> None:
        # Store the shared owner first.  If any later constructor operation is
        # interrupted, the publisher can invalidate the same slot seen by a
        # partially constructed receipt retained by an injected callback.
        self._descriptor_owner = descriptor_owner
        self.path = path
        self.record = record
        self.file_identity = file_identity
        self.parent_identity = parent_identity
        self.reused = reused
        self.orphan = orphan
        self._owner_pid = os.getpid()
        self._version_identity = version_identity

    @property
    def _descriptor(self) -> int:
        return self._descriptor_owner.descriptor

    @property
    def closed(self) -> bool:
        return self._descriptor < 0

    def read_bytes(self, *, max_bytes: int) -> bytes:
        """Read and re-authenticate bytes through the retained exact handle."""

        self._require_active()
        limit = _bounded_size(max_bytes, label="receipt read limit")
        if self.record.size > limit:
            raise ValueError("published file exceeds the receipt read limit")
        before = os.fstat(self._descriptor)
        _require_exact_regular(
            before,
            identity=self.file_identity,
            version_identity=self._version_identity,
            mode=self.record.mode,
            size=self.record.size,
            label="published file receipt",
        )
        payload = bytearray()
        digest = hashlib.sha256()
        offset = 0
        while offset < self.record.size:
            block = os.pread(
                self._descriptor,
                min(_COPY_BYTES, self.record.size - offset),
                offset,
            )
            if not block:
                raise RuntimeError("published file was truncated")
            payload.extend(block)
            digest.update(block)
            offset += len(block)
        if os.pread(self._descriptor, 1, offset):
            raise RuntimeError("published file grew")
        after = os.fstat(self._descriptor)
        _require_exact_regular(
            after,
            identity=self.file_identity,
            version_identity=self._version_identity,
            mode=self.record.mode,
            size=self.record.size,
            label="published file receipt",
        )
        if digest.hexdigest() != self.record.sha256:
            raise RuntimeError("published file bytes changed")
        return bytes(payload)

    def close(self) -> None:
        """Reject detached cleanup of this borrowed receipt view."""

        raise RuntimeError("close the PublishedFileReceiptOwner, not its receipt")

    def _close_from_owner(self, authority: object) -> None:
        if authority is not _RECEIPT_OWNER_CLOSE:
            raise RuntimeError("published file receipt close authority is invalid")
        if self._descriptor < 0:
            return
        owner_changed = os.getpid() != self._owner_pid
        self._descriptor_owner.close(
            expected_identity=self.file_identity,
            retryable=True,
        )
        if owner_changed:
            raise RuntimeError("published file receipt cannot cross a PID boundary")

    def _require_active(self) -> None:
        if self._descriptor < 0:
            raise RuntimeError("published file receipt is closed")
        if os.getpid() != self._owner_pid:
            raise RuntimeError("published file receipt cannot cross a PID boundary")


_RECEIPT_OWNER_EMPTY = object()
_RECEIPT_OWNER_CLOSED = object()


class _CancellationSafeRLock:
    """RLock whose logical depth makes interrupted transitions recoverable."""

    __slots__ = ("_depth", "_lock")

    _RECOVERY_LIMIT = 64

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._depth = threading.local()

    def run(self, callback: Callable[[], _T]) -> _T:
        baseline = self._logical_depth()
        try:
            acquisition_error = self._acquire_reconciled(baseline)
        except BaseException as primary_error:  # noqa: B036 - restore baseline
            self._release_preserving(baseline, primary_error)
            raise
        if acquisition_error is not None:
            self._release_preserving(baseline, acquisition_error)
            raise acquisition_error
        try:
            result = callback()
        except BaseException as primary_error:  # noqa: B036 - restore then preserve
            self._release_preserving(baseline, primary_error)
            raise
        try:
            release_error = self._release_reconciled(baseline)
        except BaseException:
            raise
        if release_error is not None:
            raise release_error
        return result

    def _acquire(self) -> None:
        depth = self._logical_depth()
        if depth:
            self._set_logical_depth(depth + 1)
            return
        if not self._lock.acquire():
            raise RuntimeError("receipt owner lifecycle lock acquisition failed")
        self._set_logical_depth(1)

    def _release(self) -> None:
        depth = self._logical_depth()
        if depth < 1 or not self._native_is_owned():
            raise RuntimeError("cannot release an unowned receipt lifecycle lock")
        if depth > 1:
            self._set_logical_depth(depth - 1)
            return
        self._lock.release()
        self._set_logical_depth(0)

    def _logical_depth(self) -> int:
        return int(getattr(self._depth, "value", 0))

    def _set_logical_depth(self, depth: int) -> None:
        self._depth.value = depth

    def _native_is_owned(self) -> bool:
        ownership_check = getattr(self._lock, "_is_owned", None)
        if not callable(ownership_check):
            raise RuntimeError("receipt lifecycle lock lacks owner tracking")
        return bool(ownership_check())

    def held_by_current_thread(self) -> bool:
        """Return whether this thread is already inside a lifecycle lease."""

        return self._logical_depth() > 0 and self._native_is_owned()

    @staticmethod
    def _defer_failure(
        deferred: BaseException | None,
        failure: BaseException,
    ) -> BaseException:
        if deferred is None:
            return failure
        if failure is not deferred:
            _annotate_error(
                deferred,
                "receipt lifecycle lock recovery also failed",
                failure,
            )
        return deferred

    def _acquire_reconciled(self, baseline: int) -> BaseException | None:
        """Acquire one logical level without repeating a completed acquire."""

        deferred: BaseException | None = None
        for _attempt in range(self._RECOVERY_LIMIT):
            try:
                depth = self._logical_depth()
                native_owned = self._native_is_owned()
            except BaseException as error:  # noqa: B036 - bounded recovery
                deferred = self._defer_failure(deferred, error)
                continue

            if native_owned:
                if depth > baseline:
                    return deferred
                try:
                    # A completed zero-depth acquire, or an interrupted
                    # reentrant logical acquire, needs no second native call.
                    self._set_logical_depth(baseline + 1)
                except BaseException as error:  # noqa: B036
                    deferred = self._defer_failure(deferred, error)
                    continue
                return deferred

            if depth > baseline:
                try:
                    self._set_logical_depth(baseline)
                except BaseException as error:  # noqa: B036
                    deferred = self._defer_failure(deferred, error)
                    continue

            if deferred is not None:
                # A successful ownership probe now proves that an interrupted
                # acquire did not take the native lock.  This operation will
                # not execute its callback, so reacquiring would only defer
                # cancellation behind an unrelated owner.
                return deferred

            try:
                self._acquire()
                return deferred
            except BaseException as error:  # noqa: B036 - reconcile next
                deferred = self._defer_failure(deferred, error)

        if deferred is not None:
            raise deferred
        raise RuntimeError("receipt lifecycle lock acquisition did not converge")

    def _release_reconciled(self, baseline: int) -> BaseException | None:
        """Restore the logical baseline without repeating a completed release."""

        deferred: BaseException | None = None
        for _attempt in range(self._RECOVERY_LIMIT):
            try:
                depth = self._logical_depth()
                native_owned = self._native_is_owned()
            except BaseException as error:  # noqa: B036 - bounded recovery
                deferred = self._defer_failure(deferred, error)
                continue

            if baseline:
                if not native_owned:
                    try:
                        self._set_logical_depth(0)
                    except BaseException as error:  # noqa: B036
                        deferred = self._defer_failure(deferred, error)
                        continue
                    return self._defer_failure(
                        deferred,
                        RuntimeError(
                            "receipt lifecycle lock lost its outer native owner"
                        ),
                    )
                if depth != baseline:
                    try:
                        self._set_logical_depth(baseline)
                    except BaseException as error:  # noqa: B036
                        deferred = self._defer_failure(deferred, error)
                        continue
                return deferred

            if not native_owned:
                if depth:
                    try:
                        self._set_logical_depth(0)
                    except BaseException as error:  # noqa: B036
                        deferred = self._defer_failure(deferred, error)
                        continue
                return deferred

            if depth != 1:
                try:
                    self._set_logical_depth(1)
                except BaseException as error:  # noqa: B036
                    deferred = self._defer_failure(deferred, error)
                    continue

            try:
                self._release()
            except BaseException as error:  # noqa: B036 - reconcile next
                deferred = self._defer_failure(deferred, error)

        if deferred is not None:
            raise deferred
        raise RuntimeError("receipt lifecycle lock release did not converge")

    def _release_preserving(
        self,
        baseline: int,
        primary_error: BaseException,
    ) -> None:
        try:
            release_error = self._release_reconciled(baseline)
        except BaseException as release_error:  # noqa: B036 - keep primary
            _annotate_error(
                primary_error,
                "receipt lifecycle lock release also failed",
                release_error,
            )
            return
        if release_error is not None:
            _annotate_error(
                primary_error,
                "receipt lifecycle lock release was interrupted",
                release_error,
            )


class PublishedFileReceiptOwner:
    """One-shot caller-owned slot for a borrowed published-file receipt.

    The caller creates this owner before publication.  Publication reserves it
    before any rename and installs the receipt before returning, so an
    asynchronous exception at any later Python return/STORE boundary cannot
    strand the retained descriptor on the evaluation stack.
    """

    __slots__ = ("_slot", "_lock", "_owner_pid")

    def __init__(self) -> None:
        self._slot: object = _RECEIPT_OWNER_EMPTY
        self._lock = _CancellationSafeRLock()
        self._owner_pid = os.getpid()

    @property
    def state(self) -> str:
        self._require_owner_pid()

        def observe() -> str:
            self._normalize_closed_locked()
            return self._state_locked()

        deferred: BaseException | None = None
        for _attempt in range(_OWNER_STATE_RECOVERY_LIMIT):
            try:
                observed = self._lock.run(observe)
            except BaseException as state_error:  # noqa: B036 - converge state
                deferred = _remember_first_cleanup_error(deferred, state_error)
                continue
            if deferred is not None:
                raise deferred
            return observed
        if deferred is not None:
            raise deferred
        raise RuntimeError("receipt owner state observation did not converge")

    @property
    def active(self) -> bool:
        return self.state == "active"

    @property
    def closed(self) -> bool:
        return self.state == "closed"

    @property
    def receipt(self) -> PublishedFileReceipt:
        """Return a borrowed receipt while this owner retains its ownership."""

        self._require_owner_pid()

        def borrow() -> PublishedFileReceipt:
            self._normalize_closed_locked()
            if not isinstance(self._slot, PublishedFileReceipt):
                raise RuntimeError(
                    "published file receipt owner is "
                    f"{self._state_locked()}, expected active"
                )
            return self._slot

        return self._lock.run(borrow)

    def consume(self, callback: Callable[[PublishedFileReceipt], object]) -> None:
        """Run a synchronous callback with a borrowed, never-detached receipt."""

        if not callable(callback):
            raise TypeError("published file receipt consumer must be callable")
        self._require_owner_pid()
        if self._lock.held_by_current_thread():
            raise RuntimeError("published file receipt owner consume is reentrant")

        def consume_locked() -> None:
            self._normalize_closed_locked()
            if not isinstance(self._slot, PublishedFileReceipt):
                raise RuntimeError(
                    "published file receipt owner is "
                    f"{self._state_locked()}, expected active"
                )
            receipt = self._slot
            callback(receipt)
            self._normalize_closed_locked()

        # Holding the lifecycle lease for the callback makes consume and close
        # linear: a concurrent close either wins first or waits for the entire
        # synchronous borrow to finish.
        self._lock.run(consume_locked)

    def close(self) -> None:
        owner_changed = os.getpid() != self._owner_pid
        if self._lock.held_by_current_thread():
            raise RuntimeError("published file receipt owner close is reentrant")

        def close_locked() -> None:
            self._normalize_closed_locked()
            if self._slot is _RECEIPT_OWNER_CLOSED:
                return
            if isinstance(self._slot, PublishedFileReceipt):
                receipt = self._slot
                # Keep the lifecycle lock across descriptor reconciliation.  A
                # receipt never moves through a return/store handoff inside
                # close, and another thread cannot start a competing cleanup.
                try:
                    receipt._close_from_owner(_RECEIPT_OWNER_CLOSE)
                except BaseException as close_error:  # noqa: B036 - settle state
                    try:
                        settle_error = self._settle_receipt_locked(receipt)
                    except BaseException as observation_error:  # noqa: B036
                        _annotate_error(
                            close_error,
                            "receipt owner close-state observation also failed",
                            observation_error,
                        )
                        raise close_error
                    if settle_error is not None:
                        _annotate_error(
                            close_error,
                            "receipt owner close-state cleanup also failed",
                            settle_error,
                        )
                    raise
                settle_error = self._settle_receipt_locked(receipt)
                if settle_error is not None:
                    raise settle_error
                return
            if self._slot is _RECEIPT_OWNER_EMPTY:
                self._slot = _RECEIPT_OWNER_CLOSED
                return
            raise RuntimeError("published file receipt publication is in progress")

        close_error: BaseException | None = None
        try:
            self._lock.run(close_locked)
        except BaseException as exc:  # noqa: B036 - report PID after cleanup
            close_error = exc

        if owner_changed:
            boundary_error = RuntimeError(
                "published file receipt owner cannot cross a PID boundary"
            )
            if close_error is not None:
                raise boundary_error from close_error
            raise boundary_error
        if close_error is not None:
            raise close_error

    def _settle_receipt_locked(
        self,
        receipt: PublishedFileReceipt,
    ) -> BaseException | None:
        deferred: BaseException | None = None
        for _attempt in range(_OWNER_STATE_RECOVERY_LIMIT):
            try:
                closed = receipt.closed
            except BaseException as state_error:  # noqa: B036 - bounded recovery
                deferred = _remember_first_cleanup_error(deferred, state_error)
                continue
            if not closed:
                return deferred
            try:
                if self._slot is receipt:
                    self._slot = _RECEIPT_OWNER_CLOSED
            except BaseException as state_error:  # noqa: B036 - observe next
                deferred = _remember_first_cleanup_error(deferred, state_error)
                continue
            if self._slot is not receipt:
                return deferred
        return _remember_first_cleanup_error(
            deferred,
            RuntimeError("receipt owner close state did not converge"),
        )

    def close_after_error(self, primary_error: BaseException) -> None:
        try:
            self.close()
        except BaseException as cleanup_error:  # noqa: B036 - keep primary
            _annotate_error(
                primary_error,
                "published receipt owner cleanup also failed",
                cleanup_error,
            )

    def _reserve(self, reservation: object) -> None:
        self._require_owner_pid()

        def reserve() -> None:
            if self._slot is not _RECEIPT_OWNER_EMPTY:
                raise RuntimeError(
                    "published file receipt owner is "
                    f"{self._state_locked()}, expected empty"
                )
            self._slot = reservation

        self._lock.run(reserve)

    def _install(
        self,
        reservation: object,
        receipt: PublishedFileReceipt,
    ) -> None:
        self._require_owner_pid()

        def install() -> None:
            if self._slot is not reservation:
                raise RuntimeError("published file receipt reservation changed")
            self._slot = receipt

        self._lock.run(install)

    def _owns_descriptor_owner(self, descriptor_owner: _DescriptorOwner) -> bool:
        def owns() -> bool:
            return (
                isinstance(self._slot, PublishedFileReceipt)
                and self._slot._descriptor_owner is descriptor_owner
            )

        return self._lock.run(owns)

    def _cancel_reservation(self, reservation: object) -> None:
        deferred: BaseException | None = None
        for _attempt in range(_OWNER_STATE_RECOVERY_LIMIT):
            try:
                terminal = self._lock.run(
                    lambda: self._cancel_reservation_locked(reservation)
                )
            except BaseException as cleanup_error:  # noqa: B036 - converge state
                deferred = _remember_first_cleanup_error(deferred, cleanup_error)
                continue
            if terminal:
                if deferred is not None:
                    raise deferred
                return
        if deferred is not None:
            raise deferred
        raise RuntimeError("receipt reservation cancellation did not converge")

    def _cancel_reservation_locked(self, reservation: object) -> bool:
        if self._slot is reservation:
            self._slot = _RECEIPT_OWNER_CLOSED
        return self._slot is not reservation

    def _close_terminal_after_error(self, primary_error: BaseException) -> None:
        """Terminally clean a helper-local owner that cannot escape to retry."""

        deferred: BaseException | None = None
        for _attempt in range(_OWNER_STATE_RECOVERY_LIMIT):
            try:
                terminal = self._lock.run(
                    lambda: self._close_terminal_locked(primary_error)
                )
            except BaseException as cleanup_error:  # noqa: B036 - converge state
                deferred = _remember_first_cleanup_error(deferred, cleanup_error)
                continue
            if terminal:
                if deferred is not None:
                    _annotate_error(
                        primary_error,
                        "terminal receipt owner cleanup was interrupted",
                        deferred,
                    )
                return
        convergence_error = RuntimeError(
            "terminal receipt owner cleanup did not converge"
        )
        deferred = _remember_first_cleanup_error(deferred, convergence_error)
        _annotate_error(
            primary_error,
            "terminal receipt owner cleanup also failed",
            deferred,
        )

    def _close_terminal_locked(self, primary_error: BaseException) -> bool:
        self._normalize_closed_locked()
        if isinstance(self._slot, PublishedFileReceipt):
            receipt = self._slot
            receipt._descriptor_owner.close_after_error(primary_error)
            if receipt.closed and self._slot is receipt:
                self._slot = _RECEIPT_OWNER_CLOSED
            return receipt.closed
        if self._slot is not _RECEIPT_OWNER_CLOSED:
            self._slot = _RECEIPT_OWNER_CLOSED
        return self._slot is _RECEIPT_OWNER_CLOSED

    def _normalize_closed_locked(self) -> None:
        if isinstance(self._slot, PublishedFileReceipt) and self._slot.closed:
            self._slot = _RECEIPT_OWNER_CLOSED

    def _state_locked(self) -> str:
        if self._slot is _RECEIPT_OWNER_EMPTY:
            return "empty"
        if self._slot is _RECEIPT_OWNER_CLOSED:
            return "closed"
        if isinstance(self._slot, PublishedFileReceipt):
            return "active"
        return "reserved"

    def _require_owner_pid(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError(
                "published file receipt owner cannot cross a PID boundary"
            )

    def __enter__(self) -> PublishedFileReceiptOwner:
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


class OwnedFilePublicationAuthority:
    """Linear, PID-bound authority for producing and publishing one file."""

    __slots__ = (
        "destination",
        "max_bytes",
        "mode",
        "_authority_owner",
        "_stage_owner",
        "_owner_pid",
        "_stage_name",
        "_state",
        "_record",
        "_stage_identity",
        "_renamed",
    )

    def __init__(
        self,
        destination: Path,
        *,
        max_bytes: int,
        mode: int = 0o644,
        parent_resource: int | None = None,
    ) -> None:
        self.destination = _atomic.lexical_directory_path(Path(destination))
        _atomic._simple_child_name(
            self.destination.name,
            label="owned file destination",
        )
        self.max_bytes = _bounded_size(max_bytes, label="publication byte limit")
        self.mode = _file_mode(mode)
        self._authority_owner = _PublicationAuthorityOwner()
        self._stage_owner = _DescriptorOwner()
        self._owner_pid = os.getpid()
        self._stage_name = ""
        self._state = "opening"
        self._record: PublishedFileRecord | None = None
        self._stage_identity: tuple[int, ...] | None = None
        self._renamed = False

        try:
            require_owned_file_publication_support()
        except BaseException:
            self._state = "closed"
            raise

        try:
            if parent_resource is None:
                self._authority_owner.acquire(self.destination.parent)
            else:
                self._authority_owner.acquire(
                    self.destination.parent,
                    parent_resource=parent_resource,
                )
            self._stage_name = self._create_stage()
            self._state = "staged"
        except BaseException as primary_error:
            self._state = "failed"
            self._stage_owner.close_after_error(primary_error)
            self._authority_owner.close_after_error(primary_error)
            raise

    @property
    def _authority(self) -> _atomic._PublicationAuthority | None:
        return self._authority_owner.authority

    @property
    def _descriptor(self) -> int:
        return self._stage_owner.descriptor

    @property
    def state(self) -> str:
        return self._state

    @property
    def orphan(self) -> OwnedFileOrphan | None:
        if (
            self._record is None
            or self._stage_identity is None
            or not self._stage_name
            or self._renamed
        ):
            return None
        assert self._authority is not None
        return OwnedFileOrphan(
            path=self.destination.parent / self._stage_name,
            parent_identity=self._authority.identity,
            file_identity=self._stage_identity,
            record=self._record,
        )

    def write(self, chunks: Iterable[bytes] | bytes) -> PublishedFileRecord:
        """Consume one bounded producer exactly once, then flush the file.

        A closeable iterator is always closed.  If iteration already failed, a
        close failure is attached as a note and the iteration error remains the
        primary exception.
        """

        self._require_state("staged")
        self._state = "writing"
        iterator: Iterator[bytes] | None = None
        primary_error: BaseException | None = None
        digest = hashlib.sha256()
        size = 0
        try:
            iterator = iter((chunks,) if isinstance(chunks, bytes) else chunks)
            for block in iterator:
                if not isinstance(block, bytes):
                    raise TypeError("publication producer must yield bytes")
                if len(block) > self.max_bytes - size:
                    raise ValueError("publication producer exceeds its byte limit")
                view = memoryview(block)
                while view:
                    written = os.write(self._descriptor, view)
                    if written <= 0:
                        raise RuntimeError("publication write made no progress")
                    digest.update(view[:written])
                    size += written
                    view = view[written:]
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            self._record = PublishedFileRecord(
                size=size,
                sha256=digest.hexdigest(),
                mode=self.mode,
            )
            if iterator is not None:
                close = getattr(iterator, "close", None)
                if callable(close):
                    try:
                        close()
                    except BaseException as close_error:
                        if primary_error is None:
                            self._state = "failed"
                            raise
                        _annotate_error(
                            primary_error,
                            "publication iterator cleanup also failed",
                            close_error,
                        )
            if primary_error is not None:
                self._state = "failed"

        record = PublishedFileRecord(
            size=size,
            sha256=digest.hexdigest(),
            mode=self.mode,
        )
        self._record = record
        try:
            self._bind_stage(record)
            os.fsync(self._descriptor)
            self._bind_stage(record)
        except BaseException:
            self._state = "failed"
            raise
        self._state = "written"
        return record

    def publish_into(self, receipt_owner: PublishedFileReceiptOwner) -> None:
        """Publish and install the receipt in a caller-precreated owner.

        The owner is reserved before any rename.  Installation is complete
        before this method returns, so even an asynchronous exception at the
        caller's first bytecode cannot lose the descriptor-owning object.
        """

        self._require_state("written")
        if not isinstance(receipt_owner, PublishedFileReceiptOwner):
            raise TypeError("receipt_owner must be a PublishedFileReceiptOwner")

        reservation = object()
        receipt_descriptor_owner = _DescriptorOwner()
        try:
            self._state = "publishing"
            receipt_owner._reserve(reservation)
            assert self._authority is not None
            assert self._record is not None
            self._bind_stage(self._record)
            self._authenticate_descriptor(
                self._descriptor,
                expected=self._record,
                identity=self._stage_identity,
                label="owned file stage",
            )
            self._authority.verify_path_binding()

            try:
                self._authority.rename_noreplace(
                    self._stage_name,
                    self.destination.name,
                )
                renamed = True
            except FileExistsError:
                renamed = False
            except BaseException as rename_error:
                # The syscall may have committed before an injected exception.
                # That outcome is recorded only to avoid mislabelling a final
                # name as an orphan.  An ambiguous operation never installs a
                # receipt; a fresh call must authenticate the existing file.
                self._state = "failed"
                self._renamed = True
                try:
                    self._renamed = self._rename_committed()
                except BaseException as reconciliation_error:  # noqa: B036
                    _annotate_error(
                        rename_error,
                        "rename outcome reconciliation also failed",
                        reconciliation_error,
                    )
                raise
            self._renamed = renamed

            if renamed:
                metadata = self._authenticate_descriptor(
                    self._descriptor,
                    expected=self._record,
                    identity=self._stage_identity,
                    label="published file",
                )
                receipt_descriptor = receipt_descriptor_owner.duplicate(
                    self._descriptor
                )
                receipt_metadata = os.fstat(receipt_descriptor)
                receipt_descriptor_owner.bind_identity(receipt_metadata)
                if _file_version_identity(receipt_metadata) != _file_version_identity(
                    metadata
                ):
                    raise RuntimeError("published receipt descriptor changed")
                os.set_inheritable(receipt_descriptor, False)
                orphan = None
                receipt_record = self._record
            else:
                try:
                    metadata, observed = self._authenticate_existing(
                        receipt_descriptor_owner
                    )
                except ValueError as exc:
                    orphan = self.orphan
                    assert orphan is not None
                    self._state = "failed"
                    raise OwnedFileConflictError(
                        self.destination,
                        self._record,
                        None,
                        orphan,
                    ) from exc
                if (
                    observed.mode != self._record.mode
                    or observed.size != self._record.size
                    or observed.sha256 != self._record.sha256
                ):
                    orphan = self.orphan
                    assert orphan is not None
                    self._state = "failed"
                    raise OwnedFileConflictError(
                        self.destination,
                        self._record,
                        observed,
                        orphan,
                    )
                orphan = self.orphan
                receipt_record = observed

            os.fsync(self._authority.resource)
            self._authority.verify_path_binding()
            rebound = os.stat(
                self.destination.name,
                dir_fd=self._authority.resource,
                follow_symlinks=False,
            )
            if _file_version_identity(rebound) != _file_version_identity(metadata):
                raise RuntimeError("published file binding changed before receipt")
            self._close_stage_descriptor()
            parent_identity = self._authority.identity
            self._close_parent_authority()
            receipt = PublishedFileReceipt(
                path=self.destination,
                record=receipt_record,
                file_identity=_file_identity(metadata),
                version_identity=_file_version_identity(metadata),
                parent_identity=parent_identity,
                reused=not renamed,
                orphan=orphan,
                descriptor_owner=receipt_descriptor_owner,
            )
            receipt_owner._install(reservation, receipt)
            self._state = "published"
            self._stage_name = ""
            return None
        except BaseException as primary_error:
            try:
                installed = receipt_owner._owns_descriptor_owner(
                    receipt_descriptor_owner
                )
            except BaseException as ownership_error:  # noqa: B036
                _annotate_error(
                    primary_error,
                    "receipt ownership reconciliation also failed",
                    ownership_error,
                )
                installed = False
            if installed:
                receipt_owner.close_after_error(primary_error)
            else:
                receipt_descriptor_owner.close_after_error(primary_error)
                try:
                    receipt_owner._cancel_reservation(reservation)
                except BaseException as reservation_error:  # noqa: B036
                    _annotate_error(
                        primary_error,
                        "receipt reservation cleanup also failed",
                        reservation_error,
                    )
            if self._state != "failed":
                self._state = "failed"
            raise

    def publish(self, *, receipt_owner: PublishedFileReceiptOwner) -> None:
        """Compatibility spelling for :meth:`publish_into`, with strict owner."""

        self.publish_into(receipt_owner)

    def close(self) -> None:
        if self._state == "closed":
            return
        owner_changed = os.getpid() != self._owner_pid
        close_error: BaseException | None = None
        if self._stage_owner.descriptor >= 0:
            try:
                self._close_stage_descriptor()
            except BaseException as exc:  # noqa: B036 - finish all cleanup
                close_error = exc
        if self._authority_owner.authority is not None:
            try:
                self._close_parent_authority()
            except BaseException as exc:  # noqa: B036 - finish all cleanup
                if close_error is None:
                    close_error = exc
                else:
                    _annotate_error(
                        close_error,
                        "parent authority close also failed",
                        exc,
                    )
        cleanup_complete = (
            self._stage_owner.descriptor < 0 and self._authority_owner.authority is None
        )
        if self._state != "published" and cleanup_complete:
            self._state = "closed"
        elif not cleanup_complete:
            self._state = "close-failed"
        if owner_changed:
            raise RuntimeError(
                "owned file publication authority cannot cross a PID boundary"
            ) from close_error
        if close_error is not None:
            raise close_error

    def _create_stage(self) -> str:
        assert self._authority is not None
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        destination_digest = hashlib.sha256(
            os.fsencode(self.destination.name)
        ).hexdigest()[:16]
        for _attempt in range(_MAX_STAGE_ATTEMPTS):
            name = f".codenib-file-{destination_digest}-{secrets.token_hex(16)}"
            _atomic._simple_child_name(name, label="owned file stage")
            try:
                try:
                    descriptor = self._stage_owner.open(
                        name,
                        flags,
                        0o600,
                        dir_fd=self._authority.resource,
                    )
                except FileExistsError:
                    continue
                created = os.fstat(descriptor)
                self._stage_owner.bind_identity(created)
                os.fchmod(descriptor, self.mode)
                metadata = os.fstat(descriptor)
                _require_new_regular(metadata, mode=self.mode)
                self._stage_owner.bind_identity(metadata)
                self._stage_identity = _file_identity(metadata)
                return name
            except BaseException as primary_error:
                self._stage_owner.close_after_error(primary_error)
                raise
        raise RuntimeError("could not reserve a bounded owned file stage")

    def _bind_stage(self, record: PublishedFileRecord) -> None:
        assert self._authority is not None
        descriptor_metadata = os.fstat(self._descriptor)
        _require_exact_regular(
            descriptor_metadata,
            identity=self._stage_identity,
            version_identity=None,
            mode=record.mode,
            size=record.size,
            label="owned file stage",
        )
        try:
            bound = os.stat(
                self._stage_name,
                dir_fd=self._authority.resource,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "owned file stage namespace binding disappeared"
            ) from exc
        _require_exact_regular(
            bound,
            identity=self._stage_identity,
            version_identity=None,
            mode=record.mode,
            size=record.size,
            label="owned file stage binding",
        )

    def _open_regular(
        self,
        name: str,
        *,
        label: str,
        owner: _DescriptorOwner,
    ) -> os.stat_result:
        assert self._authority is not None
        try:
            before = os.stat(
                name,
                dir_fd=self._authority.resource,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"{label} disappeared") from exc
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError(f"{label} must be an unlinked regular file")
        flags = (
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = owner.open(name, flags, dir_fd=self._authority.resource)
            opened = os.fstat(descriptor)
            owner.bind_identity(opened)
            if _file_identity(opened) != _file_identity(before):
                raise RuntimeError(f"{label} changed while it was opened")
            _require_new_regular(opened, mode=stat.S_IMODE(opened.st_mode))
            return opened
        except BaseException as primary_error:
            owner.close_after_error(primary_error)
            raise

    def _authenticate_existing(
        self,
        owner: _DescriptorOwner,
    ) -> tuple[os.stat_result, PublishedFileRecord]:
        assert self._authority is not None
        before = self._open_regular(
            self.destination.name,
            label="existing publication",
            owner=owner,
        )
        descriptor = owner.descriptor
        try:
            if before.st_size < 0 or before.st_size > self.max_bytes:
                assert self._record is not None
                orphan = self.orphan
                assert orphan is not None
                raise OwnedFileConflictError(
                    self.destination,
                    self._record,
                    None,
                    orphan,
                )
            digest = hashlib.sha256()
            offset = 0
            while offset < before.st_size:
                block = os.pread(
                    descriptor,
                    min(_COPY_BYTES, before.st_size - offset),
                    offset,
                )
                if not block:
                    raise RuntimeError("existing publication was truncated")
                digest.update(block)
                offset += len(block)
            if os.pread(descriptor, 1, offset):
                raise RuntimeError("existing publication grew")
            after = os.fstat(descriptor)
            if _file_version_identity(after) != _file_version_identity(before):
                raise RuntimeError("existing publication changed while authenticated")
            observed_mode = _file_mode(stat.S_IMODE(after.st_mode))
            rebound = os.stat(
                self.destination.name,
                dir_fd=self._authority.resource,
                follow_symlinks=False,
            )
            if _file_version_identity(rebound) != _file_version_identity(after):
                raise RuntimeError("existing publication binding changed")
            return (
                after,
                PublishedFileRecord(
                    size=after.st_size,
                    sha256=digest.hexdigest(),
                    mode=observed_mode,
                ),
            )
        except BaseException as primary_error:
            owner.close_after_error(primary_error)
            raise

    def _authenticate_descriptor(
        self,
        descriptor: int,
        *,
        expected: PublishedFileRecord,
        identity: tuple[int, ...] | None,
        label: str,
    ) -> os.stat_result:
        before = os.fstat(descriptor)
        _require_exact_regular(
            before,
            identity=identity,
            version_identity=None,
            mode=expected.mode,
            size=expected.size,
            label=label,
        )
        digest = hashlib.sha256()
        offset = 0
        while offset < expected.size:
            block = os.pread(
                descriptor,
                min(_COPY_BYTES, expected.size - offset),
                offset,
            )
            if not block:
                raise RuntimeError(f"{label} was truncated")
            digest.update(block)
            offset += len(block)
        if os.pread(descriptor, 1, offset):
            raise RuntimeError(f"{label} grew")
        after = os.fstat(descriptor)
        if _file_version_identity(after) != _file_version_identity(before):
            raise RuntimeError(f"{label} changed while authenticated")
        if digest.hexdigest() != expected.sha256:
            raise RuntimeError(f"{label} bytes differ from the producer digest")
        return after

    def _rename_committed(self) -> bool:
        assert self._authority is not None
        try:
            source = os.stat(
                self._stage_name,
                dir_fd=self._authority.resource,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            source = None
        try:
            destination = os.stat(
                self.destination.name,
                dir_fd=self._authority.resource,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            destination = None
        return (
            source is None
            and destination is not None
            and _file_identity(destination) == self._stage_identity
        )

    def _close_stage_descriptor(self) -> None:
        assert self._stage_identity is not None
        self._stage_owner.close(expected_identity=self._stage_identity)

    def _close_parent_authority(self) -> None:
        assert self._authority_owner.authority is not None
        self._authority_owner.close()

    def _require_state(self, expected: str) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError(
                "owned file publication authority cannot cross a PID boundary"
            )
        if self._state != expected:
            raise RuntimeError(
                f"owned file publication is {self._state}, expected {expected}"
            )

    def __enter__(self) -> OwnedFilePublicationAuthority:
        self._require_state("staged")
        return self

    def __exit__(
        self,
        _exc_type: object,
        exc: BaseException | None,
        _traceback: object,
    ) -> None:
        try:
            self.close()
        except BaseException as close_error:
            if exc is None:
                raise
            _annotate_error(exc, "publication cleanup also failed", close_error)


def publish_owned_file(
    destination: Path,
    chunks: Iterable[bytes] | bytes,
    *,
    max_bytes: int,
    mode: int = 0o644,
    parent_resource: int | None = None,
    receipt_owner: PublishedFileReceiptOwner | None = None,
    consume: Callable[[PublishedFileReceipt], object] | None = None,
) -> None:
    """Publish into a caller owner or synchronously consume a borrowed receipt.

    The receipt retains the exact authenticated handle, but a same-UID process
    can still mutate a normal writable filesystem after publication.  A
    consumer-only receipt is valid solely for the synchronous callback and is
    terminally cleaned before this helper returns or propagates an exception.
    """

    if (receipt_owner is None) == (consume is None):
        raise TypeError("provide exactly one of receipt_owner or consume")
    if receipt_owner is not None and not isinstance(
        receipt_owner, PublishedFileReceiptOwner
    ):
        raise TypeError("receipt_owner must be a PublishedFileReceiptOwner")
    if consume is not None and not callable(consume):
        raise TypeError("published file receipt consumer must be callable")

    if receipt_owner is not None:
        with OwnedFilePublicationAuthority(
            destination,
            max_bytes=max_bytes,
            mode=mode,
            parent_resource=parent_resource,
        ) as publication:
            publication.write(chunks)
            publication.publish_into(receipt_owner)
        return None

    local_owner = PublishedFileReceiptOwner()
    try:
        with OwnedFilePublicationAuthority(
            destination,
            max_bytes=max_bytes,
            mode=mode,
            parent_resource=parent_resource,
        ) as publication:
            publication.write(chunks)
            publication.publish_into(local_owner)
        assert consume is not None
        local_owner.consume(consume)
    except BaseException as primary_error:
        try:
            local_owner._close_terminal_after_error(primary_error)
        except BaseException as cleanup_error:  # noqa: B036 - keep primary
            _annotate_error(
                primary_error,
                "helper-local receipt owner cleanup also failed",
                cleanup_error,
            )
        raise
    try:
        local_owner.close()
    except BaseException as close_error:
        try:
            local_owner._close_terminal_after_error(close_error)
        except BaseException as cleanup_error:  # noqa: B036 - keep close error
            _annotate_error(
                close_error,
                "helper-local terminal cleanup also failed",
                cleanup_error,
            )
        raise
    return None


def _bounded_size(value: int, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < 0 or value > (64 << 30):
        raise ValueError(f"{label} is out of bounds")
    return value


def _file_mode(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("publication mode must be an integer")
    if value < 0 or value > 0o777 or value & 0o022:
        raise ValueError("publication mode must be a non-writable-group POSIX mode")
    return value


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _file_version_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _annotate_error(
    primary_error: BaseException,
    label: str,
    cleanup_error: BaseException,
) -> None:
    message = f"{label}: {cleanup_error!r}"
    if hasattr(primary_error, "add_note"):
        primary_error.add_note(message)
        return
    notes = list(getattr(primary_error, "_codenib_cleanup_notes", ()))
    notes.append(message)
    primary_error._codenib_cleanup_notes = tuple(notes)  # type: ignore[attr-defined]
    if primary_error.__cause__ is None:
        primary_error.__cause__ = cleanup_error


def _remember_first_cleanup_error(
    deferred: BaseException | None,
    failure: BaseException,
) -> BaseException:
    if deferred is None:
        return failure
    if failure is not deferred:
        _annotate_error(deferred, "additional cleanup interruption", failure)
    return deferred


def _arm_descriptor_for_close(
    descriptor: int,
    *,
    cookie_owner: _DescriptorOwner | None = None,
) -> int:
    """Place an unguessable cookie on this open-file description before close."""

    cookie = _CLOSE_COOKIE_FLOOR + secrets.randbelow(_CLOSE_COOKIE_SPAN)
    observed = os.lseek(descriptor, cookie, os.SEEK_SET)
    if observed != cookie or os.lseek(descriptor, 0, os.SEEK_CUR) != cookie:
        raise RuntimeError("could not arm descriptor close ownership")
    if cookie_owner is not None:
        if cookie_owner.descriptor != descriptor:
            raise RuntimeError("descriptor close-cookie owner changed")
        # Install before returning so interruption at the caller's first
        # bytecode still leaves a proof for any later retry.
        cookie_owner._close_cookie = cookie
    return cookie


def _descriptor_still_exact(
    descriptor: int,
    identity: tuple[int, ...],
    *,
    close_cookie: int | None,
) -> bool:
    """Best-effort recognition of the pre-close open-file description.

    A same-process ``dup2`` of an alias sharing both inode and file offset is an
    intentionally documented, unprovable ABA boundary.
    """

    if close_cookie is None:
        return False

    try:
        metadata = os.fstat(descriptor)
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return False
        raise
    if _file_identity(metadata) != identity:
        return False
    try:
        return os.lseek(descriptor, 0, os.SEEK_CUR) == close_cookie
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return False
        raise


def _finish_close_after_error(
    descriptor: int,
    identity: tuple[int, ...],
    close_error: BaseException,
    *,
    close_cookie: int | None,
) -> bool:
    """Finish an ambiguous close unless the descriptor is observably reused."""

    try:
        still_exact = _descriptor_still_exact(
            descriptor,
            identity,
            close_cookie=close_cookie,
        )
    except BaseException as reconciliation_error:  # noqa: B036 - keep close error
        _annotate_error(
            close_error,
            "descriptor close reconciliation failed",
            reconciliation_error,
        )
        return False
    if not still_exact:
        return True
    try:
        _REAL_OS_CLOSE(descriptor)
    except BaseException as cleanup_error:  # noqa: B036 - keep close error
        _annotate_error(
            close_error,
            "descriptor fallback close failed",
            cleanup_error,
        )
        try:
            return not _descriptor_still_exact(
                descriptor,
                identity,
                close_cookie=close_cookie,
            )
        except BaseException as reconciliation_error:  # noqa: B036 - keep primary
            _annotate_error(
                close_error,
                "descriptor fallback reconciliation failed",
                reconciliation_error,
            )
            return False
    return True


def _cleanup_descriptor_after_error(
    descriptor: int,
    identity: tuple[int, ...] | None,
    primary_error: BaseException,
    *,
    cookie_owner: _DescriptorOwner | None = None,
) -> bool:
    if identity is None:
        return _cleanup_untransferred_descriptor(descriptor, primary_error)
    close_cookie: int | None = None
    try:
        close_cookie = _arm_descriptor_for_close(
            descriptor,
            cookie_owner=cookie_owner,
        )
    except BaseException as ownership_error:  # noqa: B036 - keep primary error
        _annotate_error(
            primary_error,
            "descriptor cleanup ownership check failed",
            ownership_error,
        )
    try:
        os.close(descriptor)
    except BaseException as close_error:  # noqa: B036 - keep primary error
        closed = _finish_close_after_error(
            descriptor,
            identity,
            close_error,
            close_cookie=close_cookie,
        )
        _annotate_error(primary_error, "descriptor cleanup also failed", close_error)
        if not closed:
            _annotate_error(
                primary_error,
                "owned descriptor close outcome remains unknown",
                close_error,
            )
        return closed
    return True


def _cleanup_untransferred_descriptor(
    descriptor: int,
    primary_error: BaseException,
) -> bool:
    try:
        metadata = os.fstat(descriptor)
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return True
        _annotate_error(primary_error, "descriptor cleanup identity failed", exc)
        try:
            os.close(descriptor)
        except BaseException as close_error:  # noqa: B036 - keep primary error
            _annotate_error(
                primary_error,
                "unidentified descriptor cleanup also failed",
                close_error,
            )
            return False
        return True
    except BaseException as identity_error:  # noqa: B036 - keep primary error
        _annotate_error(
            primary_error,
            "descriptor cleanup identity was interrupted",
            identity_error,
        )
        try:
            os.close(descriptor)
        except BaseException as close_error:  # noqa: B036 - keep primary error
            _annotate_error(
                primary_error,
                "unidentified descriptor cleanup also failed",
                close_error,
            )
            return False
        return True
    return _cleanup_descriptor_after_error(
        descriptor,
        _file_identity(metadata),
        primary_error,
    )


def _close_publication_authority(
    authority: _atomic._PublicationAuthority,
) -> None:
    """Close a shared authority while retaining the first BaseException."""

    resource = authority._resource
    resource_identity: tuple[int, ...] | None = None
    close_cookie: int | None = None
    primary_error: BaseException | None = None
    try:
        resource_identity = _file_identity(os.fstat(resource))
        close_cookie = _arm_descriptor_for_close(resource)
    except BaseException as ownership_error:  # noqa: B036 - cleanup must continue
        primary_error = ownership_error

    try:
        authority.close()
    except BaseException as close_error:  # noqa: B036 - finish all cleanup
        if primary_error is None:
            primary_error = close_error
        else:
            _annotate_error(
                primary_error,
                "parent authority close also failed",
                close_error,
            )
        try:
            # The callback owns the complete lexical walk.  It is deliberately
            # resumed after an interrupt even though the authority is closed.
            authority._close_callback(resource)
        except BaseException as cleanup_error:  # noqa: B036 - keep first error
            _annotate_error(
                primary_error,
                "parent authority cleanup also failed",
                cleanup_error,
            )

    if resource_identity is not None:
        try:
            still_open = _descriptor_still_exact(
                resource,
                resource_identity,
                close_cookie=close_cookie,
            )
        except BaseException as reconciliation_error:  # noqa: B036
            if primary_error is None:
                primary_error = reconciliation_error
            else:
                _annotate_error(
                    primary_error,
                    "parent descriptor reconciliation failed",
                    reconciliation_error,
                )
        else:
            if still_open:
                try:
                    _REAL_OS_CLOSE(resource)
                except BaseException as cleanup_error:  # noqa: B036
                    if primary_error is None:
                        primary_error = cleanup_error
                    else:
                        _annotate_error(
                            primary_error,
                            "parent descriptor cleanup failed",
                            cleanup_error,
                        )

    if primary_error is not None:
        raise primary_error


def _require_new_regular(metadata: os.stat_result, *, mode: int) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise RuntimeError("owned file must be one exact regular inode")


def _require_exact_regular(
    metadata: os.stat_result,
    *,
    identity: tuple[int, ...] | None,
    version_identity: tuple[int, ...] | None,
    mode: int,
    size: int,
    label: str,
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_size != size
        or (identity is not None and _file_identity(metadata) != identity)
        or (
            version_identity is not None
            and _file_version_identity(metadata) != version_identity
        )
    ):
        raise RuntimeError(f"{label} identity changed")


__all__ = [
    "OwnedFileConflictError",
    "OwnedFileOrphan",
    "OwnedFilePublicationAuthority",
    "PublishedFileReceipt",
    "PublishedFileReceiptOwner",
    "PublishedFileRecord",
    "publish_owned_file",
    "require_owned_file_publication_support",
]
