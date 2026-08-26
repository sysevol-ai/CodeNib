# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rollback-safe publication of fully staged directory trees."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import heapq
import os
import secrets
import stat
import sys
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import (
    Callable,
    ContextManager,
    Iterable,
    Iterator,
    Literal,
    Protocol,
    Sequence,
    TypeVar,
)

from . import _windows_fs_authority as _windows_fs
from . import _workspace_owner as _native_workspace_owner

_WINDOWS_DELETE = _windows_fs.DELETE
_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = _windows_fs.FILE_ATTRIBUTE_DIRECTORY
_WINDOWS_FILE_ATTRIBUTE_READONLY = _windows_fs.FILE_ATTRIBUTE_READONLY
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = _windows_fs.FILE_ATTRIBUTE_REPARSE_POINT
_WINDOWS_FILE_LIST_DIRECTORY = _windows_fs.FILE_LIST_DIRECTORY
_WINDOWS_FILE_READ_ATTRIBUTES = _windows_fs.FILE_READ_ATTRIBUTES
_WINDOWS_FILE_READ_DATA = _windows_fs.FILE_READ_DATA
_WINDOWS_SYNCHRONIZE = _windows_fs.SYNCHRONIZE
_WindowsDirectoryEntry = _windows_fs.WindowsDirectoryEntry
_WindowsHandleMetadata = _windows_fs.WindowsHandleMetadata
_WindowsKernelApi = _windows_fs.WindowsKernelApi
_windows_extended_path = _windows_fs.windows_extended_path
_windows_mode_from_attributes = _windows_fs.windows_mode_from_attributes
_shared_windows_kernel_api = _windows_fs.windows_kernel_api
_shared_windows_require_publication_api = _windows_fs.windows_require_publication_api

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_EXPECTED_DESTINATION_UNSET = object()
_EXPECTED_OWNERSHIP_UNSET = object()
_MAX_SAFE_REMOVAL_DEPTH = 256
_MAX_OWNERSHIP_ENTRIES = 100_000
_MAX_OWNERSHIP_BYTES = 64 << 30
_MAX_OWNERSHIP_METADATA_BYTES = 16 << 20
_MAX_OWNERSHIP_PATH_BYTES = 4_096
_MAX_OWNERSHIP_COMPONENT_BYTES = 255
_OWNERSHIP_COPY_BYTES = 1 << 20
_MAX_PUBLICATION_SNAPSHOT_BYTES = 64 << 20
_MAX_PUBLICATION_STREAM_READ_BYTES = 8 << 20
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x4
_MAX_ORPHAN_NAME_ATTEMPTS = 128
_MAX_ORDERED_ACTION_CANCELLATION_RETRIES = 8
_OWNERSHIP_SORT_RUN_ENTRIES = 256
_DURABLE_PUBLICATION_FSYNC_BACKENDS = frozenset(
    {"linux-native-workspace-owner", "linux-renameat2"}
)
_NATIVE_REPLACEMENT_PUBLICATION_BACKEND = "linux-native-workspace-replacement"
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_SAFE_OWNERSHIP_DIRECTORY_FDS = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.scandir in os.supports_fd
)

_T = TypeVar("_T")


def _interruptible_sorted_ownership_items(
    entries: Sequence[_T],
    *,
    key: Callable[[_T], object] | None,
    check_cancelled: Callable[[], None] | None,
) -> tuple[_T, ...]:
    """Sort through bounded stable runs and an interruptible stable merge."""

    if check_cancelled is None:
        return tuple(sorted(entries, key=key))
    entry_count = len(entries)
    runs: list[list[_T]] = []
    for start in range(0, entry_count, _OWNERSHIP_SORT_RUN_ENTRIES):
        end = min(start + _OWNERSHIP_SORT_RUN_ENTRIES, entry_count)
        run = list(entries[start:end])
        run.sort(key=key)
        runs.append(run)
        if end < entry_count:
            check_cancelled()
    if not runs:
        return ()
    while len(runs) > 1:
        check_cancelled()
        merged_runs: list[list[_T]] = []
        run_count = len(runs)
        for run_index in range(0, run_count, 2):
            left = runs[run_index]
            if run_index + 1 == run_count:
                merged_runs.append(left)
            else:
                right = runs[run_index + 1]
                merged_run: list[_T] = []
                merged_count = len(left) + len(right)
                for index, item in enumerate(heapq.merge(left, right, key=key)):
                    merged_run.append(item)
                    if index + 1 < merged_count:
                        check_cancelled()
                merged_runs.append(merged_run)
            if run_index + 2 < run_count:
                check_cancelled()
        runs = merged_runs

    def ordered_items() -> Iterator[_T]:
        for index, item in enumerate(runs[0]):
            yield item
            if index + 1 < entry_count:
                check_cancelled()

    return tuple(ordered_items())


def _contains_required_ownership_marker(
    entries: Sequence[_T],
    *,
    matches: Callable[[_T], bool],
    check_cancelled: Callable[[], None] | None,
) -> bool:
    """Search a bounded collection without polling past its final result."""

    if check_cancelled is None:
        return any(matches(entry) for entry in entries)
    entry_count = len(entries)
    for index, entry in enumerate(entries):
        if matches(entry):
            return True
        if index + 1 < entry_count:
            check_cancelled()
    return False


def _interruptible_ownership_tuple(
    entries: Sequence[_T],
    *,
    check_cancelled: Callable[[], None] | None,
) -> tuple[_T, ...]:
    """Materialize a bounded tuple while polling only before future items."""

    if check_cancelled is None:
        return tuple(entries)
    entry_count = len(entries)

    def items() -> Iterator[_T]:
        for index, entry in enumerate(entries):
            yield entry
            if index + 1 < entry_count:
                check_cancelled()

    return tuple(items())


@dataclass(slots=True)
class _OwnershipBudget:
    entries: int = 0
    byte_count: int = 0
    metadata_bytes: int = 0


@dataclass(frozen=True, slots=True)
class TreeFileRecord:
    """Canonical content record captured before an owned file is consumed."""

    path: str
    mode: int
    size: int
    sha256: str


DirectoryEntryPolicy = Callable[[str, Literal["directory", "file"], int, int], None]


@dataclass(frozen=True, slots=True)
class _TreeOwnership:
    root_identity: tuple[int, ...]
    root_version_identity: tuple[int, ...]
    digest: str
    entries: int
    byte_count: int
    metadata_bytes: int
    inventory: tuple[tuple[str, str], ...]
    file_records: tuple[TreeFileRecord, ...]
    entry_identities: tuple[tuple[str, str, tuple[int, ...]], ...]


@dataclass(frozen=True, slots=True)
class _ExpectedPublicationFile:
    record: TreeFileRecord
    file_identity: tuple[int, ...]
    directory_identities: tuple[tuple[str, tuple[int, ...]], ...]


@dataclass(frozen=True, slots=True)
class PublicationFileSnapshot:
    """Immutable bytes and their authenticated canonical file record."""

    record: TreeFileRecord
    _payload: bytes

    def read_bytes(self) -> bytes:
        return self._payload

    def iter_bytes(self, *, chunk_size: int = _OWNERSHIP_COPY_BYTES) -> Iterator[bytes]:
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
            raise TypeError("publication snapshot chunk size must be an integer")
        if chunk_size <= 0 or chunk_size > _OWNERSHIP_COPY_BYTES:
            raise ValueError("publication snapshot chunk size is out of bounds")
        for offset in range(0, len(self._payload), chunk_size):
            yield self._payload[offset : offset + chunk_size]


class PublicationAuthenticatedFile:
    """One bounded file stream whose handle is verified when its context exits."""

    __slots__ = (
        "path",
        "mode",
        "size",
        "_remaining",
        "_read_callback",
        "_verify_callback",
        "_digest",
        "_record",
        "_closed",
        "_finalized",
        "_pid",
        "_lifetime",
    )

    def __init__(
        self,
        *,
        path: str,
        mode: int,
        size: int,
        read_callback: Callable[[int], bytes],
        verify_callback: Callable[[], None],
    ) -> None:
        self.path = path
        self.mode = mode
        self.size = size
        self._remaining = size
        self._read_callback = read_callback
        self._verify_callback = verify_callback
        self._digest = hashlib.sha256()
        self._record: TreeFileRecord | None = None
        self._closed = False
        self._finalized = False
        self._pid = os.getpid()
        self._lifetime: _PublicationReaderLifetime | None = None

    def _bind_lifetime(self, lifetime: _PublicationReaderLifetime) -> None:
        if self._lifetime is not None and self._lifetime is not lifetime:
            raise RuntimeError("publication authenticated file lifetime changed")
        self._lifetime = lifetime

    @property
    def record(self) -> TreeFileRecord:
        if self._record is None:
            raise RuntimeError(
                "publication file record is available after context verification"
            )
        return self._record

    def read(self, size: int = -1) -> bytes:
        if os.getpid() != self._pid:
            self._closed = True
            raise RuntimeError(
                "publication authenticated file cannot cross a process boundary"
            )
        lifetime = self._lifetime
        if lifetime is not None and (
            not lifetime.active or lifetime.pid != os.getpid()
        ):
            self._closed = True
            raise ValueError("publication authenticated file is closed")
        if self._closed:
            raise ValueError("publication authenticated file is closed")
        if isinstance(size, bool) or not isinstance(size, int):
            raise TypeError("publication authenticated read size must be an integer")
        if size < 0:
            if self._remaining > _MAX_PUBLICATION_STREAM_READ_BYTES:
                raise ValueError(
                    "unbounded publication reads are disabled for large files"
                )
            requested = self._remaining
        else:
            if size > _MAX_PUBLICATION_STREAM_READ_BYTES:
                raise ValueError("publication read size exceeds its per-call limit")
            requested = min(size, self._remaining)
        if requested == 0:
            return b""
        block = self._read_callback(requested)
        if not isinstance(block, bytes) or len(block) > requested:
            raise RuntimeError("publication file backend returned invalid bytes")
        if not block:
            raise RuntimeError("publication authenticated file was truncated")
        self._remaining -= len(block)
        self._digest.update(block)
        return block

    def iter_bytes(
        self,
        *,
        chunk_size: int = _OWNERSHIP_COPY_BYTES,
    ) -> Iterator[bytes]:
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
            raise TypeError("publication stream chunk size must be an integer")
        if chunk_size <= 0 or chunk_size > _MAX_PUBLICATION_STREAM_READ_BYTES:
            raise ValueError("publication stream chunk size is out of bounds")
        while self._remaining:
            yield self.read(min(chunk_size, self._remaining))

    def _finalize(self) -> None:
        if self._finalized:
            return
        try:
            if self._closed:
                return
            while self._remaining:
                self.read(min(_OWNERSHIP_COPY_BYTES, self._remaining))
            extra = self._read_callback(1)
            self._verify_callback()
            if extra:
                raise RuntimeError("publication authenticated file grew while read")
            self._record = TreeFileRecord(
                path=self.path,
                mode=self.mode,
                size=self.size,
                sha256=self._digest.hexdigest(),
            )
        except BaseException:
            self._closed = True
            raise
        else:
            self._closed = True
            self._finalized = True

    def _abort(self) -> None:
        """Terminate a failed consumer stream without reading more source bytes."""

        self._closed = True


class _PublicationReaderLifetime:
    """Process-local lifetime shared by one reader and every subtree facade."""

    __slots__ = ("pid", "active", "authentication_failed", "open_files")

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.active = True
        self.authentication_failed = False
        self.open_files: list[_PublicationAuthenticatedFileContext] = []


class _PublicationAuthenticatedFileBackendContext:
    """Expose the at-most-once handoff into one backend context exit.

    ``exit_handed_off`` records delegation, not backend completion.  Production
    backends register their descriptors or HANDLEs with the retained
    publication authority before yielding, so an interruption at the nested
    ``__exit__`` call can safely defer physical cleanup to that owner.  A
    directly supplied custom context remains responsible for its own resources
    after the handoff; this adapter cannot manufacture ownership for it.
    """

    __slots__ = ("_context", "exit_handed_off")

    def __init__(
        self,
        context: ContextManager[PublicationAuthenticatedFile],
    ) -> None:
        self._context = context
        self.exit_handed_off = False

    def __enter__(self) -> PublicationAuthenticatedFile:
        return self._context.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool | None:
        # Mark the at-most-once handoff before the nested CALL.  An interruption
        # at that CALL is indistinguishable in pure Python from one just inside
        # an arbitrary context's ``__exit__``.  Authority-backed production
        # contexts remain physically owned even when this call never begins.
        self.exit_handed_off = True
        return self._context.__exit__(exc_type, exc, traceback)


class _PublicationAuthenticatedFileContext:
    """Own one callback-scoped file context until it is verified or aborted."""

    __slots__ = (
        "_reader",
        "_relative",
        "_authority_relative",
        "_max_bytes",
        "_expected",
        "_authority_expected",
        "_backend_context",
        "_authenticated",
        "_entered",
        "_finished",
    )

    def __init__(
        self,
        reader: PublicationDirectoryReader,
        *,
        relative: PurePosixPath,
        authority_relative: PurePosixPath,
        max_bytes: int,
        expected: _ExpectedPublicationFile,
        authority_expected: _ExpectedPublicationFile,
    ) -> None:
        self._reader = reader
        self._relative = relative
        self._authority_relative = authority_relative
        self._max_bytes = max_bytes
        self._expected = expected
        self._authority_expected = authority_expected
        self._backend_context: _PublicationAuthenticatedFileBackendContext | None = None
        self._authenticated: PublicationAuthenticatedFile | None = None
        self._entered = False
        self._finished = False

    def __enter__(self) -> PublicationAuthenticatedFile:
        if self._entered:
            raise RuntimeError("publication authenticated file context is single-use")
        self._reader._require_active()
        self._entered = True
        self._reader._register_open_file(self)
        context: _PublicationAuthenticatedFileBackendContext | None = None
        try:
            context = _PublicationAuthenticatedFileBackendContext(
                self._reader._open_file(
                    self._authority_relative,
                    self._max_bytes,
                    self._authority_expected,
                )
            )
            self._backend_context = context
            authenticated = context.__enter__()
            self._authenticated = authenticated
            authenticated._bind_lifetime(self._reader._lifetime)
            if (
                authenticated.path,
                authenticated.mode,
                authenticated.size,
            ) != (
                self._authority_expected.record.path,
                self._authority_expected.record.mode,
                self._authority_expected.record.size,
            ):
                raise RuntimeError(
                    "publication file metadata differs from captured ownership"
                )
            # The backend authenticates the authority-root-relative path.  A
            # subtree facade presents only its own relative namespace.
            authenticated.path = self._relative.as_posix()
            return authenticated
        except BaseException as primary_error:
            self._reader._mark_authentication_failed()
            if context is not None:
                authenticated = self._authenticated
                if authenticated is not None:
                    authenticated._abort()
                try:
                    context.__exit__(
                        type(primary_error),
                        primary_error,
                        primary_error.__traceback__,
                    )
                except BaseException as cleanup_error:  # noqa: B036
                    if cleanup_error is not primary_error:
                        _annotate_secondary_error(
                            primary_error,
                            "publication authenticated file entry cleanup also failed",
                            cleanup_error,
                        )
            if context is None or context.exit_handed_off:
                self._reader._forget_open_file(self)
                self._finished = True
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> Literal[False]:
        try:
            self._finish(exc, exc_type=exc_type, traceback=traceback)
        except BaseException as cleanup_error:  # noqa: B036 - keep body primary
            if exc is None:
                raise
            if cleanup_error is not exc:
                _annotate_secondary_error(
                    exc,
                    "publication authenticated file exit also failed",
                    cleanup_error,
                )
        return False

    def _finish(
        self,
        primary_error: BaseException | None,
        *,
        exc_type: type[BaseException] | None = None,
        traceback: object = None,
    ) -> None:
        if self._finished:
            return
        if not self._entered:
            self._finished = True
            return
        context = self._backend_context
        authenticated = self._authenticated
        if context is None:
            self._reader._forget_open_file(self)
            self._finished = True
            return
        if context.exit_handed_off:
            self._reader._forget_open_file(self)
            self._finished = True
            return
        if primary_error is not None:
            self._reader._mark_authentication_failed()
            if authenticated is not None:
                authenticated._abort()
            if exc_type is None:
                exc_type = type(primary_error)
            if traceback is None:
                traceback = primary_error.__traceback__
        try:
            context.__exit__(exc_type, primary_error, traceback)
            if primary_error is None:
                if (
                    authenticated is None
                    or authenticated.record != self._expected.record
                ):
                    raise RuntimeError(
                        "publication file bytes differ from captured ownership"
                    )
        except BaseException:
            self._reader._mark_authentication_failed()
            raise
        finally:
            if context.exit_handed_off:
                self._reader._forget_open_file(self)
                self._finished = True

    def _abort_and_close(self) -> None:
        """Abort an escaped stream and close it without authenticating more bytes."""

        if self._finished:
            return
        authenticated = self._authenticated
        context = self._backend_context
        if context is not None and context.exit_handed_off:
            self._reader._forget_open_file(self)
            self._finished = True
            return
        if authenticated is not None:
            authenticated._abort()
        try:
            if context is not None:
                context.__exit__(None, None, None)
        except BaseException:
            self._reader._mark_authentication_failed()
            raise
        finally:
            if context is None or context.exit_handed_off:
                self._reader._forget_open_file(self)
                self._finished = True


class PublicationDirectoryReader:
    """A child tree that is usable only during an authority-owned callback.

    Direct construction with a custom ``open_file`` context is a testing and
    integration seam.  Such a provider owns its resources independently: if
    exit delegation is interrupted, it must retain and eventually release
    them just as the built-in publication-authority backends do.
    """

    __slots__ = (
        "_diagnostic_path",
        "root_identity",
        "_capture",
        "_capture_supports_cancellation",
        "_open_file",
        "_expected_ownership",
        "_records_by_path",
        "_entries_by_path",
        "_authority_records_by_path",
        "_authority_entries_by_path",
        "_authority_expected_ownership",
        "_path_prefix",
        "_lifetime",
    )

    def __init__(
        self,
        display_path: Path,
        root_identity: tuple[int, ...],
        capture: Callable[
            [str | None, bool, DirectoryEntryPolicy | None], _TreeOwnership
        ],
        open_file: Callable[
            [PurePosixPath, int, _ExpectedPublicationFile],
            ContextManager[PublicationAuthenticatedFile],
        ],
        expected_ownership: _TreeOwnership | None,
        *,
        _authority_expected_ownership: _TreeOwnership | None = None,
        _path_prefix: PurePosixPath | None = None,
        _lifetime: _PublicationReaderLifetime | None = None,
        _capture_supports_cancellation: bool = False,
    ) -> None:
        self._diagnostic_path = display_path
        self.root_identity = root_identity
        self._capture = capture
        self._capture_supports_cancellation = _capture_supports_cancellation
        self._open_file = open_file
        self._expected_ownership = expected_ownership
        self._authority_expected_ownership = (
            expected_ownership
            if _authority_expected_ownership is None
            else _authority_expected_ownership
        )
        self._path_prefix = _path_prefix
        self._lifetime = (
            _PublicationReaderLifetime() if _lifetime is None else _lifetime
        )
        self._records_by_path = (
            {}
            if expected_ownership is None
            else {record.path: record for record in expected_ownership.file_records}
        )
        self._entries_by_path = (
            {}
            if expected_ownership is None
            else {
                path: (kind, identity)
                for path, kind, identity in expected_ownership.entry_identities
            }
        )
        authority_ownership = self._authority_expected_ownership
        self._authority_records_by_path = (
            {}
            if authority_ownership is None
            else {record.path: record for record in authority_ownership.file_records}
        )
        self._authority_entries_by_path = (
            {}
            if authority_ownership is None
            else {
                path: (kind, identity)
                for path, kind, identity in authority_ownership.entry_identities
            }
        )
        if (
            expected_ownership is not None
            and root_identity != expected_ownership.root_identity
        ):
            self._mark_authentication_failed()
            raise RuntimeError(
                "publication callback root differs from captured ownership"
            )

    @property
    def _authentication_failed(self) -> bool:
        return self._lifetime.authentication_failed

    @_authentication_failed.setter
    def _authentication_failed(self, value: bool) -> None:
        self._lifetime.authentication_failed = value

    @property
    def _active(self) -> bool:
        return self._lifetime.active and self._lifetime.pid == os.getpid()

    @_active.setter
    def _active(self, value: bool) -> None:
        self._lifetime.active = value

    def _require_active(self) -> None:
        if self._lifetime.pid != os.getpid():
            self._lifetime.active = False
            raise RuntimeError(
                "publication tree reader cannot cross a process boundary"
            )
        if not self._lifetime.active:
            raise RuntimeError("publication tree reader is no longer active")

    def _mark_authentication_failed(self) -> None:
        self._lifetime.authentication_failed = True

    def _register_open_file(
        self,
        context: _PublicationAuthenticatedFileContext,
    ) -> None:
        self._require_active()
        self._lifetime.open_files.append(context)

    def _forget_open_file(
        self,
        context: _PublicationAuthenticatedFileContext,
    ) -> None:
        self._lifetime.open_files[:] = [
            candidate
            for candidate in self._lifetime.open_files
            if candidate is not context
        ]

    def _close_open_files(self, primary_error: BaseException | None) -> None:
        actions = tuple(
            _OrderedAction(
                label=("publication escaped authenticated file cleanup also failed"),
                action=lambda context=context: context._finish(primary_error),
                complete=lambda context=context: context._finished,
                retry_incomplete="cancellation",
            )
            for context in reversed(tuple(self._lifetime.open_files))
        )
        failures = _OrderedActionState(
            actions=actions,
            iteration_failure_label=(
                "publication escaped file cleanup iteration also failed"
            ),
            primary_error=None,
        )
        _run_ordered_actions(failures)
        if failures.primary_error is not None:
            raise failures.primary_error

    def _abort_open_files(self) -> None:
        actions = tuple(
            _OrderedAction(
                label="publication escaped authenticated file abort also failed",
                action=context._abort_and_close,
                complete=lambda context=context: context._finished,
                retry_incomplete="cancellation",
            )
            for context in reversed(tuple(self._lifetime.open_files))
        )
        failures = _OrderedActionState(
            actions=actions,
            iteration_failure_label=(
                "publication escaped file abort iteration also failed"
            ),
            primary_error=None,
        )
        _run_ordered_actions(failures)
        if failures.primary_error is not None:
            raise failures.primary_error

    def capture_ownership(
        self,
        *,
        required_root_file: str | None = None,
        allow_empty_root: bool = False,
        entry_policy: DirectoryEntryPolicy | None = None,
        check_cancelled: Callable[[], None] | None = None,
    ) -> _TreeOwnership:
        """Capture through the already-open directory authority."""

        self._require_active()
        if check_cancelled is not None and not callable(check_cancelled):
            raise TypeError("directory ownership cancellation check must be callable")
        try:
            if self._path_prefix is not None:
                return self._projected_capture(
                    required_root_file=required_root_file,
                    allow_empty_root=allow_empty_root,
                    entry_policy=entry_policy,
                    check_cancelled=check_cancelled,
                )
            if self._capture_supports_cancellation:
                observed = self._capture(  # type: ignore[call-arg]
                    required_root_file,
                    allow_empty_root,
                    entry_policy,
                    check_cancelled,
                )
            else:
                observed = self._capture(
                    required_root_file,
                    allow_empty_root,
                    entry_policy,
                )
            if self._expected_ownership is not None:
                _require_matching_ownership(
                    observed,
                    self._expected_ownership,
                    label="publication callback tree",
                    allow_root_rename=True,
                )
            return observed
        except BaseException:
            self._mark_authentication_failed()
            raise

    def _projected_capture(
        self,
        *,
        required_root_file: str | None,
        allow_empty_root: bool,
        entry_policy: DirectoryEntryPolicy | None,
        check_cancelled: Callable[[], None] | None,
    ) -> _TreeOwnership:
        ownership = self._expected_ownership
        if ownership is None:
            raise RuntimeError("publication subtree requires captured ownership")
        marker = _required_root_file_bytes(required_root_file)
        if marker is not None:
            marker_name = os.fsdecode(marker)
            if check_cancelled is None:
                roots = {
                    path: kind for path, kind in ownership.inventory if "/" not in path
                }
            else:
                roots: dict[str, str] = {}
                inventory_count = len(ownership.inventory)
                for index, (path, kind) in enumerate(ownership.inventory):
                    if "/" not in path:
                        roots[path] = kind
                    if index + 1 < inventory_count:
                        check_cancelled()
            if (
                not (allow_empty_root and not roots)
                and roots.get(marker_name) != "file"
            ):
                raise RuntimeError(
                    "directory ownership root is missing its required marker"
                )
        if entry_policy is not None:
            if check_cancelled is None:
                records = {record.path: record for record in ownership.file_records}
                identities = {
                    path: identity
                    for path, _kind, identity in ownership.entry_identities
                }
            else:
                records = {}
                record_count = len(ownership.file_records)
                for index, record in enumerate(ownership.file_records):
                    records[record.path] = record
                    if index + 1 < record_count:
                        check_cancelled()
                identities = {}
                identity_count = len(ownership.entry_identities)
                for index, (path, _kind, identity) in enumerate(
                    ownership.entry_identities
                ):
                    identities[path] = identity
                    if index + 1 < identity_count:
                        check_cancelled()
            inventory_count = len(ownership.inventory)
            for index, (path, kind) in enumerate(ownership.inventory):
                if kind == "file":
                    record = records[path]
                    entry_policy(path, "file", record.mode, record.size)
                else:
                    entry_policy(
                        path,
                        "directory",
                        stat.S_IMODE(identities[path][2]),
                        0,
                    )
                if check_cancelled is not None and index + 1 < inventory_count:
                    check_cancelled()
        return ownership

    def subtree(
        self,
        prefix: str | PurePosixPath,
    ) -> PublicationDirectoryReader:
        """Return an authority-scoped, prefix-relative view of an owned subtree."""

        self._require_active()
        ownership = self._expected_ownership
        if ownership is None:
            self._mark_authentication_failed()
            raise RuntimeError("publication subtree requires captured ownership")
        try:
            normalized = PurePosixPath(_ownership_subtree_prefix(prefix))
            projected = project_directory_ownership_subtree(
                ownership,
                normalized,
            )
        except BaseException:
            self._mark_authentication_failed()
            raise
        authority_prefix = (
            normalized if self._path_prefix is None else self._path_prefix / normalized
        )
        return PublicationDirectoryReader(
            self._diagnostic_path.joinpath(*normalized.parts),
            projected.root_identity,
            self._capture,
            self._open_file,
            projected,
            _authority_expected_ownership=self._authority_expected_ownership,
            _path_prefix=authority_prefix,
            _lifetime=self._lifetime,
            _capture_supports_cancellation=self._capture_supports_cancellation,
        )

    def inventory(self) -> tuple[tuple[str, str], ...]:
        self._require_active()
        if self._expected_ownership is not None:
            return self._expected_ownership.inventory
        return self.capture_ownership().inventory

    def file_records(self) -> tuple[TreeFileRecord, ...]:
        self._require_active()
        if self._expected_ownership is not None:
            return self._expected_ownership.file_records
        return self.capture_ownership().file_records

    def _require_expected_ownership_token(self) -> _TreeOwnership:
        """Return only framework-installed callback ownership, never recapturing."""

        self._require_active()
        if self._expected_ownership is None:
            self._mark_authentication_failed()
            raise RuntimeError(
                "publication callback has no framework expected ownership"
            )
        return self._expected_ownership

    def authenticated_snapshot(
        self,
        relative: str | PurePosixPath,
        *,
        max_bytes: int,
    ) -> PublicationFileSnapshot:
        self._require_active()
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise TypeError("publication snapshot limit must be an integer")
        if max_bytes < 0 or max_bytes > _MAX_PUBLICATION_SNAPSHOT_BYTES:
            raise ValueError("publication snapshot limit is out of bounds")
        normalized = _publication_relative_path(relative)
        chunks: list[bytes] = []
        with self.open_authenticated_file(
            normalized,
            max_bytes=max_bytes,
        ) as authenticated:
            chunks.extend(authenticated.iter_bytes())
        return PublicationFileSnapshot(
            record=authenticated.record,
            _payload=b"".join(chunks),
        )

    def read_bytes(
        self,
        relative: str | PurePosixPath,
        *,
        max_bytes: int,
    ) -> bytes:
        return self.authenticated_snapshot(
            relative,
            max_bytes=max_bytes,
        ).read_bytes()

    def open_authenticated_file(
        self,
        relative: str | PurePosixPath,
        *,
        max_bytes: int,
    ) -> ContextManager[PublicationAuthenticatedFile]:
        self._require_active()
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise TypeError("publication stream limit must be an integer")
        if max_bytes < 0 or max_bytes > _MAX_OWNERSHIP_BYTES:
            raise ValueError("publication stream limit is out of bounds")
        normalized = _publication_relative_path(relative)
        expected = self._expected_file(normalized)
        try:
            authority_relative = (
                normalized
                if self._path_prefix is None
                else self._path_prefix / normalized
            )
            authority_expected = self._expected_file(
                authority_relative,
                authority=True,
            )
            return _PublicationAuthenticatedFileContext(
                self,
                relative=normalized,
                authority_relative=authority_relative,
                max_bytes=max_bytes,
                expected=expected,
                authority_expected=authority_expected,
            )
        except BaseException:
            self._mark_authentication_failed()
            raise

    @contextmanager
    def iter_authenticated_chunks(
        self,
        relative: str | PurePosixPath,
        *,
        max_bytes: int,
        chunk_size: int = _OWNERSHIP_COPY_BYTES,
    ) -> Iterator[Iterator[bytes]]:
        with self.open_authenticated_file(
            relative,
            max_bytes=max_bytes,
        ) as authenticated:
            yield authenticated.iter_bytes(chunk_size=chunk_size)

    def _expected_file(
        self,
        relative: PurePosixPath,
        *,
        authority: bool = False,
    ) -> _ExpectedPublicationFile:
        ownership = (
            self._authority_expected_ownership
            if authority
            else self._expected_ownership
        )
        if ownership is None:
            self._mark_authentication_failed()
            raise RuntimeError(
                "publication file reads require captured callback ownership"
            )
        normalized = relative.as_posix()
        records_by_path = (
            self._authority_records_by_path if authority else self._records_by_path
        )
        entries_by_path = (
            self._authority_entries_by_path if authority else self._entries_by_path
        )
        try:
            record = records_by_path[normalized]
            kind, file_identity = entries_by_path[normalized]
        except KeyError as exc:
            self._mark_authentication_failed()
            raise ValueError(
                f"publication file is absent from captured ownership: {normalized}"
            ) from exc
        if kind != "file":
            self._mark_authentication_failed()
            raise ValueError(f"publication path is not a captured file: {normalized}")
        directories: list[tuple[str, tuple[int, ...]]] = []
        for depth in range(1, len(relative.parts)):
            directory = "/".join(relative.parts[:depth])
            try:
                directory_kind, identity = entries_by_path[directory]
            except KeyError as exc:
                self._mark_authentication_failed()
                raise RuntimeError(
                    f"publication directory is absent from ownership: {directory}"
                ) from exc
            if directory_kind != "directory":
                self._mark_authentication_failed()
                raise RuntimeError(
                    f"publication ancestor is not a directory: {directory}"
                )
            directories.append((directory, identity))
        return _ExpectedPublicationFile(
            record=record,
            file_identity=file_identity,
            directory_identities=tuple(directories),
        )

    def _require_valid(self) -> None:
        if self._authentication_failed:
            raise RuntimeError("publication callback suppressed authentication failure")

    def _deactivate(self) -> None:
        failures = _OrderedActionState(
            actions=(),
            iteration_failure_label=(
                "publication reader deactivation iteration also failed"
            ),
            primary_error=None,
        )
        try:
            _force_publication_reader_inactive(self._lifetime, failures)
        except BaseException as deactivation_error:  # noqa: B036 - cleanup-all
            failures.retain(
                "publication reader deactivation also failed",
                deactivation_error,
            )
        if self._lifetime.open_files:
            self._mark_authentication_failed()
        failures.actions = (
            _OrderedAction(
                label="publication reader escaped stream abort also failed",
                action=self._abort_open_files,
                complete=lambda: all(
                    context._finished for context in self._lifetime.open_files
                ),
                retry_incomplete="cancellation",
            ),
        )
        _run_ordered_actions(failures)
        if failures.primary_error is not None:
            raise failures.primary_error


