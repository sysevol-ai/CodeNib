# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rollback-safe publication of fully staged directory trees."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import secrets
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Callable, ContextManager, Iterator, Literal, Protocol, TypeVar

from . import _windows_fs_authority as _windows_fs

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
_DURABLE_PUBLICATION_FSYNC_BACKENDS = frozenset({"linux-renameat2"})
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

    @property
    def record(self) -> TreeFileRecord:
        if self._record is None:
            raise RuntimeError(
                "publication file record is available after context verification"
            )
        return self._record

    def read(self, size: int = -1) -> bytes:
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
        if self._closed:
            return
        try:
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
        finally:
            self._closed = True


class PublicationDirectoryReader:
    """A child tree that is usable only during an authority-owned callback."""

    __slots__ = (
        "_diagnostic_path",
        "root_identity",
        "_capture",
        "_open_file",
        "_expected_ownership",
        "_records_by_path",
        "_entries_by_path",
        "_authentication_failed",
        "_active",
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
    ) -> None:
        self._diagnostic_path = display_path
        self.root_identity = root_identity
        self._capture = capture
        self._open_file = open_file
        self._expected_ownership = expected_ownership
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
        self._authentication_failed = False
        self._active = True
        if (
            expected_ownership is not None
            and root_identity != expected_ownership.root_identity
        ):
            self._authentication_failed = True
            raise RuntimeError(
                "publication callback root differs from captured ownership"
            )

    def capture_ownership(
        self,
        *,
        required_root_file: str | None = None,
        allow_empty_root: bool = False,
        entry_policy: DirectoryEntryPolicy | None = None,
    ) -> _TreeOwnership:
        """Capture through the already-open directory authority."""

        if not self._active:
            raise RuntimeError("publication tree reader is no longer active")
        try:
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
            self._authentication_failed = True
            raise

    def inventory(self) -> tuple[tuple[str, str], ...]:
        if not self._active:
            raise RuntimeError("publication tree reader is no longer active")
        if self._expected_ownership is not None:
            return self._expected_ownership.inventory
        return self.capture_ownership().inventory

    def file_records(self) -> tuple[TreeFileRecord, ...]:
        if not self._active:
            raise RuntimeError("publication tree reader is no longer active")
        if self._expected_ownership is not None:
            return self._expected_ownership.file_records
        return self.capture_ownership().file_records

    def authenticated_snapshot(
        self,
        relative: str | PurePosixPath,
        *,
        max_bytes: int,
    ) -> PublicationFileSnapshot:
        if not self._active:
            raise RuntimeError("publication tree reader is no longer active")
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

    @contextmanager
    def open_authenticated_file(
        self,
        relative: str | PurePosixPath,
        *,
        max_bytes: int,
    ) -> Iterator[PublicationAuthenticatedFile]:
        if not self._active:
            raise RuntimeError("publication tree reader is no longer active")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise TypeError("publication stream limit must be an integer")
        if max_bytes < 0 or max_bytes > _MAX_OWNERSHIP_BYTES:
            raise ValueError("publication stream limit is out of bounds")
        normalized = _publication_relative_path(relative)
        expected = self._expected_file(normalized)
        try:
            with self._open_file(normalized, max_bytes, expected) as authenticated:
                if (
                    authenticated.path,
                    authenticated.mode,
                    authenticated.size,
                ) != (
                    expected.record.path,
                    expected.record.mode,
                    expected.record.size,
                ):
                    raise RuntimeError(
                        "publication file metadata differs from captured ownership"
                    )
                yield authenticated
            if authenticated.record != expected.record:
                raise RuntimeError(
                    "publication file bytes differ from captured ownership"
                )
        except BaseException:
            self._authentication_failed = True
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
    ) -> _ExpectedPublicationFile:
        ownership = self._expected_ownership
        if ownership is None:
            self._authentication_failed = True
            raise RuntimeError(
                "publication file reads require captured callback ownership"
            )
        normalized = relative.as_posix()
        try:
            record = self._records_by_path[normalized]
            kind, file_identity = self._entries_by_path[normalized]
        except KeyError as exc:
            self._authentication_failed = True
            raise ValueError(
                f"publication file is absent from captured ownership: {normalized}"
            ) from exc
        if kind != "file":
            self._authentication_failed = True
            raise ValueError(f"publication path is not a captured file: {normalized}")
        directories: list[tuple[str, tuple[int, ...]]] = []
        for depth in range(1, len(relative.parts)):
            directory = "/".join(relative.parts[:depth])
            try:
                directory_kind, identity = self._entries_by_path[directory]
            except KeyError as exc:
                self._authentication_failed = True
                raise RuntimeError(
                    f"publication directory is absent from ownership: {directory}"
                ) from exc
            if directory_kind != "directory":
                self._authentication_failed = True
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
        self._active = False


