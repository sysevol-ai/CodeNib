# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic content identity for repository files visible to CodeNib."""

from __future__ import annotations

import errno
import hashlib
import ntpath
import os
import re
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from ._contained_source import (
    SECURE_CONTAINED_SYMLINKS,
    _attach_source_cleanup_owner,
    _binding_identity,
    _identity_is_reliable,
    _inherit_source_cleanup_owner,
    _open_windows_pinned_repository_root,
    _resolved_repository_file_at,
    _resolved_windows_repository_file_at,
    _SourceCleanupSlot,
    _verify_windows_pinned_repository_root,
    _version_identity,
    _windows_entry_is_reparse,
    _windows_find_child,
    _windows_identity_is_reliable,
    _windows_link_target_text,
    _windows_metadata_is_reparse,
    _windows_open_child,
    _windows_version_identity,
)
from ._windows_fs_authority import (
    IO_REPARSE_TAG_SYMLINK as _WINDOWS_IO_REPARSE_TAG_SYMLINK,
)
from ._windows_fs_authority import WindowsDirectoryEntry as _WindowsDirectoryEntry
from ._windows_fs_authority import WindowsHandleMetadata as _WindowsHandleMetadata
from ._windows_fs_authority import WindowsKernelApi as _WindowsKernelApi
from ._windows_fs_authority import (
    _WindowsHandleCleanup,
)
from .repository_filters import (
    REPOSITORY_FILTER_POLICY_VERSION,
    repository_path_is_visible,
)

SOURCE_FINGERPRINT_VERSION = 2

_SOURCE_FINGERPRINT_V1_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_FINGERPRINT_V2_RE = re.compile(r"^sha256-v2:[0-9a-f]{64}$")
_V2_MAGIC = b"codenib-source-fingerprint\x00\x02"
_MAX_FRAME_BYTES = (1 << 64) - 1
_MAX_SOURCE_COMPONENTS = 256
_MAX_SOURCE_PATH_BYTES = 4_096
_MAX_SOURCE_ENTRIES = 500_000
_MAX_SOURCE_METADATA_BYTES = 128 * 1024 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class RepositoryChangedError(RuntimeError):
    """Raised when repository contents change while they are being inspected."""


def _remember_interruption(
    deferred: BaseException | None,
    interruption: BaseException,
) -> BaseException:
    return deferred if deferred is not None else interruption