_PublicationTreeReader = PublicationDirectoryReader


def _set_publication_reader_inactive(
    lifetime: _PublicationReaderLifetime,
) -> None:
    """Apply the reader lifetime transition through a stable fault seam."""

    lifetime.active = False


def _force_publication_reader_inactive(
    lifetime: _PublicationReaderLifetime,
    failures: _OrderedActionState,
) -> None:
    """Finish the idempotent inactive transition after an injected interruption."""

    try:
        while lifetime.active:
            try:
                _set_publication_reader_inactive(lifetime)
            except BaseException as transition_error:  # noqa: B036 - retry state
                failures.retain(
                    "publication reader inactive transition also failed",
                    transition_error,
                )
    except BaseException as iteration_error:  # noqa: B036 - retry transition
        failures.retain(
            "publication reader inactive transition iteration also failed",
            iteration_error,
        )
        if lifetime.active:
            _force_publication_reader_inactive(lifetime, failures)


def _annotate_secondary_error(
    primary_error: BaseException,
    label: str,
    secondary_error: BaseException,
) -> None:
    """Best-effort diagnostics that can never replace the primary error."""

    if primary_error is secondary_error:
        return
    try:
        message = f"{label}: {secondary_error!r}"
        add_note = getattr(BaseException, "add_note", None)
        if add_note is not None:
            # Bypass a hostile BaseException subclass override.  Diagnostics
            # must not execute code supplied by the exception being preserved.
            add_note(primary_error, message)
            return

        try:
            notes = BaseException.__getattribute__(
                primary_error,
                "_codenib_cleanup_notes",
            )
        except AttributeError:
            notes = ()
        if not isinstance(notes, tuple):
            notes = ()
        BaseException.__setattr__(
            primary_error,
            "_codenib_cleanup_notes",
            (*notes, message),
        )
        if BaseException.__getattribute__(primary_error, "__cause__") is None:
            BaseException.__setattr__(
                primary_error,
                "__cause__",
                secondary_error,
            )
    except BaseException:  # noqa: B036 - diagnostics are strictly non-primary
        return


def _publication_cleanup_owner_is_closed(owner: object) -> bool:
    """Best-effort completion probe for an exception-retained cleanup owner."""

    try:
        return bool(owner.closed)  # type: ignore[attr-defined]
    except BaseException:  # noqa: B036 - uncertain ownership stays reachable
        return False


def _attach_publication_cleanup_owner(
    failure: BaseException,
    owner: object | None,
) -> None:
    """Keep an incomplete idempotent cleanup owner reachable from ``failure``."""

    if owner is None or _publication_cleanup_owner_is_closed(owner):
        return
    try:
        close = owner.close  # type: ignore[attr-defined]
        if not callable(close):
            return
        try:
            existing = BaseException.__getattribute__(
                failure,
                "publication_cleanup_owners",
            )
        except AttributeError:
            existing = ()
        # A callback-controlled exception may expose a tuple subclass whose
        # iteration executes hostile code.  Retain only the inert built-in
        # representation; any other value is not a trustworthy owner list.
        if type(existing) is not tuple:
            existing = ()
        retained = tuple(
            candidate
            for candidate in existing
            if not _publication_cleanup_owner_is_closed(candidate)
        )
        if any(candidate is owner for candidate in retained):
            return
        BaseException.__setattr__(
            failure,
            "publication_cleanup_owners",
            (*retained, owner),
        )
    except BaseException:  # noqa: B036 - local ownership remains authoritative
        return


def _prune_publication_cleanup_owners(failure: BaseException | None) -> None:
    """Drop completed owners after an eagerly protected cleanup finishes."""

    if failure is None:
        return
    try:
        try:
            existing = BaseException.__getattribute__(
                failure,
                "publication_cleanup_owners",
            )
        except AttributeError:
            return
        if type(existing) is not tuple:
            return
        retained = tuple(
            candidate
            for candidate in existing
            if not _publication_cleanup_owner_is_closed(candidate)
        )
        BaseException.__setattr__(
            failure,
            "publication_cleanup_owners",
            retained,
        )
    except BaseException:  # noqa: B036 - diagnostics are strictly best-effort
        return


def _retain_first_error(
    primary_error: BaseException | None,
    label: str,
    secondary_error: BaseException,
) -> BaseException:
    if primary_error is None:
        return secondary_error
    _annotate_secondary_error(primary_error, label, secondary_error)
    return primary_error


@dataclass(slots=True)
class _OrderedAction:
    """One cleanup step with an optional observable completion boundary."""

    label: str
    action: Callable[[], None]
    complete: Callable[[], bool] | None = None
    retry_incomplete: (
        Literal["never", "cancellation"] | Callable[[BaseException | None], bool]
    ) = "never"
    incomplete_owner: object | None = None


_OrderedActionInput = _OrderedAction | tuple[str, Callable[[], None]]


@dataclass(slots=True)
class _OrderedActionState:
    actions: tuple[_OrderedActionInput, ...]
    iteration_failure_label: str
    primary_error: BaseException | None
    next_index: int = 0
    cancellation_retries: int = 0
    retry_error_retained: bool = False
    retained_diagnostics: set[tuple[int, str]] = field(default_factory=set)

    def retain(self, label: str, error: BaseException) -> None:
        if self.primary_error is None:
            self.primary_error = error
            return
        _annotate_secondary_error(self.primary_error, label, error)

    def retain_once(
        self,
        marker: str,
        label: str,
        error: BaseException,
    ) -> None:
        key = (self.next_index, marker)
        if key in self.retained_diagnostics:
            return
        self.retained_diagnostics.add(key)
        self.retain(label, error)

    def retain_retry_error(self, label: str, error: BaseException) -> None:
        if self.retry_error_retained:
            return
        self.retry_error_retained = True
        self.retain(label, error)

    def reset_retry_state(self) -> None:
        self.cancellation_retries = 0
        self.retry_error_retained = False

    def protect_pending_owners(self) -> None:
        """Make every not-yet-run cleanup owner retryable from the primary."""

        if self.primary_error is None:
            return
        for action in self.actions[self.next_index :]:
            if isinstance(action, _OrderedAction):
                _attach_publication_cleanup_owner(
                    self.primary_error,
                    action.incomplete_owner,
                )


def _coerce_ordered_action(value: _OrderedActionInput) -> _OrderedAction:
    if isinstance(value, _OrderedAction):
        return value
    label, action = value
    return _OrderedAction(label=label, action=action)


def _ordered_action_complete(
    state: _OrderedActionState,
    ordered: _OrderedAction,
) -> bool:
    complete = ordered.complete
    if complete is None:
        return False
    try:
        return complete() is True
    except BaseException as completion_error:  # noqa: B036 - keep first
        state.retain_once(
            "completion-observation",
            f"{ordered.label} completion observation also failed",
            completion_error,
        )
        return False


def _attempt_ordered_action(
    state: _OrderedActionState,
    ordered: _OrderedAction,
) -> BaseException | None:
    """Attempt an action inside the region that catches pre-call cancellation."""

    try:
        ordered.action()
    except BaseException as action_error:  # noqa: B036 - keep first
        return action_error
    return None


def _advance_ordered_action(state: _OrderedActionState) -> None:
    """Advance only after completion, keeping the cursor restartable."""

    state.next_index += 1
    state.reset_retry_state()


def _retain_incomplete_ordered_action(
    state: _OrderedActionState,
    ordered: _OrderedAction,
) -> None:
    state.retain_once(
        "incomplete",
        f"{ordered.label} did not reach completion",
        RuntimeError("ordered cleanup action did not complete"),
    )
    if state.primary_error is not None:
        _attach_publication_cleanup_owner(
            state.primary_error,
            ordered.incomplete_owner,
        )


def _exhaust_ordered_action_retries(
    state: _OrderedActionState,
    ordered: _OrderedAction | None,
    *,
    label: str,
) -> None:
    """Stop one non-progressing action and leave later actions runnable."""

    state.retain_once(
        "cancellation-retry-exhausted",
        f"{label} cancellation retry limit also exhausted",
        RuntimeError(
            "ordered cleanup progress remained interrupted after "
            f"{_MAX_ORDERED_ACTION_CANCELLATION_RETRIES} cancellation retries"
        ),
    )
    if ordered is not None and _ordered_action_complete(state, ordered):
        state.next_index += 1
        state.reset_retry_state()
        return
    if state.primary_error is not None and ordered is not None:
        _attach_publication_cleanup_owner(
            state.primary_error,
            ordered.incomplete_owner,
        )
    # Do not call the separately patchable advance seam here.  Its persistent
    # interruption is one of the no-progress cases this boundary must contain.
    state.next_index += 1
    state.reset_retry_state()


def _retry_ordered_action(
    state: _OrderedActionState,
    ordered: _OrderedAction | None,
    error: BaseException,
    *,
    label: str,
) -> bool:
    """Retain one cancellation and grant only a bounded number of retries."""

    state.retain_retry_error(label, error)
    if state.cancellation_retries >= _MAX_ORDERED_ACTION_CANCELLATION_RETRIES:
        _exhaust_ordered_action_retries(
            state,
            ordered,
            label=label,
        )
        return False
    state.cancellation_retries += 1
    return True


def _run_plain_ordered_action(
    state: _OrderedActionState,
    ordered: _OrderedAction,
) -> None:
    """Keep validation actions at-most-once across result/loop cancellation."""

    state.next_index += 1
    try:
        ordered.action()
    except BaseException as action_error:  # noqa: B036 - keep first
        state.retain(ordered.label, action_error)


def _run_ordered_actions_pass(state: _OrderedActionState) -> bool:
    """Run until completion or one cancellation-sensitive seam interrupts."""

    active_index: int | None = None
    ordered: _OrderedAction | None = None
    try:
        while state.next_index < len(state.actions):
            active_index = state.next_index
            ordered = None
            action_input = state.actions[state.next_index]
            if isinstance(action_input, _OrderedAction):
                ordered = action_input
            ordered = _coerce_ordered_action(action_input)
            if ordered.complete is None:
                _run_plain_ordered_action(state, ordered)
                continue
            if _ordered_action_complete(state, ordered):
                _advance_ordered_action(state)
                continue
            action_error = _attempt_ordered_action(state, ordered)
            completed = _ordered_action_complete(state, ordered)
            retry_policy = ordered.retry_incomplete
            if callable(retry_policy):
                retry_incomplete = retry_policy(action_error)
            else:
                retry_incomplete = (
                    retry_policy == "cancellation"
                    and action_error is not None
                    and not isinstance(action_error, Exception)
                )
            if completed:
                if action_error is not None:
                    state.retain_retry_error(ordered.label, action_error)
                _advance_ordered_action(state)
                continue
            if retry_incomplete:
                retry_error = action_error or RuntimeError(
                    "ordered cleanup action requested retry without an error"
                )
                if _retry_ordered_action(
                    state,
                    ordered,
                    retry_error,
                    label=ordered.label,
                ):
                    continue
                continue
            if action_error is not None:
                state.retain_retry_error(ordered.label, action_error)
            _retain_incomplete_ordered_action(state, ordered)
            _advance_ordered_action(state)
    except BaseException as iteration_error:  # noqa: B036 - resume remaining
        if active_index is not None and state.next_index == active_index:
            _retry_ordered_action(
                state,
                ordered,
                iteration_error,
                label=state.iteration_failure_label,
            )
        else:
            state.retain_once(
                "iteration-after-progress",
                state.iteration_failure_label,
                iteration_error,
            )
            state.reset_retry_state()
    return state.next_index >= len(state.actions)


def _run_ordered_actions_trampoline_pass(state: _OrderedActionState) -> bool:
    """Contain repeated failures at the Python-to-runner call boundary."""

    active_index = state.next_index
    ordered: _OrderedAction | None = None
    try:
        if active_index < len(state.actions):
            action_input = state.actions[active_index]
            if isinstance(action_input, _OrderedAction):
                ordered = action_input
        return _run_ordered_actions_pass(state)
    except BaseException as entry_error:  # noqa: B036 - bounded resumption
        if active_index < len(state.actions) and state.next_index == active_index:
            _retry_ordered_action(
                state,
                ordered,
                entry_error,
                label=state.iteration_failure_label,
            )
        else:
            state.retain_once(
                "trampoline-after-progress",
                state.iteration_failure_label,
                entry_error,
            )
            state.reset_retry_state()
        return state.next_index >= len(state.actions)


def _run_ordered_actions(state: _OrderedActionState) -> None:
    """Run ordered actions and resume every cancellation-sensitive seam.

    Cleanup steps may expose an idempotent completion predicate.  Cancellation
    immediately before their call receives a bounded retry window, while a close
    that completed before cancellation is observed and never called again.  An
    owner that remains incomplete after that window is attached to the primary
    exception for explicit retry.  Plain validation actions retain the
    historical one-attempt behavior: their failure already prevents a result
    from escaping, but they own no resource that needs retry.
    """

    # ``iter(callable, sentinel)`` and a zero-length deque provide a C-level
    # trampoline.  Each interrupted pass returns before the next one starts, so
    # the bounded cancellation window does not grow the Python stack or retain
    # one object per retry.
    deque(
        iter(partial(_run_ordered_actions_trampoline_pass, state), True),
        maxlen=0,
    )


@contextmanager
def _run_context_with_cleanup_actions(
    cleanup_actions: tuple[_OrderedActionInput, ...],
    *,
    cleanup_on_success: bool = True,
) -> Iterator[None]:
    """Run every cleanup action without replacing a context-body primary."""

    if not isinstance(cleanup_actions, tuple):
        raise TypeError("context cleanup actions must be a tuple")
    # Construct the cleanup state before the caller can acquire any resource.
    # The finalizer then only installs the observed primary and starts the
    # already-owned plan.
    failures = _OrderedActionState(
        actions=cleanup_actions,
        iteration_failure_label=(
            "publication authenticated cleanup iteration also failed"
        ),
        primary_error=None,
    )
    # An outer ``except`` leaves an ambient value in ``sys.exc_info()``. Track
    # it separately so cleanup-only failure still propagates from a successful
    # context body entered while that unrelated exception is being handled.
    ambient_error = sys.exc_info()[1]
    context_error: BaseException | None = None
    # These values are read after the protected finalizer boundary.  Publish
    # them before entering the caller's body so an exception injected at the
    # first finalizer opcode can never expose an unbound local.
    locally_unwinding = False
    primary_error: BaseException | None = None
    try:
        yield
    except BaseException as error:  # noqa: B036 - preserve exact local primary
        context_error = error
        failures.primary_error = error
        failures.protect_pending_owners()
        raise
    finally:
        try:
            locally_unwinding = context_error is not None
            primary_error = context_error
            failures.primary_error = primary_error
            active_error = sys.exc_info()[1]
            locally_unwinding = context_error is not None or (
                active_error is not None and active_error is not ambient_error
            )
            if primary_error is None and locally_unwinding:
                primary_error = active_error
            failures.primary_error = primary_error
            if cleanup_on_success or locally_unwinding:
                failures.protect_pending_owners()
                _run_ordered_actions(failures)
                _prune_publication_cleanup_owners(failures.primary_error)
        except BaseException as boundary_error:  # noqa: B036 - retain recovery
            failures.retain_once(
                "outer-trampoline-entry",
                failures.iteration_failure_label,
                boundary_error,
            )
            failures.protect_pending_owners()
        if not locally_unwinding and failures.primary_error is not None:
            raise failures.primary_error


def _run_callback_with_post_validations(
    callback: Callable[[], _T],
    post_validations: tuple[tuple[str, Callable[[], None]], ...],
) -> _T:
    """Run one callback and ordered postconditions without losing first-primary.

    Postconditions live in ``finally`` around the callback result store, so
    cancellation after a callback has returned cannot turn an unchecked result
    into success.  Every postcondition is attempted in order.  A callback
    failure remains primary and receives each postcondition failure as a direct
    note; without an earlier failure, the first postcondition failure is
    primary and later failures become its notes.
    """

    if not callable(callback) or not isinstance(post_validations, tuple):
        raise TypeError("callback validation requires callable inputs")
    for label, validation in post_validations:
        if not isinstance(label, str) or not label or not callable(validation):
            raise TypeError("callback post-validations must be labeled callables")
    # Own the complete post-validation plan before invoking callback code.
    failures = _OrderedActionState(
        actions=post_validations,
        iteration_failure_label=(
            "publication callback post-validation iteration also failed"
        ),
        primary_error=None,
    )
    # ``sys.exc_info()`` is inherited from an active ``except`` in a caller.
    # Snapshot that ambient exception so it is never mistaken for a failure
    # raised by this callback or its result/return cancellation window.
    ambient_error = sys.exc_info()[1]
    callback_error: BaseException | None = None
    try:
        result = callback()
        return result
    except BaseException as error:  # noqa: B036 - preserve exact local primary
        callback_error = error
        raise
    finally:
        # The explicit callback state normally identifies the locally active
        # exception.  The active-vs-ambient comparison also covers a new
        # BaseException injected in the result/return seam before the handler
        # can store it.  Let locally active failures keep unwinding via the
        # original bare raise so their traceback is unchanged.
        locally_unwinding = callback_error is not None
        primary_error = callback_error
        failures.primary_error = primary_error
        try:
            active_error = sys.exc_info()[1]
            locally_unwinding = callback_error is not None or (
                active_error is not None and active_error is not ambient_error
            )
            if primary_error is None and locally_unwinding:
                primary_error = active_error
            failures.primary_error = primary_error
            _run_ordered_actions(failures)
        except BaseException as boundary_error:  # noqa: B036 - retain recovery
            failures.retain_once(
                "outer-trampoline-entry",
                failures.iteration_failure_label,
                boundary_error,
            )
        if not locally_unwinding and failures.primary_error is not None:
            raise failures.primary_error


def _run_publication_reader_callback(
    reader: PublicationDirectoryReader,
    callback: Callable[[PublicationDirectoryReader], _T],
    validate_child_binding: Callable[[], None],
) -> _T:
    """Run a reader callback, close escaped streams, and recheck its authority."""

    ambient_error = sys.exc_info()[1]
    callback_error: BaseException | None = None
    try:
        result = callback(reader)
        return result
    except BaseException as error:  # noqa: B036 - preserve exact callback primary
        callback_error = error
        raise
    finally:
        active_error = sys.exc_info()[1]
        locally_unwinding = callback_error is not None or (
            active_error is not None and active_error is not ambient_error
        )
        primary_error = callback_error
        if primary_error is None and locally_unwinding:
            primary_error = active_error
        failures = _OrderedActionState(
            actions=(
                _OrderedAction(
                    label="publication reader escaped file cleanup also failed",
                    action=lambda: reader._close_open_files(primary_error),
                    complete=lambda: all(
                        context._finished for context in reader._lifetime.open_files
                    ),
                    retry_incomplete="cancellation",
                ),
                (
                    "publication reader validity validation also failed",
                    reader._require_valid,
                ),
                (
                    "publication reader child namespace validation also failed",
                    validate_child_binding,
                ),
            ),
            iteration_failure_label=(
                "publication reader post-validation iteration also failed"
            ),
            primary_error=primary_error,
        )
        _run_ordered_actions(failures)
        if not locally_unwinding and failures.primary_error is not None:
            raise failures.primary_error


@dataclass(slots=True)
class _PosixDescriptorRecord:
    descriptor: int
    identity: tuple[int, ...] | None = None


@dataclass(slots=True)
class _ExactResourceCleanupOwner:
    """Retry one exact resource record without sweeping its aggregate owner."""

    action: Callable[[], None]
    complete: Callable[[], bool]

    @property
    def closed(self) -> bool:
        return self.complete() is True

    def close(self) -> None:
        self.action()