_PublicationTreeReader = PublicationDirectoryReader


def _annotate_secondary_error(
    primary_error: BaseException,
    label: str,
    secondary_error: BaseException,
) -> None:
    """Best-effort diagnostics that can never replace the primary error."""

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
class _OrderedActionState:
    actions: tuple[tuple[str, Callable[[], None]], ...]
    iteration_failure_label: str
    primary_error: BaseException | None
    next_index: int = 0

    def retain(self, label: str, error: BaseException) -> None:
        if self.primary_error is None:
            self.primary_error = error
            return
        _annotate_secondary_error(self.primary_error, label, error)


def _run_ordered_actions(state: _OrderedActionState) -> None:
    """Run claimed actions in order and resume after a loop-edge interruption.

    The outer exception region includes the normal loop back-edge and the
    back-edge after an action failure is retained.  A transition interruption
    therefore becomes another first-primary candidate instead of escaping and
    skipping all remaining actions.  Claiming before invocation keeps cleanup
    actions at-most-once when a close succeeds before raising.
    """

    try:
        for index in range(state.next_index, len(state.actions)):
            label, action = state.actions[index]
            state.next_index = index + 1
            try:
                action()
            except BaseException as action_error:  # noqa: B036 - keep first
                state.retain(label, action_error)
    except BaseException as iteration_error:  # noqa: B036 - resume remaining
        state.retain(state.iteration_failure_label, iteration_error)
        if state.next_index < len(state.actions):
            _run_ordered_actions(state)


@contextmanager
def _run_context_with_cleanup_actions(
    cleanup_actions: Callable[
        [],
        tuple[tuple[str, Callable[[], None]], ...],
    ],
) -> Iterator[None]:
    """Run every cleanup action without replacing a context-body primary."""

    if not callable(cleanup_actions):
        raise TypeError("context cleanup actions must be callable")
    # An outer ``except`` leaves an ambient value in ``sys.exc_info()``. Track
    # it separately so cleanup-only failure still propagates from a successful
    # context body entered while that unrelated exception is being handled.
    ambient_error = sys.exc_info()[1]
    context_error: BaseException | None = None
    try:
        yield
    except BaseException as error:  # noqa: B036 - preserve exact local primary
        context_error = error
        raise
    finally:
        active_error = sys.exc_info()[1]
        locally_unwinding = context_error is not None or (
            active_error is not None and active_error is not ambient_error
        )
        primary_error = context_error
        if primary_error is None and locally_unwinding:
            primary_error = active_error
        failures = _OrderedActionState(
            actions=(),
            iteration_failure_label=(
                "publication authenticated cleanup iteration also failed"
            ),
            primary_error=primary_error,
        )
        try:
            failures.actions = cleanup_actions()
        except BaseException as action_error:  # noqa: B036 - keep first primary
            failures.retain(
                "publication authenticated cleanup planning also failed",
                action_error,
            )
        else:
            _run_ordered_actions(failures)
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
        active_error = sys.exc_info()[1]
        locally_unwinding = callback_error is not None or (
            active_error is not None and active_error is not ambient_error
        )
        primary_error = callback_error
        if primary_error is None and locally_unwinding:
            primary_error = active_error
        failures = _OrderedActionState(
            actions=post_validations,
            iteration_failure_label=(
                "publication callback post-validation iteration also failed"
            ),
            primary_error=primary_error,
        )
        _run_ordered_actions(failures)
        if not locally_unwinding and failures.primary_error is not None:
            raise failures.primary_error