class _SourceLifecycleRLock:
    """RLock with an owner-depth token visible after interrupted acquisition."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._depth = threading.local()

    def depth(self) -> int:
        return int(getattr(self._depth, "value", 0))

    def _set_depth(self, depth: int) -> None:
        self._depth.value = depth

    def _native_is_owned(self) -> bool:
        runtime: Any = self._lock
        ownership_check = getattr(runtime, "_is_owned", None)
        if not callable(ownership_check):
            raise RuntimeError("source lifecycle lock lacks owner tracking")
        return bool(ownership_check())

    def acquire(self) -> bool:
        depth = self.depth()
        if depth:
            self._set_depth(depth + 1)
            return True
        acquired = self._lock.acquire()
        if acquired:
            self._set_depth(1)
        return acquired

    def release(self) -> None:
        depth = self.depth()
        if depth < 1 or not self._native_is_owned():
            raise RuntimeError("cannot release an unowned source lifecycle lock")
        if depth > 1:
            self._set_depth(depth - 1)
            return
        self._lock.release()
        self._set_depth(0)


_SOURCE_LOCK_RECOVERY_LIMIT = 64


def _defer_source_lock_failure(
    deferred: BaseException | None,
    failure: BaseException,
) -> BaseException:
    """Retain the first lock interruption while reconciliation completes."""

    return _remember_interruption(deferred, failure)


def _acquire_source_lock(
    lock: _SourceLifecycleRLock,
    baseline: int,
    deferred: BaseException | None = None,
) -> BaseException | None:
    """Acquire exactly one logical lease without repeating a native acquire.

    Once a native acquire may have run, ownership is reconciled before another
    acquire is attempted.  This prevents a second cancellation in the recovery
    probe from adding an untracked native RLock recursion level.
    """

    for _attempt in range(_SOURCE_LOCK_RECOVERY_LIMIT):
        try:
            depth = lock.depth()
            native_owned = lock._native_is_owned()
        except BaseException as exc:  # noqa: B036 - bounded reconciliation
            deferred = _defer_source_lock_failure(deferred, exc)
            continue

        if native_owned:
            if depth > baseline:
                return deferred
            try:
                # Native ownership already proves that a zero-depth acquire
                # completed, or that this is a reentrant logical acquisition.
                # Publish the one logical level without touching the RLock.
                lock._set_depth(baseline + 1)
            except BaseException as exc:  # noqa: B036 - confirm publication
                deferred = _defer_source_lock_failure(deferred, exc)
                continue
            return deferred

        if depth > baseline:
            try:
                # A prior attempt changed only the logical token.  Roll that
                # unowned token back before attempting the native lock.
                lock._set_depth(baseline)
            except BaseException as exc:  # noqa: B036 - confirm rollback
                deferred = _defer_source_lock_failure(deferred, exc)
                continue

        try:
            if lock.acquire():
                return deferred
        except BaseException as exc:  # noqa: B036 - reconcile before retry
            deferred = _defer_source_lock_failure(deferred, exc)

    if deferred is not None:
        raise deferred
    raise RuntimeError("source lifecycle lock acquisition recovery did not converge")


def _release_source_lock(
    lock: _SourceLifecycleRLock,
    baseline: int,
    deferred: BaseException | None = None,
) -> BaseException | None:
    """Restore the lease baseline without repeating a completed native release."""

    for _attempt in range(_SOURCE_LOCK_RECOVERY_LIMIT):
        try:
            depth = lock.depth()
            native_owned = lock._native_is_owned()
        except BaseException as exc:  # noqa: B036 - bounded reconciliation
            deferred = _defer_source_lock_failure(deferred, exc)
            continue

        if baseline:
            if not native_owned:
                # An outer logical lease must retain the collapsed native lock.
                # Reacquiring here could block behind a replacement owner, so
                # fail explicitly after making the lost-ownership state visible.
                try:
                    lock._set_depth(0)
                except BaseException as exc:  # noqa: B036 - confirm publication
                    deferred = _defer_source_lock_failure(deferred, exc)
                    continue
                return _defer_source_lock_failure(
                    deferred,
                    RuntimeError("source lifecycle lock lost its outer native owner"),
                )
            if depth != baseline:
                try:
                    lock._set_depth(baseline)
                except BaseException as exc:  # noqa: B036 - confirm publication
                    deferred = _defer_source_lock_failure(deferred, exc)
                    continue
            return deferred

        if not native_owned:
            if depth:
                try:
                    lock._set_depth(0)
                except BaseException as exc:  # noqa: B036 - confirm publication
                    deferred = _defer_source_lock_failure(deferred, exc)
                    continue
            return deferred

        if depth != 1:
            try:
                lock._set_depth(1)
            except BaseException as exc:  # noqa: B036 - confirm publication
                deferred = _defer_source_lock_failure(deferred, exc)
                continue

        try:
            lock.release()
        except BaseException as exc:  # noqa: B036 - probe before another release
            deferred = _defer_source_lock_failure(deferred, exc)

    if deferred is not None:
        raise deferred
    raise RuntimeError("source lifecycle lock release recovery did not converge")


class _SourceLockLease:
    """Pair one cancellation-safe lock acquisition with one release."""

    def __init__(self, lock: _SourceLifecycleRLock) -> None:
        self._lock = lock
        self._baseline = lock.depth()

    def __enter__(self) -> BaseException | None:
        return _acquire_source_lock(self._lock, self._baseline)

    def __exit__(
        self,
        _exc_type: object,
        exc_value: object,
        _traceback: object,
    ) -> None:
        deferred = _release_source_lock(self._lock, self._baseline)
        if deferred is not None and exc_value is None:
            raise deferred


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    """Content identity and file count for one observed repository state."""

    value: str
    file_count: int


@dataclass(frozen=True, slots=True)
class _RepositoryEntry:
    relative: str
    metadata: os.stat_result | _WindowsHandleMetadata
    link_target: str | None = None


@dataclass(frozen=True, slots=True)
class _RepositoryScan:
    entries: tuple[_RepositoryEntry, ...]
    inventory_digest: str
    inventory_entries: int


@dataclass(slots=True)
class _RepositoryScanBudget:
    entries: int = 0
    metadata_bytes: int = 0


@dataclass(frozen=True, slots=True)
class RepositorySourceFileRecord:
    """One regular source payload authenticated by a repository binding."""

    path: str
    size: int
    sha256: str
    lexical_identity: tuple[object, ...]


class _HashFanout:
    __slots__ = ("_targets",)

    def __init__(self, *targets: object) -> None:
        self._targets = targets

    def update(self, payload: bytes) -> None:
        for target in self._targets:
            target.update(payload)


class RepositorySourceBinding:
    """Retained root authority and exact records for verified live reads."""

    __slots__ = (
        "root",
        "fingerprint",
        "file_count",
        "file_records",
        "_records",
        "_inventory_digest",
        "_inventory_entries",
        "_excluded",
        "_root_descriptor",
        "_posix_authority",
        "_root_identity",
        "_windows_api",
        "_windows_authority",
        "_pid",
        "_lock",
        "_closed",
        "_poisoned",
        "_session_depth",
        "_failure_reason",
    )

    def __init__(
        self,
        *,
        root: Path,
        fingerprint: SourceFingerprint,
        file_records: Iterable[RepositorySourceFileRecord],
        inventory_digest: str,
        inventory_entries: int,
        excluded: tuple[tuple[str, ...], ...],
        root_identity: tuple[object, ...],
        root_descriptor: int = -1,
        posix_authority: object | None = None,
        windows_api: _WindowsKernelApi | None = None,
        windows_authority: object | None = None,
    ) -> None:
        records = tuple(file_records)
        self.root = root
        self.fingerprint = fingerprint.value
        self.file_count = fingerprint.file_count
        self.file_records = records
        self._records = {record.path: record for record in records}
        if len(self._records) != len(records):
            raise ValueError("repository source binding contains duplicate records")
        self._inventory_digest = inventory_digest
        self._inventory_entries = inventory_entries
        self._excluded = excluded
        self._root_descriptor = root_descriptor
        self._posix_authority = posix_authority
        self._root_identity = root_identity
        self._windows_api = windows_api
        self._windows_authority = windows_authority
        self._pid = os.getpid()
        self._lock = _SourceLifecycleRLock()
        self._closed = False
        self._poisoned = False
        self._session_depth = 0
        self._failure_reason: str | None = None

    def __enter__(self) -> RepositorySourceBinding:
        self._require_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def usable(self) -> bool:
        """Return whether this process may still attempt authenticated reads."""

        with self._state_lease():
            return not self._closed and not self._poisoned and self._pid == os.getpid()

    @property
    def closed(self) -> bool:
        """Return whether every retained filesystem authority is closed."""

        with self._state_lease():
            return self._closed

    @property
    def failure_reason(self) -> str | None:
        """Return the first authentication failure retained for diagnostics."""

        with self._state_lease():
            return self._failure_reason

    @contextmanager
    def _state_lease(self) -> Iterator[None]:
        with _SourceLockLease(self._lock) as acquisition_failure:
            if acquisition_failure is not None:
                raise acquisition_failure
            yield

    def _poison(self, reason: object) -> None:
        try:
            with _SourceLockLease(self._lock) as deferred:
                if self._failure_reason is None:
                    self._failure_reason = str(reason) or "repository source changed"
                self._poisoned = True
                try:
                    self.close()
                except BaseException as exc:  # noqa: B036 - defer cleanup
                    deferred = _remember_interruption(deferred, exc)
                if deferred is not None:
                    raise deferred
        except BaseException:  # noqa: B036 - preserve auth failure
            # Authentication failures retain the operation's original
            # exception after cleanup has visited every authority.
            pass

    def _require_open(self) -> None:
        if self._poisoned:
            raise RepositoryChangedError("repository source binding is poisoned")
        if self._closed:
            raise RuntimeError("repository source binding is closed")
        if self._pid != os.getpid():
            raise RuntimeError("repository source binding cannot cross processes")

    @contextmanager
    def _authentication_lease(self) -> Iterator[None]:
        """Hold the reentrant lock and poison on every interrupted operation."""

        try:
            with _SourceLockLease(self._lock) as acquisition_failure:
                if acquisition_failure is not None:
                    raise acquisition_failure
                yield
        except BaseException as exc:
            self._poison(exc)
            raise

    def _verify_inventory(self) -> None:
        self._require_open()
        if self._windows_authority is not None:
            api = self._windows_api
            if api is None:  # pragma: no cover - constructor invariant
                raise AssertionError("Windows repository binding has no API")
            authority = self._windows_authority
            scan = _scan_pinned_windows_repository(
                api,
                authority.handle,
                excluded=self._excluded,
                collect_entries=False,
            )
            if (
                scan.inventory_digest != self._inventory_digest
                or scan.inventory_entries != self._inventory_entries
            ):
                raise RepositoryChangedError("repository source inventory changed")
            _verify_windows_pinned_repository_root(
                api,
                authority,
                self._root_identity,
            )
            return
        scan = _scan_pinned_repository(
            self._root_descriptor,
            excluded=self._excluded,
            collect_entries=False,
        )
        if (
            scan.inventory_digest != self._inventory_digest
            or scan.inventory_entries != self._inventory_entries
        ):
            raise RepositoryChangedError("repository source inventory changed")
        authority = self._posix_authority
        if authority is None:  # pragma: no cover - constructor invariant
            raise AssertionError("POSIX repository binding has no root authority")
        _verify_pinned_repository_root(
            authority,
            self._root_identity,  # type: ignore[arg-type]
        )

    def verify_snapshot(self) -> None:
        """Revalidate the whole retained v2 inventory or poison this binding."""

        with self._authentication_lease():
            try:
                self._verify_inventory()
            except BaseException as exc:  # noqa: B036 - preserve body failure
                self._poison(exc)
                if isinstance(exc, RepositoryChangedError):
                    raise
                if not isinstance(exc, (OSError, RuntimeError, ValueError)):
                    raise
                raise RepositoryChangedError(
                    "repository source changed after it was authenticated"
                ) from exc

    @contextmanager
    def read_session(self) -> Iterator[RepositorySourceBinding]:
        """Gate exact reads with one whole-tree check per side.

        Cancellation is recovered once execution is inside this generator. The
        outer ``contextlib`` yield/enter handoff remains Python's context-manager
        protocol boundary; security-sensitive callers must use the temporary
        form ``with binding.read_session():`` and must not retain or manually
        enter the returned generator manager.
        """

        with self._authentication_lease():
            if self._session_depth == 0:
                try:
                    self._verify_inventory()
                except BaseException as exc:
                    self._poison(exc)
                    if isinstance(exc, RepositoryChangedError):
                        raise
                    if not isinstance(exc, (OSError, RuntimeError, ValueError)):
                        raise
                    raise RepositoryChangedError(
                        "repository source changed before a read session"
                    ) from exc
            self._session_depth += 1
            primary_failure: BaseException | None = None
            try:
                yield self
            except BaseException as exc:  # noqa: B036 - preserve body failure
                primary_failure = exc
            finally:
                self._session_depth -= 1
                exit_failure: BaseException | None = None
                if self._session_depth == 0 and not self._closed and not self._poisoned:
                    try:
                        self._verify_inventory()
                    except BaseException as exc:  # noqa: B036 - defer exit fault
                        self._poison(exc)
                        exit_failure = exc
                if primary_failure is not None:
                    if exit_failure is not None:
                        raise primary_failure from exit_failure
                    raise primary_failure
                if exit_failure is not None:
                    if isinstance(exit_failure, RepositoryChangedError):
                        raise exit_failure
                    if not isinstance(
                        exit_failure,
                        (OSError, RuntimeError, ValueError),
                    ):
                        raise exit_failure
                    raise RepositoryChangedError(
                        "repository source changed during a read session"
                    ) from exit_failure

    def _read_record(
        self,
        relative: str,
        record: RepositorySourceFileRecord,
        *,
        max_bytes: int,
    ) -> bytes:
        if self._windows_authority is not None:
            api = self._windows_api
            if api is None:  # pragma: no cover - constructor invariant
                raise AssertionError("Windows repository binding has no API")
            resolver = _resolved_windows_repository_file_at(
                self.root,
                self._windows_authority,
                relative,
                expected_root_identity=self._root_identity,
                expected_final_identity=record.lexical_identity,
                api=api,
            )
        else:
            resolver = _resolved_repository_file_at(
                self.root,
                self._root_descriptor,
                relative,
                expected_root_identity=self._root_identity,  # type: ignore[arg-type]
                expected_final_identity=record.lexical_identity,  # type: ignore[arg-type]
            )
        with resolver as source:
            if not source.is_regular:
                raise ValueError("source path is no longer a regular file")
            payload = source.read_bytes(max_bytes=max_bytes)
        if (
            len(payload) != record.size
            or hashlib.sha256(payload).hexdigest() != record.sha256
        ):
            raise RepositoryChangedError(
                "repository source file differs from its authenticated record"
            )
        return payload

    def read_bytes(self, relative: str, *, max_bytes: int) -> bytes:
        """Read one exact captured regular file and rebind the whole inventory."""

        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("source byte limit must be positive")
        if not isinstance(relative, str):
            raise TypeError("repository source path must be text")
        record = self._records.get(relative)
        if record is None:
            raise ValueError("source path is not in the authenticated repository")
        if record.size > max_bytes:
            raise ValueError("source file exceeds its bounded read limit")

        with self._authentication_lease():
            try:
                if self._session_depth == 0:
                    self._verify_inventory()
                payload = self._read_record(
                    relative,
                    record,
                    max_bytes=max_bytes,
                )
                if self._session_depth == 0:
                    self._verify_inventory()
                return payload
            except BaseException as exc:
                self._poison(exc)
                if isinstance(exc, RepositoryChangedError):
                    raise
                if not isinstance(exc, (OSError, RuntimeError, ValueError)):
                    raise
                raise RepositoryChangedError(
                    "repository source changed while it was being read"
                ) from exc

    def close(self) -> None:
        with _SourceLockLease(self._lock) as first_failure:
            if not self._closed:
                # Closing may finish only part of an authority chain before an
                # operational error. Never permit reads through that partially
                # dismantled authority; a later close call may still retry the
                # retained resource owners.
                self._poisoned = True
                posix_authority = self._posix_authority
                if posix_authority is not None:
                    try:
                        posix_authority.close()
                    except BaseException as exc:  # noqa: B036 - finish cleanup
                        first_failure = _remember_interruption(first_failure, exc)
                    if posix_authority.closed:
                        self._posix_authority = None
                        self._root_descriptor = -1
                authority = self._windows_authority
                if authority is not None:
                    try:
                        authority.close()
                    except BaseException as exc:  # noqa: B036 - finish cleanup
                        first_failure = _remember_interruption(first_failure, exc)
                    if authority.closed:
                        self._windows_authority = None
                self._closed = (
                    self._posix_authority is None and self._windows_authority is None
                )
            if first_failure is not None:
                raise first_failure


def source_fingerprint_version(value: object) -> int | None:
    """Return the recognized canonical source-fingerprint version."""

    if not isinstance(value, str):
        return None
    if _SOURCE_FINGERPRINT_V2_RE.fullmatch(value):
        return 2
    if _SOURCE_FINGERPRINT_V1_RE.fullmatch(value):
        return 1
    return None


def is_secure_source_fingerprint_v2(value: object) -> bool:
    """Return whether *value* is a canonical, trust-eligible v2 identity."""

    return source_fingerprint_version(value) == SOURCE_FINGERPRINT_VERSION


def is_legacy_source_fingerprint_v1(value: object) -> bool:
    """Return whether *value* is a canonical legacy diagnostic identity."""

    return source_fingerprint_version(value) == 1


def lexical_repository_path(value: str | Path) -> Path:
    """Return an absolute repository path without following replaceable links."""

    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def _lexical_windows_repository_path(value: str | Path) -> Path:
    """Return Windows lexical absolute syntax even in platform-neutral fakes."""

    expanded = os.path.expanduser(os.fspath(value))
    return Path(ntpath.abspath(expanded))


def _entry_version_identity(
    metadata: os.stat_result | _WindowsHandleMetadata,
) -> tuple[object, ...]:
    if isinstance(metadata, _WindowsHandleMetadata):
        return _windows_version_identity(metadata)
    return _version_identity(metadata)


def _update_frame_header(hasher: object, domain: bytes, payload_size: int) -> None:
    """Write one unambiguous v2 frame header before a buffered or streamed value."""

    if (
        not domain
        or len(domain) > 0xFFFF
        or payload_size < 0
        or payload_size > _MAX_FRAME_BYTES
    ):
        raise ValueError("source fingerprint frame is outside its encoding limits")
    update = getattr(hasher, "update", None)
    if not callable(update):  # pragma: no cover - internal hashlib contract
        raise TypeError("hasher must provide an update method")
    update(len(domain).to_bytes(2, "big"))
    update(domain)
    update(payload_size.to_bytes(8, "big"))


def _update_frame(hasher: object, domain: bytes, payload: bytes) -> None:
    _update_frame_header(hasher, domain, len(payload))
    hasher.update(payload)


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _excluded_relative_roots(
    root: Path,
    exclude_roots: Iterable[str | Path],
) -> tuple[tuple[str, ...], ...]:
    excluded: list[tuple[str, ...]] = []
    for value in exclude_roots:
        candidate = lexical_repository_path(value)
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        excluded.append(relative.parts)
    return tuple(excluded)


def _excluded_windows_relative_roots(
    root: Path,
    exclude_roots: Iterable[str | Path],
) -> tuple[tuple[str, ...], ...]:
    root_parts = tuple(
        part.casefold() for part in ntpath.normpath(str(root)).split("\\")
    )
    excluded: list[tuple[str, ...]] = []
    for value in exclude_roots:
        candidate = ntpath.normpath(str(_lexical_windows_repository_path(value)))
        candidate_parts = tuple(candidate.split("\\"))
        if len(candidate_parts) < len(root_parts) or any(
            observed.casefold() != expected
            for observed, expected in zip(
                candidate_parts,
                root_parts,
                strict=False,
            )
        ):
            continue
        excluded.append(candidate_parts[len(root_parts) :])
    return tuple(excluded)


def _relative_is_excluded(
    parts: tuple[str, ...],
    excluded: tuple[tuple[str, ...], ...],
) -> bool:
    return any(parts[: len(prefix)] == prefix for prefix in excluded)


def _reserve_scan_entry(
    budget: _RepositoryScanBudget,
    relative: str,
) -> None:
    encoded = os.fsencode(relative)
    if (
        len(relative.split("/")) > _MAX_SOURCE_COMPONENTS
        or len(encoded) > _MAX_SOURCE_PATH_BYTES
    ):
        raise ValueError("repository source path exceeds its structural limit")
    budget.entries += 1
    budget.metadata_bytes += 256 + len(encoded)
    if budget.entries > _MAX_SOURCE_ENTRIES:
        raise ValueError("repository source scan exceeds its entry limit")
    if budget.metadata_bytes > _MAX_SOURCE_METADATA_BYTES:
        raise ValueError("repository source scan exceeds its metadata limit")


def _reserve_link_target(
    budget: _RepositoryScanBudget,
    link_target: str,
) -> None:
    budget.metadata_bytes += len(os.fsencode(link_target))
    if budget.metadata_bytes > _MAX_SOURCE_METADATA_BYTES:
        raise ValueError("repository source scan exceeds its metadata limit")


def _update_inventory_record(
    hasher: object,
    *,
    relative: str,
    kind: bytes,
    metadata: os.stat_result | _WindowsHandleMetadata,
    link_target: str | None,
) -> None:
    _update_frame(hasher, b"inventory-kind", kind)
    _update_frame(hasher, b"inventory-path", os.fsencode(relative))
    identity = b",".join(
        repr(value).encode("ascii") for value in _entry_version_identity(metadata)
    )
    _update_frame(hasher, b"inventory-identity", identity)
    if link_target is not None:
        _update_frame(hasher, b"inventory-link-target", os.fsencode(link_target))


def _scan_pinned_repository(
    root_descriptor: int,
    *,
    excluded: tuple[tuple[str, ...], ...],
    collect_entries: bool,
) -> _RepositoryScan:
    """Enumerate one stable view through an already-open repository root."""

    inventory = hashlib.sha256()
    entries: list[_RepositoryEntry] = []
    budget = _RepositoryScanBudget()
    cleanup = _PosixPartialCleanup()

    def scan_directory(
        descriptor: int,
        parts: tuple[str, ...],
    ) -> None:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(before.st_mode)
            or _is_link_or_reparse(before)
            or not _identity_is_reliable(before)
        ):
            raise ValueError("repository source directory is not stably bound")

        names: list[str] = []
        with _owned_scan_descriptor(
            cleanup,
            lambda: os.open(".", _directory_flags(), dir_fd=descriptor),
        ) as scan_descriptor:
            with os.scandir(scan_descriptor) as children:
                for child in children:
                    child_parts = (*parts, child.name)
                    relative = "/".join(child_parts)
                    if _relative_is_excluded(child_parts, excluded) or not (
                        repository_path_is_visible(relative)
                    ):
                        continue
                    # Charge the name before retaining it. A hostile wide
                    # directory therefore stops at the first over-budget
                    # entry rather than being materialized before limits run.
                    _reserve_scan_entry(budget, relative)
                    names.append(child.name)
        names.sort()

        directories: list[tuple[str, os.stat_result, tuple[str, ...]]] = []
        for name in names:
            child_parts = (*parts, name)
            relative = "/".join(child_parts)
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not _identity_is_reliable(metadata):
                raise ValueError(
                    f"repository source entry has no reliable identity: {relative}"
                )

            link_target: str | None = None
            mode = metadata.st_mode
            if _is_link_or_reparse(metadata):
                if not stat.S_ISLNK(mode):
                    raise ValueError(
                        f"repository source contains a reparse point: {relative}"
                    )
                link_target = os.readlink(name, dir_fd=descriptor)
                _reserve_link_target(budget, link_target)
                link_after = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if _version_identity(link_after) != _version_identity(metadata):
                    raise ValueError(
                        f"repository source link changed while scanning: {relative}"
                    )
                # Never follow a source link from the inventory pass.  Safe
                # regular/unresolved/directory classification happens through
                # the pinned contained resolver in the content pass.
                kind = b"link"
            elif stat.S_ISDIR(mode):
                kind = b"directory"
            elif stat.S_ISREG(mode):
                kind = b"file"
            else:
                kind = f"special-{stat.S_IFMT(mode):o}".encode("ascii")

            _update_inventory_record(
                inventory,
                relative=relative,
                kind=kind,
                metadata=metadata,
                link_target=link_target,
            )
            if kind == b"directory":
                directories.append((name, metadata, child_parts))
            elif collect_entries and kind in {b"file", b"link"}:
                entries.append(
                    _RepositoryEntry(
                        relative=relative,
                        metadata=metadata,
                        link_target=link_target,
                    )
                )

        del names

        for name, metadata, child_parts in directories:
            with _owned_scan_descriptor(
                cleanup,
                lambda name=name, descriptor=descriptor: os.open(
                    name,
                    _directory_flags(),
                    dir_fd=descriptor,
                ),
            ) as child_descriptor:
                opened = os.fstat(child_descriptor)
                if _binding_identity(opened) != _binding_identity(metadata):
                    raise ValueError(
                        "repository source directory changed while opening: "
                        + "/".join(child_parts)
                    )
                scan_directory(child_descriptor, child_parts)
                after = os.fstat(child_descriptor)
                rebound = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if _version_identity(after) != _version_identity(
                    opened
                ) or _version_identity(rebound) != _version_identity(metadata):
                    raise ValueError(
                        "repository source directory changed while scanning: "
                        + "/".join(child_parts)
                    )

        after = os.fstat(descriptor)
        if _version_identity(after) != _version_identity(before):
            label = "/".join(parts) or "."
            raise ValueError(
                f"repository source directory changed while scanning: {label}"
            )

    scan_directory(root_descriptor, ())
    return _RepositoryScan(
        entries=tuple(entries),
        inventory_digest=inventory.hexdigest(),
        inventory_entries=budget.entries,
    )


def _windows_relative_is_excluded(
    parts: tuple[str, ...],
    excluded: tuple[tuple[str, ...], ...],
) -> bool:
    folded = tuple(part.casefold() for part in parts)
    return any(
        folded[: len(prefix)] == tuple(part.casefold() for part in prefix)
        for prefix in excluded
    )


def _iter_windows_directory(
    api: _WindowsKernelApi,
    handle: int,
) -> Iterable[_WindowsDirectoryEntry]:
    iterator = getattr(api, "iter_directory", None)
    if callable(iterator):
        return iterator(handle)
    return api.enumerate_directory(handle)


def _scan_pinned_windows_repository(
    api: _WindowsKernelApi,
    root_handle: int,
    *,
    excluded: tuple[tuple[str, ...], ...],
    collect_entries: bool,
) -> _RepositoryScan:
    """Enumerate through one retained Windows root without following reparses."""

    inventory = hashlib.sha256()
    entries: list[_RepositoryEntry] = []
    budget = _RepositoryScanBudget()
    cleanup = _WindowsHandleCleanup(api)

    def scan_directory(handle: int, parts: tuple[str, ...]) -> None:
        before = api.metadata(handle)
        if (
            not stat.S_ISDIR(before.st_mode)
            or _windows_metadata_is_reparse(before)
            or not _windows_identity_is_reliable(before)
        ):
            raise ValueError("Windows repository directory is not stably bound")

        children: list[tuple[str, _WindowsDirectoryEntry]] = []
        for child in _iter_windows_directory(api, handle):
            child_parts = (*parts, child.name)
            relative = "/".join(child_parts)
            if _windows_relative_is_excluded(child_parts, excluded) or not (
                repository_path_is_visible(relative)
            ):
                continue
            _reserve_scan_entry(budget, relative)
            children.append((child.name, child))
        children.sort(key=lambda item: item[0])

        directories: list[
            tuple[_WindowsDirectoryEntry, tuple[object, ...], tuple[str, ...]]
        ] = []
        for name, child in children:
            child_parts = (*parts, name)
            relative = "/".join(child_parts)
            with _owned_scan_handle(
                cleanup,
                lambda handle=handle, child=child: _windows_open_child(
                    api,
                    handle,
                    child,
                    cleanup=cleanup,
                ),
            ) as (child_handle, metadata):
                if metadata.st_dev != before.st_dev:
                    raise ValueError("Windows repository scan crosses a volume")
                link_target: str | None = None
                if _windows_entry_is_reparse(child):
                    if metadata.reparse_tag != _WINDOWS_IO_REPARSE_TAG_SYMLINK:
                        raise ValueError(
                            f"Windows repository contains unsupported reparse: {relative}"
                        )
                    point = api.query_reparse_point(child_handle)
                    if point.tag != _WINDOWS_IO_REPARSE_TAG_SYMLINK:
                        raise ValueError(
                            f"Windows repository reparse tag changed: {relative}"
                        )
                    link_target = _windows_link_target_text(point)
                    _reserve_link_target(budget, link_target)
                    kind = b"link"
                elif stat.S_ISDIR(metadata.st_mode):
                    kind = b"directory"
                elif stat.S_ISREG(metadata.st_mode):
                    kind = b"file"
                else:
                    kind = f"special-{stat.S_IFMT(metadata.st_mode):o}".encode("ascii")

                after = api.metadata(child_handle)
                if _windows_version_identity(after) != _windows_version_identity(
                    metadata
                ):
                    raise ValueError(
                        f"Windows repository entry changed while scanning: {relative}"
                    )
                rebound = _windows_find_child(api, handle, name)
                if rebound is None or rebound.file_id_128 != child.file_id_128:
                    raise ValueError(
                        f"Windows repository entry changed while scanning: {relative}"
                    )
                _update_inventory_record(
                    inventory,
                    relative=relative,
                    kind=kind,
                    metadata=metadata,
                    link_target=link_target,
                )
                if kind == b"directory":
                    directories.append(
                        (child, _windows_version_identity(metadata), child_parts)
                    )
                elif collect_entries and kind in {b"file", b"link"}:
                    entries.append(
                        _RepositoryEntry(
                            relative=relative,
                            metadata=metadata,
                            link_target=link_target,
                        )
                    )

        del children
        for child, expected_identity, child_parts in directories:
            with _owned_scan_handle(
                cleanup,
                lambda handle=handle, child=child: _windows_open_child(
                    api,
                    handle,
                    child,
                    cleanup=cleanup,
                ),
            ) as (child_handle, opened):
                if _windows_version_identity(opened) != expected_identity:
                    raise ValueError(
                        "Windows repository directory changed while reopening: "
                        + "/".join(child_parts)
                    )
                scan_directory(child_handle, child_parts)
                after = api.metadata(child_handle)
                rebound = _windows_find_child(api, handle, child.name)
                if (
                    _windows_version_identity(after) != expected_identity
                    or rebound is None
                    or rebound.file_id_128 != child.file_id_128
                ):
                    raise ValueError(
                        "Windows repository directory changed while scanning: "
                        + "/".join(child_parts)
                    )

        after = api.metadata(handle)
        if _windows_version_identity(after) != _windows_version_identity(before):
            label = "/".join(parts) or "."
            raise ValueError(
                f"Windows repository directory changed while scanning: {label}"
            )

    scan_directory(root_handle, ())
    return _RepositoryScan(
        entries=tuple(entries),
        inventory_digest=inventory.hexdigest(),
        inventory_entries=budget.entries,
    )


class _PosixRepositoryRootAuthority:
    __slots__ = (
        "root",
        "descriptor",
        "root_identity",
        "_resources",
        "_resource_identities",
        "_anchor_identity",
        "_bindings",
        "_closed",
    )

    def __init__(
        self,
        *,
        root: Path,
        descriptor: int,
        root_identity: tuple[int, ...],
        resources: list[int],
        resource_identities: dict[int, tuple[int, ...]],
        anchor_identity: tuple[int, ...],
        bindings: list[tuple[int, str, int, tuple[int, ...]]],
    ) -> None:
        self.root = root
        self.descriptor = descriptor
        self.root_identity = root_identity
        self._resources = resources
        self._resource_identities = resource_identities
        self._anchor_identity = anchor_identity
        self._bindings = bindings
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def verify(self, expected_root_identity: tuple[int, ...]) -> None:
        if self._closed:
            raise ValueError("repository root authority is closed")
        anchor = self._resources[0]
        if _binding_identity(os.fstat(anchor)) != self._anchor_identity:
            raise ValueError("repository root anchor changed")
        for parent, name, child, expected_binding in self._bindings:
            observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(child)
            if (
                _is_link_or_reparse(observed)
                or not stat.S_ISDIR(observed.st_mode)
                or _binding_identity(observed) != expected_binding
                or _binding_identity(opened) != expected_binding
            ):
                raise ValueError("repository root path changed")
        if _version_identity(os.fstat(self.descriptor)) != expected_root_identity:
            raise ValueError("repository root changed while it was retained")

    def close(self) -> None:
        if self._closed:
            return
        failure = _close_posix_descriptors(
            self._resources,
            self._resource_identities,
        )
        self._closed = not self._resources
        self.descriptor = -1
        if failure is not None:
            raise failure


def _close_posix_descriptors(
    descriptors: list[int],
    expected_identities: dict[int, tuple[int, ...]],
) -> BaseException | None:
    """Visit every descriptor and retain the first delayed close failure."""

    deferred: BaseException | None = None
    for descriptor in reversed(tuple(descriptors)):
        closed = False
        while True:
            try:
                observed = os.fstat(descriptor)
                expected = expected_identities.get(descriptor)
                if expected is not None and _binding_identity(observed) != expected:
                    deferred = _remember_interruption(
                        deferred,
                        RuntimeError("repository descriptor ownership changed"),
                    )
                    closed = True
                    break
            except OSError as exc:
                if exc.errno == errno.EBADF:
                    closed = True
                    break
                deferred = _remember_interruption(deferred, exc)
                break
            except BaseException as exc:  # noqa: B036 - defer cancellation
                if isinstance(exc, Exception):
                    deferred = _remember_interruption(deferred, exc)
                    break
                deferred = _remember_interruption(deferred, exc)
                continue

            expected_at_close = _binding_identity(observed)
            try:
                os.close(descriptor)
                closed = True
            except BaseException as exc:  # noqa: B036 - confirm close result
                deferred = _remember_interruption(deferred, exc)
                while True:
                    try:
                        visible = os.fstat(descriptor)
                    except OSError as probe_error:
                        if probe_error.errno == errno.EBADF:
                            closed = True
                        else:
                            deferred = _remember_interruption(
                                deferred,
                                probe_error,
                            )
                        break
                    except BaseException as probe_error:  # noqa: B036
                        if isinstance(probe_error, Exception):
                            deferred = _remember_interruption(
                                deferred,
                                probe_error,
                            )
                            break
                        deferred = _remember_interruption(deferred, probe_error)
                        continue
                    if _binding_identity(visible) != expected_at_close:
                        # The owned fd closed and its number was reused. Never
                        # close the replacement descriptor.
                        closed = True
                    break
                if closed or isinstance(exc, Exception):
                    break
                continue
            break

        if closed:
            while descriptor in descriptors:
                try:
                    descriptors.pop(descriptors.index(descriptor))
                except BaseException as exc:
                    if isinstance(exc, Exception):
                        raise
                    deferred = _remember_interruption(deferred, exc)
            while descriptor in expected_identities:
                try:
                    expected_identities.pop(descriptor, None)
                except BaseException as exc:
                    if isinstance(exc, Exception):
                        raise
                    deferred = _remember_interruption(deferred, exc)
    return deferred


class _PosixPartialCleanup:
    """Retryable owner installed before temporary descriptor acquisition."""

    __slots__ = ("descriptors", "expected_identities")

    def __init__(self) -> None:
        self.descriptors: list[int] = []
        self.expected_identities: dict[int, tuple[int, ...]] = {}

    @property
    def closed(self) -> bool:
        return not self.descriptors

    def retain(self, descriptor: int, metadata: os.stat_result | None = None) -> int:
        deferred = _append_owned_descriptor(self.descriptors, descriptor)
        if deferred is not None:
            raise deferred
        if metadata is not None:
            self.expected_identities[descriptor] = _binding_identity(metadata)
        return descriptor

    def close_descriptor(self, descriptor: int) -> None:
        selected = [descriptor] if descriptor in self.descriptors else []
        identities = {
            descriptor: self.expected_identities[descriptor]
            for descriptor in selected
            if descriptor in self.expected_identities
        }
        failure = _close_posix_descriptors(selected, identities)
        if descriptor not in selected:
            while descriptor in self.descriptors:
                self.descriptors.remove(descriptor)
            self.expected_identities.pop(descriptor, None)
        if failure is not None:
            raise failure

    def close(self) -> None:
        failure = _close_posix_descriptors(
            self.descriptors,
            self.expected_identities,
        )
        if failure is not None:
            raise failure


@contextmanager
def _owned_scan_descriptor(
    cleanup: _PosixPartialCleanup,
    operation: Callable[[], int],
) -> Iterator[int]:
    """Acquire one scan fd and preserve a primary across failed cleanup."""

    descriptor = -1
    try:
        descriptor = operation()
        cleanup.retain(descriptor)
    except BaseException as primary:  # noqa: B036 - retain partial native return
        if descriptor >= 0:
            try:
                cleanup.retain(descriptor)
            except BaseException:  # noqa: B036 - preserve acquisition primary
                pass
        cleanup_failure: BaseException | None = None
        try:
            cleanup.close()
        except BaseException as exc:  # noqa: B036
            cleanup_failure = exc
        _attach_source_cleanup_owner(primary, cleanup)
        if cleanup_failure is not None:
            raise primary from cleanup_failure
        raise
    try:
        yield descriptor
    except BaseException as primary:  # noqa: B036 - preserve scan primary
        cleanup_failure = None
        try:
            cleanup.close_descriptor(descriptor)
        except BaseException as exc:  # noqa: B036
            cleanup_failure = exc
        _attach_source_cleanup_owner(primary, cleanup)
        if cleanup_failure is not None:
            raise primary from cleanup_failure
        raise
    else:
        try:
            cleanup.close_descriptor(descriptor)
        except BaseException as cleanup_failure:  # noqa: B036
            _attach_source_cleanup_owner(cleanup_failure, cleanup)
            raise


@contextmanager
def _owned_scan_handle(
    cleanup: _WindowsHandleCleanup,
    operation: Callable[[], tuple[int, _WindowsHandleMetadata]],
) -> Iterator[tuple[int, _WindowsHandleMetadata]]:
    """Retain one scan HANDLE and preserve primary/cleanup priority."""

    try:
        handle, metadata = operation()
        cleanup.retain(handle, metadata)
    except BaseException as primary:  # noqa: B036 - helper may carry an owner
        nested = getattr(primary, "source_cleanup_owner", None)
        if nested is not None:
            _attach_source_cleanup_owner(primary, nested)
        cleanup_failure: BaseException | None = None
        try:
            cleanup.close()
        except BaseException as exc:  # noqa: B036
            cleanup_failure = exc
        _attach_source_cleanup_owner(primary, cleanup)
        if cleanup_failure is not None:
            raise primary from cleanup_failure
        raise
    try:
        yield handle, metadata
    except BaseException as primary:  # noqa: B036 - preserve scan primary
        selected = _WindowsHandleCleanup(cleanup.api)
        if handle in cleanup.handles:
            selected.handles.append(handle)
            if handle in cleanup.expected_identities:
                selected.expected_identities[handle] = cleanup.expected_identities[
                    handle
                ]
        cleanup_failure = None
        try:
            selected.close()
        except BaseException as exc:  # noqa: B036
            cleanup_failure = exc
        if selected.closed:
            while handle in cleanup.handles:
                cleanup.handles.remove(handle)
            cleanup.expected_identities.pop(handle, None)
        _attach_source_cleanup_owner(primary, cleanup)
        if cleanup_failure is not None:
            raise primary from cleanup_failure
        raise
    else:
        selected = _WindowsHandleCleanup(cleanup.api)
        if handle in cleanup.handles:
            selected.handles.append(handle)
            if handle in cleanup.expected_identities:
                selected.expected_identities[handle] = cleanup.expected_identities[
                    handle
                ]
        try:
            selected.close()
        except BaseException as cleanup_failure:  # noqa: B036
            _attach_source_cleanup_owner(cleanup_failure, cleanup)
            raise
        finally:
            if selected.closed:
                while handle in cleanup.handles:
                    cleanup.handles.remove(handle)
                cleanup.expected_identities.pop(handle, None)


def _append_owned_descriptor(
    descriptors: list[int],
    descriptor: int,
    deferred: BaseException | None = None,
) -> BaseException | None:
    while descriptor not in descriptors:
        try:
            descriptors.append(descriptor)
        except BaseException as exc:
            if isinstance(exc, Exception):
                raise
            deferred = _remember_interruption(deferred, exc)
    return deferred


def _open_owned_posix_directory(
    path: str,
    *,
    descriptors: list[int],
    dir_fd: int | None = None,
) -> int:
    """Open and register a directory fd at the first Python-owned opportunity.

    A raw integer can still be lost if arbitrary opcode injection lands between
    the native ``os.open`` return and Python's result store. Covering that
    interpreter boundary requires a native helper that returns an owning object.
    """

    descriptor = -1
    try:
        if dir_fd is None:
            descriptor = os.open(path, _directory_flags())
        else:
            descriptor = os.open(path, _directory_flags(), dir_fd=dir_fd)
        deferred = _append_owned_descriptor(descriptors, descriptor)
        if deferred is not None:
            raise deferred
        return descriptor
    except BaseException:  # noqa: B036 - commit descriptor ownership
        if descriptor >= 0:
            try:
                _append_owned_descriptor(descriptors, descriptor)
            except BaseException:  # noqa: B036 - preserve primary failure
                pass
        raise


def _open_pinned_repository_root(
    root: Path,
    *,
    cleanup_slot: _SourceCleanupSlot | None = None,
) -> _PosixRepositoryRootAuthority:
    if not (SECURE_CONTAINED_SYMLINKS and os.scandir in getattr(os, "supports_fd", ())):
        raise ValueError(
            "secure source fingerprints require directory-fd traversal support"
        )
    if not root.is_absolute() or root.anchor != os.path.sep:
        raise ValueError("repository root must be an absolute lexical path")
    cleanup = _PosixPartialCleanup()
    if cleanup_slot is not None:
        cleanup_slot.own(cleanup)
    resources = cleanup.descriptors
    resource_identities = cleanup.expected_identities
    try:
        anchor = _open_owned_posix_directory(
            root.anchor,
            descriptors=resources,
        )
        anchor_metadata = os.fstat(anchor)
        if not _identity_is_reliable(anchor_metadata):
            raise ValueError("repository root anchor has no reliable identity")
        anchor_identity = _binding_identity(anchor_metadata)
        resource_identities[anchor] = anchor_identity
        bindings: list[tuple[int, str, int, tuple[int, ...]]] = []
        descriptor = anchor
        for part in root.parts[1:]:
            before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if (
                _is_link_or_reparse(before)
                or not stat.S_ISDIR(before.st_mode)
                or not _identity_is_reliable(before)
            ):
                raise ValueError(
                    "repository root ancestors must be real directories with identity"
                )
            child = _open_owned_posix_directory(
                part,
                descriptors=resources,
                dir_fd=descriptor,
            )
            opened = os.fstat(child)
            expected_binding = _binding_identity(before)
            if _binding_identity(opened) != expected_binding:
                raise ValueError("repository root changed while it was being pinned")
            resource_identities[child] = expected_binding
            bindings.append((descriptor, part, child, expected_binding))
            descriptor = child
        root_identity = _version_identity(os.fstat(descriptor))
        authority = _PosixRepositoryRootAuthority(
            root=root,
            descriptor=descriptor,
            root_identity=root_identity,
            resources=resources,
            resource_identities=resource_identities,
            anchor_identity=anchor_identity,
            bindings=bindings,
        )
        authority.verify(root_identity)
        if cleanup_slot is not None:
            cleanup_slot.own(authority)
        return authority
    except BaseException as primary:  # noqa: B036 - preserve root failure
        cleanup_failure: BaseException | None = None
        try:
            cleanup.close()
        except BaseException as exc:  # noqa: B036 - retain retry owner
            cleanup_failure = exc
        owner = cleanup_slot or cleanup
        _attach_source_cleanup_owner(primary, owner)
        if cleanup_failure is not None:
            raise primary from cleanup_failure
        raise


def _verify_pinned_repository_root(
    authority: _PosixRepositoryRootAuthority,
    expected_identity: tuple[int, ...],
) -> None:
    authority.verify(expected_identity)


def _fingerprint_windows_repository(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path],
    version: int,
    api: _WindowsKernelApi | None = None,
    retain_binding: bool = False,
    source_owner: Callable[[object], None] | None = None,
    cleanup_slot: _SourceCleanupSlot | None = None,
) -> SourceFingerprint | RepositorySourceBinding:
    """Hash a Windows repository through one retained HANDLE authority."""

    if retain_binding and cleanup_slot is None:
        cleanup_slot = _SourceCleanupSlot()
        if source_owner is not None:
            source_owner(cleanup_slot)
    root_path = _lexical_windows_repository_path(root)
    hasher = hashlib.sha256()
    if version == 1:
        hasher.update(
            (
                "codenib-source-fingerprint:1:" f"{REPOSITORY_FILTER_POLICY_VERSION}\0"
            ).encode("ascii")
        )
    else:
        hasher.update(_V2_MAGIC)
        _update_frame(
            hasher,
            b"filter-policy-version",
            str(REPOSITORY_FILTER_POLICY_VERSION).encode("ascii"),
        )
    excluded = _excluded_windows_relative_roots(root_path, exclude_roots)
    file_count = 0
    file_records: list[RepositorySourceFileRecord] = []
    root_authority = None
    selected: _WindowsKernelApi | None = None
    completed = False
    try:
        selected, root_authority, root_identity = _open_windows_pinned_repository_root(
            root_path,
            api=api,
            cleanup_slot=cleanup_slot,
        )
        initial_scan = _scan_pinned_windows_repository(
            selected,
            root_authority.handle,
            excluded=excluded,
            collect_entries=True,
        )
        for entry in initial_scan.entries:
            metadata = entry.metadata
            if not isinstance(metadata, _WindowsHandleMetadata):
                raise AssertionError("Windows source scan returned POSIX metadata")
            relative_text = entry.relative
            relative = relative_text.encode("utf-8", errors="strict")
            path = root_path.joinpath(*relative_text.split("/"))

            if _windows_metadata_is_reparse(metadata):
                raw_target = entry.link_target
                if raw_target is None:
                    raise AssertionError("Windows source link has no captured target")
                target = raw_target.encode("utf-8", errors="strict")
                try:
                    with _resolved_windows_repository_file_at(
                        root_path,
                        root_authority,
                        relative_text,
                        expected_root_identity=root_identity,
                        expected_final_identity=_windows_version_identity(metadata),
                        api=selected,
                    ) as binding:
                        if binding.is_directory:
                            continue
                        if version == 1:
                            hasher.update(b"L\0")
                            hasher.update(relative)
                            hasher.update(b"\0")
                            hasher.update(target)
                            hasher.update(b"\0")
                        else:
                            _update_frame(hasher, b"entry-kind", b"link")
                            _update_frame(hasher, b"entry-path", relative)
                            _update_frame(hasher, b"link-target", target)
                        link_is_regular = binding.is_regular
                        if link_is_regular:
                            if not hasattr(binding, "opened_size"):
                                raise AssertionError(
                                    "Windows regular binding has no opened size"
                                )
                            if version == 1:
                                hasher.update(b"C\0")
                            else:
                                _update_frame(
                                    hasher,
                                    b"link-target-state",
                                    b"regular",
                                )
                                _update_frame_header(
                                    hasher,
                                    b"link-target-content",
                                    binding.opened_size,
                                )
                            content_digest = hashlib.sha256()
                            binding.update_hash(_HashFanout(hasher, content_digest))
                            file_records.append(
                                RepositorySourceFileRecord(
                                    path=relative_text,
                                    size=binding.opened_size,
                                    sha256=content_digest.hexdigest(),
                                    lexical_identity=_windows_version_identity(
                                        metadata
                                    ),
                                )
                            )
                        elif version != 1:
                            _update_frame(
                                hasher,
                                b"link-target-state",
                                b"unresolved",
                            )
                except (OSError, ValueError) as exc:
                    raise RepositoryChangedError(
                        "repository link target could not be read consistently: "
                        f"{path}"
                    ) from exc
                if version == 1 and link_is_regular:
                    hasher.update(b"\0")
                file_count += 1
                continue

            if not stat.S_ISREG(metadata.st_mode):
                continue
            if version == 1:
                hasher.update(b"F\0")
                hasher.update(relative)
                hasher.update(b"\0")
            else:
                _update_frame(hasher, b"entry-kind", b"file")
                _update_frame(hasher, b"entry-path", relative)
                _update_frame_header(hasher, b"file-content", metadata.st_size)
            try:
                with _resolved_windows_repository_file_at(
                    root_path,
                    root_authority,
                    relative_text,
                    expected_root_identity=root_identity,
                    expected_final_identity=_windows_version_identity(metadata),
                    api=selected,
                ) as binding:
                    if not binding.is_regular:
                        raise ValueError("Windows repository file became nonregular")
                    content_digest = hashlib.sha256()
                    binding.update_hash(_HashFanout(hasher, content_digest))
                    file_records.append(
                        RepositorySourceFileRecord(
                            path=relative_text,
                            size=metadata.st_size,
                            sha256=content_digest.hexdigest(),
                            lexical_identity=_windows_version_identity(metadata),
                        )
                    )
            except (OSError, ValueError) as exc:
                raise RepositoryChangedError(
                    f"repository file could not be read consistently: {path}"
                ) from exc
            if version == 1:
                hasher.update(b"\0")
            file_count += 1

        final_scan = _scan_pinned_windows_repository(
            selected,
            root_authority.handle,
            excluded=excluded,
            collect_entries=False,
        )
        if (
            final_scan.inventory_digest != initial_scan.inventory_digest
            or final_scan.inventory_entries != initial_scan.inventory_entries
        ):
            raise RepositoryChangedError(
                "repository entries changed while they were being fingerprinted"
            )
        _verify_windows_pinned_repository_root(
            selected,
            root_authority,
            root_identity,
        )
        completed = True
    except RepositoryChangedError as exc:
        if isinstance(exc.__cause__, BaseException):
            _inherit_source_cleanup_owner(exc, exc.__cause__)
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        failure = RepositoryChangedError(
            "repository changed while it was being fingerprinted"
        )
        _inherit_source_cleanup_owner(failure, exc)
        raise failure from exc
    finally:
        if root_authority is not None and (not retain_binding or not completed):
            active_failure = sys.exc_info()[1]
            owner = cleanup_slot or root_authority
            try:
                owner.close()  # type: ignore[attr-defined]
            except BaseException as cleanup_failure:  # noqa: B036
                if active_failure is not None:
                    _attach_source_cleanup_owner(active_failure, owner)
                    raise active_failure from cleanup_failure
                _attach_source_cleanup_owner(cleanup_failure, owner)
                raise

    if version != 1:
        _update_frame(hasher, b"entry-count", file_count.to_bytes(8, "big"))
    fingerprint = SourceFingerprint(
        value=(
            f"sha256:{hasher.hexdigest()}"
            if version == 1
            else f"sha256-v2:{hasher.hexdigest()}"
        ),
        file_count=file_count,
    )
    if retain_binding:
        if version != SOURCE_FINGERPRINT_VERSION:
            raise ValueError("only source fingerprint v2 can retain read authority")
        if selected is None or root_authority is None:  # pragma: no cover - invariant
            raise AssertionError("Windows source authority was not retained")
        retained = root_authority
        binding: RepositorySourceBinding | None = None
        try:
            binding = RepositorySourceBinding(
                root=root_path,
                fingerprint=fingerprint,
                file_records=file_records,
                inventory_digest=initial_scan.inventory_digest,
                inventory_entries=initial_scan.inventory_entries,
                excluded=excluded,
                root_identity=root_identity,
                windows_api=selected,
                windows_authority=retained,
            )
            if cleanup_slot is not None:
                cleanup_slot.own(binding)
            root_authority = None
            return binding
        except BaseException as primary:  # noqa: B036 - preserve construction
            owner = cleanup_slot or binding or retained
            cleanup_failure: BaseException | None = None
            try:
                owner.close()  # type: ignore[attr-defined]
            except BaseException as exc:  # noqa: B036 - expose retry owner
                cleanup_failure = exc
            _attach_source_cleanup_owner(primary, owner)
            if cleanup_failure is not None:
                raise primary from cleanup_failure
            raise
    return fingerprint


def _fingerprint_repository(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path],
    version: int,
    retain_binding: bool = False,
    source_owner: Callable[[object], None] | None = None,
) -> SourceFingerprint | RepositorySourceBinding:
    """Hash visible repository contents with v2 or the diagnostic v1 format."""

    if version not in {1, SOURCE_FINGERPRINT_VERSION}:
        raise ValueError(f"unsupported source fingerprint version: {version}")
    cleanup_slot: _SourceCleanupSlot | None = None
    if retain_binding:
        cleanup_slot = _SourceCleanupSlot()
        if source_owner is not None:
            source_owner(cleanup_slot)
    if sys.platform == "win32":
        return _fingerprint_windows_repository(
            root,
            exclude_roots=exclude_roots,
            version=version,
            retain_binding=retain_binding,
            cleanup_slot=cleanup_slot,
        )

    # Do not call Path.resolve() here. A reversible root-to-symlink swap during
    # resolution could permanently redirect the rest of this operation to a
    # foreign tree. The lexical path remains the rebind target for the pinned
    # no-follow descriptor throughout the operation.
    root_path = lexical_repository_path(root)

    hasher = hashlib.sha256()
    if version == 1:
        hasher.update(
            (
                "codenib-source-fingerprint:1:" f"{REPOSITORY_FILTER_POLICY_VERSION}\0"
            ).encode("ascii")
        )
    else:
        hasher.update(_V2_MAGIC)
        _update_frame(
            hasher,
            b"filter-policy-version",
            str(REPOSITORY_FILTER_POLICY_VERSION).encode("ascii"),
        )
    file_count = 0
    file_records: list[RepositorySourceFileRecord] = []
    excluded = _excluded_relative_roots(root_path, exclude_roots)
    root_descriptor = -1
    root_authority: _PosixRepositoryRootAuthority | None = None
    completed = False
    try:
        root_authority = _open_pinned_repository_root(
            root_path,
            cleanup_slot=cleanup_slot,
        )
        root_descriptor = root_authority.descriptor
        root_identity = root_authority.root_identity
        initial_scan = _scan_pinned_repository(
            root_descriptor,
            excluded=excluded,
            collect_entries=True,
        )

        for entry in initial_scan.entries:
            relative_text = entry.relative
            relative = os.fsencode(relative_text)
            initial_metadata = entry.metadata
            path = root_path.joinpath(*relative_text.split("/"))

            mode = initial_metadata.st_mode
            if stat.S_ISLNK(mode):
                raw_target = entry.link_target
                if raw_target is None:  # pragma: no cover - scan invariant
                    raise AssertionError("source link has no captured target")
                target = os.fsencode(raw_target)
                try:
                    with _resolved_repository_file_at(
                        root_path,
                        root_descriptor,
                        relative_text,
                        expected_root_identity=root_identity,
                        expected_final_identity=_version_identity(initial_metadata),
                    ) as binding:
                        if binding.is_directory:
                            # Preserve v1's directory-link exclusion and keep
                            # v2 host/container identities byte-for-byte equal.
                            continue
                        if version == 1:
                            hasher.update(b"L\0")
                            hasher.update(relative)
                            hasher.update(b"\0")
                            hasher.update(target)
                            hasher.update(b"\0")
                        else:
                            _update_frame(hasher, b"entry-kind", b"link")
                            _update_frame(hasher, b"entry-path", relative)
                            _update_frame(hasher, b"link-target", target)
                        link_is_regular = binding.is_regular
                        if link_is_regular:
                            if version == 1:
                                hasher.update(b"C\0")
                            else:
                                opened = os.fstat(binding.descriptor)
                                _update_frame(
                                    hasher,
                                    b"link-target-state",
                                    b"regular",
                                )
                                _update_frame_header(
                                    hasher,
                                    b"link-target-content",
                                    opened.st_size,
                                )
                            opened = os.fstat(binding.descriptor)
                            content_digest = hashlib.sha256()
                            binding.update_hash(_HashFanout(hasher, content_digest))
                            file_records.append(
                                RepositorySourceFileRecord(
                                    path=relative_text,
                                    size=opened.st_size,
                                    sha256=content_digest.hexdigest(),
                                    lexical_identity=_version_identity(
                                        initial_metadata
                                    ),
                                )
                            )
                        elif version != 1:
                            _update_frame(
                                hasher,
                                b"link-target-state",
                                b"unresolved",
                            )
                except (OSError, ValueError) as exc:
                    raise RepositoryChangedError(
                        "repository link target could not be read consistently: "
                        f"{path}"
                    ) from exc
                if version == 1 and link_is_regular:
                    hasher.update(b"\0")
                file_count += 1
                continue

            if not stat.S_ISREG(mode):  # pragma: no cover - scan invariant
                continue

            if version == 1:
                hasher.update(b"F\0")
                hasher.update(relative)
                hasher.update(b"\0")
            else:
                _update_frame(hasher, b"entry-kind", b"file")
                _update_frame(hasher, b"entry-path", relative)
                _update_frame_header(
                    hasher,
                    b"file-content",
                    initial_metadata.st_size,
                )
            try:
                with _resolved_repository_file_at(
                    root_path,
                    root_descriptor,
                    relative_text,
                    expected_root_identity=root_identity,
                    expected_final_identity=_version_identity(initial_metadata),
                ) as binding:
                    if not binding.is_regular:  # pragma: no cover - expected regular
                        raise ValueError("repository file became nonregular")
                    content_digest = hashlib.sha256()
                    binding.update_hash(_HashFanout(hasher, content_digest))
                    file_records.append(
                        RepositorySourceFileRecord(
                            path=relative_text,
                            size=initial_metadata.st_size,
                            sha256=content_digest.hexdigest(),
                            lexical_identity=_version_identity(initial_metadata),
                        )
                    )
            except (OSError, ValueError) as exc:
                raise RepositoryChangedError(
                    f"repository file could not be read consistently: {path}"
                ) from exc
            if version == 1:
                hasher.update(b"\0")
            file_count += 1

        final_scan = _scan_pinned_repository(
            root_descriptor,
            excluded=excluded,
            collect_entries=False,
        )
        if (
            final_scan.inventory_digest != initial_scan.inventory_digest
            or final_scan.inventory_entries != initial_scan.inventory_entries
        ):
            raise RepositoryChangedError(
                "repository entries changed while they were being fingerprinted"
            )
        _verify_pinned_repository_root(root_authority, root_identity)
        completed = True
    except RepositoryChangedError as exc:
        if isinstance(exc.__cause__, BaseException):
            _inherit_source_cleanup_owner(exc, exc.__cause__)
        raise
    except (OSError, ValueError) as exc:
        failure = RepositoryChangedError(
            "repository changed while it was being fingerprinted"
        )
        _inherit_source_cleanup_owner(failure, exc)
        raise failure from exc
    finally:
        if root_authority is not None and (not retain_binding or not completed):
            active_failure = sys.exc_info()[1]
            owner = cleanup_slot or root_authority
            try:
                owner.close()  # type: ignore[attr-defined]
            except BaseException as cleanup_failure:  # noqa: B036
                if active_failure is not None:
                    _attach_source_cleanup_owner(active_failure, owner)
                    raise active_failure from cleanup_failure
                _attach_source_cleanup_owner(cleanup_failure, owner)
                raise

    if version != 1:
        _update_frame(
            hasher,
            b"entry-count",
            file_count.to_bytes(8, "big"),
        )
    fingerprint = SourceFingerprint(
        value=(
            f"sha256:{hasher.hexdigest()}"
            if version == 1
            else f"sha256-v2:{hasher.hexdigest()}"
        ),
        file_count=file_count,
    )
    if retain_binding:
        if version != SOURCE_FINGERPRINT_VERSION:
            raise ValueError("only source fingerprint v2 can retain read authority")
        if root_authority is None:  # pragma: no cover - invariant
            raise AssertionError("POSIX source authority was not retained")
        retained_authority = root_authority
        retained_descriptor = retained_authority.descriptor
        binding: RepositorySourceBinding | None = None
        try:
            binding = RepositorySourceBinding(
                root=root_path,
                fingerprint=fingerprint,
                file_records=file_records,
                inventory_digest=initial_scan.inventory_digest,
                inventory_entries=initial_scan.inventory_entries,
                excluded=excluded,
                root_identity=root_identity,
                root_descriptor=retained_descriptor,
                posix_authority=retained_authority,
            )
            if cleanup_slot is not None:
                cleanup_slot.own(binding)
            root_authority = None
            root_descriptor = -1
            return binding
        except BaseException as primary:  # noqa: B036 - preserve construction
            owner = cleanup_slot or binding or retained_authority
            cleanup_failure: BaseException | None = None
            try:
                owner.close()  # type: ignore[attr-defined]
            except BaseException as exc:  # noqa: B036 - expose retry owner
                cleanup_failure = exc
            _attach_source_cleanup_owner(primary, owner)
            if cleanup_failure is not None:
                raise primary from cleanup_failure
            raise
    return fingerprint


def fingerprint_repository(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
) -> SourceFingerprint:
    """Hash visible paths and contents with the canonical v2 framing."""

    result = _fingerprint_repository(
        root,
        exclude_roots=exclude_roots,
        version=SOURCE_FINGERPRINT_VERSION,
    )
    if not isinstance(result, SourceFingerprint):  # pragma: no cover - invariant
        result.close()
        raise AssertionError("fingerprint unexpectedly retained source authority")
    return result


def capture_repository_source(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
    _source_owner: Callable[[object], None] | None = None,
) -> RepositorySourceBinding:
    """Capture source fingerprint v2 and retain its exact read authority.

    ``_source_owner`` is an internal handoff sink used by callers that must own
    a stable cleanup slot before acquisition and the completed binding before
    this Python frame returns.
    """

    result = _fingerprint_repository(
        root,
        exclude_roots=exclude_roots,
        version=SOURCE_FINGERPRINT_VERSION,
        retain_binding=True,
        source_owner=_source_owner,
    )
    if not isinstance(result, RepositorySourceBinding):  # pragma: no cover
        raise AssertionError("source capture did not retain repository authority")
    return result


def fingerprint_repository_v1_for_diagnostics(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
) -> SourceFingerprint:
    """Reproduce legacy v1 solely for migration diagnostics.

    V1 used delimiter concatenation and is structurally ambiguous.  Its result
    must never authorize source reads, native indexes, or artifact reuse.
    """

    result = _fingerprint_repository(root, exclude_roots=exclude_roots, version=1)
    if not isinstance(result, SourceFingerprint):  # pragma: no cover - invariant
        result.close()
        raise AssertionError("diagnostic fingerprint retained source authority")
    return result


def repository_source_is_dirty(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
) -> bool:
    """Return whether Git reports source-visible worktree changes.

    Non-Git repositories return ``True`` so callers conservatively avoid
    Git-based incremental update paths.
    """

    # Keep the same lexical-root contract as fingerprint_repository().  A
    # resolve() here would let a reversible root swap redirect Git's status
    # query and incorrectly authorize an incremental update for another tree.
    root_path = lexical_repository_path(root)
    excluded = tuple(lexical_repository_path(path) for path in exclude_roots)
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root_path),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except Exception:
        return True
    if result.returncode != 0:
        return True

    records = result.stdout.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        status_code = record[:2]
        paths = [record[3:]]
        if b"R" in status_code or b"C" in status_code:
            if index < len(records) and records[index]:
                paths.append(records[index])
                index += 1

        for encoded in paths:
            relative = Path(os.fsdecode(encoded))
            absolute = root_path / relative
            if any(absolute == path or path in absolute.parents for path in excluded):
                continue
            if repository_path_is_visible(relative):
                return True
    return False


__all__ = [
    "RepositoryChangedError",
    "RepositorySourceBinding",
    "RepositorySourceFileRecord",
    "SOURCE_FINGERPRINT_VERSION",
    "SourceFingerprint",
    "capture_repository_source",
    "fingerprint_repository",
    "fingerprint_repository_v1_for_diagnostics",
    "is_legacy_source_fingerprint_v1",
    "is_secure_source_fingerprint_v2",
    "lexical_repository_path",
    "repository_source_is_dirty",
    "source_fingerprint_version",
]