class _PosixResourceOwner:
    """Track POSIX descriptors without removing ownership before close.

    Python can recover every failure after an integer has reached a local or
    this owner.  It cannot cover an arbitrary opcode interruption between a raw
    ``os.open``/``os.dup`` return and Python's first STORE; closing that final
    P2 gap requires a native owning object.
    """

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: list[_PosixDescriptorRecord] = []

    @property
    def closed(self) -> bool:
        # A compatibility cleanup path may close a retained descriptor with a
        # previously captured real close function.  Reconcile that observable
        # outcome before reporting ownership completion.
        for record in self._records:
            descriptor = record.descriptor
            if descriptor < 0:
                continue
            try:
                observed = os.fstat(descriptor)
            except OSError as probe_error:
                if probe_error.errno == errno.EBADF:
                    record.descriptor = -1
                continue
            if (
                record.identity is not None
                and _resource_owner_identity(observed) != record.identity
            ):
                record.descriptor = -1
        return all(record.descriptor < 0 for record in self._records)

    def open(
        self,
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        # Publish an empty owner record before the native acquisition.  The
        # only remaining unowned edge is the documented native-return to first
        # STORE_ATTR boundary; record construction and registration can no
        # longer fail after a live descriptor already exists.
        record = _PosixDescriptorRecord(-1)
        self._records.append(record)
        exact_owner = self._exact_record_cleanup_owner(record)

        with _run_context_with_cleanup_actions(
            (
                _OrderedAction(
                    label="descriptor acquisition cleanup also failed",
                    action=exact_owner.close,
                    complete=lambda: exact_owner.closed,
                    retry_incomplete="cancellation",
                    incomplete_owner=exact_owner,
                ),
            ),
            cleanup_on_success=False,
        ):
            if dir_fd is None:
                record.descriptor = os.open(path, flags, mode)
            else:
                record.descriptor = os.open(path, flags, mode, dir_fd=dir_fd)
            record.identity = _resource_owner_identity(os.fstat(record.descriptor))
            return record.descriptor

    def duplicate(self, descriptor: int) -> int:
        record = _PosixDescriptorRecord(-1)
        self._records.append(record)
        exact_owner = self._exact_record_cleanup_owner(record)

        with _run_context_with_cleanup_actions(
            (
                _OrderedAction(
                    label="descriptor duplication cleanup also failed",
                    action=exact_owner.close,
                    complete=lambda: exact_owner.closed,
                    retry_incomplete="cancellation",
                    incomplete_owner=exact_owner,
                ),
            ),
            cleanup_on_success=False,
        ):
            record.descriptor = os.dup(descriptor)
            record.identity = _resource_owner_identity(os.fstat(record.descriptor))
            return record.descriptor

    def close_descriptor(self, descriptor: int) -> None:
        record = self._record_for(descriptor)
        if record is None:
            return
        self.close_record(record)

    def record_for_cleanup(self, descriptor: int) -> _PosixDescriptorRecord:
        record = self._record_for(descriptor)
        if record is None:
            raise RuntimeError("publication descriptor is not owned")
        return record

    def close_record(self, record: _PosixDescriptorRecord) -> None:
        """Close an exact record returned by :meth:`record_for_cleanup`."""

        close_error = self._close_record(record)
        if close_error is not None:
            raise close_error

    def close_record_after_error(
        self,
        record: _PosixDescriptorRecord,
        primary_error: BaseException,
    ) -> None:
        self._run_record_cleanup_after_error(
            self._record_cleanup_state(record, primary_error),
            primary_error,
        )

    def _record_cleanup_complete(self, record: _PosixDescriptorRecord) -> bool:
        descriptor = record.descriptor
        if descriptor < 0:
            return True
        try:
            observed = os.fstat(descriptor)
        except OSError as probe_error:
            if probe_error.errno == errno.EBADF:
                record.descriptor = -1
                return True
            return False
        except BaseException:  # noqa: B036 - uncertain ownership stays retained
            return False
        try:
            observed_identity = _resource_owner_identity(observed)
        except BaseException:  # noqa: B036 - uncertain ownership stays retained
            return False
        if record.identity is not None and observed_identity != record.identity:
            record.descriptor = -1
            return True
        return False

    def _record_cleanup_state(
        self,
        record: _PosixDescriptorRecord,
        primary_error: BaseException,
    ) -> _OrderedActionState:
        exact_owner = self._exact_record_cleanup_owner(record)
        cleanup = _OrderedActionState(
            actions=(
                _OrderedAction(
                    label="descriptor cleanup also failed",
                    action=exact_owner.close,
                    complete=lambda: exact_owner.closed,
                    retry_incomplete="cancellation",
                    incomplete_owner=exact_owner,
                ),
            ),
            iteration_failure_label="descriptor cleanup iteration also failed",
            primary_error=primary_error,
        )
        cleanup.protect_pending_owners()
        return cleanup

    def _exact_record_cleanup_owner(
        self,
        record: _PosixDescriptorRecord,
    ) -> _ExactResourceCleanupOwner:
        return _ExactResourceCleanupOwner(
            action=partial(self.close_record, record),
            complete=partial(self._record_cleanup_complete, record),
        )

    @staticmethod
    def _run_record_cleanup_after_error(
        cleanup: _OrderedActionState,
        primary_error: BaseException,
    ) -> None:
        try:
            cleanup.primary_error = primary_error
            cleanup.protect_pending_owners()
            _run_ordered_actions(cleanup)
            _prune_publication_cleanup_owners(primary_error)
        except BaseException as boundary_error:  # noqa: B036 - keep first
            cleanup.retain_once(
                "outer-trampoline-entry",
                cleanup.iteration_failure_label,
                boundary_error,
            )
            cleanup.protect_pending_owners()

    def close_all(self) -> None:
        primary_error: BaseException | None = None
        for record in reversed(tuple(self._records)):
            close_error = self._close_record(record)
            if close_error is not None:
                primary_error = _retain_first_error(
                    primary_error,
                    "additional descriptor cleanup failed",
                    close_error,
                )
        if primary_error is not None:
            raise primary_error

    def close(self) -> None:
        """Retry cleanup through the shared exception-owner protocol."""

        self.close_all()

    def close_all_after_error(self, primary_error: BaseException) -> None:
        try:
            self.close_all()
        except BaseException as close_error:  # noqa: B036 - keep primary
            _annotate_secondary_error(
                primary_error,
                "publication authority cleanup also failed",
                close_error,
            )
        _attach_publication_cleanup_owner(primary_error, self)

    def _record_for(self, descriptor: int) -> _PosixDescriptorRecord | None:
        for record in reversed(self._records):
            if record.descriptor == descriptor:
                return record
        return None

    def _close_record(
        self,
        record: _PosixDescriptorRecord,
    ) -> BaseException | None:
        descriptor = record.descriptor
        if descriptor < 0:
            return None
        try:
            observed = os.fstat(descriptor)
        except OSError as probe_error:
            if probe_error.errno == errno.EBADF:
                record.descriptor = -1
                return None
            return probe_error
        except BaseException as probe_error:  # noqa: B036 - retain owner
            return probe_error
        observed_identity = _resource_owner_identity(observed)
        if record.identity is None:
            record.identity = observed_identity
        elif observed_identity != record.identity:
            record.descriptor = -1
            return RuntimeError("publication descriptor ownership changed")

        try:
            os.close(descriptor)
            record.descriptor = -1
        except BaseException as close_error:  # noqa: B036 - reconcile close
            if record.descriptor < 0:
                return close_error
            try:
                rebound = os.fstat(descriptor)
            except OSError as probe_error:
                if probe_error.errno == errno.EBADF:
                    record.descriptor = -1
                else:
                    _annotate_secondary_error(
                        close_error,
                        "descriptor close reconciliation failed",
                        probe_error,
                    )
            except BaseException as probe_error:  # noqa: B036 - keep close error
                _annotate_secondary_error(
                    close_error,
                    "descriptor close reconciliation failed",
                    probe_error,
                )
            else:
                if _resource_owner_identity(rebound) != record.identity:
                    # The owned fd closed and the number is observably reused.
                    # Do not close the replacement descriptor.
                    record.descriptor = -1
                # An exact dup2 ABA of the same inode is indistinguishable from
                # a before-close failure in pure Python.  Proving that final P2
                # case requires a native owner for the open file description.
            return close_error
        return None


@dataclass(slots=True)
class _WindowsHandleRecord:
    handle: int
    identity: tuple[int, ...] | None = None


def _windows_handle_is_invalid_error(exc: OSError) -> bool:
    return exc.errno == errno.EBADF or getattr(exc, "winerror", None) == 6


class _WindowsResourceOwner:
    """Track Windows HANDLEs through one-pass, retryable cleanup.

    As with POSIX file descriptors, Python cannot own a raw HANDLE during the
    arbitrary-opcode gap between a native call's integer return and the first
    Python STORE.  Closing that final P2 gap requires a native owning object;
    this class covers failures once the value has reached ``acquire``'s local.
    """

    __slots__ = ("_api", "_records")

    def __init__(self, api: _WindowsKernelApi) -> None:
        self._api = api
        self._records: list[_WindowsHandleRecord] = []

    @property
    def closed(self) -> bool:
        for record in self._records:
            handle = record.handle
            if not handle:
                continue
            try:
                observed = self._api.metadata(handle)
            except KeyError:
                record.handle = 0
                continue
            except OSError as probe_error:
                if _windows_handle_is_invalid_error(probe_error):
                    record.handle = 0
                continue
            if (
                record.identity is not None
                and _resource_owner_identity(observed) != record.identity
            ):
                record.handle = 0
        return all(record.handle == 0 for record in self._records)

    def acquire(self, callback: Callable[[], int]) -> int:
        # As on POSIX, publish the empty record before invoking the native
        # acquisition callback so Python-side construction/registration cannot
        # orphan an already returned HANDLE.
        record = _WindowsHandleRecord(0)
        self._records.append(record)
        exact_owner = self._exact_record_cleanup_owner(record)

        with _run_context_with_cleanup_actions(
            (
                _OrderedAction(
                    label="Windows HANDLE acquisition cleanup also failed",
                    action=exact_owner.close,
                    complete=lambda: exact_owner.closed,
                    retry_incomplete="cancellation",
                    incomplete_owner=exact_owner,
                ),
            ),
            cleanup_on_success=False,
        ):
            record.handle = callback()
            metadata = self._api.metadata(record.handle)
            record.identity = _resource_owner_identity(metadata)
            return record.handle

    def bind_identity(self, handle: int, metadata: _WindowsHandleMetadata) -> None:
        record = self._record_for(handle)
        if record is None:
            raise RuntimeError("Windows HANDLE is not owned")
        record.identity = _resource_owner_identity(metadata)

    def release(self, handle: int) -> int:
        """Relinquish a handle to a raw-return caller at the documented P2 edge."""

        record = self._record_for(handle)
        if record is None:
            raise RuntimeError("Windows HANDLE is not owned")
        record.handle = 0
        return handle

    def close_handle(self, handle: int) -> None:
        record = self._record_for(handle)
        if record is None:
            return
        self.close_record(record)

    def record_for_cleanup(self, handle: int) -> _WindowsHandleRecord:
        record = self._record_for(handle)
        if record is None:
            raise RuntimeError("publication HANDLE is not owned")
        return record

    def close_record(self, record: _WindowsHandleRecord) -> None:
        """Close an exact record returned by :meth:`record_for_cleanup`."""

        close_error = self._close_record(record)
        if close_error is not None:
            raise close_error

    def close_record_after_error(
        self,
        record: _WindowsHandleRecord,
        primary_error: BaseException,
    ) -> None:
        self._run_record_cleanup_after_error(
            self._record_cleanup_state(record, primary_error),
            primary_error,
        )

    def _record_cleanup_complete(self, record: _WindowsHandleRecord) -> bool:
        handle = record.handle
        if not handle:
            return True
        try:
            observed = self._api.metadata(handle)
        except KeyError:
            record.handle = 0
            return True
        except OSError as probe_error:
            if _windows_handle_is_invalid_error(probe_error):
                record.handle = 0
                return True
            return False
        except BaseException:  # noqa: B036 - uncertain ownership stays retained
            return False
        try:
            observed_identity = _resource_owner_identity(observed)
        except BaseException:  # noqa: B036 - uncertain ownership stays retained
            return False
        if record.identity is not None and observed_identity != record.identity:
            record.handle = 0
            return True
        return False

    def _record_cleanup_state(
        self,
        record: _WindowsHandleRecord,
        primary_error: BaseException,
    ) -> _OrderedActionState:
        exact_owner = self._exact_record_cleanup_owner(record)
        cleanup = _OrderedActionState(
            actions=(
                _OrderedAction(
                    label="Windows HANDLE cleanup also failed",
                    action=exact_owner.close,
                    complete=lambda: exact_owner.closed,
                    retry_incomplete="cancellation",
                    incomplete_owner=exact_owner,
                ),
            ),
            iteration_failure_label=("Windows HANDLE cleanup iteration also failed"),
            primary_error=primary_error,
        )
        cleanup.protect_pending_owners()
        return cleanup

    def _exact_record_cleanup_owner(
        self,
        record: _WindowsHandleRecord,
    ) -> _ExactResourceCleanupOwner:
        return _ExactResourceCleanupOwner(
            action=partial(self.close_record, record),
            complete=partial(self._record_cleanup_complete, record),
        )

    @staticmethod
    def _run_record_cleanup_after_error(
        cleanup: _OrderedActionState,
        primary_error: BaseException,
    ) -> None:
        try:
            cleanup.primary_error = primary_error
            cleanup.protect_pending_owners()
            _run_ordered_actions(cleanup)
            _prune_publication_cleanup_owners(primary_error)
        except BaseException as boundary_error:  # noqa: B036 - keep first
            cleanup.retain_once(
                "outer-trampoline-entry",
                cleanup.iteration_failure_label,
                boundary_error,
            )
            cleanup.protect_pending_owners()

    def close_all(self) -> None:
        primary_error: BaseException | None = None
        for record in reversed(tuple(self._records)):
            close_error = self._close_record(record)
            if close_error is not None:
                primary_error = _retain_first_error(
                    primary_error,
                    "additional Windows HANDLE cleanup failed",
                    close_error,
                )
        if primary_error is not None:
            raise primary_error

    def close(self) -> None:
        """Retry cleanup through the shared exception-owner protocol."""

        self.close_all()

    def close_all_after_error(self, primary_error: BaseException) -> None:
        try:
            self.close_all()
        except BaseException as close_error:  # noqa: B036 - keep primary
            _annotate_secondary_error(
                primary_error,
                "Windows authority cleanup also failed",
                close_error,
            )
        _attach_publication_cleanup_owner(primary_error, self)

    def _record_for(self, handle: int) -> _WindowsHandleRecord | None:
        for record in reversed(self._records):
            if record.handle == handle:
                return record
        return None

    def _close_record(
        self,
        record: _WindowsHandleRecord,
    ) -> BaseException | None:
        handle = record.handle
        if not handle:
            return None
        try:
            observed = self._api.metadata(handle)
        except KeyError:
            record.handle = 0
            return None
        except OSError as probe_error:
            if _windows_handle_is_invalid_error(probe_error):
                record.handle = 0
                return None
            return probe_error
        except BaseException as probe_error:  # noqa: B036 - retain owner
            return probe_error
        observed_identity = _resource_owner_identity(observed)
        if record.identity is None:
            record.identity = observed_identity
        elif observed_identity != record.identity:
            record.handle = 0
            return RuntimeError("publication HANDLE ownership changed")

        try:
            self._api.close(handle)
            record.handle = 0
        except BaseException as close_error:  # noqa: B036 - reconcile close
            if not record.handle:
                return close_error
            try:
                rebound = self._api.metadata(handle)
            except KeyError:
                record.handle = 0
            except OSError as probe_error:
                if _windows_handle_is_invalid_error(probe_error):
                    record.handle = 0
                else:
                    _annotate_secondary_error(
                        close_error,
                        "Windows HANDLE close reconciliation failed",
                        probe_error,
                    )
            except BaseException as probe_error:  # noqa: B036 - keep close error
                _annotate_secondary_error(
                    close_error,
                    "Windows HANDLE close reconciliation failed",
                    probe_error,
                )
            else:
                if _resource_owner_identity(rebound) != record.identity:
                    record.handle = 0
                # Exact same-FILE_ID HANDLE reuse is the Windows equivalent of
                # the POSIX dup2 ABA above and likewise needs a native owner.
            return close_error
        return None


class _WindowsLexicalAuthorityOwner:
    """Retain a component-pinned Windows authority across handoff failures."""

    __slots__ = ("_resource",)

    def __init__(self) -> None:
        self._resource: object | None = None

    def own(self, resource: object) -> None:
        current = self._resource
        if current is not None and current is not resource:
            current_handles = getattr(current, "handles", None)
            replacement_handles = getattr(resource, "handles", None)
            if current_handles is not replacement_handles:
                raise RuntimeError("Windows lexical authority ownership changed")
        self._resource = resource

    @property
    def authority(self) -> _windows_fs.WindowsDirectoryAuthority:
        resource = self._resource
        if not isinstance(resource, _windows_fs.WindowsDirectoryAuthority):
            raise RuntimeError("Windows lexical authority was not acquired")
        return resource

    @property
    def closed(self) -> bool:
        resource = self._resource
        if resource is None:
            return True
        handles = getattr(resource, "handles", None)
        if isinstance(handles, list):
            return not handles
        return bool(resource.closed)  # type: ignore[attr-defined]

    def close(self) -> None:
        resource = self._resource
        if resource is None:
            return
        handles = getattr(resource, "handles", None)
        api = getattr(resource, "api", None)
        if not isinstance(handles, list) or api is None:
            resource.close()  # type: ignore[attr-defined]
            return
        if not handles:
            if isinstance(resource, _windows_fs.WindowsDirectoryAuthority):
                resource.closed = True
            return

        stored_identities: dict[int, tuple[int, ...]] = {}
        if isinstance(resource, _windows_fs.WindowsDirectoryAuthority):
            full_identities = {resource.handles[0]: resource.anchor_identity}
            full_identities.update(
                {
                    observation.child_handle: observation.child_identity
                    for observation in resource.observations
                }
            )
            for handle, identity in full_identities.items():
                file_id = identity[1]
                if not isinstance(file_id, bytes):
                    raise RuntimeError("Windows lexical FILE_ID is invalid")
                stored_identities[handle] = (
                    int(identity[0]),
                    1,
                    int.from_bytes(file_id, "big"),
                    stat.S_IFMT(int(identity[2])),
                )

        primary_error: BaseException | None = None
        for handle in reversed(tuple(handles)):
            expected = stored_identities.get(handle)
            if expected is None:
                cleanup_identities = getattr(resource, "expected_identities", {})
                cleanup_identity = cleanup_identities.get(handle)
                try:
                    observed = api.metadata(handle)
                except KeyError:
                    while handle in handles:
                        handles.remove(handle)
                    cleanup_identities.pop(handle, None)
                    continue
                except OSError as probe_error:
                    if _windows_handle_is_invalid_error(probe_error):
                        while handle in handles:
                            handles.remove(handle)
                        cleanup_identities.pop(handle, None)
                        continue
                    primary_error = _retain_first_error(
                        primary_error,
                        "additional Windows lexical HANDLE probe failed",
                        probe_error,
                    )
                    continue
                except BaseException as probe_error:  # noqa: B036
                    primary_error = _retain_first_error(
                        primary_error,
                        "additional Windows lexical HANDLE probe failed",
                        probe_error,
                    )
                    continue
                if (
                    cleanup_identity is not None
                    and (
                        observed.st_dev,
                        observed.file_id_128,
                    )
                    != cleanup_identity
                ):
                    primary_error = _retain_first_error(
                        primary_error,
                        "additional Windows lexical HANDLE ownership changed",
                        RuntimeError("Windows lexical HANDLE ownership changed"),
                    )
                    continue
                expected = _resource_owner_identity(observed)
            record = _WindowsHandleRecord(handle, expected)
            temporary_owner = _WindowsResourceOwner(api)
            close_error = temporary_owner._close_record(record)
            if not record.handle:
                while handle in handles:
                    handles.remove(handle)
                cleanup_identities = getattr(resource, "expected_identities", {})
                cleanup_identities.pop(handle, None)
            if close_error is not None:
                primary_error = _retain_first_error(
                    primary_error,
                    "additional Windows lexical HANDLE cleanup failed",
                    close_error,
                )
        if isinstance(resource, _windows_fs.WindowsDirectoryAuthority):
            resource.closed = not handles
        if primary_error is not None:
            raise primary_error

    def close_after_error(self, primary_error: BaseException) -> None:
        try:
            self.close()
        except BaseException as close_error:  # noqa: B036 - keep primary
            _annotate_secondary_error(
                primary_error,
                "Windows lexical authority cleanup also failed",
                close_error,
            )
        _attach_publication_cleanup_owner(primary_error, self)


class _PublicationAuthorityOwner:
    """Pre-existing slot for cancellation-safe authority handoff."""

    __slots__ = ("_authority", "_pid")

    def __init__(self) -> None:
        self._authority: _PublicationAuthority | None = None
        self._pid = os.getpid()
        # Registration precedes acquisition of any authority resource.  Once
        # install succeeds, an interrupted or persistently failing close stays
        # reachable for an explicit retry instead of becoming an fd/HANDLE
        # leak hidden in a dead stack frame.
        _register_publication_authority_owner(self)

    @property
    def authority(self) -> _PublicationAuthority | None:
        return self._authority

    @property
    def closed(self) -> bool:
        authority = self._authority
        return authority is None or authority._closed

    def install(self, authority: _PublicationAuthority) -> None:
        if self._authority is not None:
            raise RuntimeError("publication authority owner is already active")
        _register_publication_authority_owner(self)
        self._authority = authority

    def close(
        self,
        *,
        _attempt_started: Callable[[], None] | None = None,
    ) -> None:
        if _attempt_started is not None:
            _attempt_started()
        _synchronize_retained_publication_process()
        with _RETAINED_PUBLICATION_AUTHORITY_LOCK:
            authority = self._authority
            if authority is None:
                _forget_publication_authority_owner(self)
                return
            primary_error: BaseException | None = None
            try:
                authority.close()
            except BaseException as close_error:  # noqa: B036 - reconcile owner state
                primary_error = close_error
            try:
                close_complete = authority._closed
            except BaseException as reconciliation_error:  # noqa: B036
                if primary_error is None:
                    primary_error = reconciliation_error
                else:
                    _annotate_secondary_error(
                        primary_error,
                        "publication authority owner reconciliation also failed",
                        reconciliation_error,
                    )
            else:
                if close_complete:
                    self._authority = None
            if self._authority is None:
                try:
                    _forget_publication_authority_owner(self)
                except BaseException as retention_error:  # noqa: B036
                    if primary_error is None:
                        primary_error = retention_error
                    else:
                        _annotate_secondary_error(
                            primary_error,
                            "publication authority retry release also failed",
                            retention_error,
                        )
            if primary_error is not None:
                _attach_publication_cleanup_owner(primary_error, self)
                raise primary_error

    def close_after_error(self, primary_error: BaseException) -> None:
        try:
            self.close()
        except BaseException as close_error:  # noqa: B036 - keep primary
            _annotate_secondary_error(
                primary_error,
                "publication authority owner cleanup also failed",
                close_error,
            )
        _attach_publication_cleanup_owner(primary_error, self)

    def __enter__(self) -> _PublicationAuthorityOwner:
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
            self.close_after_error(exc)


_RETAINED_PUBLICATION_AUTHORITY_OWNERS: list[_PublicationAuthorityOwner] = []
_RETAINED_PUBLICATION_AUTHORITY_LOCK = RLock()
_RETAINED_PUBLICATION_AUTHORITY_PID = os.getpid()


def _synchronize_retained_publication_process() -> None:
    """Re-home inherited retry state after a real or simulated process fork."""

    global _RETAINED_PUBLICATION_AUTHORITY_LOCK
    global _RETAINED_PUBLICATION_AUTHORITY_PID
    pid = os.getpid()
    if pid == _RETAINED_PUBLICATION_AUTHORITY_PID:
        return
    # A lock held by a vanished thread cannot be reused safely in the child.
    _RETAINED_PUBLICATION_AUTHORITY_LOCK = RLock()
    _RETAINED_PUBLICATION_AUTHORITY_PID = pid
    for owner in _RETAINED_PUBLICATION_AUTHORITY_OWNERS:
        owner._pid = pid


def _register_publication_authority_owner(
    owner: _PublicationAuthorityOwner,
) -> None:
    _synchronize_retained_publication_process()
    with _RETAINED_PUBLICATION_AUTHORITY_LOCK:
        owner._pid = os.getpid()
        if any(
            retained is owner for retained in _RETAINED_PUBLICATION_AUTHORITY_OWNERS
        ):
            return
        _RETAINED_PUBLICATION_AUTHORITY_OWNERS.append(owner)


def _forget_publication_authority_owner(
    owner: _PublicationAuthorityOwner,
) -> None:
    _synchronize_retained_publication_process()
    with _RETAINED_PUBLICATION_AUTHORITY_LOCK:
        _RETAINED_PUBLICATION_AUTHORITY_OWNERS[:] = [
            retained
            for retained in _RETAINED_PUBLICATION_AUTHORITY_OWNERS
            if retained is not owner
        ]


def _publication_authority_owner_released(
    owner: _PublicationAuthorityOwner,
) -> bool:
    return owner.authority is None and not any(
        retained is owner for retained in _RETAINED_PUBLICATION_AUTHORITY_OWNERS
    )


@dataclass(slots=True)
class _RetainedAuthorityCloseAttempt:
    """Expose whether a retained owner accepted one close attempt."""

    owner: _PublicationAuthorityOwner
    entered: bool = False

    def _mark_entered(self) -> None:
        self.entered = True

    def __call__(self) -> None:
        self.owner.close(_attempt_started=self._mark_entered)

    def retry_incomplete(self, error: BaseException | None) -> bool:
        """Retry pre-handoff cancellation and interrupted registry release."""

        if error is None or isinstance(error, Exception):
            return False
        if not self.entered:
            return True
        return self.owner.authority is None and not (
            _publication_authority_owner_released(self.owner)
        )


def retry_retained_publication_cleanup() -> None:
    """Retry every retained authority close after publication work is quiescent."""

    _synchronize_retained_publication_process()
    with _RETAINED_PUBLICATION_AUTHORITY_LOCK:
        attempts = tuple(
            _RetainedAuthorityCloseAttempt(owner)
            for owner in tuple(_RETAINED_PUBLICATION_AUTHORITY_OWNERS)
        )
        failures = _OrderedActionState(
            actions=tuple(
                _OrderedAction(
                    label=("additional retained publication authority cleanup failed"),
                    action=attempt,
                    complete=lambda attempt=attempt: (
                        _publication_authority_owner_released(attempt.owner)
                    ),
                    retry_incomplete=attempt.retry_incomplete,
                )
                for attempt in attempts
            ),
            iteration_failure_label=(
                "retained publication cleanup iteration also failed"
            ),
            primary_error=None,
        )
        _run_ordered_actions(failures)
    if failures.primary_error is not None:
        raise failures.primary_error


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_synchronize_retained_publication_process)


class _PublicationAuthority:
    """One pinned parent namespace and its platform-specific operations.

    Child handles never escape this object.  Callers receive a bounded reader
    only for the duration of ``read_child``; this keeps validation and hashing
    on the same parent/source authority without relying on procfs aliases.
    """

    __slots__ = (
        "display_parent",
        "identity",
        "backend_tag",
        "_resource",
        "_close_callback",
        "_metadata_callback",
        "_reader_callback",
        "_rename_callback",
        "_sync_callback",
        "_verify_callback",
        "_close_complete_callback",
        "_close_state",
        "_pid",
    )

    def __init__(
        self,
        *,
        display_parent: Path,
        identity: tuple[int, ...],
        backend_tag: str,
        resource: int,
        close_callback: Callable[[int], None],
        metadata_callback: Callable[[str, Path, str], object | None],
        reader_callback: Callable[
            [
                str,
                Path,
                str,
                _TreeOwnership | None,
                Callable[[_PublicationTreeReader], _T],
            ],
            _T,
        ],
        rename_callback: Callable[[str, str], object | None],
        verify_callback: Callable[[], None],
        close_complete_callback: Callable[[], bool],
        sync_callback: Callable[[], None] | None = None,
    ) -> None:
        self.display_parent = display_parent
        self.identity = identity
        self.backend_tag = backend_tag
        self._resource = resource
        self._close_callback = close_callback
        self._metadata_callback = metadata_callback
        self._reader_callback = reader_callback
        self._rename_callback = rename_callback
        self._sync_callback = (
            sync_callback if sync_callback is not None else lambda: os.fsync(resource)
        )
        self._verify_callback = verify_callback
        self._close_complete_callback = close_complete_callback
        self._close_state = False
        self._pid = os.getpid()

    def _require_process(self) -> None:
        if self._pid != os.getpid():
            raise RuntimeError("publication authority cannot cross a process boundary")

    @property
    def _closed(self) -> bool:
        # Compatibility cleanup may resume the callback directly after an
        # interruption.  Reflect the resource owner's state whenever callers
        # inspect completion so a successful resumed cleanup is not forgotten.
        if not self._close_state and self._close_complete_callback():
            self._close_state = True
        return self._close_state

    @_closed.setter
    def _closed(self, value: bool) -> None:
        self._close_state = value

    @property
    def resource(self) -> int:
        self._require_process()
        if self._closed:
            raise RuntimeError("publication authority is closed")
        return self._resource

    def child_metadata(
        self,
        name: str,
        *,
        path: Path,
        label: str,
    ) -> object | None:
        self._require_process()
        if self._closed:
            raise RuntimeError("publication authority is closed")
        child_name = _simple_child_name(name, label=label)
        return self._metadata_callback(child_name, path, label)

    def read_child(
        self,
        name: str,
        *,
        path: Path,
        label: str,
        expected_ownership: _TreeOwnership | None = None,
        callback: Callable[[_PublicationTreeReader], _T],
    ) -> _T:
        self._require_process()
        if self._closed:
            raise RuntimeError("publication authority is closed")
        child_name = _simple_child_name(name, label=label)
        return self._reader_callback(
            child_name,
            path,
            label,
            expected_ownership,
            callback,
        )

    def capture_child(
        self,
        name: str,
        *,
        path: Path,
        label: str,
        required_root_file: str | None = None,
        allow_empty_root: bool = False,
        entry_policy: DirectoryEntryPolicy | None = None,
        check_cancelled: Callable[[], None] | None = None,
    ) -> _TreeOwnership:
        return self.read_child(
            name,
            path=path,
            label=label,
            expected_ownership=None,
            callback=lambda reader: reader.capture_ownership(
                required_root_file=required_root_file,
                allow_empty_root=allow_empty_root,
                entry_policy=entry_policy,
                check_cancelled=check_cancelled,
            ),
        )

    def rename_noreplace(self, source: str, destination: str) -> object | None:
        self._require_process()
        if self._closed:
            raise RuntimeError("publication authority is closed")
        return self._rename_callback(
            _simple_child_name(source, label="rename source"),
            _simple_child_name(destination, label="rename destination"),
        )

    def verify_path_binding(self) -> None:
        self._require_process()
        if self._closed:
            raise RuntimeError("publication authority is closed")
        self._verify_callback()

    def sync_parent(self) -> None:
        """Durably synchronize the authenticated parent authority."""

        self._require_process()
        if self._closed:
            raise RuntimeError("publication authority is closed")
        self._sync_callback()

    def close(self) -> None:
        if self._close_state:
            return
        primary_error: BaseException | None = None
        try:
            self._close_callback(self._resource)
        except BaseException as close_error:  # noqa: B036 - reconcile owner state
            primary_error = close_error
        try:
            close_complete = self._close_complete_callback()
        except BaseException as reconciliation_error:  # noqa: B036
            if primary_error is None:
                raise
            _annotate_secondary_error(
                primary_error,
                "publication authority close-state reconciliation also failed",
                reconciliation_error,
            )
        else:
            # Cleanup owns the close state.  A before-close interruption or
            # persistent EIO retains the resource and keeps close retryable;
            # an after-close interruption may still mark the authority closed.
            self._close_state = close_complete
        if primary_error is not None:
            raise primary_error

    def __enter__(self) -> _PublicationAuthority:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class _DirectoryOrphanLocator:
    """A display path bound to a parent authority and complete tree token."""

    parent_path: Path
    child_name: str
    backend_tag: str
    parent_identity: tuple[int, ...]
    ownership: _TreeOwnership


@dataclass(frozen=True, slots=True)
class DirectoryOrphan:
    """Bounded isolation receipt for a tree left for cooperative GC.

    ``verified_at_isolation`` records only the publication boundary.  It is not
    a persistent proof: a quiescent GC must capture and verify the tree again.
    """

    locator: _DirectoryOrphanLocator
    ownership_digest: str
    entries: int
    byte_count: int
    verified_at_isolation: bool = True

    @property
    def path(self) -> Path:
        """Return the display path; callers must not treat it as authority."""

        return self.locator.parent_path / self.locator.child_name

    @property
    def parent_identity(self) -> tuple[int, ...]:
        return self.locator.parent_identity

    def reopen(
        self,
        callback: Callable[[_PublicationTreeReader], _T],
    ) -> _T:
        """Reopen this orphan through its parent identity and verify both sides."""

        with _PublicationAuthorityOwner() as authority_owner:
            authority = _open_publication_authority(
                self.locator.parent_path,
                parent_resource=None,
                expected_parent_identity=self.locator.parent_identity,
                authority_owner=authority_owner,
            )
            if authority.backend_tag != self.locator.backend_tag:
                raise RuntimeError("directory orphan platform authority changed")

            def use_verified(reader: _PublicationTreeReader) -> _T:
                before: _TreeOwnership | None = None

                def consume() -> _T:
                    nonlocal before
                    before = reader.capture_ownership()
                    _require_matching_ownership(
                        before,
                        self.locator.ownership,
                        label="directory orphan",
                        allow_root_rename=True,
                    )
                    return callback(reader)

                def validate_after_ownership() -> None:
                    after = reader.capture_ownership()
                    _require_matching_ownership(
                        after,
                        self.locator.ownership,
                        label="directory orphan",
                        allow_root_rename=True,
                    )
                    if before is not None and after != before:
                        raise RuntimeError("directory orphan changed while reopened")

                return _run_callback_with_post_validations(
                    consume,
                    (
                        (
                            "directory orphan post-callback ownership validation "
                            "also failed",
                            validate_after_ownership,
                        ),
                    ),
                )

            return _run_callback_with_post_validations(
                lambda: authority.read_child(
                    self.locator.child_name,
                    path=self.path,
                    label="directory orphan",
                    expected_ownership=self.locator.ownership,
                    callback=use_verified,
                ),
                (
                    (
                        "directory orphan authority path validation also failed",
                        authority.verify_path_binding,
                    ),
                ),
            )

    def rebind(self) -> _TreeOwnership:
        """Return a fresh complete token after authority-bound verification."""

        return self.reopen(lambda reader: reader.capture_ownership())


class _PreviousOutputIdentityLost(RuntimeError):
    """The moved previous tree can no longer be trusted for rollback."""


def lexical_directory_path(path: Path) -> Path:
    """Return one absolute lexical path without following any component."""

    candidate = path.expanduser()
    if candidate.name in {"", ".", ".."}:
        raise ValueError(f"directory path has no safe final component: {path}")
    return Path(os.path.abspath(os.fspath(candidate)))