def _run_publication_reader_callback(
    reader: PublicationDirectoryReader,
    callback: Callable[[PublicationDirectoryReader], _T],
    validate_child_binding: Callable[[], None],
) -> _T:
    """Run a reader callback and always recheck validity plus child binding."""

    return _run_callback_with_post_validations(
        lambda: callback(reader),
        (
            (
                "publication reader validity validation also failed",
                reader._require_valid,
            ),
            (
                "publication reader child namespace validation also failed",
                validate_child_binding,
            ),
        ),
    )


@dataclass(slots=True)
class _PosixDescriptorRecord:
    descriptor: int
    identity: tuple[int, ...] | None = None


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
        descriptor = -1
        record: _PosixDescriptorRecord | None = None
        try:
            if dir_fd is None:
                descriptor = os.open(path, flags, mode)
            else:
                descriptor = os.open(path, flags, mode, dir_fd=dir_fd)
            record = _PosixDescriptorRecord(descriptor)
            self._records.append(record)
            record.identity = _resource_owner_identity(os.fstat(descriptor))
            return descriptor
        except BaseException as primary_error:
            if descriptor >= 0:
                if record is None:
                    record = _PosixDescriptorRecord(descriptor)
                if not any(candidate is record for candidate in self._records):
                    try:
                        self._records.append(record)
                    except BaseException as registration_error:  # noqa: B036
                        _annotate_secondary_error(
                            primary_error,
                            "descriptor ownership registration also failed",
                            registration_error,
                        )
                        close_error = self._close_record(record)
                        if close_error is not None:
                            _annotate_secondary_error(
                                primary_error,
                                "unregistered descriptor cleanup also failed",
                                close_error,
                            )
                        raise primary_error
                self.close_record_after_error(record, primary_error)
            raise

    def duplicate(self, descriptor: int) -> int:
        duplicate = -1
        record: _PosixDescriptorRecord | None = None
        try:
            duplicate = os.dup(descriptor)
            record = _PosixDescriptorRecord(duplicate)
            self._records.append(record)
            record.identity = _resource_owner_identity(os.fstat(duplicate))
            return duplicate
        except BaseException as primary_error:
            if duplicate >= 0:
                if record is None:
                    record = _PosixDescriptorRecord(duplicate)
                if not any(candidate is record for candidate in self._records):
                    try:
                        self._records.append(record)
                    except BaseException as registration_error:  # noqa: B036
                        _annotate_secondary_error(
                            primary_error,
                            "descriptor ownership registration also failed",
                            registration_error,
                        )
                        close_error = self._close_record(record)
                        if close_error is not None:
                            _annotate_secondary_error(
                                primary_error,
                                "unregistered descriptor cleanup also failed",
                                close_error,
                            )
                        raise primary_error
                self.close_record_after_error(record, primary_error)
            raise

    def close_descriptor(self, descriptor: int) -> None:
        record = self._record_for(descriptor)
        if record is None:
            return
        close_error = self._close_record(record)
        if close_error is not None:
            raise close_error

    def close_record_after_error(
        self,
        record: _PosixDescriptorRecord,
        primary_error: BaseException,
    ) -> None:
        close_error = self._close_record(record)
        if close_error is not None:
            _annotate_secondary_error(
                primary_error,
                "descriptor cleanup also failed",
                close_error,
            )

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

    def close_all_after_error(self, primary_error: BaseException) -> None:
        try:
            self.close_all()
        except BaseException as close_error:  # noqa: B036 - keep primary
            _annotate_secondary_error(
                primary_error,
                "publication authority cleanup also failed",
                close_error,
            )

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
        handle = 0
        record: _WindowsHandleRecord | None = None
        try:
            handle = callback()
            record = _WindowsHandleRecord(handle)
            self._records.append(record)
            metadata = self._api.metadata(handle)
            record.identity = _resource_owner_identity(metadata)
            return handle
        except BaseException as primary_error:
            if handle:
                if record is None:
                    record = _WindowsHandleRecord(handle)
                if not any(candidate is record for candidate in self._records):
                    try:
                        self._records.append(record)
                    except BaseException as registration_error:  # noqa: B036
                        _annotate_secondary_error(
                            primary_error,
                            "HANDLE ownership registration also failed",
                            registration_error,
                        )
                        close_error = self._close_record(record)
                        if close_error is not None:
                            _annotate_secondary_error(
                                primary_error,
                                "unregistered HANDLE cleanup also failed",
                                close_error,
                            )
                        raise primary_error
                self.close_record_after_error(record, primary_error)
            raise

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
        close_error = self._close_record(record)
        if close_error is not None:
            raise close_error

    def close_record_after_error(
        self,
        record: _WindowsHandleRecord,
        primary_error: BaseException,
    ) -> None:
        close_error = self._close_record(record)
        if close_error is not None:
            _annotate_secondary_error(
                primary_error,
                "Windows HANDLE cleanup also failed",
                close_error,
            )

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

    def close_all_after_error(self, primary_error: BaseException) -> None:
        try:
            self.close_all()
        except BaseException as close_error:  # noqa: B036 - keep primary
            _annotate_secondary_error(
                primary_error,
                "Windows authority cleanup also failed",
                close_error,
            )

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


