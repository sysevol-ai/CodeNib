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
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Mapping

from ._contained_source import (
    SECURE_CONTAINED_SYMLINKS,
    _arm_or_reuse_descriptor_close_cookie,
    _attach_source_cleanup_owner,
    _binding_identity,
    _descriptor_has_close_cookie,
    _descriptor_ownership_identity,
    _discard_descriptor_close_cookie,
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
from ._windows_fs_authority import _WindowsHandleCleanup
from .repository_filters import (
    REPOSITORY_FILTER_POLICY_VERSION,
    repository_path_is_visible,
)

SOURCE_FINGERPRINT_VERSION = 2

_SOURCE_FINGERPRINT_V1_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_FINGERPRINT_V2_RE = re.compile(r"^sha256-v2:[0-9a-f]{64}$")
_SOURCE_NEWLINE_RE = re.compile(rb"[\r\n]")
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
        self._pid = os.getpid()
        self._process_states = {self._pid: (self._lock, self._depth)}

    def _refresh_after_fork(self) -> None:
        """Discard an inherited native lock whose owning thread no longer exists."""

        process_id = os.getpid()
        if process_id == self._pid:
            return
        # The child may start several threads before this inherited object is
        # first touched. CPython's single dict.setdefault call installs one
        # process-local state before its return can be interrupted, so every
        # child thread converges on the same non-inherited native lock.
        child_state = self._process_states.setdefault(
            process_id,
            (threading.RLock(), threading.local()),
        )
        self._lock, self._depth = child_state
        self._pid = process_id

    def depth(self) -> int:
        self._refresh_after_fork()
        return int(getattr(self._depth, "value", 0))

    def _set_depth(self, depth: int) -> None:
        self._depth.value = depth

    def _native_is_owned(self) -> bool:
        self._refresh_after_fork()
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
    link_target: str | None = None


@dataclass(frozen=True, slots=True)
class _RepositorySourceLinkRecord:
    """Captured state reached through one visible lexical source link."""

    path: str
    lexical_identity: tuple[object, ...]
    link_target: str
    target_state: str


@dataclass(frozen=True, slots=True)
class RepositorySourceIdentitySnapshot:
    """Detached public identity rebuilt from one retained private authority."""

    root: Path
    fingerprint: str
    file_count: int
    file_records: tuple[RepositorySourceFileRecord, ...]


class _HashFanout:
    __slots__ = ("_targets",)

    def __init__(self, *targets: object) -> None:
        self._targets = targets

    def update(self, payload: bytes) -> None:
        for target in self._targets:
            target.update(payload)


class _AuthenticatedPrefix:
    """Hash a complete source payload while retaining one bounded prefix."""

    __slots__ = ("byte_count", "digest", "prefix", "_limit")

    def __init__(self, limit: int) -> None:
        self.byte_count = 0
        self.digest = hashlib.sha256()
        self.prefix = bytearray()
        self._limit = limit

    def update(self, payload: bytes) -> None:
        self.byte_count += len(payload)
        self.digest.update(payload)
        remaining = self._limit - len(self.prefix)
        if remaining > 0:
            self.prefix.extend(payload[:remaining])


class _AuthenticatedLineRange:
    """Hash a complete source payload while retaining selected whole lines."""

    __slots__ = (
        "byte_count",
        "digest",
        "payload",
        "overflow",
        "_end_line",
        "_limit",
        "_line_number",
        "_pending_cr",
        "_start_line",
    )

    def __init__(self, start_line: int, end_line: int, limit: int) -> None:
        self.byte_count = 0
        self.digest = hashlib.sha256()
        self.payload = bytearray()
        self.overflow = False
        self._start_line = start_line
        self._end_line = end_line
        self._limit = limit
        self._line_number = 1
        self._pending_cr = False

    def _retain(self, payload: bytes, start: int, stop: int) -> None:
        if not (
            stop > start and self._start_line <= self._line_number <= self._end_line
        ):
            return
        remaining = self._limit - len(self.payload)
        segment_size = stop - start
        if segment_size > remaining:
            if remaining > 0:
                self.payload.extend(payload[start : start + remaining])
            self.overflow = True
        elif remaining > 0:
            self.payload.extend(payload[start:stop])

    def update(self, payload: bytes) -> None:
        self.byte_count += len(payload)
        self.digest.update(payload)

        offset = 0
        if self._pending_cr:
            if not payload:
                return
            self._pending_cr = False
            if payload.startswith(b"\n"):
                self._retain(payload, 0, 1)
                self._line_number += 1
                offset = 1
            else:
                self._line_number += 1

        segment_start = offset
        for match in _SOURCE_NEWLINE_RE.finditer(payload, offset):
            boundary = match.start()
            if boundary < offset:
                continue
            value = payload[boundary]
            if value == 0x0A:
                offset = boundary + 1
                self._retain(payload, segment_start, offset)
                self._line_number += 1
                segment_start = offset
                continue

            offset = boundary + 1
            if offset == len(payload):
                self._retain(payload, segment_start, offset)
                self._pending_cr = True
                segment_start = offset
                continue
            if payload[offset] == 0x0A:
                offset += 1
            self._retain(payload, segment_start, offset)
            self._line_number += 1
            segment_start = offset

        self._retain(payload, segment_start, len(payload))


def _snapshot_source_identity_value(value: object) -> object:
    if type(value) is tuple:
        return tuple(_snapshot_source_identity_value(item) for item in value)
    if value is None or type(value) in {bool, int, str, bytes}:
        return value
    raise TypeError("repository source identity contains a non-exact value")


def _same_source_identity_value(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is tuple:
        return len(left) == len(right) and all(  # type: ignore[arg-type]
            _same_source_identity_value(left_item, right_item)
            for left_item, right_item in zip(  # type: ignore[arg-type]
                left,
                right,
                strict=True,
            )
        )
    return left == right


def _same_source_file_record(
    left: RepositorySourceFileRecord,
    right: RepositorySourceFileRecord,
) -> bool:
    return (
        type(left) is RepositorySourceFileRecord
        and type(right) is RepositorySourceFileRecord
        and type(left.path) is type(right.path) is str
        and left.path == right.path
        and type(left.size) is type(right.size) is int
        and left.size == right.size
        and type(left.sha256) is type(right.sha256) is str
        and left.sha256 == right.sha256
        and type(left.link_target) is type(right.link_target)
        and left.link_target == right.link_target
        and _same_source_identity_value(
            left.lexical_identity,
            right.lexical_identity,
        )
    )


def _snapshot_source_file_record(record: object) -> RepositorySourceFileRecord:
    if type(record) is not RepositorySourceFileRecord:
        raise TypeError("repository source records must use the exact record type")
    if (
        type(record.path) is not str
        or type(record.size) is not int
        or type(record.sha256) is not str
        or type(record.lexical_identity) is not tuple
        or (record.link_target is not None and type(record.link_target) is not str)
    ):
        raise TypeError("repository source record fields must use exact types")
    if (
        not record.path
        or record.size < 0
        or not re.fullmatch(r"[0-9a-f]{64}", record.sha256, re.ASCII)
    ):
        raise ValueError("repository source record identity is invalid")
    return RepositorySourceFileRecord(
        path=record.path,
        size=record.size,
        sha256=record.sha256,
        lexical_identity=_snapshot_source_identity_value(
            record.lexical_identity
        ),  # type: ignore[arg-type]
        link_target=record.link_target,
    )


def _verify_resolved_repository_link(
    source: object,
    link: _RepositorySourceLinkRecord,
    records: Mapping[str, RepositorySourceFileRecord],
) -> None:
    observed_state = (
        "regular"
        if source.is_regular  # type: ignore[attr-defined]
        else (
            "directory" if source.is_directory else "unresolved"
        )  # type: ignore[attr-defined]
    )
    if observed_state != link.target_state:
        raise RepositoryChangedError(
            "repository source link target state changed after authentication"
        )
    if observed_state != "regular":
        return
    record = records.get(link.path)
    if record is None or record.link_target != link.link_target:
        raise RepositoryChangedError(
            "repository source link record changed after authentication"
        )
    content_digest = hashlib.sha256()
    source.update_hash(content_digest)  # type: ignore[attr-defined]
    opened_size = getattr(source, "opened_size", None)
    if opened_size is None:
        descriptor = getattr(source, "descriptor", -1)
        opened_size = os.fstat(descriptor).st_size
    if opened_size != record.size or content_digest.hexdigest() != record.sha256:
        raise RepositoryChangedError(
            "repository source link target differs from its authenticated record"
        )


def _verify_posix_repository_links(
    root: Path,
    root_descriptor: int,
    root_identity: tuple[int, ...],
    links: tuple[_RepositorySourceLinkRecord, ...],
    records: Mapping[str, RepositorySourceFileRecord],
) -> None:
    for link in links:
        with _resolved_repository_file_at(
            root,
            root_descriptor,
            link.path,
            expected_root_identity=root_identity,
            expected_final_identity=link.lexical_identity,  # type: ignore[arg-type]
            expected_final_link_target=link.link_target,
        ) as source:
            _verify_resolved_repository_link(source, link, records)


def _verify_windows_repository_links(
    root: Path,
    root_authority: object,
    root_identity: tuple[object, ...],
    links: tuple[_RepositorySourceLinkRecord, ...],
    records: Mapping[str, RepositorySourceFileRecord],
    *,
    api: _WindowsKernelApi,
) -> None:
    for link in links:
        with _resolved_windows_repository_file_at(
            root,
            root_authority,
            link.path,
            expected_root_identity=root_identity,
            expected_final_identity=link.lexical_identity,
            expected_final_link_target=link.link_target,
            api=api,
        ) as source:
            _verify_resolved_repository_link(source, link, records)


class RepositorySourceBinding:
    """Retained root authority and exact records for verified live reads."""

    __slots__ = (
        "root",
        "fingerprint",
        "file_count",
        "file_records",
        "_authenticated_root",
        "_authenticated_fingerprint",
        "_authenticated_file_count",
        "_authenticated_file_records",
        "_authenticated_link_records",
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
        "_process_locks",
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
        link_records: Iterable[_RepositorySourceLinkRecord],
        inventory_digest: str,
        inventory_entries: int,
        excluded: tuple[tuple[str, ...], ...],
        root_identity: tuple[object, ...],
        root_descriptor: int = -1,
        posix_authority: object | None = None,
        windows_api: _WindowsKernelApi | None = None,
        windows_authority: object | None = None,
    ) -> None:
        if type(root) is not type(Path()) or type(fingerprint) is not SourceFingerprint:
            raise TypeError("repository source identity fields must use exact types")
        if (
            type(fingerprint.value) is not str
            or type(fingerprint.file_count) is not int
            or fingerprint.file_count < 0
            or not _SOURCE_FINGERPRINT_V2_RE.fullmatch(fingerprint.value)
        ):
            raise ValueError("repository source identity is invalid")
        records = tuple(_snapshot_source_file_record(record) for record in file_records)
        authenticated_records = tuple(
            _snapshot_source_file_record(record) for record in records
        )
        root_snapshot = type(root)(os.fspath(root))
        self.root = type(root)(os.fspath(root))
        self.fingerprint = fingerprint.value
        self.file_count = fingerprint.file_count
        self.file_records = records
        self._authenticated_root = root_snapshot
        self._authenticated_fingerprint = fingerprint.value
        self._authenticated_file_count = fingerprint.file_count
        self._authenticated_file_records = authenticated_records
        authenticated_links: list[_RepositorySourceLinkRecord] = []
        for record in link_records:
            if (
                type(record) is not _RepositorySourceLinkRecord
                or type(record.path) is not str
                or type(record.lexical_identity) is not tuple
                or type(record.link_target) is not str
                or record.target_state not in {"regular", "directory", "unresolved"}
            ):
                raise ValueError(
                    "repository source binding contains invalid link records"
                )
            authenticated_links.append(
                _RepositorySourceLinkRecord(
                    path=record.path,
                    lexical_identity=_snapshot_source_identity_value(
                        record.lexical_identity
                    ),  # type: ignore[arg-type]
                    link_target=record.link_target,
                    target_state=record.target_state,
                )
            )
        self._authenticated_link_records = tuple(authenticated_links)
        self._records = {record.path: record for record in authenticated_records}
        if len(self._records) != len(authenticated_records):
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
        self._process_locks = {self._pid: self._lock}
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

        if self._pid != os.getpid():
            return False
        with self._state_lease():
            return not self._closed and not self._poisoned

    @property
    def closed(self) -> bool:
        """Return whether every retained filesystem authority is closed."""

        if self._pid != os.getpid():
            # A forked child owns an independent copy of these flags. Avoid an
            # inherited parent-thread RLock while its cleanup reconciles fds.
            return self._closed
        with self._state_lease():
            return self._closed

    @property
    def failure_reason(self) -> str | None:
        """Return the first authentication failure retained for diagnostics."""

        with self._state_lease():
            return self._failure_reason

    @contextmanager
    def _state_lease(self) -> Iterator[None]:
        self._require_owner_pid()
        with _SourceLockLease(self._lock) as acquisition_failure:
            if acquisition_failure is not None:
                raise acquisition_failure
            yield

    def _poison(self, reason: object) -> None:
        self._require_owner_pid()
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
        self._require_owner_pid()
        if self._poisoned:
            raise RepositoryChangedError("repository source binding is poisoned")
        if self._closed:
            raise RuntimeError("repository source binding is closed")

    def _require_owner_pid(self) -> None:
        if self._pid != os.getpid():
            raise RuntimeError("repository source binding cannot cross processes")

    @contextmanager
    def _authentication_lease(self) -> Iterator[None]:
        """Hold the reentrant lock and poison on every interrupted operation."""

        self._require_owner_pid()
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
        self._verify_public_projection()
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
            self._verify_retained_link_targets()
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
        self._verify_retained_link_targets()

    def _verify_public_projection(self) -> None:
        if (
            type(self.root) is not type(self._authenticated_root)
            or self.root != self._authenticated_root
            or type(self.fingerprint) is not str
            or self.fingerprint != self._authenticated_fingerprint
            or type(self.file_count) is not int
            or self.file_count != self._authenticated_file_count
            or type(self.file_records) is not tuple
            or len(self.file_records) != len(self._authenticated_file_records)
        ):
            raise RepositoryChangedError(
                "repository source public identity changed after authentication"
            )
        for observed, expected in zip(
            self.file_records,
            self._authenticated_file_records,
            strict=True,
        ):
            try:
                detached = _snapshot_source_file_record(observed)
            except (TypeError, ValueError) as exc:
                raise RepositoryChangedError(
                    "repository source public records changed after authentication"
                ) from exc
            if not _same_source_file_record(detached, expected):
                raise RepositoryChangedError(
                    "repository source public records changed after authentication"
                )
        if len(self._records) != len(self._authenticated_file_records) or any(
            (
                not _same_source_file_record(self._records[expected.path], expected)
                if expected.path in self._records
                else True
            )
            for expected in self._authenticated_file_records
        ):
            raise RepositoryChangedError(
                "repository source retained records changed after authentication"
            )

    def _verify_retained_link_targets(self) -> None:
        """Rebind every link target state, including excluded or missing targets."""

        if self._windows_authority is not None:
            api = self._windows_api
            if api is None:  # pragma: no cover - constructor invariant
                raise AssertionError("Windows repository binding has no API")
            _verify_windows_repository_links(
                self._authenticated_root,
                self._windows_authority,
                self._root_identity,
                self._authenticated_link_records,
                self._records,
                api=api,
            )
            return
        _verify_posix_repository_links(
            self._authenticated_root,
            self._root_descriptor,
            self._root_identity,  # type: ignore[arg-type]
            self._authenticated_link_records,
            self._records,
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

    def authenticated_identity_snapshot(self) -> RepositorySourceIdentitySnapshot:
        """Verify and return identity values caller mutation cannot replace."""

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
            return RepositorySourceIdentitySnapshot(
                root=type(self._authenticated_root)(
                    os.fspath(self._authenticated_root)
                ),
                fingerprint=self._authenticated_fingerprint,
                file_count=self._authenticated_file_count,
                file_records=tuple(
                    _snapshot_source_file_record(record)
                    for record in self._authenticated_file_records
                ),
            )

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
                self._authenticated_root,
                self._windows_authority,
                relative,
                expected_root_identity=self._root_identity,
                expected_final_identity=record.lexical_identity,
                expected_final_link_target=record.link_target,
                api=api,
            )
        else:
            resolver = _resolved_repository_file_at(
                self._authenticated_root,
                self._root_descriptor,
                relative,
                expected_root_identity=self._root_identity,  # type: ignore[arg-type]
                expected_final_identity=record.lexical_identity,  # type: ignore[arg-type]
                expected_final_link_target=record.link_target,
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

    def _read_record_prefix(
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
        authenticated = _AuthenticatedPrefix(max_bytes)
        with resolver as source:
            if not source.is_regular:
                raise ValueError("source path is no longer a regular file")
            source.update_hash(authenticated)
        if (
            authenticated.byte_count != record.size
            or authenticated.digest.hexdigest() != record.sha256
        ):
            raise RepositoryChangedError(
                "repository source file differs from its authenticated record"
            )
        return bytes(authenticated.prefix)

    def _read_record_line_range(
        self,
        relative: str,
        record: RepositorySourceFileRecord,
        *,
        start_line: int,
        end_line: int,
        max_bytes: int,
    ) -> tuple[bytes, bool]:
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
        authenticated = _AuthenticatedLineRange(start_line, end_line, max_bytes)
        with resolver as source:
            if not source.is_regular:
                raise ValueError("source path is no longer a regular file")
            source.update_hash(authenticated)
        if (
            authenticated.byte_count != record.size
            or authenticated.digest.hexdigest() != record.sha256
        ):
            raise RepositoryChangedError(
                "repository source file differs from its authenticated record"
            )
        return bytes(authenticated.payload), authenticated.overflow

    def captured_relative_path(self, path: str) -> str | None:
        """Return the exact captured POSIX path without consulting the filesystem."""

        with self._state_lease():
            self._require_open()
            if not isinstance(path, str):
                raise TypeError("repository source path must be text")
            if not path or "\x00" in path:
                return None

            current_root_label = os.fspath(self.root)
            current_root_bytes = os.fsencode(current_root_label)
            label_byte_limit = len(current_root_bytes) + 1 + _MAX_SOURCE_PATH_BYTES
            if len(path) > label_byte_limit:
                return None
            try:
                encoded_path = os.fsencode(path)
            except UnicodeError:
                return None
            if len(encoded_path) > label_byte_limit:
                return None

            portable = path.replace("\\", "/")
            drive, tail = ntpath.splitdrive(portable)
            # Python 3.13 no longer treats one leading slash as absolute in
            # ntpath. POSIX-rooted persisted paths remain absolute regardless
            # of the host running this compatibility lookup.
            absolute = portable.startswith("/") or ntpath.isabs(portable)
            if drive and not absolute:
                return None

            native_candidate = portable.replace("/", os.sep)
            native_exact = os.path.isabs(native_candidate) and (
                os.name != "nt" or bool(drive)
            )
            if native_exact:
                lexical = Path(os.path.abspath(native_candidate))
                try:
                    relative_path = lexical.relative_to(self.root)
                except ValueError:
                    pass
                else:
                    relative = PurePosixPath(*relative_path.parts).as_posix()
                    return relative if relative in self._records else None

            component_text = tail.lstrip("/") if absolute else tail
            parts = tuple(component_text.split("/"))
            if not parts or any(part in {"", ".", ".."} for part in parts):
                return None

            if (
                len(encoded_path) > _MAX_SOURCE_PATH_BYTES
                or len(parts) > _MAX_SOURCE_COMPONENTS
            ):
                return None

            if not absolute:
                relative = PurePosixPath(*parts).as_posix()
                return relative if relative in self._records else None

            for offset in range(len(parts)):
                relative = PurePosixPath(*parts[offset:]).as_posix()
                if relative in self._records:
                    return relative
            return None

    def borrow_reader(self) -> RepositorySourceReader:
        """Return a non-owning reader that becomes unusable when this closes."""

        with self._state_lease():
            self._require_open()
            return RepositorySourceReader(self)

    def _borrowed_file_paths(self) -> frozenset[str]:
        with self._state_lease():
            self._require_open()
            return frozenset(self._records)

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

    def read_prefix(self, relative: str, *, max_bytes: int) -> bytes:
        """Authenticate a complete captured file while retaining a bounded prefix."""

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

        with self._authentication_lease():
            try:
                if self._session_depth == 0:
                    self._verify_inventory()
                payload = self._read_record_prefix(
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

    def read_line_range(
        self,
        relative: str,
        *,
        start_line: int,
        end_line: int,
        max_bytes: int,
    ) -> bytes:
        """Authenticate a whole file while retaining a bounded 1-based range."""

        for name, value in (("start", start_line), ("end", end_line)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"source {name} line must be a positive integer")
        if end_line < start_line:
            raise ValueError("source end line must not precede the start line")
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

        overflow = False
        with self._authentication_lease():
            try:
                if self._session_depth == 0:
                    self._verify_inventory()
                payload, overflow = self._read_record_line_range(
                    relative,
                    record,
                    start_line=start_line,
                    end_line=end_line,
                    max_bytes=max_bytes,
                )
                if self._session_depth == 0:
                    self._verify_inventory()
            except BaseException as exc:
                self._poison(exc)
                if isinstance(exc, RepositoryChangedError):
                    raise
                if not isinstance(exc, (OSError, RuntimeError, ValueError)):
                    raise
                raise RepositoryChangedError(
                    "repository source changed while it was being read"
                ) from exc
        if overflow:
            raise ValueError("source line range exceeds its bounded read limit")
        return payload

    def _close_with_lock(self, lock: _SourceLifecycleRLock) -> None:
        with _SourceLockLease(lock) as first_failure:
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

    def close(self) -> None:
        current_pid = os.getpid()
        if current_pid == self._pid:
            self._close_with_lock(self._lock)
            return

        # A lock held by another thread at fork can never be released in the
        # child. Serialize child-only fd cleanup on a process-local lock, then
        # report the ownership boundary after every inherited authority has
        # been visited. The parent's object and descriptor table are unchanged.
        child_lock = self._process_locks.setdefault(
            current_pid,
            _SourceLifecycleRLock(),
        )
        cleanup_failure: BaseException | None = None
        try:
            self._close_with_lock(child_lock)
        except BaseException as exc:  # noqa: B036 - report PID boundary
            cleanup_failure = exc
        boundary = RuntimeError("repository source binding cannot cross processes")
        if cleanup_failure is not None:
            raise boundary from cleanup_failure
        raise boundary


class RepositorySourceReader:
    """Borrowed authenticated source reader without ownership operations."""

    __slots__ = ("_binding",)

    def __init__(self, binding: RepositorySourceBinding) -> None:
        self._binding = binding

    @property
    def file_paths(self) -> frozenset[str]:
        return self._binding._borrowed_file_paths()

    def captured_relative_path(self, path: str) -> str | None:
        return self._binding.captured_relative_path(path)

    def read_prefix(self, relative: str, *, max_bytes: int) -> bytes:
        return self._binding.read_prefix(relative, max_bytes=max_bytes)

    def read_line_range(
        self,
        relative: str,
        *,
        start_line: int,
        end_line: int,
        max_bytes: int,
    ) -> bytes:
        return self._binding.read_line_range(
            relative,
            start_line=start_line,
            end_line=end_line,
            max_bytes=max_bytes,
        )


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
        "_resource_close_cookies",
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
        self._resource_close_cookies: dict[int, int] = {}
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
            self._resource_close_cookies,
        )
        self._closed = not self._resources
        self.descriptor = -1
        if failure is not None:
            raise failure


def _close_posix_descriptors(
    descriptors: list[int],
    expected_identities: dict[int, tuple[int, ...]],
    close_cookies: dict[int, int],
) -> BaseException | None:
    """Visit every descriptor and retain the first delayed close failure."""

    deferred: BaseException | None = None
    for descriptor in reversed(tuple(descriptors)):
        closed = False
        while True:
            try:
                observed = os.fstat(descriptor)
                expected = expected_identities.get(descriptor)
                close_cookie = close_cookies.get(descriptor)
                if (
                    expected is not None
                    and _descriptor_ownership_identity(observed) != expected
                ):
                    deferred = _remember_interruption(
                        deferred,
                        RuntimeError("repository descriptor ownership changed"),
                    )
                    closed = True
                    break
                if close_cookie is not None and not _descriptor_has_close_cookie(
                    descriptor,
                    close_cookie,
                ):
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

            expected_at_close = _descriptor_ownership_identity(observed)
            try:
                if close_cookie is None:
                    close_cookie = _arm_or_reuse_descriptor_close_cookie(
                        descriptor,
                        close_cookies.values(),
                    )
                    if close_cookie is not None:
                        close_cookies[descriptor] = close_cookie
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
                    if _descriptor_ownership_identity(visible) != expected_at_close or (
                        close_cookie is not None
                        and not _descriptor_has_close_cookie(
                            descriptor,
                            close_cookie,
                        )
                    ):
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
            _discard_descriptor_close_cookie(close_cookies, descriptor)
    return deferred


class _PosixPartialCleanup:
    """Retryable owner installed before temporary descriptor acquisition."""

    __slots__ = ("descriptors", "expected_identities", "close_cookies")

    def __init__(self) -> None:
        self.descriptors: list[int] = []
        self.expected_identities: dict[int, tuple[int, ...]] = {}
        self.close_cookies: dict[int, int] = {}

    @property
    def closed(self) -> bool:
        return not self.descriptors

    def retain(self, descriptor: int, metadata: os.stat_result | None = None) -> int:
        deferred = _append_owned_descriptor(self.descriptors, descriptor)
        if deferred is not None:
            raise deferred
        if metadata is not None:
            self.expected_identities[descriptor] = _descriptor_ownership_identity(
                metadata
            )
        return descriptor

    def close_descriptor(self, descriptor: int) -> None:
        selected = [descriptor] if descriptor in self.descriptors else []
        identities = {
            descriptor: self.expected_identities[descriptor]
            for descriptor in selected
            if descriptor in self.expected_identities
        }
        cookies = {
            descriptor: self.close_cookies[descriptor]
            for descriptor in selected
            if descriptor in self.close_cookies
        }
        failure = _close_posix_descriptors(selected, identities, cookies)
        if descriptor not in selected:
            while descriptor in self.descriptors:
                self.descriptors.remove(descriptor)
            self.expected_identities.pop(descriptor, None)
            self.close_cookies.pop(descriptor, None)
        elif descriptor not in cookies:
            self.close_cookies.pop(descriptor, None)
        else:
            self.close_cookies[descriptor] = cookies[descriptor]
        if failure is not None:
            raise failure

    def close(self) -> None:
        failure = _close_posix_descriptors(
            self.descriptors,
            self.expected_identities,
            self.close_cookies,
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
        resource_identities[anchor] = _descriptor_ownership_identity(anchor_metadata)
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
            resource_identities[child] = _descriptor_ownership_identity(opened)
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
    link_records: list[_RepositorySourceLinkRecord] = []
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
                        expected_final_link_target=raw_target,
                        api=selected,
                    ) as binding:
                        target_state = (
                            "regular"
                            if binding.is_regular
                            else "directory" if binding.is_directory else "unresolved"
                        )
                        link_records.append(
                            _RepositorySourceLinkRecord(
                                path=relative_text,
                                lexical_identity=_windows_version_identity(metadata),
                                link_target=raw_target,
                                target_state=target_state,
                            )
                        )
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
                                    link_target=raw_target,
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
        _verify_windows_repository_links(
            root_path,
            root_authority,
            root_identity,
            tuple(link_records),
            {record.path: record for record in file_records},
            api=selected,
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
                link_records=link_records,
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
    link_records: list[_RepositorySourceLinkRecord] = []
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
                        expected_final_link_target=raw_target,
                    ) as binding:
                        target_state = (
                            "regular"
                            if binding.is_regular
                            else "directory" if binding.is_directory else "unresolved"
                        )
                        link_records.append(
                            _RepositorySourceLinkRecord(
                                path=relative_text,
                                lexical_identity=_version_identity(initial_metadata),
                                link_target=raw_target,
                                target_state=target_state,
                            )
                        )
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
                                    link_target=raw_target,
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
        _verify_posix_repository_links(
            root_path,
            root_descriptor,
            root_identity,
            tuple(link_records),
            {record.path: record for record in file_records},
        )
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
                link_records=link_records,
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
    "RepositorySourceIdentitySnapshot",
    "RepositorySourceReader",
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
