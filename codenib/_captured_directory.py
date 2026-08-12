# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Descriptor-bound reads and writes for already-captured directory trees."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Iterator

from . import _windows_fs_authority as _windows_fs
from ._atomic_directory import (
    TreeFileRecord,
    capture_directory_ownership,
    directory_ownership_entry_identities,
    directory_ownership_file_records,
    directory_ownership_root_identity,
    directory_ownership_root_version_identity,
    discard_owned_directory,
    lexical_directory_path,
    publish_staged_directory,
)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_MAX_RELATIVE_PATH_BYTES = 4_096
_MAX_COMPONENTS = 256
_COPY_BYTES = 1024 * 1024


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
        or len(os.fsencode(raw)) > _MAX_RELATIVE_PATH_BYTES
    ):
        raise ValueError("captured directory path must be normalized and bounded")
    return relative


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

    def rewind_authenticated(self) -> int:
        """Authenticate the bytes, rewind the same fd/HANDLE, and return it."""

        self.authenticate()
        try:
            self._rewind_callback()
        except OSError as exc:
            raise ValueError(
                f"captured file cannot be rewound: {self.record.path}"
            ) from exc
        return self.descriptor

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
    def authenticated_descriptor(
        self,
        relative: str | Path | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ) -> Iterator[tuple[int, TreeFileRecord]]:
        if self._windows_authority is not None:
            raise RuntimeError(
                "captured authenticated descriptors are available only on POSIX"
            )
        source = self.open_file(relative, max_bytes=max_bytes)
        try:
            descriptor = source.rewind_authenticated()
            yield descriptor, source.record
            source.verify_unchanged()
        finally:
            source.close()

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


class OwnedDirectoryStage:
    """Build one new private sibling tree without path-based file mutation."""

    def __init__(self, destination: Path) -> None:
        self.destination = lexical_directory_path(destination)
        self.path = lexical_directory_path(
            Path(
                tempfile.mkdtemp(
                    prefix=f".{self.destination.name}.normalize-",
                    dir=str(self.destination.parent),
                )
            )
        )
        self._initial = capture_directory_ownership(self.path)
        self._cleanup_ownership = self._initial
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        self._descriptor = os.open(self.path, flags)
        self._published = False

    def close(self) -> None:
        if self._descriptor >= 0:
            try:
                os.close(self._descriptor)
            except OSError:
                pass
            self._descriptor = -1

    def discard(self) -> None:
        if not self._published:
            try:
                self._cleanup_ownership = self._capture_current_ownership()
            except (OSError, RuntimeError, ValueError):
                pass
        self.close()
        if not self._published:
            discard_owned_directory(self.path, self._cleanup_ownership)

    def _capture_current_ownership(self):
        observed = capture_directory_ownership(self.path)
        if directory_ownership_root_identity(observed) != (
            directory_ownership_root_identity(self._initial)
        ):
            raise RuntimeError("owned stage root changed")
        return observed

    def _parent_descriptor(self, relative: PurePosixPath) -> int:
        descriptor = os.dup(self._descriptor)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            for part in relative.parts[:-1]:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = -1
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                    opened = os.fstat(child)
                    if not stat.S_ISDIR(opened.st_mode):
                        raise ValueError(
                            f"owned stage component is not a directory: {relative}"
                        )
                except BaseException:
                    if child >= 0:
                        os.close(child)
                    raise
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
        parent = self._parent_descriptor(normalized)
        temporary = f".{normalized.name}.tmp-{secrets.token_hex(12)}"
        descriptor = -1
        renamed = False
        byte_count = 0
        iterator = iter(chunks)
        try:
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
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("could not write owned stage file")
                    view = view[written:]
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            try:
                os.stat(
                    normalized.name,
                    dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise ValueError(f"owned stage file already exists: {normalized}")
            os.rename(
                temporary,
                normalized.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
            )
            renamed = True
            os.fsync(parent)
        finally:
            close_iterator = getattr(iterator, "close", None)
            if callable(close_iterator):
                close_iterator()
            if descriptor >= 0:
                os.close(descriptor)
            if not renamed:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except OSError:
                    pass
            os.close(parent)

    def publish(
        self,
        *,
        expected_destination_ownership: object,
        validate_staged_directory=None,
        validate_published_destination=None,
    ) -> None:
        self._cleanup_ownership = self._capture_current_ownership()
        self.close()
        publish_staged_directory(
            self.path,
            self.destination,
            expected_stage_root_ownership=self._cleanup_ownership,
            expected_destination_ownership=expected_destination_ownership,
            validate_staged_directory=validate_staged_directory,
            validate_published_destination=validate_published_destination,
        )
        self._published = True


__all__ = [
    "AuthenticatedFile",
    "CapturedDirectoryReader",
    "OwnedDirectoryStage",
]