class _PublicationAuthorityOwner:
    """Pre-existing slot for cancellation-safe authority handoff."""

    __slots__ = ("_authority",)

    def __init__(self) -> None:
        self._authority: _PublicationAuthority | None = None

    @property
    def authority(self) -> _PublicationAuthority | None:
        return self._authority

    def install(self, authority: _PublicationAuthority) -> None:
        if self._authority is not None:
            raise RuntimeError("publication authority owner is already active")
        self._authority = authority

    def close(self) -> None:
        authority = self._authority
        if authority is None:
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
                raise
            _annotate_secondary_error(
                primary_error,
                "publication authority owner reconciliation also failed",
                reconciliation_error,
            )
        else:
            if close_complete:
                self._authority = None
        if primary_error is not None:
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
        "_verify_callback",
        "_close_complete_callback",
        "_close_state",
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
        rename_callback: Callable[[str, str], None],
        verify_callback: Callable[[], None],
        close_complete_callback: Callable[[], bool],
    ) -> None:
        self.display_parent = display_parent
        self.identity = identity
        self.backend_tag = backend_tag
        self._resource = resource
        self._close_callback = close_callback
        self._metadata_callback = metadata_callback
        self._reader_callback = reader_callback
        self._rename_callback = rename_callback
        self._verify_callback = verify_callback
        self._close_complete_callback = close_complete_callback
        self._close_state = False

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
            ),
        )

    def rename_noreplace(self, source: str, destination: str) -> None:
        if self._closed:
            raise RuntimeError("publication authority is closed")
        self._rename_callback(
            _simple_child_name(source, label="rename source"),
            _simple_child_name(destination, label="rename destination"),
        )

    def verify_path_binding(self) -> None:
        if self._closed:
            raise RuntimeError("publication authority is closed")
        self._verify_callback()

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
            reader: _PublicationTreeReader | None = None
            primary_error: BaseException | None = None
            try:
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
                    lambda required_root_file, allow_empty_root, entry_policy: (
                        _capture_posix_directory_descriptor(
                            child_descriptor,
                            display_path,
                            required_root_file=required_root_file,
                            allow_empty_root=allow_empty_root,
                            entry_policy=entry_policy,
                        )
                    ),
                    lambda relative, max_bytes, expected: _open_posix_authenticated_file(
                        child_descriptor,
                        display_path,
                        relative,
                        max_bytes=max_bytes,
                        expected=expected,
                    ),
                    expected_ownership,
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
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                if reader is not None:
                    reader._deactivate()
                try:
                    resources.close_descriptor(child_descriptor)
                except BaseException as close_error:  # noqa: B036
                    if primary_error is None:
                        raise
                    _annotate_secondary_error(
                        primary_error,
                        "publication child descriptor cleanup also failed",
                        close_error,
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
        or len(raw.encode("utf-8", errors="strict")) > _MAX_OWNERSHIP_PATH_BYTES
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
) -> _WindowsDirectoryEntry | None:
    folded = name.casefold()
    matches = [
        entry
        for entry in api.enumerate_directory(parent_handle)
        if entry.name.casefold() == folded
    ]
    if len(matches) > 1:
        raise RuntimeError("Windows directory contains ambiguous child names")
    return matches[0] if matches else None