def _open_posix_publication_authority(
    path: Path,
    *,
    parent_resource: int | None,
    expected_parent_identity: tuple[int, ...] | None,
    create_missing: bool = False,
    authority_owner: _PublicationAuthorityOwner | None = None,
) -> _PublicationAuthority:
    if not _SAFE_OWNERSHIP_DIRECTORY_FDS:
        raise RuntimeError(
            "publication authority requires no-follow directory-fd support"
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    resources = _PosixResourceOwner()
    authority: _PublicationAuthority | None = None
    try:
        if not path.is_absolute() or path.anchor != os.path.sep:
            raise ValueError("publication parent must be an absolute lexical path")
        root_descriptor = resources.open(path.anchor, flags)
        root_identity = _ownership_binding_identity(os.fstat(root_descriptor))
        if not root_identity[0] or not root_identity[1]:
            raise RuntimeError("publication root has no reliable identity")
        path_bindings: list[tuple[int, str, int, tuple[int, ...]]] = []
        path_descriptor = root_descriptor
        for part in path.parts[1:]:
            try:
                before = os.stat(
                    part,
                    dir_fd=path_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not create_missing:
                    raise
                try:
                    os.mkdir(part, mode=0o755, dir_fd=path_descriptor)
                except FileExistsError as exc:
                    raise RuntimeError(
                        "publication parent appeared while it was created"
                    ) from exc
                os.fsync(path_descriptor)
                before = os.stat(
                    part,
                    dir_fd=path_descriptor,
                    follow_symlinks=False,
                )
            if (
                _is_link_or_reparse(before)
                or not stat.S_ISDIR(before.st_mode)
                or not before.st_dev
                or not before.st_ino
            ):
                raise ValueError("publication parent must be a real directory")
            child = resources.open(part, flags, dir_fd=path_descriptor)
            opened_child = os.fstat(child)
            binding_identity = _ownership_binding_identity(opened_child)
            if binding_identity != _ownership_binding_identity(before):
                raise RuntimeError("publication parent changed while it was opened")
            path_bindings.append((path_descriptor, part, child, binding_identity))
            path_descriptor = child

        path_identity = publication_parent_identity(path_descriptor)
        if parent_resource is None:
            owned_descriptor = path_descriptor
        else:
            owned_descriptor = resources.duplicate(parent_resource)
            if publication_parent_identity(owned_descriptor) != path_identity:
                raise RuntimeError("publication parent path changed")
        identity = publication_parent_identity(owned_descriptor)
        if expected_parent_identity is not None and identity != tuple(
            expected_parent_identity
        ):
            raise RuntimeError("publication parent identity does not match authority")

        def verify_callback() -> None:
            if (
                _ownership_binding_identity(os.fstat(root_descriptor)) != root_identity
                or publication_parent_identity(owned_descriptor) != identity
            ):
                raise RuntimeError("publication parent authority changed")
            for parent, name, child, binding_identity in path_bindings:
                try:
                    observed = os.stat(
                        name,
                        dir_fd=parent,
                        follow_symlinks=False,
                    )
                    opened_child = os.fstat(child)
                except OSError as exc:
                    raise RuntimeError("publication parent path changed") from exc
                if (
                    _is_link_or_reparse(observed)
                    or not stat.S_ISDIR(observed.st_mode)
                    or _ownership_binding_identity(observed) != binding_identity
                    or _ownership_binding_identity(opened_child) != binding_identity
                ):
                    raise RuntimeError("publication parent path changed")

        verify_callback()

        def metadata_callback(
            name: str,
            display_path: Path,
            label: str,
        ) -> os.stat_result | None:
            try:
                metadata = os.stat(
                    name,
                    dir_fd=owned_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return None
            if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(
                    f"{label} is not a directory or is a link: {display_path}"
                )
            return metadata

        def reader_callback(
            name: str,
            display_path: Path,
            label: str,
            expected_ownership: _TreeOwnership | None,
            callback: Callable[[_PublicationTreeReader], _T],
        ) -> _T:
            metadata = metadata_callback(name, display_path, label)
            if metadata is None:
                raise RuntimeError(f"{label} disappeared: {display_path}")
            child_descriptor = resources.open(
                name,
                flags,
                dir_fd=owned_descriptor,
            )
            child_record = resources.record_for_cleanup(child_descriptor)
            child_owner = resources._exact_record_cleanup_owner(child_record)
            reader: _PublicationTreeReader | None = None

            def deactivate_reader() -> None:
                if reader is not None:
                    reader._deactivate()

            cleanup_actions = (
                _OrderedAction(
                    label="publication reader deactivation also failed",
                    action=deactivate_reader,
                    complete=lambda: reader is None or not reader._lifetime.active,
                    retry_incomplete="cancellation",
                ),
                _OrderedAction(
                    label="publication child descriptor cleanup also failed",
                    action=child_owner.close,
                    complete=lambda owner=child_owner: owner.closed,
                    retry_incomplete="cancellation",
                    incomplete_owner=child_owner,
                ),
            )
            with _run_context_with_cleanup_actions(cleanup_actions):
                opened_child = os.fstat(child_descriptor)
                if (
                    _is_link_or_reparse(opened_child)
                    or not stat.S_ISDIR(opened_child.st_mode)
                    or _ownership_binding_identity(opened_child)
                    != _ownership_binding_identity(metadata)
                ):
                    raise RuntimeError(f"{label} changed while it was opened")
                root_identity = _directory_inode_identity(opened_child)
                reader = _PublicationTreeReader(
                    display_path,
                    root_identity,
                    lambda root_file, allow_empty, policy, cancelled: (
                        _capture_posix_directory_descriptor(
                            child_descriptor,
                            display_path,
                            resources=resources,
                            required_root_file=root_file,
                            allow_empty_root=allow_empty,
                            entry_policy=policy,
                            check_cancelled=cancelled,
                        )
                    ),
                    lambda relative, max_bytes, expected: _open_posix_authenticated_file(
                        child_descriptor,
                        display_path,
                        relative,
                        resources=resources,
                        max_bytes=max_bytes,
                        expected=expected,
                    ),
                    expected_ownership,
                    _capture_supports_cancellation=True,
                )

                def validate_child_binding() -> None:
                    after = os.stat(
                        name,
                        dir_fd=owned_descriptor,
                        follow_symlinks=False,
                    )
                    if _directory_inode_identity(after) != root_identity:
                        raise RuntimeError(f"{label} namespace binding changed")

                return _run_publication_reader_callback(
                    reader,
                    callback,
                    validate_child_binding,
                )

        def rename_callback(source: str, destination: str) -> None:
            _rename_noreplace_at(
                source,
                destination,
                owned_descriptor,
                owned_descriptor,
            )

        def close_resources(_resource: int) -> None:
            resources.close_all()

        authority = _PublicationAuthority(
            display_parent=path,
            identity=identity,
            backend_tag=(
                "linux-renameat2"
                if sys.platform.startswith("linux")
                else "darwin-renameatx-np"
            ),
            resource=owned_descriptor,
            close_callback=close_resources,
            metadata_callback=metadata_callback,
            reader_callback=reader_callback,
            rename_callback=rename_callback,
            sync_callback=lambda: os.fsync(owned_descriptor),
            verify_callback=verify_callback,
            close_complete_callback=lambda: resources.closed,
        )
        if authority_owner is not None:
            authority_owner.install(authority)
        return authority
    except BaseException as primary_error:
        if (
            authority is not None
            and authority_owner is not None
            and authority_owner.authority is authority
        ):
            authority_owner.close_after_error(primary_error)
        else:
            resources.close_all_after_error(primary_error)
        raise


def _open_publication_authority(
    path: Path,
    *,
    parent_resource: int | None,
    expected_parent_identity: tuple[int, ...] | None,
    create_missing: bool = False,
    authority_owner: _PublicationAuthorityOwner | None = None,
) -> _PublicationAuthority:
    """Open a platform authority before any namespace mutation."""

    _require_rename_noreplace_platform()
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        return _open_posix_publication_authority(
            path,
            parent_resource=parent_resource,
            expected_parent_identity=expected_parent_identity,
            create_missing=create_missing,
            authority_owner=authority_owner,
        )
    if sys.platform == "win32":
        if create_missing:
            raise RuntimeError(
                "safe creation of publication parents is unavailable on Windows"
            )
        return _open_windows_publication_authority(
            path,
            parent_resource=parent_resource,
            expected_parent_identity=expected_parent_identity,
            authority_owner=authority_owner,
        )
    raise RuntimeError("atomic directory publication is unsupported on this host")


def _adopt_native_posix_publication_authority(
    path: Path,
    *,
    native_owner: object,
    publication_permit: object,
    authority_owner: _PublicationAuthorityOwner,
) -> _PublicationAuthority:
    """Borrow publication callbacks from one exact native aggregate owner.

    The native aggregate remains the sole descriptor owner.  Borrowing these
    integers creates no Python close authority and performs no ``open`` or
    ``dup`` operation during adoption.
    """

    if not sys.platform.startswith("linux"):
        raise RuntimeError("native workspace adoption requires Linux")
    if authority_owner.authority is not None:
        raise RuntimeError("publication authority owner is already active")
    owner = _native_workspace_owner.require_exact_owner(native_owner)
    rename_with_permit = _native_workspace_owner._bind_owner_publish_permit(
        publication_permit
    )
    parent_descriptor = _native_workspace_owner.borrow_owner_parent_descriptor(owner)
    root_descriptor = _native_workspace_owner.borrow_owner_root_descriptor(owner)
    identity = publication_parent_identity(parent_descriptor)
    root_identity = _directory_inode_identity(os.fstat(root_descriptor))

    def verify_callback() -> None:
        _native_workspace_owner.verify_owner_authority(owner)
        if publication_parent_identity(parent_descriptor) != identity:
            raise RuntimeError("native workspace parent authority changed")
        if _directory_inode_identity(os.fstat(root_descriptor)) != root_identity:
            raise RuntimeError("native workspace root authority changed")

    def metadata_callback(
        name: str,
        display_path: Path,
        label: str,
    ) -> os.stat_result | None:
        try:
            metadata = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{label} is not a directory or is a link: {display_path}")
        return metadata

    def reader_callback(
        name: str,
        display_path: Path,
        label: str,
        expected_ownership: _TreeOwnership | None,
        callback: Callable[[_PublicationTreeReader], _T],
    ) -> _T:
        metadata = metadata_callback(name, display_path, label)
        if metadata is None:
            raise RuntimeError(f"{label} disappeared: {display_path}")
        if _directory_inode_identity(metadata) != root_identity:
            raise RuntimeError(f"{label} differs from the native workspace root")
        verify_callback()
        reader: _PublicationTreeReader | None = None

        def deactivate_reader() -> None:
            if reader is not None:
                reader._deactivate()

        cleanup_actions = (
            _OrderedAction(
                label="publication reader deactivation also failed",
                action=deactivate_reader,
                complete=lambda: reader is None or not reader._lifetime.active,
                retry_incomplete="cancellation",
            ),
        )
        with _run_context_with_cleanup_actions(cleanup_actions):
            reader = _PublicationTreeReader(
                display_path,
                root_identity,
                lambda required_root_file, allow_empty_root, entry_policy, check_cancelled: (
                    _capture_posix_directory_descriptor(
                        root_descriptor,
                        display_path,
                        required_root_file=required_root_file,
                        allow_empty_root=allow_empty_root,
                        entry_policy=entry_policy,
                        check_cancelled=check_cancelled,
                    )
                ),
                lambda relative, max_bytes, expected: (
                    _open_posix_authenticated_file(
                        root_descriptor,
                        display_path,
                        relative,
                        max_bytes=max_bytes,
                        expected=expected,
                    )
                ),
                expected_ownership,
                _capture_supports_cancellation=True,
            )

            def validate_child_binding() -> None:
                observed = metadata_callback(name, display_path, label)
                if (
                    observed is None
                    or _directory_inode_identity(observed) != root_identity
                ):
                    raise RuntimeError(f"{label} namespace binding changed")
                verify_callback()

            return _run_publication_reader_callback(
                reader,
                callback,
                validate_child_binding,
            )

    def rename_callback(source: str, destination: str) -> object | None:
        return rename_with_permit(
            os.fsencode(source),
            os.fsencode(destination),
        )

    facade_closed = False

    def close_callback(_resource: int) -> None:
        nonlocal facade_closed
        facade_closed = True

    authority = _PublicationAuthority(
        display_parent=path,
        identity=identity,
        backend_tag="linux-native-workspace-owner",
        resource=parent_descriptor,
        close_callback=close_callback,
        metadata_callback=metadata_callback,
        reader_callback=reader_callback,
        rename_callback=rename_callback,
        verify_callback=verify_callback,
        close_complete_callback=lambda: facade_closed,
        sync_callback=lambda: _native_workspace_owner.sync_owner_parent(owner),
    )
    try:
        verify_callback()
        authority_owner.install(authority)
    except BaseException as primary_error:  # noqa: B036 - facade-only cleanup
        if authority_owner.authority is authority:
            authority_owner.close_after_error(primary_error)
        raise
    return authority


class _NativeReplacementPublication:
    """One borrowed dual-root view of a native replacement transaction.

    The native aggregate remains the sole descriptor owner.  Before receipt
    settlement this facade authenticates the candidate and incumbent through
    distinct, already-borrowed root descriptors.  After settlement it becomes
    candidate-only: the displaced incumbent is represented solely by its
    independently reopenable :class:`DirectoryOrphan` locator.
    """

    __slots__ = (
        "candidate_descriptor",
        "candidate_identity",
        "destination_name",
        "display_parent",
        "exchanged",
        "incumbent_descriptor",
        "incumbent_identity",
        "native_owner",
        "parent_descriptor",
        "parent_identity",
        "receipted",
        "replacement_slot",
        "_authority_pair_verifier",
        "_exchange_callback",
        "_native_state_callback",
        "_verify_native_callback",
        "_verify_parent_path_callback",
    )

    def __init__(
        self,
        *,
        display_parent: Path,
        native_owner: object,
        parent_descriptor: int,
        candidate_descriptor: int,
        incumbent_descriptor: int,
        destination_name: str,
        replacement_slot: str,
        exchange_callback: Callable[[bytes, bytes, int], object],
        expected_parent_identity: tuple[int, ...],
        expected_incumbent_identity: tuple[int, ...],
    ) -> None:
        if not sys.platform.startswith("linux"):
            raise RuntimeError("native workspace replacement requires Linux")
        self.display_parent = lexical_directory_path(display_parent)
        self.native_owner = _native_workspace_owner.require_exact_owner(native_owner)
        self.parent_descriptor = parent_descriptor
        self.candidate_descriptor = candidate_descriptor
        self.incumbent_descriptor = incumbent_descriptor
        self.destination_name = _simple_child_name(
            destination_name,
            label="workspace replacement destination",
        )
        self.replacement_slot = _simple_child_name(
            replacement_slot,
            label="workspace replacement slot",
        )
        if self.destination_name == self.replacement_slot:
            raise ValueError("workspace replacement roots must have distinct names")
        if not callable(exchange_callback):
            raise TypeError("workspace replacement exchange callback is invalid")
        self._exchange_callback = exchange_callback
        verify_native_exact = _native_workspace_owner.verify_owner_authority
        native_state_exact = _native_workspace_owner.owner_state
        verify_parent_path_exact = _require_publication_parent_path
        self._verify_native_callback = lambda: verify_native_exact(self.native_owner)
        self._native_state_callback = lambda: native_state_exact(self.native_owner)
        self._verify_parent_path_callback = lambda: verify_parent_path_exact(
            self.display_parent,
            self.parent_identity,
        )
        self._authority_pair_verifier: (
            Callable[[_PublicationAuthority], None] | None
        ) = None
        self.parent_identity = publication_parent_identity(parent_descriptor)
        self.candidate_identity = self._descriptor_identity(
            candidate_descriptor,
            label="workspace replacement candidate",
        )
        self.incumbent_identity = self._descriptor_identity(
            incumbent_descriptor,
            label="workspace replacement incumbent",
        )
        if self.parent_identity != expected_parent_identity:
            raise RuntimeError("workspace replacement parent authority changed")
        if self.incumbent_identity != expected_incumbent_identity:
            raise RuntimeError("workspace replacement incumbent authority changed")
        if self.candidate_identity == self.incumbent_identity:
            raise RuntimeError("workspace replacement roots are not distinct")
        if (
            self.candidate_identity[0] != self.parent_identity[0]
            or self.incumbent_identity[0] != self.parent_identity[0]
        ):
            raise ValueError("workspace replacement roots cross the parent device")
        self.exchanged = False
        self.receipted = False
        self.verify_current()

    @staticmethod
    def _descriptor_identity(descriptor: int, *, label: str) -> tuple[int, ...]:
        if type(descriptor) is not int or descriptor < 0:
            raise ValueError(f"{label} descriptor is invalid")
        metadata = os.fstat(descriptor)
        if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"{label} descriptor is not a directory")
        identity = _directory_inode_identity(metadata)
        if not identity[0] or not identity[1]:
            raise RuntimeError(f"{label} has no reliable identity")
        return identity

    def _linked_identity(self, name: str, *, label: str) -> tuple[int, ...]:
        try:
            metadata = os.stat(
                name,
                dir_fd=self.parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise RuntimeError(f"{label} namespace binding changed") from exc
        if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"{label} namespace binding changed")
        return _directory_inode_identity(metadata)

    def _verify_descriptor_identities(self) -> None:
        if publication_parent_identity(self.parent_descriptor) != self.parent_identity:
            raise RuntimeError("workspace replacement parent authority changed")
        if (
            self._descriptor_identity(
                self.candidate_descriptor,
                label="workspace replacement candidate",
            )
            != self.candidate_identity
            or self._descriptor_identity(
                self.incumbent_descriptor,
                label="workspace replacement incumbent",
            )
            != self.incumbent_identity
        ):
            raise RuntimeError("workspace replacement descriptor authority changed")

    def _verify_candidate_only(self) -> None:
        if not self.receipted or not self.exchanged:
            raise RuntimeError("workspace replacement is not receipted")
        if publication_parent_identity(self.parent_descriptor) != self.parent_identity:
            raise RuntimeError("workspace replacement parent authority changed")
        if (
            self._descriptor_identity(
                self.candidate_descriptor,
                label="workspace replacement candidate",
            )
            != self.candidate_identity
            or self._linked_identity(
                self.destination_name,
                label="workspace replacement candidate",
            )
            != self.candidate_identity
        ):
            raise RuntimeError("workspace replacement candidate authority changed")
        self._verify_parent_path_callback()

    def verify_current(self) -> None:
        if self.receipted:
            self._verify_candidate_only()
            return
        self._verify_native_callback()
        self._verify_descriptor_identities()
        expected_candidate = (
            self.destination_name if self.exchanged else self.replacement_slot
        )
        expected_incumbent = (
            self.replacement_slot if self.exchanged else self.destination_name
        )
        if (
            self._linked_identity(
                expected_candidate,
                label="workspace replacement candidate",
            )
            != self.candidate_identity
            or self._linked_identity(
                expected_incumbent,
                label="workspace replacement incumbent",
            )
            != self.incumbent_identity
        ):
            raise RuntimeError("workspace replacement dual-root binding changed")

    def _read_descriptor(
        self,
        *,
        descriptor: int,
        identity: tuple[int, ...],
        name: str,
        display_path: Path,
        label: str,
        expected_ownership: _TreeOwnership | None,
        callback: Callable[[_PublicationTreeReader], _T],
        verify: Callable[[], None],
    ) -> _T:
        verify()
        if self._linked_identity(name, label=label) != identity:
            raise RuntimeError(f"{label} namespace binding changed")
        reader: _PublicationTreeReader | None = None

        def deactivate_reader() -> None:
            if reader is not None:
                reader._deactivate()

        cleanup_actions = (
            _OrderedAction(
                label="publication reader deactivation also failed",
                action=deactivate_reader,
                complete=lambda: reader is None or not reader._lifetime.active,
                retry_incomplete="cancellation",
            ),
        )
        with _run_context_with_cleanup_actions(cleanup_actions):
            reader = _PublicationTreeReader(
                display_path,
                identity,
                lambda required_root_file, allow_empty_root, entry_policy, check_cancelled: (
                    _capture_posix_directory_descriptor(
                        descriptor,
                        display_path,
                        required_root_file=required_root_file,
                        allow_empty_root=allow_empty_root,
                        entry_policy=entry_policy,
                        check_cancelled=check_cancelled,
                    )
                ),
                lambda relative, max_bytes, expected: (
                    _open_posix_authenticated_file(
                        descriptor,
                        display_path,
                        relative,
                        max_bytes=max_bytes,
                        expected=expected,
                    )
                ),
                expected_ownership,
                _capture_supports_cancellation=True,
            )

            def validate_binding() -> None:
                if self._linked_identity(name, label=label) != identity:
                    raise RuntimeError(f"{label} namespace binding changed")
                verify()

            return _run_publication_reader_callback(
                reader,
                callback,
                validate_binding,
            )

    @property
    def candidate_name(self) -> str:
        return self.destination_name if self.exchanged else self.replacement_slot

    @property
    def incumbent_name(self) -> str:
        if self.receipted:
            raise RuntimeError("receipted replacement has no incumbent reader")
        return self.replacement_slot if self.exchanged else self.destination_name

    def candidate_metadata(
        self,
        name: str,
        display_path: Path,
        label: str,
    ) -> os.stat_result:
        if name != self.candidate_name:
            raise RuntimeError(f"{label} is not the replacement candidate")
        self.verify_current()
        metadata = os.stat(
            name,
            dir_fd=self.parent_descriptor,
            follow_symlinks=False,
        )
        if _directory_inode_identity(metadata) != self.candidate_identity:
            raise RuntimeError(f"{label} namespace binding changed: {display_path}")
        return metadata

    def read_candidate(
        self,
        name: str,
        display_path: Path,
        label: str,
        expected_ownership: _TreeOwnership | None,
        callback: Callable[[_PublicationTreeReader], _T],
    ) -> _T:
        if name != self.candidate_name:
            raise RuntimeError(f"{label} is not the replacement candidate")
        return self._read_descriptor(
            descriptor=self.candidate_descriptor,
            identity=self.candidate_identity,
            name=name,
            display_path=display_path,
            label=label,
            expected_ownership=expected_ownership,
            callback=callback,
            verify=self.verify_current,
        )

    def read_incumbent(
        self,
        name: str,
        display_path: Path,
        label: str,
        expected_ownership: _TreeOwnership | None,
        callback: Callable[[_PublicationTreeReader], _T],
    ) -> _T:
        if self.receipted or name != self.incumbent_name:
            raise RuntimeError(f"{label} is not the replacement incumbent")
        return self._read_descriptor(
            descriptor=self.incumbent_descriptor,
            identity=self.incumbent_identity,
            name=name,
            display_path=display_path,
            label=label,
            expected_ownership=expected_ownership,
            callback=callback,
            verify=self.verify_current,
        )

    def capture_incumbent(
        self,
        *,
        path: Path,
        label: str,
        check_cancelled: Callable[[], None] | None = None,
    ) -> _TreeOwnership:
        return self.read_incumbent(
            self.incumbent_name,
            path,
            label,
            None,
            lambda reader: reader.capture_ownership(
                allow_empty_root=True,
                check_cancelled=check_cancelled,
            ),
        )

    def exchange(self, deadline_ns: int) -> object:
        if self.receipted or self.exchanged:
            raise RuntimeError("workspace replacement exchange is unavailable")
        token = self._exchange_callback(
            os.fsencode(self.replacement_slot),
            os.fsencode(self.destination_name),
            deadline_ns,
        )
        self.exchanged = True
        return token

    def mark_receipted(self) -> None:
        if not self.exchanged:
            raise RuntimeError("workspace replacement was not exchanged")
        if self._native_state_callback() != "replacement-receipted":
            raise RuntimeError("native workspace replacement is not receipted")
        self.receipted = True

    def _seal_publication_authority(
        self,
        authority: _PublicationAuthority,
        *,
        metadata_callback: Callable[[str, Path, str], object | None],
        reader_callback: Callable[
            [
                str,
                Path,
                str,
                _TreeOwnership | None,
                Callable[[_PublicationTreeReader], _T],
            ],
            _T,
        ],
        verify_callback: Callable[[], None],
    ) -> None:
        """One-shot seal this context to its exact callback authority."""

        if self._authority_pair_verifier is not None:
            raise RuntimeError("workspace replacement authority is already sealed")
        if (
            getattr(metadata_callback, "__self__", None) is not self
            or getattr(reader_callback, "__self__", None) is not self
            or getattr(verify_callback, "__self__", None) is not self
        ):
            raise RuntimeError("workspace replacement callbacks are not bound")
        authority_type = _PublicationAuthority
        expected_parent = self.display_parent
        expected_identity = self.parent_identity
        expected_resource = self.parent_descriptor

        def verify_pair(candidate: _PublicationAuthority) -> None:
            if type(candidate) is not authority_type or candidate is not authority:
                raise TypeError("workspace replacement authority pairing is invalid")
            if (
                candidate.display_parent != expected_parent
                or candidate.identity != expected_identity
                or candidate.backend_tag != _NATIVE_REPLACEMENT_PUBLICATION_BACKEND
                or candidate._resource != expected_resource
                or candidate._metadata_callback is not metadata_callback
                or candidate._reader_callback is not reader_callback
                or candidate._verify_callback is not verify_callback
            ):
                raise RuntimeError("workspace replacement authority seal changed")

        verify_pair(authority)
        self._authority_pair_verifier = verify_pair


_NATIVE_REPLACEMENT_PUBLICATION_TYPE = _NativeReplacementPublication


def _adopt_native_posix_replacement_authority(
    path: Path,
    *,
    native_owner: object,
    parent_descriptor: int,
    candidate_descriptor: int,
    incumbent_descriptor: int,
    expected_parent_identity: tuple[int, ...],
    expected_incumbent_identity: tuple[int, ...],
    destination_name: str,
    replacement_slot: str,
    exchange_callback: Callable[[bytes, bytes, int], object],
    authority_owner: _PublicationAuthorityOwner,
) -> tuple[_PublicationAuthority, _NativeReplacementPublication]:
    """Adopt separate candidate/incumbent readers from one native owner."""

    if not sys.platform.startswith("linux"):
        raise RuntimeError("native workspace replacement adoption requires Linux")
    if authority_owner.authority is not None:
        raise RuntimeError("publication authority owner is already active")
    owner = _native_workspace_owner.require_exact_owner(native_owner)
    replacement = _NativeReplacementPublication(
        display_parent=path,
        native_owner=owner,
        parent_descriptor=parent_descriptor,
        candidate_descriptor=candidate_descriptor,
        incumbent_descriptor=incumbent_descriptor,
        destination_name=destination_name,
        replacement_slot=replacement_slot,
        exchange_callback=exchange_callback,
        expected_parent_identity=expected_parent_identity,
        expected_incumbent_identity=expected_incumbent_identity,
    )

    def reject_generic_rename(_source: str, _destination: str) -> object | None:
        raise RuntimeError(
            "native workspace replacement requires its dedicated exchange path"
        )

    def reject_generic_sync() -> None:
        raise RuntimeError(
            "native workspace replacement parent sync is owned by settlement"
        )

    facade_closed = False

    def close_callback(_resource: int) -> None:
        nonlocal facade_closed
        facade_closed = True

    metadata_callback = replacement.candidate_metadata
    reader_callback = replacement.read_candidate
    verify_callback = replacement.verify_current
    authority = _PublicationAuthority(
        display_parent=replacement.display_parent,
        identity=replacement.parent_identity,
        backend_tag=_NATIVE_REPLACEMENT_PUBLICATION_BACKEND,
        resource=parent_descriptor,
        close_callback=close_callback,
        metadata_callback=metadata_callback,
        reader_callback=reader_callback,
        rename_callback=reject_generic_rename,
        verify_callback=verify_callback,
        close_complete_callback=lambda: facade_closed,
        sync_callback=reject_generic_sync,
    )
    try:
        replacement._seal_publication_authority(
            authority,
            metadata_callback=metadata_callback,
            reader_callback=reader_callback,
            verify_callback=verify_callback,
        )
        replacement.verify_current()
        authority_owner.install(authority)
    except BaseException as primary_error:  # noqa: B036 - facade-only cleanup
        if authority_owner.authority is authority:
            authority_owner.close_after_error(primary_error)
        raise
    return authority, replacement


def _publish_native_replacement_with_authority(
    publication_authority: _PublicationAuthority,
    replacement: _NativeReplacementPublication,
    stage: Path,
    destination: Path,
    *,
    expected_stage_root_ownership: _TreeOwnership,
    expected_destination_ownership: _TreeOwnership,
    deadline_ns: int,
    validate_staged_directory: (
        Callable[[PublicationDirectoryReader], None] | None
    ) = None,
    validate_published_destination: (
        Callable[[PublicationDirectoryReader], None] | None
    ) = None,
    commit_callback: Callable[
        [_TreeOwnership, _TreeOwnership, DirectoryOrphan, object],
        None,
    ],
    check_cancelled: Callable[[], None] | None = None,
) -> DirectoryOrphan:
    """Publish one native replacement without generic isolation or sync."""

    stage_display = lexical_directory_path(stage)
    destination_display = lexical_directory_path(destination)
    if type(replacement) is not _NATIVE_REPLACEMENT_PUBLICATION_TYPE:
        raise TypeError("workspace replacement context is invalid")
    verify_authority_pair_exact = replacement._authority_pair_verifier
    if not callable(verify_authority_pair_exact):
        raise RuntimeError("workspace replacement authority is not sealed")
    verify_authority_pair_exact(publication_authority)
    if publication_authority.backend_tag != _NATIVE_REPLACEMENT_PUBLICATION_BACKEND:
        raise TypeError("workspace replacement publication authority is invalid")
    if stage_display.parent != destination_display.parent:
        raise ValueError("workspace replacement roots must share one parent")
    if stage_display.parent != publication_authority.display_parent:
        raise ValueError("workspace replacement roots differ from their authority")
    if (
        stage_display.name != replacement.replacement_slot
        or destination_display.name != replacement.destination_name
    ):
        raise ValueError("workspace replacement names differ from their authority")
    if type(expected_stage_root_ownership) is not _TreeOwnership:
        raise TypeError("workspace replacement candidate ownership is invalid")
    if type(expected_destination_ownership) is not _TreeOwnership:
        raise TypeError("workspace replacement incumbent ownership is invalid")
    if type(deadline_ns) is not int or deadline_ns <= 0:
        raise ValueError("workspace replacement deadline is invalid")
    if validate_staged_directory is not None and not callable(
        validate_staged_directory
    ):
        raise TypeError("workspace replacement staged validator is invalid")
    if validate_published_destination is not None and not callable(
        validate_published_destination
    ):
        raise TypeError("workspace replacement published validator is invalid")
    if not callable(commit_callback):
        raise TypeError("workspace replacement commit callback is invalid")
    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("workspace replacement cancellation check must be callable")

    # Freeze every authority method, capability receiver, ownership check, and
    # receipt constructor before either validator runs.  A validator may
    # mutate public/private module or class attributes for fault injection, but
    # it cannot intercept the native exchange/receipt token in this frame.
    verify_replacement_exact = replacement.verify_current
    capture_incumbent_exact = replacement.capture_incumbent
    exchange_replacement_exact = replacement.exchange
    read_candidate_exact = publication_authority.read_child
    verify_path_binding_exact = publication_authority.verify_path_binding
    capture_ownership_exact = _PublicationTreeReader.capture_ownership
    require_publishable_exact = require_publishable_directory_ownership
    require_matching_exact = _require_matching_ownership
    locator_type = _DirectoryOrphanLocator
    locator_new = object.__new__
    locator_init = locator_type.__init__
    orphan_type = DirectoryOrphan
    orphan_new = object.__new__
    orphan_init = orphan_type.__init__

    def capture_candidate_exact(
        name: str,
        *,
        path: Path,
        label: str,
        cancellation_check: Callable[[], None] | None = None,
    ) -> _TreeOwnership:
        return read_candidate_exact(
            name,
            path=path,
            label=label,
            expected_ownership=None,
            callback=lambda reader: capture_ownership_exact(
                reader,
                allow_empty_root=True,
                check_cancelled=cancellation_check,
            ),
        )

    verify_replacement_exact()
    if check_cancelled is not None:
        check_cancelled()
    staged = capture_candidate_exact(
        stage_display.name,
        path=stage_display,
        label="workspace replacement candidate",
        cancellation_check=check_cancelled,
    )
    require_publishable_exact(
        staged,
        label="workspace replacement candidate",
    )
    if staged != expected_stage_root_ownership:
        raise RuntimeError("workspace replacement candidate changed before exchange")
    if check_cancelled is not None:
        check_cancelled()
    if validate_staged_directory is not None:
        read_candidate_exact(
            stage_display.name,
            path=stage_display,
            label="workspace replacement candidate",
            expected_ownership=staged,
            callback=validate_staged_directory,
        )
        verify_authority_pair_exact(publication_authority)
        validated_staged = capture_candidate_exact(
            stage_display.name,
            path=stage_display,
            label="workspace replacement candidate",
            cancellation_check=check_cancelled,
        )
        if validated_staged != staged:
            raise RuntimeError(
                "workspace replacement candidate changed during validation"
            )
        if check_cancelled is not None:
            check_cancelled()

    incumbent = (
        capture_incumbent_exact(
            path=destination_display,
            label="workspace replacement incumbent",
        )
        if check_cancelled is None
        else capture_incumbent_exact(
            path=destination_display,
            label="workspace replacement incumbent",
            check_cancelled=check_cancelled,
        )
    )
    if incumbent != expected_destination_ownership:
        raise RuntimeError("workspace replacement incumbent changed before exchange")
    verify_authority_pair_exact(publication_authority)
    verify_replacement_exact()
    if check_cancelled is not None:
        check_cancelled()

    receipt_token = exchange_replacement_exact(deadline_ns)
    verify_replacement_exact()

    published = capture_candidate_exact(
        destination_display.name,
        path=destination_display,
        label="published workspace replacement candidate",
    )
    require_matching_exact(
        published,
        staged,
        label="published workspace replacement candidate",
        allow_root_rename=True,
    )
    if validate_published_destination is not None:
        read_candidate_exact(
            destination_display.name,
            path=destination_display,
            label="published workspace replacement candidate",
            expected_ownership=published,
            callback=validate_published_destination,
        )
        verify_authority_pair_exact(publication_authority)
    published = capture_candidate_exact(
        destination_display.name,
        path=destination_display,
        label="published workspace replacement candidate",
    )
    require_matching_exact(
        published,
        staged,
        label="published workspace replacement candidate",
        allow_root_rename=True,
    )

    displaced = capture_incumbent_exact(
        path=stage_display,
        label="displaced workspace incumbent",
    )
    require_matching_exact(
        displaced,
        incumbent,
        label="displaced workspace incumbent",
        allow_root_rename=True,
    )
    locator = locator_new(locator_type)
    locator_init(
        locator,
        parent_path=publication_authority.display_parent,
        child_name=stage_display.name,
        # Reopening uses the ordinary Linux parent authority.  The native
        # replacement backend exists only for this live transaction.
        backend_tag="linux-renameat2",
        parent_identity=publication_authority.identity,
        ownership=displaced,
    )
    previous_orphan = orphan_new(orphan_type)
    orphan_init(
        previous_orphan,
        locator=locator,
        ownership_digest=displaced.digest,
        entries=displaced.entries,
        byte_count=displaced.byte_count,
        verified_at_isolation=True,
    )
    verify_replacement_exact()
    verify_authority_pair_exact(publication_authority)
    verify_path_binding_exact()
    commit_callback(staged, published, previous_orphan, receipt_token)
    return previous_orphan


def _require_publication_parent_path(
    path: Path,
    expected_identity: tuple[int, ...],
) -> None:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise RuntimeError("publication parent path changed") from exc
    if _directory_inode_identity(observed) != expected_identity:
        raise RuntimeError("publication parent path changed")


def _directory_or_missing_at(
    parent_authority: _PublicationAuthority | int,
    name: str,
    *,
    path: Path,
    label: str,
) -> object | None:
    if isinstance(parent_authority, _PublicationAuthority):
        return parent_authority.child_metadata(name, path=path, label=label)
    try:
        metadata = os.stat(
            name,
            dir_fd=parent_authority,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} is not a directory or is a link: {path}")
    return metadata


def _random_orphan_path(
    *,
    display_parent: Path,
    destination_name: str,
    purpose: str,
) -> Path:
    destination_digest = hashlib.sha256(os.fsencode(destination_name)).hexdigest()[:16]
    compatible_prefix = f".{destination_name}.{purpose}-"
    if len(os.fsencode(compatible_prefix)) + 32 <= _MAX_OWNERSHIP_COMPONENT_BYTES:
        prefix = compatible_prefix
    else:
        prefix = f".codenib-{purpose}-{destination_digest}-"
    name = f"{prefix}{secrets.token_hex(16)}"
    _simple_child_name(name, label="directory orphan")
    return display_parent / name


def _claim_child_as_orphan(
    parent_authority: _PublicationAuthority | int,
    source_name: str,
    *,
    display_parent: Path,
    destination_name: str,
    purpose: str,
) -> Path:
    for _attempt in range(_MAX_ORPHAN_NAME_ATTEMPTS):
        orphan = _random_orphan_path(
            display_parent=display_parent,
            destination_name=destination_name,
            purpose=purpose,
        )
        try:
            if isinstance(parent_authority, _PublicationAuthority):
                parent_authority.rename_noreplace(source_name, orphan.name)
            else:
                _rename_noreplace_at(
                    source_name,
                    orphan.name,
                    parent_authority,
                    parent_authority,
                )
        except FileExistsError:
            continue
        except BaseException:
            # A signal or injected failure can arrive after the kernel moved
            # the source.  Best-effort no-replace restoration never overwrites
            # a raced active child and never destroys the unknown candidate.
            try:
                if isinstance(parent_authority, _PublicationAuthority):
                    parent_authority.rename_noreplace(orphan.name, source_name)
                else:
                    _rename_noreplace_at(
                        orphan.name,
                        source_name,
                        parent_authority,
                        parent_authority,
                    )
            except BaseException:  # noqa: B036 - preserve the unknown object
                pass
            raise
        return orphan
    raise RuntimeError("could not claim directory under a bounded orphan name")


def _orphan_metadata(
    authority: _PublicationAuthority,
    path: Path,
    ownership: _TreeOwnership,
    *,
    verified_at_isolation: bool = True,
) -> DirectoryOrphan:
    rebound = ownership
    if verified_at_isolation:
        rebound = authority.capture_child(
            path.name,
            path=path,
            label="directory orphan",
        )
        _require_matching_ownership(
            rebound,
            ownership,
            label="directory orphan",
            allow_root_rename=True,
        )
    return DirectoryOrphan(
        locator=_DirectoryOrphanLocator(
            parent_path=authority.display_parent,
            child_name=path.name,
            backend_tag=authority.backend_tag,
            parent_identity=authority.identity,
            ownership=rebound,
        ),
        ownership_digest=rebound.digest,
        entries=rebound.entries,
        byte_count=rebound.byte_count,
        verified_at_isolation=verified_at_isolation,
    )


def _restore_exact_previous_directory(
    backup: Path,
    destination: Path,
    *,
    destination_was_missing: bool,
    parent_authority: _PublicationAuthority,
    ownership: _TreeOwnership,
) -> None:
    if destination_was_missing:
        return
    _require_tree_ownership_at(
        parent_authority,
        backup.name,
        path=backup,
        expected=ownership,
        label="previous destination",
        allow_root_rename=True,
    )
    parent_authority.rename_noreplace(backup.name, destination.name)
    try:
        _require_tree_ownership_at(
            parent_authority,
            destination.name,
            path=destination,
            expected=ownership,
            label="moved destination",
            allow_root_rename=True,
        )
    except BaseException as ownership_error:  # noqa: B036 - async-safe rollback
        # A raced object is preserved under quarantine; never treat it as the
        # authenticated previous output merely because the rename completed.
        try:
            _quarantine_destination(
                destination,
                parent_authority=parent_authority,
            )
        except BaseException:  # noqa: B036 - preserve the primary failure
            pass
        raise _PreviousOutputIdentityLost(
            "restored destination does not match the previous ownership token"
        ) from ownership_error


def _restore_claimed_directory(
    backup: Path,
    destination: Path,
    *,
    parent_authority: _PublicationAuthority,
    context: str,
) -> None:
    """Restore an unauthenticated claim without overwriting a raced object."""

    try:
        parent_authority.rename_noreplace(backup.name, destination.name)
    except BaseException as restore_error:  # noqa: B036 - report isolation
        raise RuntimeError(
            f"{context}; claimed root remains isolated at {backup}"
        ) from restore_error


def _directory_or_missing(path: Path, *, label: str) -> os.stat_result | None:
    """Return directory metadata, rejecting links and non-directories."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    is_reparse = bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )
    if (
        stat.S_ISLNK(metadata.st_mode)
        or is_reparse
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise ValueError(f"{label} is not a directory or is a link: {path}")
    return metadata


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        getattr(metadata, "st_file_attributes", 0),
    )


def _directory_inode_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0),
    )


def publication_parent_identity(descriptor: int) -> tuple[int, ...]:
    """Return the stable identity expected by descriptor-bound publication."""

    if sys.platform == "win32":
        return _windows_directory_identity_from_handle(descriptor)
    metadata = os.fstat(descriptor)
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("publication parent descriptor is not a real directory")
    if not metadata.st_dev or not metadata.st_ino:
        raise RuntimeError("publication parent has no reliable filesystem identity")
    return _directory_inode_identity(metadata)


def _simple_child_name(value: str, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or len(os.fsencode(value)) > _MAX_OWNERSHIP_COMPONENT_BYTES
    ):
        raise ValueError(f"{label} must be one bounded file name")
    if sys.platform == "win32" and (
        value[-1] in {" ", "."}
        or any(ord(character) < 32 or character in '<>:"|?*' for character in value)
        or value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_BASENAMES
    ):
        raise ValueError(f"{label} is not a canonical Windows file name")
    return value


def _publication_relative_path(
    value: str | PurePosixPath,
) -> PurePosixPath:
    raw = value.as_posix() if isinstance(value, PurePosixPath) else value
    if (
        not isinstance(raw, str)
        or not raw
        or raw in {".", ".."}
        or "\\" in raw
        or "\x00" in raw
    ):
        raise ValueError("publication reader path must be relative POSIX")
    relative = PurePosixPath(raw)
    if (
        relative.is_absolute()
        or relative.as_posix() != raw
        or len(relative.parts) > _MAX_SAFE_REMOVAL_DEPTH
        or len(os.fsencode(raw)) > _MAX_OWNERSHIP_PATH_BYTES
    ):
        raise ValueError("publication reader path must be normalized and bounded")
    for part in relative.parts:
        _simple_child_name(part, label="publication reader component")
    return relative


def _rename_noreplace_at(
    src: str,
    dst: str,
    src_dir_fd: int,
    dst_dir_fd: int,
) -> None:
    """Atomically rename one POSIX child without replacing the destination."""

    source = _simple_child_name(src, label="rename source")
    destination = _simple_child_name(dst, label="rename destination")
    if sys.platform.startswith("linux"):
        symbol_name = "renameat2"
        flags = _RENAME_NOREPLACE
        required = "Linux renameat2"
    elif sys.platform == "darwin":
        symbol_name = "renameatx_np"
        flags = _RENAME_EXCL
        required = "macOS renameatx_np"
    else:
        raise RuntimeError("atomic no-replace POSIX rename is unsupported on this host")
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, symbol_name, None)
    if rename is None:
        raise RuntimeError(
            f"atomic no-replace directory publication requires {required}"
        )
    rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename.restype = ctypes.c_int
    result = rename(
        src_dir_fd,
        os.fsencode(source),
        dst_dir_fd,
        os.fsencode(destination),
        flags,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(
            error,
            os.strerror(error),
            destination,
        )
    if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
        raise RuntimeError(
            "filesystem does not support atomic no-replace directory publication"
        )
    raise OSError(error, os.strerror(error), source, destination)


def _require_rename_noreplace_platform() -> None:
    """Fail before publication-side effects on unsupported host platforms."""

    if sys.platform.startswith("linux"):
        required_symbol = "renameat2"
        required_label = "Linux renameat2"
    elif sys.platform == "darwin":
        required_symbol = "renameatx_np"
        required_label = "macOS renameatx_np"
    elif sys.platform == "win32":
        _windows_require_publication_api()
        return
    else:
        raise RuntimeError("atomic directory publication is unsupported on this host")
    libc = ctypes.CDLL(None, use_errno=True)
    if getattr(libc, required_symbol, None) is None:
        raise RuntimeError(
            f"atomic no-replace directory publication requires {required_label}"
        )


def _windows_kernel_api() -> _WindowsKernelApi:
    return _shared_windows_kernel_api()


def _windows_require_publication_api() -> None:
    _shared_windows_require_publication_api(_windows_kernel_api())


def _windows_directory_identity_from_handle(handle: int) -> tuple[int, ...]:
    metadata = _windows_kernel_api().metadata(handle)
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("publication parent handle is not a real directory")
    if not metadata.st_dev or not metadata.st_ino:
        raise RuntimeError("publication parent has no reliable FILE_ID identity")
    return _directory_inode_identity(metadata)


def _windows_find_child(
    api: _WindowsKernelApi,
    parent_handle: int,
    name: str,
    *,
    expected_file_id: int | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> _WindowsDirectoryEntry | None:
    folded = name.casefold()
    match: _WindowsDirectoryEntry | None = None
    for entry in api.iter_directory(parent_handle):
        if entry.name.casefold() != folded:
            if check_cancelled is not None:
                check_cancelled()
            continue
        if match is not None:
            raise RuntimeError("Windows directory contains ambiguous child names")
        match = entry
        if expected_file_id is not None and entry.file_id != expected_file_id:
            raise RuntimeError("Windows directory child identity changed")
        if check_cancelled is not None:
            check_cancelled()
    return match


def _windows_open_child_by_id(
    api: _WindowsKernelApi,
    parent_handle: int,
    entry: _WindowsDirectoryEntry,
    *,
    desired_access: int,
    expected_directory: bool | None,
    resource_owner: _WindowsResourceOwner,
) -> tuple[int, _WindowsHandleMetadata]:
    if not entry.file_id:
        raise RuntimeError("Windows directory entry has no reliable FILE_ID")
    if entry.attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
        raise RuntimeError("Windows publication refuses reparse-point content")
    is_directory = bool(entry.attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY)
    if expected_directory is not None and is_directory != expected_directory:
        raise ValueError("Windows publication child has the wrong object type")
    handle = resource_owner.acquire(
        lambda: api.open_by_id(
            parent_handle,
            entry.file_id,
            desired_access=desired_access,
            is_directory=is_directory,
        )
    )
    record = resource_owner.record_for_cleanup(handle)
    try:
        metadata = api.metadata(handle)
        if (
            not metadata.st_dev
            or not metadata.st_ino
            or metadata.st_ino != entry.file_id
            or bool(metadata.st_file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY)
            != is_directory
            or metadata.st_file_attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise RuntimeError("Windows FILE_ID child changed while it was opened")
        resource_owner.bind_identity(handle, metadata)
        return handle, metadata
    except BaseException as primary_error:
        resource_owner._run_record_cleanup_after_error(
            resource_owner._record_cleanup_state(record, primary_error),
            primary_error,
        )
        raise


def _windows_owned_file_record(
    api: _WindowsKernelApi,
    parent_handle: int,
    entry: _WindowsDirectoryEntry,
    path: Path,
    *,
    root_device: int,
    budget: _OwnershipBudget,
    relative: str,
    entry_policy: DirectoryEntryPolicy | None,
    resource_owner: _WindowsResourceOwner,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[int, str, tuple[int, ...]]:
    handle, opened = _windows_open_child_by_id(
        api,
        parent_handle,
        entry,
        desired_access=_WINDOWS_FILE_READ_DATA
        | _WINDOWS_FILE_READ_ATTRIBUTES
        | _WINDOWS_SYNCHRONIZE,
        expected_directory=False,
        resource_owner=resource_owner,
    )
    record = resource_owner.record_for_cleanup(handle)
    exact_owner = resource_owner._exact_record_cleanup_owner(record)
    with _run_context_with_cleanup_actions(
        (
            _OrderedAction(
                label="Windows ownership file HANDLE cleanup also failed",
                action=exact_owner.close,
                complete=lambda: exact_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=exact_owner,
            ),
        )
    ):
        if opened.st_dev != root_device:
            raise RuntimeError(f"directory ownership crosses a volume: {path}")
        size = opened.st_size
        if entry_policy is not None:
            entry_policy(relative, "file", stat.S_IMODE(opened.st_mode), size)
        if size < 0 or budget.byte_count + size > _MAX_OWNERSHIP_BYTES:
            raise RuntimeError("directory ownership scan exceeds its byte limit")
        digest = hashlib.sha256()
        remaining = size
        while remaining:
            if check_cancelled is not None:
                check_cancelled()
            block = api.read(handle, min(remaining, _OWNERSHIP_COPY_BYTES))
            if not block:
                raise RuntimeError(f"directory ownership file was truncated: {path}")
            digest.update(block)
            remaining -= len(block)
        if api.read(handle, 1):
            raise RuntimeError(f"directory ownership file grew while read: {path}")
        after = api.metadata(handle)
        if _ownership_version_identity(after) != _ownership_version_identity(opened):
            raise RuntimeError(f"directory ownership file changed: {path}")
        rebound = _windows_find_child(
            api,
            parent_handle,
            entry.name,
            expected_file_id=entry.file_id,
            check_cancelled=check_cancelled,
        )
        if rebound is None or rebound.file_id != entry.file_id:
            raise RuntimeError(f"directory ownership file changed: {path}")
    budget.byte_count += size
    return size, digest.hexdigest(), _ownership_version_identity(after)


@contextmanager
def _open_windows_authenticated_file(
    api: _WindowsKernelApi,
    root_handle: int,
    root_path: Path,
    relative: PurePosixPath,
    *,
    resources: _WindowsResourceOwner | None = None,
    max_bytes: int,
    expected: _ExpectedPublicationFile,
) -> Iterator[PublicationAuthenticatedFile]:
    owns_resources = resources is None
    selected_resources = _WindowsResourceOwner(api) if resources is None else resources
    root_before = api.metadata(root_handle)
    parent_handle = root_handle
    opened_directories: list[int] = []
    exact_owners: list[_ExactResourceCleanupOwner] = []
    bindings: list[tuple[int, str, int, int, tuple[int, ...]]] = []
    file_handle = 0
    authenticated: PublicationAuthenticatedFile | None = None
    cleanup_complete = False
    expected_directories = dict(expected.directory_identities)
    traversed: list[str] = []

    def finalize_authenticated_file() -> None:
        if authenticated is not None:
            authenticated._finalize()

    def close_resources() -> None:
        nonlocal cleanup_complete
        if owns_resources:
            selected_resources.close_all()
        else:
            failures = _OrderedActionState(
                actions=tuple(
                    _OrderedAction(
                        label="publication authenticated HANDLE cleanup also failed",
                        action=owner.close,
                        complete=lambda owner=owner: owner.closed,
                        retry_incomplete="cancellation",
                        incomplete_owner=owner,
                    )
                    for owner in reversed(exact_owners)
                ),
                iteration_failure_label=(
                    "publication authenticated HANDLE cleanup iteration also failed"
                ),
                primary_error=None,
            )
            _run_ordered_actions(failures)
            if failures.primary_error is not None:
                raise failures.primary_error
        cleanup_complete = True

    cleanup_actions: tuple[_OrderedActionInput, ...] = (
        (
            "publication authenticated file finalization also failed",
            finalize_authenticated_file,
        ),
        _OrderedAction(
            label=(
                "publication authenticated file HANDLE cleanup also failed; "
                "publication authenticated directory HANDLE cleanup also failed"
            ),
            action=close_resources,
            complete=lambda: cleanup_complete,
            retry_incomplete="cancellation",
            incomplete_owner=selected_resources,
        ),
    )

    with _run_context_with_cleanup_actions(cleanup_actions):
        root_before = api.metadata(root_handle)
        for part in relative.parts[:-1]:
            traversed.append(part)
            relative_directory = "/".join(traversed)
            entry = _windows_find_child(api, parent_handle, part)
            if entry is None:
                raise FileNotFoundError(relative.as_posix())
            child_handle, opened = _windows_open_child_by_id(
                api,
                parent_handle,
                entry,
                desired_access=_WINDOWS_FILE_LIST_DIRECTORY
                | _WINDOWS_FILE_READ_ATTRIBUTES
                | _WINDOWS_SYNCHRONIZE,
                expected_directory=True,
                resource_owner=selected_resources,
            )
            if not owns_resources:
                child_record = selected_resources.record_for_cleanup(child_handle)
                exact_owners.append(
                    selected_resources._exact_record_cleanup_owner(child_record)
                )
            opened_directories.append(child_handle)
            if opened.st_dev != root_before.st_dev:
                raise RuntimeError("publication stream crosses a volume")
            if expected_directories.get(
                relative_directory
            ) != _ownership_version_identity(opened):
                raise RuntimeError(
                    "publication stream directory differs from captured ownership"
                )
            bindings.append(
                (
                    parent_handle,
                    part,
                    entry.file_id,
                    child_handle,
                    _ownership_version_identity(opened),
                )
            )
            parent_handle = child_handle

        entry = _windows_find_child(api, parent_handle, relative.name)
        if entry is None:
            raise FileNotFoundError(relative.as_posix())
        file_handle, opened_file = _windows_open_child_by_id(
            api,
            parent_handle,
            entry,
            desired_access=_WINDOWS_FILE_READ_DATA
            | _WINDOWS_FILE_READ_ATTRIBUTES
            | _WINDOWS_SYNCHRONIZE,
            expected_directory=False,
            resource_owner=selected_resources,
        )
        if not owns_resources:
            file_record = selected_resources.record_for_cleanup(file_handle)
            exact_owners.append(
                selected_resources._exact_record_cleanup_owner(file_record)
            )
        if opened_file.st_dev != root_before.st_dev:
            raise RuntimeError("publication stream crosses a volume")
        if _ownership_version_identity(opened_file) != expected.file_identity:
            raise RuntimeError(
                "publication stream file differs from captured ownership"
            )
        if opened_file.st_size < 0 or opened_file.st_size > max_bytes:
            raise ValueError(
                f"publication stream exceeds its {max_bytes}-byte limit: {relative}"
            )

        def verify() -> None:
            after_file = api.metadata(file_handle)
            if _ownership_version_identity(after_file) != _ownership_version_identity(
                opened_file
            ):
                raise RuntimeError("publication stream file changed while read")
            rebound_file = _windows_find_child(api, parent_handle, relative.name)
            if rebound_file is None or rebound_file.file_id != entry.file_id:
                raise RuntimeError("publication stream file binding changed")
            for bound_parent, name, file_id, handle, expected in reversed(bindings):
                rebound = _windows_find_child(api, bound_parent, name)
                if rebound is None or rebound.file_id != file_id:
                    raise RuntimeError("publication stream directory binding changed")
                if _ownership_version_identity(api.metadata(handle)) != expected:
                    raise RuntimeError("publication stream directory changed")
            root_after = api.metadata(root_handle)
            if _ownership_version_identity(root_after) != _ownership_version_identity(
                root_before
            ):
                raise RuntimeError("publication stream root changed")

        authenticated = PublicationAuthenticatedFile(
            path=relative.as_posix(),
            mode=stat.S_IMODE(opened_file.st_mode),
            size=opened_file.st_size,
            read_callback=lambda size: api.read(file_handle, size),
            verify_callback=verify,
        )
        try:
            yield authenticated
        except BaseException:
            authenticated._abort()
            raise


def _scan_windows_owned_directory(
    api: _WindowsKernelApi,
    handle: int,
    path: Path,
    parts: tuple[bytes, ...],
    *,
    root_device: int,
    budget: _OwnershipBudget,
    inventory: list[tuple[str, str]],
    file_records: list[TreeFileRecord],
    entry_identities: list[tuple[str, str, tuple[int, ...]]],
    entry_policy: DirectoryEntryPolicy | None,
    depth: int,
    required_root_file: str | None = None,
    allow_empty_root: bool = False,
    resource_owner: _WindowsResourceOwner,
    check_cancelled: Callable[[], None] | None = None,
) -> bytes:
    if check_cancelled is not None:
        check_cancelled()
    if depth > _MAX_SAFE_REMOVAL_DEPTH:
        raise RuntimeError("directory ownership scan exceeds its depth limit")
    before = api.metadata(handle)
    if not stat.S_ISDIR(before.st_mode) or before.st_dev != root_device:
        raise RuntimeError(f"directory ownership root changed: {path}")
    entries: list[tuple[bytes, _WindowsDirectoryEntry]] = []
    for entry in api.iter_directory(handle):
        _simple_child_name(entry.name, label="directory ownership component")
        try:
            raw_name = entry.name.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise RuntimeError("Windows directory name is not valid Unicode") from exc
        if not raw_name or len(raw_name) > _MAX_OWNERSHIP_COMPONENT_BYTES:
            raise RuntimeError("directory ownership component exceeds its byte limit")
        relative = _ownership_relative_path(parts + (raw_name,))
        _reserve_ownership_record(budget, relative=relative)
        entries.append((raw_name, entry))
        if check_cancelled is not None:
            check_cancelled()
    if check_cancelled is None:
        entries.sort(key=lambda item: item[0])
        ordered_entries: Sequence[tuple[bytes, _WindowsDirectoryEntry]] = entries
    else:
        ordered_entries = _interruptible_sorted_ownership_items(
            entries,
            key=lambda item: item[0],
            check_cancelled=check_cancelled,
        )
    if (
        required_root_file is not None
        and not (allow_empty_root and not ordered_entries)
        and not _contains_required_ownership_marker(
            ordered_entries,
            matches=lambda item: item[1].name == required_root_file,
            check_cancelled=check_cancelled,
        )
    ):
        raise RuntimeError("directory ownership root is missing its required marker")

    hasher = hashlib.sha256()
    hasher.update(b"codenib.atomic-directory.v1\x00")
    hasher.update(stat.S_IMODE(before.st_mode).to_bytes(4, "big"))
    for raw_name, entry in ordered_entries:
        if check_cancelled is not None:
            check_cancelled()
        child_parts = parts + (raw_name,)
        relative_bytes = _ownership_relative_path(child_parts)
        relative = relative_bytes.decode("utf-8", errors="strict")
        child_path = path / entry.name
        if entry.attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError(
                f"directory ownership scan refuses linked content: {child_path}"
            )
        is_directory = bool(entry.attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY)
        entry_hasher = hashlib.sha256()
        if is_directory:
            child_handle, opened = _windows_open_child_by_id(
                api,
                handle,
                entry,
                desired_access=_WINDOWS_FILE_LIST_DIRECTORY
                | _WINDOWS_FILE_READ_ATTRIBUTES
                | _WINDOWS_SYNCHRONIZE,
                expected_directory=True,
                resource_owner=resource_owner,
            )
            child_record = resource_owner.record_for_cleanup(child_handle)
            exact_owner = resource_owner._exact_record_cleanup_owner(child_record)
            with _run_context_with_cleanup_actions(
                (
                    _OrderedAction(
                        label=(
                            "Windows ownership directory HANDLE cleanup also failed"
                        ),
                        action=exact_owner.close,
                        complete=lambda exact_owner=exact_owner: exact_owner.closed,
                        retry_incomplete="cancellation",
                        incomplete_owner=exact_owner,
                    ),
                )
            ):
                if opened.st_dev != root_device:
                    raise RuntimeError(
                        f"directory ownership crosses a volume: {child_path}"
                    )
                if entry_policy is not None:
                    entry_policy(
                        relative,
                        "directory",
                        stat.S_IMODE(opened.st_mode),
                        0,
                    )
                child_digest = _scan_windows_owned_directory(
                    api,
                    child_handle,
                    child_path,
                    child_parts,
                    root_device=root_device,
                    budget=budget,
                    inventory=inventory,
                    file_records=file_records,
                    entry_identities=entry_identities,
                    entry_policy=entry_policy,
                    depth=depth + 1,
                    resource_owner=resource_owner,
                    check_cancelled=check_cancelled,
                )
                after = api.metadata(child_handle)
                if _ownership_version_identity(after) != _ownership_version_identity(
                    opened
                ):
                    raise RuntimeError(
                        f"directory ownership directory changed: {child_path}"
                    )
            rebound = _windows_find_child(
                api,
                handle,
                entry.name,
                expected_file_id=entry.file_id,
                check_cancelled=check_cancelled,
            )
            if rebound is None or rebound.file_id != entry.file_id:
                raise RuntimeError(
                    f"directory ownership directory changed: {child_path}"
                )
            entry_hasher.update(b"D")
            _ownership_hash_field(entry_hasher, relative_bytes)
            entry_hasher.update(stat.S_IMODE(opened.st_mode).to_bytes(4, "big"))
            entry_hasher.update(child_digest)
            inventory.append((relative, "directory"))
            entry_identities.append(
                (relative, "directory", _ownership_version_identity(after))
            )
        else:
            size, digest, file_identity = _windows_owned_file_record(
                api,
                handle,
                entry,
                child_path,
                root_device=root_device,
                budget=budget,
                relative=relative,
                entry_policy=entry_policy,
                resource_owner=resource_owner,
                check_cancelled=check_cancelled,
            )
            digest_bytes = bytes.fromhex(digest)
            file_mode = stat.S_IMODE(file_identity[2])
            entry_hasher.update(b"F")
            _ownership_hash_field(entry_hasher, relative_bytes)
            entry_hasher.update(file_mode.to_bytes(4, "big"))
            entry_hasher.update(size.to_bytes(8, "big"))
            entry_hasher.update(digest_bytes)
            inventory.append((relative, "file"))
            file_records.append(
                TreeFileRecord(
                    path=relative,
                    mode=file_mode,
                    size=size,
                    sha256=digest,
                )
            )
            entry_identities.append((relative, "file", file_identity))
        if entry.name == required_root_file and is_directory:
            raise RuntimeError("directory ownership root marker is not a regular file")
        hasher.update(entry_hasher.digest())
    after = api.metadata(handle)
    if _ownership_version_identity(after) != _ownership_version_identity(before):
        raise RuntimeError(f"directory ownership root changed: {path}")
    return hasher.digest()


def _capture_windows_directory_handle(
    api: _WindowsKernelApi,
    handle: int,
    path: Path,
    *,
    resources: _WindowsResourceOwner | None = None,
    required_root_file: str | None,
    allow_empty_root: bool,
    entry_policy: DirectoryEntryPolicy | None,
    check_cancelled: Callable[[], None] | None = None,
) -> _TreeOwnership:
    selected_resources = _WindowsResourceOwner(api) if resources is None else resources
    _required_root_file_bytes(required_root_file)
    opened = api.metadata(handle)
    if _is_link_or_reparse(opened) or not stat.S_ISDIR(opened.st_mode):
        raise RuntimeError(f"directory ownership root changed: {path}")
    if not opened.st_dev or not opened.st_ino:
        raise RuntimeError("directory ownership root has no reliable FILE_ID identity")
    budget = _OwnershipBudget()
    inventory: list[tuple[str, str]] = []
    file_records: list[TreeFileRecord] = []
    entry_identities: list[tuple[str, str, tuple[int, ...]]] = []
    digest = _scan_windows_owned_directory(
        api,
        handle,
        path,
        (),
        root_device=opened.st_dev,
        budget=budget,
        inventory=inventory,
        file_records=file_records,
        entry_identities=entry_identities,
        entry_policy=entry_policy,
        depth=0,
        required_root_file=required_root_file,
        allow_empty_root=allow_empty_root,
        resource_owner=selected_resources,
        check_cancelled=check_cancelled,
    )
    after = api.metadata(handle)
    if _ownership_version_identity(after) != _ownership_version_identity(opened):
        raise RuntimeError(f"directory ownership root changed: {path}")
    if check_cancelled is None:
        canonical_inventory = tuple(sorted(inventory))
        canonical_records = tuple(sorted(file_records, key=lambda record: record.path))
        canonical_identities = tuple(sorted(entry_identities))
    else:
        canonical_inventory = _interruptible_sorted_ownership_items(
            inventory,
            key=None,
            check_cancelled=check_cancelled,
        )
        canonical_records = _interruptible_sorted_ownership_items(
            file_records,
            key=lambda record: record.path,
            check_cancelled=check_cancelled,
        )
        canonical_identities = _interruptible_sorted_ownership_items(
            entry_identities,
            key=None,
            check_cancelled=check_cancelled,
        )
    return _TreeOwnership(
        root_identity=_directory_inode_identity(opened),
        root_version_identity=_ownership_version_identity(opened),
        digest=digest.hex(),
        entries=budget.entries,
        byte_count=budget.byte_count,
        metadata_bytes=budget.metadata_bytes,
        inventory=canonical_inventory,
        file_records=canonical_records,
        entry_identities=canonical_identities,
    )


def _open_windows_publication_authority(
    path: Path,
    *,
    parent_resource: int | None,
    expected_parent_identity: tuple[int, ...] | None,
    authority_owner: _PublicationAuthorityOwner | None = None,
) -> _PublicationAuthority:
    api = _windows_kernel_api()
    resources = _WindowsResourceOwner(api)
    lexical_owner = _WindowsLexicalAuthorityOwner()
    lexical_authority: _windows_fs.WindowsDirectoryAuthority | None = None
    authority: _PublicationAuthority | None = None

    def close_resources() -> None:
        primary_error: BaseException | None = None
        try:
            resources.close_all()
        except BaseException as close_error:  # noqa: B036 - visit both owners
            primary_error = close_error
        try:
            lexical_owner.close()
        except BaseException as close_error:  # noqa: B036 - retain first
            primary_error = _retain_first_error(
                primary_error,
                "Windows lexical authority cleanup also failed",
                close_error,
            )
        if primary_error is not None:
            raise primary_error

    def close_resources_after_error(primary_error: BaseException) -> None:
        try:
            close_resources()
        except BaseException as close_error:  # noqa: B036 - keep primary
            _annotate_secondary_error(
                primary_error,
                "Windows publication authority cleanup also failed",
                close_error,
            )
        _attach_publication_cleanup_owner(primary_error, resources)
        _attach_publication_cleanup_owner(primary_error, lexical_owner)

    def resources_closed() -> bool:
        return resources.closed and lexical_owner.closed

    try:
        if parent_resource is None:
            _windows_fs.open_lexical_directory_authority(
                path,
                api=api,
                cleanup_slot=lexical_owner,
            )
            lexical_authority = lexical_owner.authority
            parent_handle = lexical_authority.handle
        else:
            parent_handle = resources.acquire(
                lambda: api.duplicate_handle(parent_resource)
            )
        opened = api.metadata(parent_handle)
        if parent_resource is not None:
            resources.bind_identity(parent_handle, opened)
        if _is_link_or_reparse(opened) or not stat.S_ISDIR(opened.st_mode):
            raise ValueError("publication parent must be a real directory")
        identity = _directory_inode_identity(opened)
        if not opened.st_dev or not opened.st_ino:
            raise RuntimeError("publication parent has no reliable FILE_ID identity")
        if expected_parent_identity is not None and identity != tuple(
            expected_parent_identity
        ):
            raise RuntimeError("publication parent identity does not match authority")
        if lexical_authority is None:
            path_handle = resources.acquire(lambda: api.create_directory_handle(path))
            if _directory_inode_identity(api.metadata(path_handle)) != identity:
                raise RuntimeError("publication parent path changed")
        owned_parent_handle = parent_handle

        def open_child(
            name: str,
            *,
            desired_access: int,
        ) -> tuple[int, _WindowsHandleMetadata] | None:
            entry = _windows_find_child(api, owned_parent_handle, name)
            if entry is None:
                return None
            return _windows_open_child_by_id(
                api,
                owned_parent_handle,
                entry,
                desired_access=desired_access,
                expected_directory=True,
                resource_owner=resources,
            )

        def metadata_callback(
            name: str,
            display_path: Path,
            label: str,
        ) -> _WindowsHandleMetadata | None:
            opened_child = open_child(
                name,
                desired_access=_WINDOWS_FILE_READ_ATTRIBUTES | _WINDOWS_SYNCHRONIZE,
            )
            if opened_child is None:
                return None
            handle, metadata = opened_child
            child_record = resources.record_for_cleanup(handle)
            primary_error: BaseException | None = None
            try:
                if metadata.st_dev != opened.st_dev:
                    raise RuntimeError("publication child crosses a volume")
                if _is_link_or_reparse(metadata):
                    raise ValueError(
                        f"{label} is not a directory or is a link: {display_path}"
                    )
                return metadata
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                try:
                    resources.close_record(child_record)
                except BaseException as close_error:  # noqa: B036
                    if primary_error is None:
                        raise
                    _annotate_secondary_error(
                        primary_error,
                        "Windows metadata HANDLE cleanup also failed",
                        close_error,
                    )

        def reader_callback(
            name: str,
            display_path: Path,
            label: str,
            expected_ownership: _TreeOwnership | None,
            callback: Callable[[_PublicationTreeReader], _T],
        ) -> _T:
            opened_child = open_child(
                name,
                desired_access=_WINDOWS_FILE_LIST_DIRECTORY
                | _WINDOWS_FILE_READ_ATTRIBUTES
                | _WINDOWS_SYNCHRONIZE,
            )
            if opened_child is None:
                raise RuntimeError(f"{label} disappeared: {display_path}")
            handle, metadata = opened_child
            child_record = resources.record_for_cleanup(handle)
            if metadata.st_dev != opened.st_dev:
                resources.close_record(child_record)
                raise RuntimeError("publication child crosses a volume")
            handle_record = resources.record_for_cleanup(handle)
            handle_owner = resources._exact_record_cleanup_owner(handle_record)
            reader: _PublicationTreeReader | None = None

            def deactivate_reader() -> None:
                if reader is not None:
                    reader._deactivate()

            cleanup_actions = (
                _OrderedAction(
                    label="Windows publication reader deactivation also failed",
                    action=deactivate_reader,
                    complete=lambda: reader is None or not reader._lifetime.active,
                    retry_incomplete="cancellation",
                ),
                _OrderedAction(
                    label="Windows reader HANDLE cleanup also failed",
                    action=handle_owner.close,
                    complete=lambda: handle_owner.closed,
                    retry_incomplete="cancellation",
                    incomplete_owner=handle_owner,
                ),
            )
            with _run_context_with_cleanup_actions(cleanup_actions):
                reader = _PublicationTreeReader(
                    display_path,
                    _directory_inode_identity(metadata),
                    lambda root_file, allow_empty, policy, cancelled: (
                        _capture_windows_directory_handle(
                            api,
                            handle,
                            display_path,
                            resources=resources,
                            required_root_file=root_file,
                            allow_empty_root=allow_empty,
                            entry_policy=policy,
                            check_cancelled=cancelled,
                        )
                    ),
                    lambda relative, max_bytes, expected: (
                        _open_windows_authenticated_file(
                            api,
                            handle,
                            display_path,
                            relative,
                            resources=resources,
                            max_bytes=max_bytes,
                            expected=expected,
                        )
                    ),
                    expected_ownership,
                    _capture_supports_cancellation=True,
                )

                def validate_child_binding() -> None:
                    rebound = _windows_find_child(api, owned_parent_handle, name)
                    if rebound is None or rebound.file_id != metadata.st_ino:
                        raise RuntimeError(f"{label} namespace binding changed")

                return _run_publication_reader_callback(
                    reader,
                    callback,
                    validate_child_binding,
                )

        def rename_callback(source: str, destination: str) -> None:
            opened_source = open_child(
                source,
                desired_access=_WINDOWS_DELETE
                | _WINDOWS_FILE_READ_ATTRIBUTES
                | _WINDOWS_SYNCHRONIZE,
            )
            if opened_source is None:
                raise FileNotFoundError(source)
            source_handle, _metadata = opened_source
            source_record = resources.record_for_cleanup(source_handle)
            primary_error: BaseException | None = None
            try:
                api.rename_noreplace(
                    source_handle,
                    owned_parent_handle,
                    destination,
                )
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                try:
                    resources.close_record(source_record)
                except BaseException as close_error:  # noqa: B036
                    if primary_error is None:
                        raise
                    _annotate_secondary_error(
                        primary_error,
                        "Windows rename HANDLE cleanup also failed",
                        close_error,
                    )

        def verify_callback() -> None:
            if _directory_inode_identity(api.metadata(owned_parent_handle)) != identity:
                raise RuntimeError("publication parent authority changed")
            if lexical_authority is not None:
                lexical_authority.verify_binding()
                return
            rebound_handle = resources.acquire(
                lambda: api.create_directory_handle(path)
            )
            rebound_record = resources.record_for_cleanup(rebound_handle)
            primary_error: BaseException | None = None
            try:
                if _directory_inode_identity(api.metadata(rebound_handle)) != identity:
                    raise RuntimeError("publication parent path changed")
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                try:
                    resources.close_record(rebound_record)
                except BaseException as close_error:  # noqa: B036
                    if primary_error is None:
                        raise
                    _annotate_secondary_error(
                        primary_error,
                        "Windows verification HANDLE cleanup also failed",
                        close_error,
                    )

        authority = _PublicationAuthority(
            display_parent=path,
            identity=identity,
            backend_tag="windows-file-id",
            resource=owned_parent_handle,
            close_callback=lambda _resource: close_resources(),
            metadata_callback=metadata_callback,
            reader_callback=reader_callback,
            rename_callback=rename_callback,
            sync_callback=lambda: None,
            verify_callback=verify_callback,
            close_complete_callback=resources_closed,
        )
        if authority_owner is not None:
            authority_owner.install(authority)
        return authority
    except BaseException as primary_error:
        if (
            authority is not None
            and authority_owner is not None
            and authority_owner.authority is authority
        ):
            authority_owner.close_after_error(primary_error)
        else:
            close_resources_after_error(primary_error)
        raise


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _mountinfo_path(value: str) -> str:
    """Decode the octal escapes used for paths in Linux mountinfo."""

    return (
        value.replace(r"\040", " ")
        .replace(r"\011", "\t")
        .replace(r"\012", "\n")
        .replace(r"\134", "\\")
    )


def _linux_mount_points() -> frozenset[str]:
    if os.name != "posix" or not Path("/proc/self/mountinfo").is_file():
        return frozenset()
    points: set[str] = set()
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as mountinfo:
            for line in mountinfo:
                fields = line.split()
                if len(fields) >= 5:
                    points.add(os.path.normpath(_mountinfo_path(fields[4])))
    except OSError:
        # os.path.ismount and device checks remain the portable baseline.
        return frozenset()
    return frozenset(points)


def _path_is_mount_point(
    path: Path,
    *,
    mount_points: frozenset[str] | None = None,
) -> bool:
    lexical = os.path.normpath(os.path.realpath(path))
    observed_mount_points = (
        _linux_mount_points() if mount_points is None else mount_points
    )
    return (
        os.path.ismount(path)
        or os.path.ismount(lexical)
        or lexical in observed_mount_points
    )


class _ResourceIdentityMetadata(Protocol):
    @property
    def st_dev(self) -> int: ...

    @property
    def st_ino(self) -> int: ...

    @property
    def st_mode(self) -> int: ...


def _resource_owner_identity(
    metadata: _ResourceIdentityMetadata,
) -> tuple[int, ...]:
    """Identify one owned kernel object using only immutable identity fields.

    Permission bits, sizes, timestamps, link counts, and reparse attributes may
    change while the same descriptor or HANDLE remains owned.  They therefore
    belong in namespace/content authentication, not close reconciliation.  A
    Windows 128-bit FILE_ID is preferred when present; POSIX and test backends
    use the device/inode pair.  The file type is stable for an object's life.
    """

    device = int(metadata.st_dev)
    mode = int(metadata.st_mode)
    file_id_128 = (
        metadata.file_id_128 if isinstance(metadata, _WindowsHandleMetadata) else b""
    )
    if file_id_128:
        return (device, 1, int.from_bytes(file_id_128, "big"), stat.S_IFMT(mode))
    return (device, 0, int(metadata.st_ino), stat.S_IFMT(mode))


def _ownership_binding_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Bind path and descriptor observations without cache-sensitive times."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0,
        getattr(metadata, "st_file_attributes", 0),
    )


def _ownership_version_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Detect metadata-visible mutation through one open descriptor.

    This is an endpoint version check, not a portable namespace event history.
    """

    return (
        *_ownership_binding_identity(metadata),
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


@contextmanager
def _open_posix_authenticated_file(
    root_descriptor: int,
    root_path: Path,
    relative: PurePosixPath,
    *,
    resources: _PosixResourceOwner | None = None,
    max_bytes: int,
    expected: _ExpectedPublicationFile,
) -> Iterator[PublicationAuthenticatedFile]:
    owns_resources = resources is None
    selected_resources = _PosixResourceOwner() if resources is None else resources
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directory_flags = flags | os.O_DIRECTORY | getattr(os, "O_NONBLOCK", 0)
    file_flags = flags | getattr(os, "O_NONBLOCK", 0)
    descriptors: list[int] = []
    exact_owners: list[_ExactResourceCleanupOwner] = []
    bindings: list[tuple[int, str, int, tuple[int, ...]]] = []
    file_descriptor = -1
    authenticated: PublicationAuthenticatedFile | None = None
    cleanup_complete = False
    expected_directories = dict(expected.directory_identities)
    traversed: list[str] = []

    def finalize_authenticated_file() -> None:
        if authenticated is not None:
            authenticated._finalize()

    def close_resources() -> None:
        nonlocal cleanup_complete
        if owns_resources:
            selected_resources.close_all()
        else:
            failures = _OrderedActionState(
                actions=tuple(
                    _OrderedAction(
                        label=(
                            "publication authenticated descriptor cleanup also failed"
                        ),
                        action=owner.close,
                        complete=lambda owner=owner: owner.closed,
                        retry_incomplete="cancellation",
                        incomplete_owner=owner,
                    )
                    for owner in reversed(exact_owners)
                ),
                iteration_failure_label=(
                    "publication authenticated descriptor cleanup iteration also "
                    "failed"
                ),
                primary_error=None,
            )
            _run_ordered_actions(failures)
            if failures.primary_error is not None:
                raise failures.primary_error
        cleanup_complete = True

    cleanup_actions: tuple[_OrderedActionInput, ...] = (
        (
            "publication authenticated file finalization also failed",
            finalize_authenticated_file,
        ),
        _OrderedAction(
            label=(
                "publication authenticated file descriptor cleanup also failed; "
                "publication authenticated directory descriptor cleanup also failed"
            ),
            action=close_resources,
            complete=lambda: cleanup_complete,
            retry_incomplete="cancellation",
            incomplete_owner=selected_resources,
        ),
    )

    with _run_context_with_cleanup_actions(cleanup_actions):
        root_before = os.fstat(root_descriptor)
        root_device = root_before.st_dev
        root_copy = selected_resources.duplicate(root_descriptor)
        descriptors.append(root_copy)
        if not owns_resources:
            root_record = selected_resources.record_for_cleanup(root_copy)
            exact_owners.append(
                selected_resources._exact_record_cleanup_owner(root_record)
            )
        for part in relative.parts[:-1]:
            traversed.append(part)
            relative_directory = "/".join(traversed)
            parent = descriptors[-1]
            metadata = os.stat(part, dir_fd=parent, follow_symlinks=False)
            if (
                _is_link_or_reparse(metadata)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_dev != root_device
            ):
                raise RuntimeError(
                    f"publication stream refuses directory: {root_path / relative}"
                )
            child = selected_resources.open(part, directory_flags, dir_fd=parent)
            descriptors.append(child)
            if not owns_resources:
                child_record = selected_resources.record_for_cleanup(child)
                exact_owners.append(
                    selected_resources._exact_record_cleanup_owner(child_record)
                )
            opened = os.fstat(child)
            if _ownership_binding_identity(opened) != _ownership_binding_identity(
                metadata
            ):
                raise RuntimeError("publication stream directory changed")
            if expected_directories.get(
                relative_directory
            ) != _ownership_version_identity(opened):
                raise RuntimeError(
                    "publication stream directory differs from captured ownership"
                )
            bindings.append((parent, part, child, _ownership_version_identity(opened)))

        parent = descriptors[-1]
        name = relative.name
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            _is_link_or_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_dev != root_device
        ):
            raise RuntimeError(
                f"publication stream refuses non-file: {root_path / relative}"
            )
        file_descriptor = selected_resources.open(name, file_flags, dir_fd=parent)
        if not owns_resources:
            file_record = selected_resources.record_for_cleanup(file_descriptor)
            exact_owners.append(
                selected_resources._exact_record_cleanup_owner(file_record)
            )
        opened_file = os.fstat(file_descriptor)
        if _ownership_binding_identity(opened_file) != _ownership_binding_identity(
            metadata
        ):
            raise RuntimeError("publication stream file changed while opened")
        if _ownership_version_identity(opened_file) != expected.file_identity:
            raise RuntimeError(
                "publication stream file differs from captured ownership"
            )
        if opened_file.st_size < 0 or opened_file.st_size > max_bytes:
            raise ValueError(
                f"publication stream exceeds its {max_bytes}-byte limit: {relative}"
            )

        def verify() -> None:
            after_file = os.fstat(file_descriptor)
            if _ownership_version_identity(after_file) != _ownership_version_identity(
                opened_file
            ):
                raise RuntimeError("publication stream file changed while read")
            path_after = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _ownership_binding_identity(path_after) != _ownership_binding_identity(
                opened_file
            ):
                raise RuntimeError("publication stream file binding changed")
            for parent_fd, child_name, child_fd, expected in reversed(bindings):
                child_after = os.fstat(child_fd)
                path_child_after = os.stat(
                    child_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if _ownership_version_identity(
                    child_after
                ) != expected or _ownership_binding_identity(
                    path_child_after
                ) != _ownership_binding_identity(
                    child_after
                ):
                    raise RuntimeError("publication stream directory binding changed")
            root_after = os.fstat(root_descriptor)
            if _ownership_version_identity(root_after) != _ownership_version_identity(
                root_before
            ):
                raise RuntimeError("publication stream root changed")

        authenticated = PublicationAuthenticatedFile(
            path=relative.as_posix(),
            mode=stat.S_IMODE(opened_file.st_mode),
            size=opened_file.st_size,
            read_callback=lambda size: os.read(file_descriptor, size),
            verify_callback=verify,
        )
        try:
            yield authenticated
        except BaseException:
            authenticated._abort()
            raise


def _ownership_hash_field(hasher, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _ownership_relative_path(parts: tuple[bytes, ...]) -> bytes:
    if len(parts) > _MAX_SAFE_REMOVAL_DEPTH:
        raise RuntimeError("directory ownership path exceeds its depth limit")
    relative = b"/".join(parts)
    if len(relative) > _MAX_OWNERSHIP_PATH_BYTES:
        raise RuntimeError("directory ownership path exceeds its byte limit")
    return relative


def _reserve_ownership_record(
    budget: _OwnershipBudget,
    *,
    relative: bytes,
) -> None:
    budget.entries += 1
    if budget.entries > _MAX_OWNERSHIP_ENTRIES:
        raise RuntimeError("directory ownership scan exceeds its entry limit")
    # Charge the largest record before descending so nested wide directories
    # cannot accumulate more work than the global budget before unwinding.
    budget.metadata_bytes += 8 + len(relative) + 1 + 4 + 8 + 32
    if budget.metadata_bytes > _MAX_OWNERSHIP_METADATA_BYTES:
        raise RuntimeError("directory ownership scan exceeds its metadata limit")


def _validate_ownership_inventory_budget(relative_paths: Iterable[bytes]) -> None:
    """Apply the scanner's exact entry and metadata budget to a planned tree."""

    budget = _OwnershipBudget()
    for relative in relative_paths:
        _reserve_ownership_record(budget, relative=relative)


def _hash_owned_regular_file(
    parent_descriptor: int,
    name: str,
    path: Path,
    metadata: os.stat_result,
    *,
    resources: _PosixResourceOwner,
    root_device: int,
    budget: _OwnershipBudget,
    relative: str,
    entry_policy: DirectoryEntryPolicy | None,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[int, str, tuple[int, ...]]:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = resources.open(name, flags, dir_fd=parent_descriptor)
    descriptor_record = resources.record_for_cleanup(descriptor)
    exact_owner = resources._exact_record_cleanup_owner(descriptor_record)
    with _run_context_with_cleanup_actions(
        (
            _OrderedAction(
                label="directory ownership file descriptor cleanup also failed",
                action=exact_owner.close,
                complete=lambda: exact_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=exact_owner,
            ),
        )
    ):
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != root_device
            or not opened.st_dev
            or not opened.st_ino
            or _ownership_binding_identity(opened)
            != _ownership_binding_identity(metadata)
        ):
            raise RuntimeError(f"directory ownership file changed: {path}")
        size = opened.st_size
        if entry_policy is not None:
            entry_policy(relative, "file", stat.S_IMODE(opened.st_mode), size)
        if size < 0 or budget.byte_count + size > _MAX_OWNERSHIP_BYTES:
            raise RuntimeError("directory ownership scan exceeds its byte limit")
        digest = hashlib.sha256()
        remaining = size
        while remaining:
            if check_cancelled is not None:
                check_cancelled()
            chunk = os.read(descriptor, min(remaining, _OWNERSHIP_COPY_BYTES))
            if not chunk:
                raise RuntimeError(f"directory ownership file was truncated: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise RuntimeError(f"directory ownership file grew while read: {path}")
        after = os.fstat(descriptor)
        if _ownership_version_identity(after) != _ownership_version_identity(opened):
            raise RuntimeError(f"directory ownership file changed: {path}")
        path_after = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _ownership_binding_identity(path_after) != _ownership_binding_identity(
            opened
        ):
            raise RuntimeError(f"directory ownership file changed: {path}")
    budget.byte_count += size
    return size, digest.hexdigest(), _ownership_version_identity(after)


def _scan_owned_directory(
    descriptor: int,
    path: Path,
    parts: tuple[bytes, ...],
    *,
    resources: _PosixResourceOwner,
    root_device: int,
    mount_points: frozenset[str],
    budget: _OwnershipBudget,
    inventory: list[tuple[str, str]],
    file_records: list[TreeFileRecord],
    entry_identities: list[tuple[str, str, tuple[int, ...]]],
    entry_policy: DirectoryEntryPolicy | None,
    depth: int,
    required_root_file: bytes | None = None,
    allow_empty_root: bool = False,
    check_cancelled: Callable[[], None] | None = None,
) -> bytes:
    if check_cancelled is not None:
        check_cancelled()
    if depth > _MAX_SAFE_REMOVAL_DEPTH:
        raise RuntimeError("directory ownership scan exceeds its depth limit")
    before = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(before.st_mode)
        or before.st_dev != root_device
        or not before.st_dev
        or not before.st_ino
    ):
        raise RuntimeError(f"directory ownership root changed: {path}")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    scan_descriptor = resources.open(".", flags, dir_fd=descriptor)
    scan_record = resources.record_for_cleanup(scan_descriptor)
    scan_owner = resources._exact_record_cleanup_owner(scan_record)
    names: list[tuple[bytes, str]] = []
    with _run_context_with_cleanup_actions(
        (
            _OrderedAction(
                label="directory ownership scan descriptor cleanup also failed",
                action=scan_owner.close,
                complete=lambda: scan_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=scan_owner,
            ),
        )
    ):
        with os.scandir(scan_descriptor) as entries:
            for entry in entries:
                raw_name = os.fsencode(entry.name)
                if not raw_name or len(raw_name) > _MAX_OWNERSHIP_COMPONENT_BYTES:
                    raise RuntimeError(
                        "directory ownership component exceeds its byte limit"
                    )
                relative = _ownership_relative_path(parts + (raw_name,))
                _reserve_ownership_record(budget, relative=relative)
                names.append((raw_name, entry.name))
                if check_cancelled is not None:
                    check_cancelled()
    if check_cancelled is None:
        names.sort(key=lambda item: item[0])
        ordered_names: Sequence[tuple[bytes, str]] = names
    else:
        ordered_names = _interruptible_sorted_ownership_items(
            names,
            key=lambda item: item[0],
            check_cancelled=check_cancelled,
        )
    if (
        required_root_file is not None
        and not (allow_empty_root and not ordered_names)
        and not _contains_required_ownership_marker(
            ordered_names,
            matches=lambda item: item[0] == required_root_file,
            check_cancelled=check_cancelled,
        )
    ):
        raise RuntimeError("directory ownership root is missing its required marker")

    hasher = hashlib.sha256()
    hasher.update(b"codenib.atomic-directory.v1\x00")
    hasher.update(stat.S_IMODE(before.st_mode).to_bytes(4, "big"))
    for raw_name, name in ordered_names:
        if check_cancelled is not None:
            check_cancelled()
        child_parts = parts + (raw_name,)
        relative = _ownership_relative_path(child_parts)
        child_path = path / name
        metadata = os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        if metadata.st_dev != root_device or _path_is_mount_point(
            child_path,
            mount_points=mount_points,
        ):
            raise RuntimeError(
                f"directory ownership scan refuses mounted content: {child_path}"
            )
        if _is_link_or_reparse(metadata):
            raise RuntimeError(
                f"directory ownership scan refuses linked content: {child_path}"
            )

        entry_hasher = hashlib.sha256()
        if stat.S_ISDIR(metadata.st_mode):
            if entry_policy is not None:
                entry_policy(
                    os.fsdecode(relative),
                    "directory",
                    stat.S_IMODE(metadata.st_mode),
                    0,
                )
            child_descriptor = resources.open(name, flags, dir_fd=descriptor)
            child_record = resources.record_for_cleanup(child_descriptor)
            child_owner = resources._exact_record_cleanup_owner(child_record)
            child_cleanup: tuple[_OrderedActionInput, ...] = (
                _OrderedAction(
                    label="directory ownership child descriptor cleanup also failed",
                    action=child_owner.close,
                    complete=lambda owner=child_owner: owner.closed,
                    retry_incomplete="cancellation",
                    incomplete_owner=child_owner,
                ),
            )
            with _run_context_with_cleanup_actions(child_cleanup):
                opened = os.fstat(child_descriptor)
                if _ownership_binding_identity(opened) != _ownership_binding_identity(
                    metadata
                ):
                    raise RuntimeError(
                        f"directory ownership directory changed: {child_path}"
                    )
                child_digest = _scan_owned_directory(
                    child_descriptor,
                    child_path,
                    child_parts,
                    resources=resources,
                    root_device=root_device,
                    mount_points=mount_points,
                    budget=budget,
                    inventory=inventory,
                    file_records=file_records,
                    entry_identities=entry_identities,
                    entry_policy=entry_policy,
                    depth=depth + 1,
                    check_cancelled=check_cancelled,
                )
                after = os.fstat(child_descriptor)
                if _ownership_version_identity(after) != _ownership_version_identity(
                    opened
                ):
                    raise RuntimeError(
                        f"directory ownership directory changed: {child_path}"
                    )
            path_after = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if _ownership_binding_identity(path_after) != _ownership_binding_identity(
                metadata
            ):
                raise RuntimeError(
                    f"directory ownership directory changed: {child_path}"
                )
            entry_hasher.update(b"D")
            _ownership_hash_field(entry_hasher, relative)
            entry_hasher.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
            entry_hasher.update(child_digest)
            inventory.append((os.fsdecode(relative), "directory"))
            entry_identities.append(
                (
                    os.fsdecode(relative),
                    "directory",
                    _ownership_version_identity(after),
                )
            )
        elif stat.S_ISREG(metadata.st_mode):
            size, digest, file_identity = _hash_owned_regular_file(
                descriptor,
                name,
                child_path,
                metadata,
                resources=resources,
                root_device=root_device,
                budget=budget,
                relative=os.fsdecode(relative),
                entry_policy=entry_policy,
                check_cancelled=check_cancelled,
            )
            digest_bytes = bytes.fromhex(digest)
            entry_hasher.update(b"F")
            _ownership_hash_field(entry_hasher, relative)
            entry_hasher.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
            entry_hasher.update(size.to_bytes(8, "big"))
            entry_hasher.update(digest_bytes)
            inventory.append((os.fsdecode(relative), "file"))
            file_records.append(
                TreeFileRecord(
                    path=os.fsdecode(relative),
                    mode=stat.S_IMODE(metadata.st_mode),
                    size=size,
                    sha256=digest,
                )
            )
            entry_identities.append((os.fsdecode(relative), "file", file_identity))
        else:
            raise RuntimeError(
                f"directory ownership scan refuses special content: {child_path}"
            )
        if raw_name == required_root_file and not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("directory ownership root marker is not a regular file")
        hasher.update(entry_hasher.digest())

    after = os.fstat(descriptor)
    if _ownership_version_identity(after) != _ownership_version_identity(before):
        raise RuntimeError(f"directory ownership root changed: {path}")
    return hasher.digest()


def _required_root_file_bytes(required_root_file: str | None) -> bytes | None:
    required_root_file_bytes: bytes | None = None
    if required_root_file is not None:
        if (
            not required_root_file
            or required_root_file in {".", ".."}
            or "/" in required_root_file
            or "\\" in required_root_file
        ):
            raise ValueError("directory ownership marker must be one file name")
        required_root_file_bytes = os.fsencode(required_root_file)
        if len(required_root_file_bytes) > _MAX_OWNERSHIP_COMPONENT_BYTES:
            raise ValueError("directory ownership marker exceeds its byte limit")
    return required_root_file_bytes


def _capture_posix_directory_descriptor(
    descriptor: int,
    path: Path,
    *,
    resources: _PosixResourceOwner | None = None,
    required_root_file: str | None,
    allow_empty_root: bool,
    entry_policy: DirectoryEntryPolicy | None,
    check_cancelled: Callable[[], None] | None = None,
) -> _TreeOwnership:
    """Capture one already-open POSIX directory without reacquiring its path."""

    required_root_file_bytes = _required_root_file_bytes(required_root_file)
    selected_resources = _PosixResourceOwner() if resources is None else resources
    opened = os.fstat(descriptor)
    if _is_link_or_reparse(opened) or not stat.S_ISDIR(opened.st_mode):
        raise RuntimeError(f"directory ownership root changed: {path}")
    if not opened.st_dev or not opened.st_ino:
        raise RuntimeError("directory ownership root has no reliable identity")
    if _path_is_mount_point(path):
        raise RuntimeError(f"directory ownership root is mounted: {path}")
    budget = _OwnershipBudget()
    inventory: list[tuple[str, str]] = []
    file_records: list[TreeFileRecord] = []
    entry_identities: list[tuple[str, str, tuple[int, ...]]] = []
    digest = _scan_owned_directory(
        descriptor,
        path,
        (),
        resources=selected_resources,
        root_device=opened.st_dev,
        mount_points=_linux_mount_points(),
        budget=budget,
        inventory=inventory,
        file_records=file_records,
        entry_identities=entry_identities,
        entry_policy=entry_policy,
        depth=0,
        required_root_file=required_root_file_bytes,
        allow_empty_root=allow_empty_root,
        check_cancelled=check_cancelled,
    )
    after = os.fstat(descriptor)
    if _ownership_version_identity(after) != _ownership_version_identity(opened):
        raise RuntimeError(f"directory ownership root changed: {path}")
    if check_cancelled is None:
        canonical_inventory = tuple(sorted(inventory))
        canonical_records = tuple(sorted(file_records, key=lambda record: record.path))
        canonical_identities = tuple(sorted(entry_identities))
    else:
        canonical_inventory = _interruptible_sorted_ownership_items(
            inventory,
            key=None,
            check_cancelled=check_cancelled,
        )
        canonical_records = _interruptible_sorted_ownership_items(
            file_records,
            key=lambda record: record.path,
            check_cancelled=check_cancelled,
        )
        canonical_identities = _interruptible_sorted_ownership_items(
            entry_identities,
            key=None,
            check_cancelled=check_cancelled,
        )
    return _TreeOwnership(
        root_identity=_directory_inode_identity(opened),
        root_version_identity=_ownership_version_identity(opened),
        digest=digest.hex(),
        entries=budget.entries,
        byte_count=budget.byte_count,
        metadata_bytes=budget.metadata_bytes,
        inventory=canonical_inventory,
        file_records=canonical_records,
        entry_identities=canonical_identities,
    )


def capture_directory_ownership(
    path: Path,
    *,
    required_root_file: str | None = None,
    allow_empty_root: bool = False,
    entry_policy: DirectoryEntryPolicy | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> _TreeOwnership:
    """Return a bounded token through a pinned parent/source authority."""

    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("directory ownership cancellation check must be callable")
    lexical = lexical_directory_path(path)
    with _PublicationAuthorityOwner() as authority_owner:
        if sys.platform.startswith("linux") or sys.platform == "darwin":
            authority = _open_posix_publication_authority(
                lexical.parent,
                parent_resource=None,
                expected_parent_identity=None,
                authority_owner=authority_owner,
            )
        elif sys.platform == "win32":
            authority = _open_windows_publication_authority(
                lexical.parent,
                parent_resource=None,
                expected_parent_identity=None,
                authority_owner=authority_owner,
            )
        else:
            raise RuntimeError(
                "safe directory ownership capture is unsupported on this host"
            )
        return authority.capture_child(
            lexical.name,
            path=lexical,
            label="directory ownership root",
            required_root_file=required_root_file,
            allow_empty_root=allow_empty_root,
            entry_policy=entry_policy,
            check_cancelled=check_cancelled,
        )


def capture_directory_ownership_if_exists(
    path: Path,
    *,
    required_root_file: str | None = None,
    allow_empty_root: bool = False,
    entry_policy: DirectoryEntryPolicy | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> _TreeOwnership | None:
    """Capture one existing tree or atomically observe that its child is absent.

    The missing observation and any capture use one pinned parent authority.
    A child that appears after a missing observation is not opened or blessed;
    callers can pass the returned ``None`` into a later no-replace operation.
    """

    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("directory ownership cancellation check must be callable")
    _required_root_file_bytes(required_root_file)
    lexical = lexical_directory_path(path)
    with _PublicationAuthorityOwner() as authority_owner:
        if sys.platform.startswith("linux") or sys.platform == "darwin":
            authority = _open_posix_publication_authority(
                lexical.parent,
                parent_resource=None,
                expected_parent_identity=None,
                authority_owner=authority_owner,
            )
        elif sys.platform == "win32":
            authority = _open_windows_publication_authority(
                lexical.parent,
                parent_resource=None,
                expected_parent_identity=None,
                authority_owner=authority_owner,
            )
        else:
            raise RuntimeError(
                "safe directory ownership capture is unsupported on this host"
            )
        metadata = authority.child_metadata(
            lexical.name,
            path=lexical,
            label="directory ownership root",
        )
        if metadata is None:
            authority.verify_path_binding()
            if check_cancelled is not None:
                check_cancelled()
            return None
        initial_root_identity = _directory_inode_identity(metadata)
        ownership = authority.capture_child(
            lexical.name,
            path=lexical,
            label="directory ownership root",
            required_root_file=required_root_file,
            allow_empty_root=allow_empty_root,
            entry_policy=entry_policy,
            check_cancelled=check_cancelled,
        )
        if ownership.root_identity != initial_root_identity:
            raise RuntimeError("directory ownership root changed while it was captured")
        authority.verify_path_binding()
        return ownership


def _ownership_token_path_bytes(value: object) -> bytes:
    """Return the exact path bytes represented by one captured token entry."""

    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise RuntimeError("directory ownership token contains an invalid path")
    if "\x00" in value:
        raise RuntimeError("directory ownership token contains an invalid path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise RuntimeError("directory ownership token contains a non-canonical path")
    if len(path.parts) > _MAX_SAFE_REMOVAL_DEPTH:
        raise RuntimeError("directory ownership token path exceeds its depth limit")
    try:
        encoded_parts = tuple(os.fsencode(part) for part in path.parts)
    except UnicodeEncodeError as exc:
        raise RuntimeError(
            "directory ownership token path cannot be represented"
        ) from exc
    if any(not part or part in {b".", b".."} for part in encoded_parts):
        raise RuntimeError("directory ownership token contains an invalid path")
    if any(len(part) > _MAX_OWNERSHIP_COMPONENT_BYTES for part in encoded_parts):
        raise RuntimeError("directory ownership token component exceeds its byte limit")
    return _ownership_relative_path(encoded_parts)


def _ownership_subtree_prefix(value: object) -> str:
    raw = value.as_posix() if isinstance(value, PurePosixPath) else value
    try:
        _ownership_token_path_bytes(raw)
    except RuntimeError as exc:
        raise ValueError(
            "directory ownership subtree prefix must be canonical and non-root"
        ) from exc
    if not isinstance(raw, str):
        raise ValueError(
            "directory ownership subtree prefix must be canonical and non-root"
        )
    return raw


def _validated_ownership_version_identity(
    value: object,
    *,
    kind: Literal["directory", "file"],
    root_device: int | None = None,
) -> tuple[int, ...]:
    if (
        not isinstance(value, tuple)
        or len(value) != 8
        or any(isinstance(part, bool) or not isinstance(part, int) for part in value)
    ):
        raise RuntimeError("directory ownership token contains an invalid identity")
    identity = value
    device, inode, mode, size, attributes, _mtime, _ctime, link_count = identity
    expected_type = stat.S_IFDIR if kind == "directory" else stat.S_IFREG
    if (
        device <= 0
        or inode <= 0
        or not 0 <= mode < 1 << 32
        or stat.S_IFMT(mode) != expected_type
        or size < 0
        or attributes < 0
        or attributes & _FILE_ATTRIBUTE_REPARSE_POINT
        or link_count <= 0
        or (kind == "directory" and size != 0)
        or (root_device is not None and device != root_device)
    ):
        raise RuntimeError("directory ownership token contains an invalid identity")
    return identity


def _validated_ownership_kind(
    value: object,
) -> Literal["directory", "file"]:
    if value == "directory":
        return "directory"
    if value == "file":
        return "file"
    raise RuntimeError("directory ownership token contains an invalid entry kind")


def _ownership_root_identity_from_version(
    identity: tuple[int, ...],
) -> tuple[int, ...]:
    return (identity[0], identity[1], stat.S_IFMT(identity[2]), identity[4])


def _rebuild_ownership_digest(
    root_version_identity: tuple[int, ...],
    entries: dict[str, tuple[Literal["directory", "file"], tuple[int, ...]]],
    records: dict[str, TreeFileRecord],
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> str:
    children: dict[str, list[str]] = {"": []}
    entry_count = len(entries)
    for index, (path, (kind, _identity)) in enumerate(entries.items()):
        parts = PurePosixPath(path).parts
        parent = "/".join(parts[:-1])
        if parent:
            parent_entry = entries.get(parent)
            if parent_entry is None or parent_entry[0] != "directory":
                raise RuntimeError(
                    "directory ownership token is missing a directory ancestor"
                )
        children.setdefault(parent, []).append(path)
        if kind == "directory":
            children.setdefault(path, [])
        if check_cancelled is not None and index + 1 < entry_count:
            check_cancelled()

    def raw_byte_order(paths: list[str]) -> Iterator[str]:
        terminal = -1
        trie: dict[int, object] = {}
        path_count = len(paths)
        for index, candidate in enumerate(paths):
            node = trie
            for octet in os.fsencode(PurePosixPath(candidate).name):
                child = node.get(octet)
                if child is None:
                    child = {}
                    node[octet] = child
                if not isinstance(child, dict):
                    raise RuntimeError(
                        "directory ownership token contains a duplicate raw name"
                    )
                node = child
            if terminal in node:
                raise RuntimeError(
                    "directory ownership token contains a duplicate raw name"
                )
            node[terminal] = candidate
            if check_cancelled is not None and index + 1 < path_count:
                check_cancelled()

        def visit(node: dict[int, object]) -> Iterator[str]:
            candidate = node.get(terminal)
            if candidate is not None:
                if not isinstance(candidate, str):
                    raise RuntimeError("directory ownership token trie is invalid")
                yield candidate
            keys = 0
            for octet in node:
                if octet >= 0:
                    keys |= 1 << octet
            while keys:
                lowest = keys & -keys
                octet = lowest.bit_length() - 1
                child = node[octet]
                if not isinstance(child, dict):
                    raise RuntimeError("directory ownership token trie is invalid")
                yield from visit(child)
                keys ^= lowest

        yield from visit(trie)

    def hash_directory(path: str, identity: tuple[int, ...]) -> bytes:
        hasher = hashlib.sha256()
        hasher.update(b"codenib.atomic-directory.v1\x00")
        hasher.update(stat.S_IMODE(identity[2]).to_bytes(4, "big"))
        child_paths = children.get(path, [])
        child_count = len(child_paths)
        for index, child in enumerate(raw_byte_order(child_paths)):
            kind, child_identity = entries[child]
            relative = _ownership_token_path_bytes(child)
            entry_hasher = hashlib.sha256()
            if kind == "directory":
                entry_hasher.update(b"D")
                _ownership_hash_field(entry_hasher, relative)
                entry_hasher.update(stat.S_IMODE(child_identity[2]).to_bytes(4, "big"))
                entry_hasher.update(hash_directory(child, child_identity))
            else:
                record = records[child]
                entry_hasher.update(b"F")
                _ownership_hash_field(entry_hasher, relative)
                entry_hasher.update(record.mode.to_bytes(4, "big"))
                entry_hasher.update(record.size.to_bytes(8, "big"))
                entry_hasher.update(bytes.fromhex(record.sha256))
            hasher.update(entry_hasher.digest())
            if check_cancelled is not None and index + 1 < child_count:
                check_cancelled()
        return hasher.digest()

    return hash_directory("", root_version_identity).hex()


def _validate_directory_ownership_token(
    ownership: _TreeOwnership,
    *,
    check_cancelled: Callable[[], None] | None = None,
    require_exact_types: bool = False,
) -> dict[str, tuple[Literal["directory", "file"], tuple[int, ...]]]:
    """Validate canonical structure and every redundant ownership-token field."""

    if not isinstance(ownership, _TreeOwnership):
        raise TypeError("ownership must be a captured directory ownership token")
    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("directory ownership cancellation check must be callable")
    if type(require_exact_types) is not bool:
        raise TypeError("exact ownership type policy must be a boolean")
    if require_exact_types and (
        type(ownership) is not _TreeOwnership
        or type(ownership.root_identity) is not tuple
        or any(type(item) is not int for item in ownership.root_identity)
        or type(ownership.root_version_identity) is not tuple
        or any(type(item) is not int for item in ownership.root_version_identity)
        or type(ownership.digest) is not str
        or type(ownership.entries) is not int
        or type(ownership.byte_count) is not int
        or type(ownership.metadata_bytes) is not int
        or type(ownership.inventory) is not tuple
        or type(ownership.file_records) is not tuple
        or type(ownership.entry_identities) is not tuple
    ):
        raise TypeError("directory ownership token fields are not exact")
    root_version = _validated_ownership_version_identity(
        ownership.root_version_identity,
        kind="directory",
    )
    if ownership.root_identity != _ownership_root_identity_from_version(root_version):
        raise RuntimeError("directory ownership token root identity is inconsistent")
    for value, limit, label in (
        (ownership.entries, _MAX_OWNERSHIP_ENTRIES, "entry"),
        (ownership.byte_count, _MAX_OWNERSHIP_BYTES, "byte"),
        (ownership.metadata_bytes, _MAX_OWNERSHIP_METADATA_BYTES, "metadata"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= limit
        ):
            raise RuntimeError(
                f"directory ownership token contains an invalid {label} count"
            )
    if (
        not isinstance(ownership.digest, str)
        or len(ownership.digest) != 64
        or ownership.digest != ownership.digest.lower()
    ):
        raise RuntimeError("directory ownership token contains an invalid digest")
    try:
        digest_bytes = bytes.fromhex(ownership.digest)
    except ValueError as exc:
        raise RuntimeError(
            "directory ownership token contains an invalid digest"
        ) from exc
    if len(digest_bytes) != hashlib.sha256().digest_size or any(
        character not in "0123456789abcdef" for character in ownership.digest
    ):
        raise RuntimeError("directory ownership token contains an invalid digest")
    if not all(
        isinstance(collection, tuple)
        for collection in (
            ownership.inventory,
            ownership.file_records,
            ownership.entry_identities,
        )
    ):
        raise RuntimeError("directory ownership token collections are not canonical")
    inventory_count = len(ownership.inventory)
    if ownership.entries != inventory_count:
        raise RuntimeError("directory ownership token entry count is inconsistent")
    identity_count = len(ownership.entry_identities)
    if identity_count != inventory_count:
        raise RuntimeError("directory ownership token identity count is inconsistent")
    record_count = len(ownership.file_records)
    if record_count > inventory_count:
        raise RuntimeError(
            "directory ownership token file record count is inconsistent"
        )

    entries: dict[str, tuple[Literal["directory", "file"], tuple[int, ...]]] = {}
    inventory_kinds: dict[str, Literal["directory", "file"]] = {}
    metadata_bytes = 0
    expected_file_count = 0
    previous_inventory: tuple[str, str] | None = None
    for index, inventory_item in enumerate(ownership.inventory):
        if require_exact_types and (
            type(inventory_item) is not tuple
            or len(inventory_item) != 2
            or type(inventory_item[0]) is not str
            or type(inventory_item[1]) is not str
        ):
            raise TypeError("directory ownership token inventory is not exact")
        if (
            not isinstance(inventory_item, tuple)
            or len(inventory_item) != 2
            or inventory_item[1] not in {"directory", "file"}
        ):
            raise RuntimeError("directory ownership token inventory is invalid")
        path, raw_kind = inventory_item
        kind = _validated_ownership_kind(raw_kind)
        path_bytes = _ownership_token_path_bytes(path)
        if path in inventory_kinds:
            raise RuntimeError("directory ownership token contains a duplicate path")
        current_inventory = (path, kind)
        if previous_inventory is not None and previous_inventory >= current_inventory:
            raise RuntimeError("directory ownership token inventory is not canonical")
        inventory_kinds[path] = kind
        previous_inventory = current_inventory
        metadata_bytes += 8 + len(path_bytes) + 1 + 4 + 8 + 32
        if kind == "file":
            expected_file_count += 1
        if check_cancelled is not None and index + 1 < inventory_count:
            check_cancelled()
    seen_identities: set[str] = set()
    previous_identity: tuple[str, str, tuple[int, ...]] | None = None
    for index, identity_item in enumerate(ownership.entry_identities):
        if require_exact_types and (
            type(identity_item) is not tuple
            or len(identity_item) != 3
            or type(identity_item[0]) is not str
            or type(identity_item[1]) is not str
            or type(identity_item[2]) is not tuple
            or any(type(item) is not int for item in identity_item[2])
        ):
            raise TypeError("directory ownership token identities are not exact")
        if not isinstance(identity_item, tuple) or len(identity_item) != 3:
            raise RuntimeError("directory ownership token identities are invalid")
        path, raw_kind, raw_identity = identity_item
        kind = _validated_ownership_kind(raw_kind)
        if path in seen_identities or inventory_kinds.get(path) != kind:
            raise RuntimeError("directory ownership token identities are inconsistent")
        identity = _validated_ownership_version_identity(
            raw_identity,
            kind=kind,
            root_device=root_version[0],
        )
        entries[path] = (kind, identity)
        seen_identities.add(path)
        current_identity = (path, kind, identity)
        if previous_identity is not None and previous_identity >= current_identity:
            raise RuntimeError("directory ownership token identities are not canonical")
        previous_identity = current_identity
        if check_cancelled is not None and index + 1 < identity_count:
            check_cancelled()
    if len(seen_identities) != len(inventory_kinds):
        raise RuntimeError("directory ownership token identities are not canonical")

    records: dict[str, TreeFileRecord] = {}
    byte_count = 0
    previous_record: TreeFileRecord | None = None
    for index, record in enumerate(ownership.file_records):
        if require_exact_types and (
            type(record) is not TreeFileRecord
            or type(record.path) is not str
            or type(record.mode) is not int
            or type(record.size) is not int
            or type(record.sha256) is not str
        ):
            raise TypeError("directory ownership token file records are not exact")
        if not isinstance(record, TreeFileRecord):
            raise RuntimeError("directory ownership token file records are invalid")
        entry = entries.get(record.path)
        if entry is None or entry[0] != "file" or record.path in records:
            raise RuntimeError(
                "directory ownership token file records are inconsistent"
            )
        if (
            isinstance(record.mode, bool)
            or not isinstance(record.mode, int)
            or not 0 <= record.mode <= 0o7777
            or isinstance(record.size, bool)
            or not isinstance(record.size, int)
            or record.size < 0
            or record.size != entry[1][3]
            or record.mode != stat.S_IMODE(entry[1][2])
            or not isinstance(record.sha256, str)
            or len(record.sha256) != 64
            or record.sha256 != record.sha256.lower()
        ):
            raise RuntimeError("directory ownership token file record is invalid")
        try:
            record_digest = bytes.fromhex(record.sha256)
        except ValueError as exc:
            raise RuntimeError(
                "directory ownership token file record is invalid"
            ) from exc
        if len(record_digest) != hashlib.sha256().digest_size or any(
            character not in "0123456789abcdef" for character in record.sha256
        ):
            raise RuntimeError("directory ownership token file record is invalid")
        if previous_record is not None and previous_record.path >= record.path:
            raise RuntimeError(
                "directory ownership token file records are not canonical"
            )
        records[record.path] = record
        previous_record = record
        byte_count += record.size
        if check_cancelled is not None and index + 1 < record_count:
            check_cancelled()
    if len(records) != expected_file_count:
        raise RuntimeError("directory ownership token file records are not canonical")

    if byte_count != ownership.byte_count or metadata_bytes != ownership.metadata_bytes:
        raise RuntimeError("directory ownership token accounting is inconsistent")
    rebuilt_digest = (
        _rebuild_ownership_digest(root_version, entries, records)
        if check_cancelled is None
        else _rebuild_ownership_digest(
            root_version,
            entries,
            records,
            check_cancelled=check_cancelled,
        )
    )
    if rebuilt_digest != ownership.digest:
        raise RuntimeError("directory ownership token digest is inconsistent")
    return entries


def project_directory_ownership_subtree(
    outer: _TreeOwnership,
    prefix: str | PurePosixPath,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> _TreeOwnership:
    """Derive an exact subtree token without reopening filesystem paths.

    The returned token describes data only.  It does not grant read authority;
    callers need an active ``PublicationDirectoryReader.subtree`` facade to
    consume the corresponding bytes.
    """

    entries = (
        _validate_directory_ownership_token(outer)
        if check_cancelled is None
        else _validate_directory_ownership_token(
            outer,
            check_cancelled=check_cancelled,
        )
    )
    normalized = _ownership_subtree_prefix(prefix)
    selected = entries.get(normalized)
    if selected is None:
        raise ValueError("directory ownership subtree prefix is absent")
    if selected[0] != "directory":
        raise ValueError("directory ownership subtree prefix is not a directory")
    root_version = selected[1]
    descendant_prefix = f"{normalized}/"

    projected_inventory_items: list[tuple[str, str]] = []
    metadata_bytes = 0
    inventory_count = len(outer.inventory)
    for index, (path, kind) in enumerate(outer.inventory):
        if path.startswith(descendant_prefix):
            projected_path = path[len(descendant_prefix) :]
            projected_inventory_items.append((projected_path, kind))
            metadata_bytes += (
                8 + len(_ownership_token_path_bytes(projected_path)) + 1 + 4 + 8 + 32
            )
        if check_cancelled is not None and index + 1 < inventory_count:
            check_cancelled()

    projected_record_items: list[TreeFileRecord] = []
    records: dict[str, TreeFileRecord] = {}
    byte_count = 0
    record_count = len(outer.file_records)
    for index, record in enumerate(outer.file_records):
        if record.path.startswith(descendant_prefix):
            projected_record = replace(
                record,
                path=record.path[len(descendant_prefix) :],
            )
            projected_record_items.append(projected_record)
            records[projected_record.path] = projected_record
            byte_count += projected_record.size
        if check_cancelled is not None and index + 1 < record_count:
            check_cancelled()

    projected_identity_items: list[tuple[str, str, tuple[int, ...]]] = []
    projected_entries: dict[
        str, tuple[Literal["directory", "file"], tuple[int, ...]]
    ] = {}
    identity_count = len(outer.entry_identities)
    for index, (path, kind, identity) in enumerate(outer.entry_identities):
        if path.startswith(descendant_prefix):
            projected_path = path[len(descendant_prefix) :]
            validated_kind = _validated_ownership_kind(kind)
            projected_identity_items.append((projected_path, validated_kind, identity))
            projected_entries[projected_path] = (validated_kind, identity)
        if check_cancelled is not None and index + 1 < identity_count:
            check_cancelled()

    projected_inventory = _interruptible_ownership_tuple(
        projected_inventory_items,
        check_cancelled=check_cancelled,
    )
    projected_records = _interruptible_ownership_tuple(
        projected_record_items,
        check_cancelled=check_cancelled,
    )
    projected_identities = _interruptible_ownership_tuple(
        projected_identity_items,
        check_cancelled=check_cancelled,
    )
    digest = (
        _rebuild_ownership_digest(root_version, projected_entries, records)
        if check_cancelled is None
        else _rebuild_ownership_digest(
            root_version,
            projected_entries,
            records,
            check_cancelled=check_cancelled,
        )
    )
    projected = _TreeOwnership(
        root_identity=_ownership_root_identity_from_version(root_version),
        root_version_identity=root_version,
        digest=digest,
        entries=len(projected_inventory),
        byte_count=byte_count,
        metadata_bytes=metadata_bytes,
        inventory=projected_inventory,
        file_records=projected_records,
        entry_identities=projected_identities,
    )
    if check_cancelled is None:
        _validate_directory_ownership_token(projected)
    else:
        _validate_directory_ownership_token(
            projected,
            check_cancelled=check_cancelled,
        )
    return projected


def directory_ownership_inventory(
    ownership: _TreeOwnership,
) -> tuple[tuple[str, str], ...]:
    """Return the bounded no-follow inventory captured with an ownership token."""

    if not isinstance(ownership, _TreeOwnership):
        raise TypeError("ownership must be a captured directory ownership token")
    return ownership.inventory


def directory_ownership_file_records(
    ownership: _TreeOwnership,
) -> tuple[TreeFileRecord, ...]:
    """Return canonical per-file records bound by an ownership token."""

    if not isinstance(ownership, _TreeOwnership):
        raise TypeError("ownership must be a captured directory ownership token")
    return ownership.file_records


def directory_ownership_entry_identities(
    ownership: _TreeOwnership,
) -> tuple[tuple[str, str, tuple[int, ...]], ...]:
    """Return private filesystem identities captured for every tree entry."""

    if not isinstance(ownership, _TreeOwnership):
        raise TypeError("ownership must be a captured directory ownership token")
    return ownership.entry_identities


def directory_ownership_root_version_identity(
    ownership: _TreeOwnership,
) -> tuple[int, ...]:
    """Return the captured root version identity used by pinned readers."""

    if not isinstance(ownership, _TreeOwnership):
        raise TypeError("ownership must be a captured directory ownership token")
    return ownership.root_version_identity


def directory_ownership_root_identity(
    ownership: _TreeOwnership,
) -> tuple[int, ...]:
    """Return the no-follow root identity bound by an ownership token."""

    if not isinstance(ownership, _TreeOwnership):
        raise TypeError("ownership must be a captured directory ownership token")
    return ownership.root_identity


def directory_ownership_digest(ownership: _TreeOwnership) -> str:
    """Return the canonical complete-tree digest bound by a token."""

    if not isinstance(ownership, _TreeOwnership):
        raise TypeError("ownership must be a captured directory ownership token")
    return ownership.digest


def require_publishable_directory_ownership(
    ownership: _TreeOwnership,
    *,
    label: str = "staged directory",
) -> None:
    """Require strong IDs and private regular files for a future active tree.

    Observation tokens may describe a legacy destination containing hard
    links because online publication only isolates that old root.  A staged
    tree is different: every regular file must have exactly one link, or an
    alias outside the root could mutate bytes after successful publication.
    """

    if not isinstance(ownership, _TreeOwnership):
        raise TypeError("ownership must be a captured directory ownership token")
    if not ownership.root_identity[0] or not ownership.root_identity[1]:
        raise RuntimeError(f"{label} has no reliable root identity")
    for path, kind, identity in ownership.entry_identities:
        if not identity[0] or not identity[1]:
            raise RuntimeError(f"{label} entry has no reliable identity: {path}")
        if kind == "file" and identity[-1] != 1:
            raise RuntimeError(f"{label} file has an external hard link: {path}")


def _require_matching_ownership(
    observed: _TreeOwnership,
    expected: _TreeOwnership,
    *,
    label: str,
    allow_root_rename: bool = False,
) -> None:
    allow_root_rename = allow_root_rename or label in {
        "moved destination",
        "previous destination",
        "published staged directory",
    }
    if observed != expected and not (
        allow_root_rename
        and replace(
            observed,
            root_version_identity=expected.root_version_identity,
        )
        == expected
    ):
        raise RuntimeError(f"{label} changed during directory publication")


def _require_tree_ownership_at(
    authority: _PublicationAuthority,
    name: str,
    *,
    path: Path,
    expected: _TreeOwnership,
    label: str,
    allow_root_rename: bool = False,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    callback_error: BaseException | None = None

    def poll() -> None:
        nonlocal callback_error
        assert check_cancelled is not None
        try:
            check_cancelled()
        except BaseException as error:  # noqa: B036 - preserve exact callback fault
            callback_error = error
            raise

    try:
        if check_cancelled is None:
            observed = authority.capture_child(
                name,
                path=path,
                label=label,
            )
        else:
            observed = authority.capture_child(
                name,
                path=path,
                label=label,
                check_cancelled=poll,
            )
    except (OSError, ValueError, RuntimeError) as exc:
        if exc is callback_error:
            raise
        raise RuntimeError(f"{label} changed during directory publication") from exc
    _require_matching_ownership(
        observed,
        expected,
        label=label,
        allow_root_rename=allow_root_rename,
    )
    if check_cancelled is not None:
        check_cancelled()


def _require_tree_ownership(
    path: Path,
    expected: _TreeOwnership,
    *,
    label: str,
    allow_root_rename: bool = False,
) -> None:
    """Compatibility wrapper for non-publication ownership checks."""

    try:
        observed = capture_directory_ownership(path)
    except (OSError, ValueError, RuntimeError) as exc:
        raise RuntimeError(f"{label} changed during directory publication") from exc
    _require_matching_ownership(
        observed,
        expected,
        label=label,
        allow_root_rename=allow_root_rename,
    )


def _run_authenticated_directory_callback(
    reader: PublicationDirectoryReader,
    expected_ownership: _TreeOwnership,
    callback: Callable[[PublicationDirectoryReader], _T],
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> _T:
    """Run one authenticated callback inside an exact ownership sandwich."""

    def consume() -> _T:
        before = reader.capture_ownership(
            check_cancelled=check_cancelled,
        )
        if before != expected_ownership:
            raise RuntimeError(
                "authenticated directory differs from expected ownership"
            )
        if check_cancelled is not None:
            check_cancelled()
        return callback(reader)

    def validate_after_ownership() -> None:
        if check_cancelled is None:
            after = reader.capture_ownership(check_cancelled=None)
        else:
            callback_errors: list[BaseException] = []
            reader_was_invalid = reader._authentication_failed

            def poll() -> None:
                try:
                    check_cancelled()
                except BaseException as error:  # noqa: B036 - exact provenance
                    callback_errors.append(error)
                    raise

            try:
                after = reader.capture_ownership(check_cancelled=poll)
            except BaseException as error:  # noqa: B036 - reconcile exact stop
                if not any(error is candidate for candidate in callback_errors):
                    raise
                try:
                    reconciled = reader.capture_ownership()
                except BaseException as reconciliation_error:  # noqa: B036
                    raise reconciliation_error from error
                if reconciled != expected_ownership:
                    raise RuntimeError(
                        "authenticated directory changed while it was consumed"
                    ) from error
                if not reader_was_invalid:
                    reader._authentication_failed = False
                raise
        if after != expected_ownership:
            raise RuntimeError("authenticated directory changed while it was consumed")

    return _run_callback_with_post_validations(
        consume,
        (
            (
                "authenticated directory post-callback reader validity "
                "validation also failed",
                reader._require_valid,
            ),
            (
                "authenticated directory post-callback ownership validation "
                "also failed",
                validate_after_ownership,
            ),
        ),
    )


def reopen_authenticated_directory(
    path: Path,
    expected_ownership: _TreeOwnership,
    callback: Callable[[PublicationDirectoryReader], _T],
    *,
    expected_parent_identity: tuple[int, ...] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> _T:
    """Consume an existing tree through one backend-neutral authority.

    The complete tree is freshly matched before and after ``callback``.  File
    reads remain bound to ``expected_ownership`` and the callback-scoped reader
    cannot escape as a path authority.  Linux/macOS use retained directory fds;
    Windows uses retained parent/root HANDLEs and FILE_ID traversal.
    """

    if not isinstance(expected_ownership, _TreeOwnership):
        raise TypeError(
            "expected ownership must be a captured directory ownership token"
        )
    if not callable(callback):
        raise TypeError("authenticated directory callback must be callable")
    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("authenticated directory cancellation check must be callable")
    lexical = lexical_directory_path(path)
    with _PublicationAuthorityOwner() as authority_owner:
        authority = _open_publication_authority(
            lexical.parent,
            parent_resource=None,
            expected_parent_identity=expected_parent_identity,
            authority_owner=authority_owner,
        )

        return _run_callback_with_post_validations(
            lambda: authority.read_child(
                lexical.name,
                path=lexical,
                label="authenticated directory",
                expected_ownership=expected_ownership,
                callback=lambda reader: _run_authenticated_directory_callback(
                    reader,
                    expected_ownership,
                    callback,
                    check_cancelled=check_cancelled,
                ),
            ),
            (
                (
                    "authenticated directory authority path validation also failed",
                    authority.verify_path_binding,
                ),
            ),
        )


def discard_owned_directory(
    path: Path,
    ownership: _TreeOwnership,
) -> DirectoryOrphan | None:
    """Atomically isolate an exact owned tree without recursively deleting it.

    Under a hostile same-UID process there is no safe final ``stat -> unlink``
    sequence: a validated entry can always be replaced immediately before the
    destructive syscall.  Online cleanup therefore claims the complete root
    under a random hidden name and returns bounded metadata for a later,
    explicitly cooperative GC pass.
    """

    if not isinstance(ownership, _TreeOwnership):
        raise TypeError("ownership must be a captured directory ownership token")
    lexical = lexical_directory_path(path)
    _require_rename_noreplace_platform()
    authority_owner = _PublicationAuthorityOwner()
    authority: _PublicationAuthority | None = None
    primary_error: BaseException | None = None
    try:
        authority = _open_publication_authority(
            lexical.parent,
            parent_resource=None,
            expected_parent_identity=None,
            authority_owner=authority_owner,
        )
        metadata = _directory_or_missing_at(
            authority,
            lexical.name,
            path=lexical,
            label="owned temporary directory",
        )
        if (
            metadata is None
            or _directory_inode_identity(metadata) != ownership.root_identity
        ):
            return None
        try:
            _require_tree_ownership_at(
                authority,
                lexical.name,
                path=lexical,
                expected=ownership,
                label="owned temporary directory",
            )
        except (OSError, RuntimeError, ValueError):
            return None
        orphan = _claim_child_as_orphan(
            authority,
            lexical.name,
            display_parent=lexical.parent,
            destination_name=lexical.name,
            purpose="discarded",
        )
        try:
            moved = _directory_or_missing_at(
                authority,
                orphan.name,
                path=orphan,
                label="discarded directory orphan",
            )
            if (
                moved is None
                or _directory_inode_identity(moved) != ownership.root_identity
            ):
                raise RuntimeError("discarded directory identity changed at claim")
            _require_tree_ownership_at(
                authority,
                orphan.name,
                path=orphan,
                expected=ownership,
                label="moved destination",
                allow_root_rename=True,
            )
        except (OSError, RuntimeError, ValueError):
            try:
                authority.rename_noreplace(orphan.name, lexical.name)
            except (OSError, RuntimeError):
                return _orphan_metadata(
                    authority,
                    orphan,
                    ownership,
                    verified_at_isolation=False,
                )
            return None
        try:
            return _orphan_metadata(authority, orphan, ownership)
        except (OSError, RuntimeError, ValueError):
            # Isolation completed but a raced mutation prevented a verified
            # receipt.  Restore without replacement; if that is no longer
            # possible, return an explicitly unverified authority locator.
            try:
                authority.rename_noreplace(orphan.name, lexical.name)
            except (OSError, RuntimeError):
                return _orphan_metadata(
                    authority,
                    orphan,
                    ownership,
                    verified_at_isolation=False,
                )
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if primary_error is None:
            authority_owner.close()
        else:
            authority_owner.close_after_error(primary_error)


def _quarantine_destination(
    destination: Path,
    *,
    parent_descriptor: int | None = None,
    parent_authority: _PublicationAuthority | None = None,
) -> Path | None:
    """Move an untrusted post-publication object out of the caller's stage path."""

    lexical = lexical_directory_path(destination)
    authority_owner = _PublicationAuthorityOwner()
    owned_authority: _PublicationAuthority | None = None
    authority = parent_authority
    primary_error: BaseException | None = None
    try:
        if authority is None:
            owned_authority = _open_publication_authority(
                lexical.parent,
                parent_resource=parent_descriptor,
                expected_parent_identity=None,
                authority_owner=authority_owner,
            )
            authority = owned_authority
        metadata = authority.child_metadata(
            lexical.name,
            path=lexical,
            label="quarantine destination",
        )
        if metadata is None:
            return None
        return _claim_child_as_orphan(
            authority,
            lexical.name,
            display_parent=lexical.parent,
            destination_name=lexical.name,
            purpose="quarantine",
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if primary_error is None:
            authority_owner.close()
        else:
            authority_owner.close_after_error(primary_error)


def _require_durable_publication_commit_authority(
    publication_authority: _PublicationAuthority,
) -> None:
    """Reject unsupported strict commit authorities before any mutation."""

    if (
        not sys.platform.startswith("linux")
        or publication_authority.backend_tag not in _DURABLE_PUBLICATION_FSYNC_BACKENDS
    ):
        raise RuntimeError(
            "durable directory commit requires a supported Linux publication "
            "authority"
        )
    resource = publication_authority.resource
    if isinstance(resource, bool) or not isinstance(resource, int) or resource < 0:
        raise RuntimeError(
            "durable directory commit requires a valid POSIX parent descriptor"
        )


def _fsync_publication_parent_for_commit(
    publication_authority: _PublicationAuthority,
) -> None:
    """Durably order a supported strict commit before its ownership handoff."""

    publication_authority.sync_parent()


def _publish_staged_directory_with_authority(
    publication_authority: _PublicationAuthority,
    stage: Path,
    destination: Path,
    *,
    expected_destination_identity: tuple[int, ...] | None | object = (
        _EXPECTED_DESTINATION_UNSET
    ),
    expected_stage_identity: tuple[int, ...] | None = None,
    expected_stage_root_ownership: _TreeOwnership | None = None,
    expected_destination_ownership: _TreeOwnership | None | object = (
        _EXPECTED_OWNERSHIP_UNSET
    ),
    validate_staged_directory: (
        Callable[[PublicationDirectoryReader], None] | None
    ) = None,
    validate_moved_destination: (
        Callable[[PublicationDirectoryReader], None] | None
    ) = None,
    validate_published_destination: (
        Callable[[PublicationDirectoryReader], None] | None
    ) = None,
    commit_callback: (
        Callable[
            [
                _TreeOwnership,
                _TreeOwnership,
                DirectoryOrphan | None,
                object | None,
            ],
            None,
        ]
        | None
    ) = None,
    check_cancelled: Callable[[], None] | None = None,
) -> DirectoryOrphan | None:
    """Publish a complete stage through a caller-owned parent authority.

    Every namespace mutation is an atomic no-replace rename relative to the
    pinned parent descriptor.  The previous tree is intentionally retained as
    a hidden, fully authenticated orphan for cooperative GC; online recursive
    deletion cannot be made safe against a hostile same-UID replacement race.

    This helper borrows ``publication_authority``.  It never acquires, closes,
    or transfers that authority.  ``commit_callback`` receives the sealed
    pre-rename token, a fresh post-rename token, the previous orphan, and an
    optional backend-private receipt capability.  The capability is held only
    in this protected publication frame until every validator has succeeded.
    The callback is the final fallible action after publication is fully
    authenticated and the supported parent namespace has been durably
    synchronized; once it starts, its failures propagate without rolling the
    publication back.

    Parent fsync orders only the namespace publication.  Strict callers must
    durably seal every staged file and directory before invoking this helper;
    a ``_TreeOwnership`` token authenticates observations but is not itself a
    durability receipt.
    """

    stage_display = lexical_directory_path(stage)
    destination_display = lexical_directory_path(destination)
    if stage_display == destination_display:
        raise ValueError("staged and destination directories must differ")
    if stage_display.parent != destination_display.parent:
        raise ValueError("staged and destination directories must share a parent")
    if stage_display.parent != publication_authority.display_parent:
        raise ValueError(
            "staged and destination directories must match the authority parent"
        )
    if publication_authority.backend_tag == _NATIVE_REPLACEMENT_PUBLICATION_BACKEND:
        raise RuntimeError(
            "native workspace replacement requires its dedicated exchange path"
        )
    if commit_callback is not None:
        if not callable(commit_callback):
            raise TypeError("directory commit callback must be callable")
        _require_durable_publication_commit_authority(publication_authority)
    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("directory publication cancellation check must be callable")

    def capture_pretransition_ownership(
        name: str,
        *,
        path: Path,
        label: str,
    ) -> _TreeOwnership:
        if check_cancelled is None:
            return publication_authority.capture_child(
                name,
                path=path,
                label=label,
            )
        return publication_authority.capture_child(
            name,
            path=path,
            label=label,
            check_cancelled=check_cancelled,
        )

    def require_pretransition_ownership(
        name: str,
        *,
        path: Path,
        expected: _TreeOwnership,
        label: str,
    ) -> None:
        if check_cancelled is None:
            _require_tree_ownership_at(
                publication_authority,
                name,
                path=path,
                expected=expected,
                label=label,
            )
            return
        _require_tree_ownership_at(
            publication_authority,
            name,
            path=path,
            expected=expected,
            label=label,
            check_cancelled=check_cancelled,
        )

    def perform_publication() -> DirectoryOrphan | None:
        stage_metadata = _directory_or_missing_at(
            publication_authority,
            stage_display.name,
            path=stage_display,
            label="staged directory",
        )
        if stage_metadata is None:
            raise ValueError(f"staged directory does not exist: {stage_display}")
        if (
            expected_stage_identity is not None
            and _directory_identity(stage_metadata) != expected_stage_identity
        ):
            raise RuntimeError("staged directory changed before directory publication")
        required_stage_inode_identity = _directory_inode_identity(stage_metadata)
        if (
            expected_stage_root_ownership is not None
            and required_stage_inode_identity
            != expected_stage_root_ownership.root_identity
        ):
            raise RuntimeError("staged directory root changed before publication")

        stage_ownership = capture_pretransition_ownership(
            stage_display.name,
            path=stage_display,
            label="staged directory",
        )
        require_publishable_directory_ownership(
            stage_ownership,
            label="staged directory",
        )
        if (
            expected_stage_root_ownership is not None
            and stage_ownership != expected_stage_root_ownership
        ):
            raise RuntimeError("staged directory changed before publication")
        if check_cancelled is not None:
            check_cancelled()
        if validate_staged_directory is not None:
            publication_authority.read_child(
                stage_display.name,
                path=stage_display,
                label="staged directory",
                expected_ownership=stage_ownership,
                callback=validate_staged_directory,
            )
            require_pretransition_ownership(
                stage_display.name,
                path=stage_display,
                expected=stage_ownership,
                label="staged directory",
            )

        destination_metadata = _directory_or_missing_at(
            publication_authority,
            destination_display.name,
            path=destination_display,
            label="destination",
        )
        native_candidate_cleanup = (
            publication_authority.backend_tag == "linux-native-workspace-owner"
        )
        if native_candidate_cleanup and destination_metadata is not None:
            raise FileExistsError(
                "native workspace publication requires a missing destination"
            )
        observed_destination_ownership = (
            None
            if destination_metadata is None
            else capture_pretransition_ownership(
                destination_display.name,
                path=destination_display,
                label="destination",
            )
        )
        if expected_destination_ownership is not _EXPECTED_OWNERSHIP_UNSET and (
            observed_destination_ownership != expected_destination_ownership
        ):
            raise RuntimeError("destination changed before directory publication")
        if expected_destination_identity is not _EXPECTED_DESTINATION_UNSET:
            observed_identity = (
                None
                if destination_metadata is None
                else _directory_identity(destination_metadata)
            )
            if observed_identity != expected_destination_identity:
                raise RuntimeError("destination changed before directory publication")
        if check_cancelled is not None:
            check_cancelled()

        destination_was_missing = destination_metadata is None
        require_pretransition_ownership(
            stage_display.name,
            path=stage_display,
            expected=stage_ownership,
            label="staged directory",
        )
        backup: Path | None = None
        moved_destination_ownership: _TreeOwnership | None = None
        required_destination_inode_identity: tuple[int, ...] | None = None
        if not destination_was_missing:
            assert observed_destination_ownership is not None
            moved_destination_ownership = observed_destination_ownership
            required_destination_inode_identity = _directory_inode_identity(
                destination_metadata
            )
            require_pretransition_ownership(
                destination_display.name,
                path=destination_display,
                expected=moved_destination_ownership,
                label="destination",
            )
            backup = _claim_child_as_orphan(
                publication_authority,
                destination_display.name,
                display_parent=destination_display.parent,
                destination_name=destination_display.name,
                purpose="previous",
            )

        def restore_previous(*, context: str) -> None:
            if destination_was_missing:
                return
            assert backup is not None
            assert moved_destination_ownership is not None
            try:
                _restore_exact_previous_directory(
                    backup,
                    destination_display,
                    destination_was_missing=destination_was_missing,
                    parent_authority=publication_authority,
                    ownership=moved_destination_ownership,
                )
            except BaseException as restore_error:  # noqa: B036 - report isolation
                raise RuntimeError(
                    f"{context}; previous output remains isolated at {backup}"
                ) from restore_error

        try:
            if destination_was_missing:
                require_pretransition_ownership(
                    stage_display.name,
                    path=stage_display,
                    expected=stage_ownership,
                    label="staged directory",
                )
            else:
                assert backup is not None
                assert moved_destination_ownership is not None
                assert required_destination_inode_identity is not None
                moved_metadata = _directory_or_missing_at(
                    publication_authority,
                    backup.name,
                    path=backup,
                    label="moved destination",
                )
                if (
                    moved_metadata is None
                    or _directory_inode_identity(moved_metadata)
                    != required_destination_inode_identity
                ):
                    raise RuntimeError(
                        "destination changed at the publication boundary"
                    )
                _require_tree_ownership_at(
                    publication_authority,
                    backup.name,
                    path=backup,
                    expected=moved_destination_ownership,
                    label="moved destination",
                    allow_root_rename=True,
                )
                if validate_moved_destination is not None:
                    publication_authority.read_child(
                        backup.name,
                        path=backup,
                        label="moved destination",
                        expected_ownership=moved_destination_ownership,
                        callback=validate_moved_destination,
                    )
                    _require_tree_ownership_at(
                        publication_authority,
                        backup.name,
                        path=backup,
                        expected=moved_destination_ownership,
                        label="moved destination",
                        allow_root_rename=True,
                    )
                _require_tree_ownership_at(
                    publication_authority,
                    stage_display.name,
                    path=stage_display,
                    expected=stage_ownership,
                    label="staged directory",
                )
        except BaseException:
            if backup is not None:
                _restore_claimed_directory(
                    backup,
                    destination_display,
                    parent_authority=publication_authority,
                    context="destination validation failed after isolation",
                )
            raise

        try:
            publication_token = publication_authority.rename_noreplace(
                stage_display.name,
                destination_display.name,
            )
        except BaseException:
            if native_candidate_cleanup:
                # The aggregate owner still holds the exact candidate and its
                # original stage/destination binding.  The outer receipt
                # reconciliation aborts and quarantines it without exposing a
                # second arbitrary rename capability here.
                raise
            # If the kernel completed the rename before an asynchronous error,
            # isolate only a tree that still matches the complete stage token.
            try:
                _require_tree_ownership_at(
                    publication_authority,
                    destination_display.name,
                    path=destination_display,
                    expected=stage_ownership,
                    label="published staged directory",
                    allow_root_rename=True,
                )
            except BaseException:  # noqa: B036 - probe unknown syscall outcome
                pass
            else:
                _quarantine_destination(
                    destination_display,
                    parent_authority=publication_authority,
                )
            restore_previous(context="directory publication failed")
            raise

        try:
            published_metadata = _directory_or_missing_at(
                publication_authority,
                destination_display.name,
                path=destination_display,
                label="published staged directory",
            )
            if (
                published_metadata is None
                or _directory_inode_identity(published_metadata)
                != required_stage_inode_identity
            ):
                raise RuntimeError(
                    "staged directory changed at the publication boundary"
                )
            _require_tree_ownership_at(
                publication_authority,
                destination_display.name,
                path=destination_display,
                expected=stage_ownership,
                label="published staged directory",
                allow_root_rename=True,
            )
            if validate_published_destination is not None:
                publication_authority.read_child(
                    destination_display.name,
                    path=destination_display,
                    label="published staged directory",
                    expected_ownership=stage_ownership,
                    callback=validate_published_destination,
                )
                _require_tree_ownership_at(
                    publication_authority,
                    destination_display.name,
                    path=destination_display,
                    expected=stage_ownership,
                    label="published staged directory",
                    allow_root_rename=True,
                )
        except BaseException as boundary_error:  # noqa: B036 - safe rollback
            if native_candidate_cleanup:
                # Preserve the exact validator/observation error.  The caller
                # has not installed a receipt yet, so its reserved transfer
                # deterministically invokes the native owner's abort path.
                raise
            quarantine = _quarantine_destination(
                destination_display,
                parent_authority=publication_authority,
            )
            restore_previous(
                context="published directory failed validation and rollback failed"
            )
            quarantine_message = (
                f" at {quarantine}" if quarantine is not None else " after it vanished"
            )
            raise RuntimeError(
                "published directory identity failed validation; suspect output was "
                f"quarantined{quarantine_message}"
            ) from boundary_error

        if backup is not None:
            assert moved_destination_ownership is not None
            try:
                _require_tree_ownership_at(
                    publication_authority,
                    backup.name,
                    path=backup,
                    expected=moved_destination_ownership,
                    label="previous destination",
                    allow_root_rename=True,
                )
                if validate_moved_destination is not None:
                    publication_authority.read_child(
                        backup.name,
                        path=backup,
                        label="previous destination",
                        expected_ownership=moved_destination_ownership,
                        callback=validate_moved_destination,
                    )
                    _require_tree_ownership_at(
                        publication_authority,
                        backup.name,
                        path=backup,
                        expected=moved_destination_ownership,
                        label="previous destination",
                        allow_root_rename=True,
                    )
            except BaseException as previous_error:  # noqa: B036 - safe rollback
                try:
                    _require_tree_ownership_at(
                        publication_authority,
                        backup.name,
                        path=backup,
                        expected=moved_destination_ownership,
                        label="previous destination",
                        allow_root_rename=True,
                    )
                except BaseException as identity_error:  # noqa: B036
                    raise _PreviousOutputIdentityLost(
                        "directory publication committed; previous output identity "
                        "lost; new output remains active and suspect previous root is "
                        f"at {backup}"
                    ) from identity_error
                quarantine = _quarantine_destination(
                    destination_display,
                    parent_authority=publication_authority,
                )
                restore_previous(
                    context=(
                        "previous destination validation failed and rollback failed"
                    )
                )
                raise RuntimeError(
                    "previous destination failed validation; newly published output "
                    f"was quarantined at {quarantine}"
                ) from previous_error

        previous_orphan: DirectoryOrphan | None = None
        if backup is not None:
            assert moved_destination_ownership is not None
            previous_orphan = _orphan_metadata(
                publication_authority,
                backup,
                moved_destination_ownership,
            )

        try:
            published_ownership = publication_authority.capture_child(
                destination_display.name,
                path=destination_display,
                label="published staged directory",
            )
        except (OSError, ValueError, RuntimeError) as exc:
            raise RuntimeError(
                "published staged directory changed during directory publication"
            ) from exc
        _require_matching_ownership(
            published_ownership,
            stage_ownership,
            label="published staged directory",
            allow_root_rename=True,
        )
        publication_authority.verify_path_binding()
        if commit_callback is not None:
            _fsync_publication_parent_for_commit(publication_authority)
            commit_callback(
                stage_ownership,
                published_ownership,
                previous_orphan,
                publication_token,
            )
        return previous_orphan

    return perform_publication()


def publish_staged_directory(
    stage: Path,
    destination: Path,
    *,
    expected_destination_identity: tuple[int, ...] | None | object = (
        _EXPECTED_DESTINATION_UNSET
    ),
    expected_stage_identity: tuple[int, ...] | None = None,
    expected_stage_root_ownership: _TreeOwnership | None = None,
    expected_destination_ownership: _TreeOwnership | None | object = (
        _EXPECTED_OWNERSHIP_UNSET
    ),
    validate_staged_directory: (
        Callable[[PublicationDirectoryReader], None] | None
    ) = None,
    validate_moved_destination: (
        Callable[[PublicationDirectoryReader], None] | None
    ) = None,
    validate_published_destination: (
        Callable[[PublicationDirectoryReader], None] | None
    ) = None,
    parent_descriptor: int | None = None,
    expected_parent_identity: tuple[int, ...] | None = None,
) -> DirectoryOrphan | None:
    """Publish a complete stage through one pinned parent directory authority.

    Every namespace mutation is an atomic no-replace rename relative to the
    pinned parent descriptor.  The previous tree is intentionally retained as
    a hidden, fully authenticated orphan for cooperative GC; online recursive
    deletion cannot be made safe against a hostile same-UID replacement race.

    The public compatibility wrapper owns the authority it opens.  The
    publication mechanics live in ``_publish_staged_directory_with_authority``
    so strict callers can retain a pre-opened authority across publication and
    synchronous receipt consumption.
    """

    stage_display = lexical_directory_path(stage)
    destination_display = lexical_directory_path(destination)
    if stage_display == destination_display:
        raise ValueError("staged and destination directories must differ")
    if stage_display.parent != destination_display.parent:
        raise ValueError("staged and destination directories must share a parent")

    authority_owner = _PublicationAuthorityOwner()
    primary_error: BaseException | None = None
    try:
        publication_authority = _open_publication_authority(
            stage_display.parent,
            parent_resource=parent_descriptor,
            expected_parent_identity=expected_parent_identity,
            authority_owner=authority_owner,
        )
        return _publish_staged_directory_with_authority(
            publication_authority,
            stage_display,
            destination_display,
            expected_destination_identity=expected_destination_identity,
            expected_stage_identity=expected_stage_identity,
            expected_stage_root_ownership=expected_stage_root_ownership,
            expected_destination_ownership=expected_destination_ownership,
            validate_staged_directory=validate_staged_directory,
            validate_moved_destination=validate_moved_destination,
            validate_published_destination=validate_published_destination,
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if primary_error is None:
            authority_owner.close()
        else:
            authority_owner.close_after_error(primary_error)


__all__ = [
    "DirectoryEntryPolicy",
    "DirectoryOrphan",
    "PublicationAuthenticatedFile",
    "PublicationDirectoryReader",
    "PublicationFileSnapshot",
    "TreeFileRecord",
    "capture_directory_ownership",
    "capture_directory_ownership_if_exists",
    "directory_ownership_digest",
    "directory_ownership_entry_identities",
    "directory_ownership_file_records",
    "directory_ownership_inventory",
    "directory_ownership_root_identity",
    "directory_ownership_root_version_identity",
    "discard_owned_directory",
    "lexical_directory_path",
    "publication_parent_identity",
    "project_directory_ownership_subtree",
    "publish_staged_directory",
    "reopen_authenticated_directory",
    "require_publishable_directory_ownership",
    "retry_retained_publication_cleanup",
]