def _windows_open_child_by_id(
    api: _WindowsKernelApi,
    parent_handle: int,
    entry: _WindowsDirectoryEntry,
    *,
    desired_access: int,
    expected_directory: bool | None,
    resource_owner: _WindowsResourceOwner | None = None,
) -> tuple[int, _WindowsHandleMetadata]:
    if not entry.file_id:
        raise RuntimeError("Windows directory entry has no reliable FILE_ID")
    if entry.attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
        raise RuntimeError("Windows publication refuses reparse-point content")
    is_directory = bool(entry.attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY)
    if expected_directory is not None and is_directory != expected_directory:
        raise ValueError("Windows publication child has the wrong object type")
    selected_owner = (
        _WindowsResourceOwner(api) if resource_owner is None else resource_owner
    )
    handle = selected_owner.acquire(
        lambda: api.open_by_id(
            parent_handle,
            entry.file_id,
            desired_access=desired_access,
            is_directory=is_directory,
        )
    )
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
        selected_owner.bind_identity(handle, metadata)
        if resource_owner is None:
            selected_owner.release(handle)
        return handle, metadata
    except BaseException as primary_error:
        try:
            selected_owner.close_handle(handle)
        except BaseException as close_error:  # noqa: B036 - keep primary
            _annotate_secondary_error(
                primary_error,
                "Windows child HANDLE cleanup also failed",
                close_error,
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
) -> tuple[int, str, tuple[int, ...]]:
    handle, opened = _windows_open_child_by_id(
        api,
        parent_handle,
        entry,
        desired_access=_WINDOWS_FILE_READ_DATA
        | _WINDOWS_FILE_READ_ATTRIBUTES
        | _WINDOWS_SYNCHRONIZE,
        expected_directory=False,
    )
    try:
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
        rebound = _windows_find_child(api, parent_handle, entry.name)
        if rebound is None or rebound.file_id != entry.file_id:
            raise RuntimeError(f"directory ownership file changed: {path}")
    finally:
        api.close(handle)
    budget.byte_count += size
    return size, digest.hexdigest(), _ownership_version_identity(after)


@contextmanager
def _open_windows_authenticated_file(
    api: _WindowsKernelApi,
    root_handle: int,
    root_path: Path,
    relative: PurePosixPath,
    *,
    max_bytes: int,
    expected: _ExpectedPublicationFile,
) -> Iterator[PublicationAuthenticatedFile]:
    root_before = api.metadata(root_handle)
    parent_handle = root_handle
    opened_directories: list[int] = []
    bindings: list[tuple[int, str, int, int, tuple[int, ...]]] = []
    file_handle = 0
    authenticated: PublicationAuthenticatedFile | None = None
    expected_directories = dict(expected.directory_identities)
    traversed: list[str] = []

    def cleanup_actions() -> tuple[tuple[str, Callable[[], None]], ...]:
        actions: list[tuple[str, Callable[[], None]]] = []
        if authenticated is not None:
            actions.append(
                (
                    "publication authenticated file finalization also failed",
                    lambda: authenticated._finalize(),
                )
            )
        if file_handle:
            actions.append(
                (
                    "publication authenticated file HANDLE cleanup also failed",
                    lambda: api.close(file_handle),
                )
            )
        actions.extend(
            (
                "publication authenticated directory HANDLE cleanup also failed",
                lambda handle=handle: api.close(handle),
            )
            for handle in reversed(opened_directories)
        )
        return tuple(actions)

    with _run_context_with_cleanup_actions(cleanup_actions):
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
            )
            if opened.st_dev != root_before.st_dev:
                api.close(child_handle)
                raise RuntimeError("publication stream crosses a volume")
            if expected_directories.get(
                relative_directory
            ) != _ownership_version_identity(opened):
                api.close(child_handle)
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
            opened_directories.append(child_handle)
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
        yield authenticated


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
) -> bytes:
    if depth > _MAX_SAFE_REMOVAL_DEPTH:
        raise RuntimeError("directory ownership scan exceeds its depth limit")
    before = api.metadata(handle)
    if not stat.S_ISDIR(before.st_mode) or before.st_dev != root_device:
        raise RuntimeError(f"directory ownership root changed: {path}")
    entries: list[tuple[bytes, _WindowsDirectoryEntry]] = []
    for entry in api.enumerate_directory(handle):
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
    entries.sort(key=lambda item: item[0])
    if (
        required_root_file is not None
        and not (allow_empty_root and not entries)
        and not any(entry.name == required_root_file for _raw, entry in entries)
    ):
        raise RuntimeError("directory ownership root is missing its required marker")

    hasher = hashlib.sha256()
    hasher.update(b"codenib.atomic-directory.v1\x00")
    hasher.update(stat.S_IMODE(before.st_mode).to_bytes(4, "big"))
    for raw_name, entry in entries:
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
            )
            try:
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
                )
                after = api.metadata(child_handle)
                if _ownership_version_identity(after) != _ownership_version_identity(
                    opened
                ):
                    raise RuntimeError(
                        f"directory ownership directory changed: {child_path}"
                    )
            finally:
                api.close(child_handle)
            rebound = _windows_find_child(api, handle, entry.name)
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
    required_root_file: str | None,
    allow_empty_root: bool,
    entry_policy: DirectoryEntryPolicy | None,
) -> _TreeOwnership:
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
    )
    after = api.metadata(handle)
    if _ownership_version_identity(after) != _ownership_version_identity(opened):
        raise RuntimeError(f"directory ownership root changed: {path}")
    return _TreeOwnership(
        root_identity=_directory_inode_identity(opened),
        root_version_identity=_ownership_version_identity(opened),
        digest=digest.hex(),
        entries=budget.entries,
        byte_count=budget.byte_count,
        metadata_bytes=budget.metadata_bytes,
        inventory=tuple(sorted(inventory)),
        file_records=tuple(sorted(file_records, key=lambda record: record.path)),
        entry_identities=tuple(sorted(entry_identities)),
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
    authority: _PublicationAuthority | None = None
    try:
        parent_handle = resources.acquire(
            lambda: (
                api.create_directory_handle(path)
                if parent_resource is None
                else api.duplicate_handle(parent_resource)
            )
        )
        opened = api.metadata(parent_handle)
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
                    resources.close_handle(handle)
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
            if metadata.st_dev != opened.st_dev:
                resources.close_handle(handle)
                raise RuntimeError("publication child crosses a volume")
            reader: _PublicationTreeReader | None = None
            primary_error: BaseException | None = None
            try:
                reader = _PublicationTreeReader(
                    display_path,
                    _directory_inode_identity(metadata),
                    lambda required_root_file, allow_empty_root, entry_policy: (
                        _capture_windows_directory_handle(
                            api,
                            handle,
                            display_path,
                            required_root_file=required_root_file,
                            allow_empty_root=allow_empty_root,
                            entry_policy=entry_policy,
                        )
                    ),
                    lambda relative, max_bytes, expected: (
                        _open_windows_authenticated_file(
                            api,
                            handle,
                            display_path,
                            relative,
                            max_bytes=max_bytes,
                            expected=expected,
                        )
                    ),
                    expected_ownership,
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
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                if reader is not None:
                    reader._deactivate()
                try:
                    resources.close_handle(handle)
                except BaseException as close_error:  # noqa: B036
                    if primary_error is None:
                        raise
                    _annotate_secondary_error(
                        primary_error,
                        "Windows reader HANDLE cleanup also failed",
                        close_error,
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
                    resources.close_handle(source_handle)
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
            rebound_handle = resources.acquire(
                lambda: api.create_directory_handle(path)
            )
            primary_error: BaseException | None = None
            try:
                if _directory_inode_identity(api.metadata(rebound_handle)) != identity:
                    raise RuntimeError("publication parent path changed")
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                try:
                    resources.close_handle(rebound_handle)
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
            close_callback=lambda _resource: resources.close_all(),
            metadata_callback=metadata_callback,
            reader_callback=reader_callback,
            rename_callback=rename_callback,
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
    """Detect mutation between observations made through one open descriptor."""

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
    max_bytes: int,
    expected: _ExpectedPublicationFile,
) -> Iterator[PublicationAuthenticatedFile]:
    root_before = os.fstat(root_descriptor)
    root_device = root_before.st_dev
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directory_flags = flags | os.O_DIRECTORY | getattr(os, "O_NONBLOCK", 0)
    file_flags = flags | getattr(os, "O_NONBLOCK", 0)
    descriptors: list[int] = [os.dup(root_descriptor)]
    bindings: list[tuple[int, str, int, tuple[int, ...]]] = []
    file_descriptor = -1
    authenticated: PublicationAuthenticatedFile | None = None
    expected_directories = dict(expected.directory_identities)
    traversed: list[str] = []

    def cleanup_actions() -> tuple[tuple[str, Callable[[], None]], ...]:
        actions: list[tuple[str, Callable[[], None]]] = []
        if authenticated is not None:
            actions.append(
                (
                    "publication authenticated file finalization also failed",
                    lambda: authenticated._finalize(),
                )
            )
        if file_descriptor >= 0:
            actions.append(
                (
                    "publication authenticated file descriptor cleanup also failed",
                    lambda: os.close(file_descriptor),
                )
            )
        actions.extend(
            (
                "publication authenticated directory descriptor cleanup also failed",
                lambda descriptor=descriptor: os.close(descriptor),
            )
            for descriptor in reversed(descriptors)
        )
        return tuple(actions)

    with _run_context_with_cleanup_actions(cleanup_actions):
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
            child = os.open(part, directory_flags, dir_fd=parent)
            descriptors.append(child)
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
        file_descriptor = os.open(name, file_flags, dir_fd=parent)
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
        yield authenticated


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


def _hash_owned_regular_file(
    parent_descriptor: int,
    name: str,
    path: Path,
    metadata: os.stat_result,
    *,
    root_device: int,
    budget: _OwnershipBudget,
    relative: str,
    entry_policy: DirectoryEntryPolicy | None,
) -> tuple[int, str, tuple[int, ...]]:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
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
    finally:
        os.close(descriptor)
    budget.byte_count += size
    return size, digest.hexdigest(), _ownership_version_identity(after)


def _scan_owned_directory(
    descriptor: int,
    path: Path,
    parts: tuple[bytes, ...],
    *,
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
) -> bytes:
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
    scan_descriptor = os.open(".", flags, dir_fd=descriptor)
    names: list[tuple[bytes, str]] = []
    try:
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
    finally:
        os.close(scan_descriptor)
    names.sort(key=lambda item: item[0])
    if (
        required_root_file is not None
        and not (allow_empty_root and not names)
        and not any(raw_name == required_root_file for raw_name, _name in names)
    ):
        raise RuntimeError("directory ownership root is missing its required marker")

    hasher = hashlib.sha256()
    hasher.update(b"codenib.atomic-directory.v1\x00")
    hasher.update(stat.S_IMODE(before.st_mode).to_bytes(4, "big"))
    for raw_name, name in names:
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
            child_descriptor = os.open(name, flags, dir_fd=descriptor)
            try:
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
                    root_device=root_device,
                    mount_points=mount_points,
                    budget=budget,
                    inventory=inventory,
                    file_records=file_records,
                    entry_identities=entry_identities,
                    entry_policy=entry_policy,
                    depth=depth + 1,
                )
                after = os.fstat(child_descriptor)
                if _ownership_version_identity(after) != _ownership_version_identity(
                    opened
                ):
                    raise RuntimeError(
                        f"directory ownership directory changed: {child_path}"
                    )
            finally:
                os.close(child_descriptor)
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
                root_device=root_device,
                budget=budget,
                relative=os.fsdecode(relative),
                entry_policy=entry_policy,
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
    required_root_file: str | None,
    allow_empty_root: bool,
    entry_policy: DirectoryEntryPolicy | None,
) -> _TreeOwnership:
    """Capture one already-open POSIX directory without reacquiring its path."""

    required_root_file_bytes = _required_root_file_bytes(required_root_file)
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
    )
    after = os.fstat(descriptor)
    if _ownership_version_identity(after) != _ownership_version_identity(opened):
        raise RuntimeError(f"directory ownership root changed: {path}")
    return _TreeOwnership(
        root_identity=_directory_inode_identity(opened),
        root_version_identity=_ownership_version_identity(opened),
        digest=digest.hex(),
        entries=budget.entries,
        byte_count=budget.byte_count,
        metadata_bytes=budget.metadata_bytes,
        inventory=tuple(sorted(inventory)),
        file_records=tuple(sorted(file_records, key=lambda record: record.path)),
        entry_identities=tuple(sorted(entry_identities)),
    )


def capture_directory_ownership(
    path: Path,
    *,
    required_root_file: str | None = None,
    allow_empty_root: bool = False,
    entry_policy: DirectoryEntryPolicy | None = None,
) -> _TreeOwnership:
    """Return a bounded token through a pinned parent/source authority."""

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
        )


def capture_directory_ownership_if_exists(
    path: Path,
    *,
    required_root_file: str | None = None,
    allow_empty_root: bool = False,
    entry_policy: DirectoryEntryPolicy | None = None,
) -> _TreeOwnership | None:
    """Capture one existing tree or atomically observe that its child is absent.

    The missing observation and any capture use one pinned parent authority.
    A child that appears after a missing observation is not opened or blessed;
    callers can pass the returned ``None`` into a later no-replace operation.
    """

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
            return None
        initial_root_identity = _directory_inode_identity(metadata)
        ownership = authority.capture_child(
            lexical.name,
            path=lexical,
            label="directory ownership root",
            required_root_file=required_root_file,
            allow_empty_root=allow_empty_root,
            entry_policy=entry_policy,
        )
        if ownership.root_identity != initial_root_identity:
            raise RuntimeError("directory ownership root changed while it was captured")
        authority.verify_path_binding()
        return ownership


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
) -> None:
    try:
        observed = authority.capture_child(
            name,
            path=path,
            label=label,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise RuntimeError(f"{label} changed during directory publication") from exc
    _require_matching_ownership(
        observed,
        expected,
        label=label,
        allow_root_rename=allow_root_rename,
    )


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
) -> _T:
    """Run one authenticated callback inside an exact ownership sandwich."""

    def consume() -> _T:
        before = reader.capture_ownership()
        if before != expected_ownership:
            raise RuntimeError(
                "authenticated directory differs from expected ownership"
            )
        return callback(reader)

    def validate_after_ownership() -> None:
        after = reader.capture_ownership()
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

    os.fsync(publication_authority.resource)


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
        Callable[[_TreeOwnership, _TreeOwnership, DirectoryOrphan | None], None] | None
    ) = None,
) -> DirectoryOrphan | None:
    """Publish a complete stage through a caller-owned parent authority.

    Every namespace mutation is an atomic no-replace rename relative to the
    pinned parent descriptor.  The previous tree is intentionally retained as
    a hidden, fully authenticated orphan for cooperative GC; online recursive
    deletion cannot be made safe against a hostile same-UID replacement race.

    This helper borrows ``publication_authority``.  It never acquires, closes,
    or transfers that authority.  ``commit_callback`` receives the sealed
    pre-rename token, a fresh post-rename token, and the previous orphan.  It is
    the final fallible action after publication is fully authenticated and the
    supported parent namespace has been durably synchronized; once the callback
    starts, its failures propagate without rolling the publication back.

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
    if commit_callback is not None:
        if not callable(commit_callback):
            raise TypeError("directory commit callback must be callable")
        _require_durable_publication_commit_authority(publication_authority)

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

        stage_ownership = publication_authority.capture_child(
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
        if validate_staged_directory is not None:
            publication_authority.read_child(
                stage_display.name,
                path=stage_display,
                label="staged directory",
                expected_ownership=stage_ownership,
                callback=validate_staged_directory,
            )
            _require_tree_ownership_at(
                publication_authority,
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
        observed_destination_ownership = (
            None
            if destination_metadata is None
            else publication_authority.capture_child(
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

        destination_was_missing = destination_metadata is None
        _require_tree_ownership_at(
            publication_authority,
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
            _require_tree_ownership_at(
                publication_authority,
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
                _require_tree_ownership_at(
                    publication_authority,
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
            publication_authority.rename_noreplace(
                stage_display.name,
                destination_display.name,
            )
        except BaseException:
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
            commit_callback(stage_ownership, published_ownership, previous_orphan)
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
    "publish_staged_directory",
    "reopen_authenticated_directory",
    "require_publishable_directory_ownership",
]
