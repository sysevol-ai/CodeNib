# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded, traversal-safe extraction for context artifact archives."""

from __future__ import annotations

import io
import os
import stat
import struct
import sys
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

from .. import _windows_fs_authority as _windows_fs
from .._atomic_directory import (
    PublicationDirectoryReader,
    _annotate_secondary_error,
    lexical_directory_path,
)
from .._captured_directory import (
    OwnedDirectoryStage,
    _create_sealable_memfd,
    _seal_snapshot_descriptor,
    _SnapshotUnavailable,
    _write_all,
)
from .._contained_source import _PosixDescriptorCleanup
from .context import CONTEXT_ARTIFACT_MANIFEST
from .runtime import VerifiedContextArtifact, verify_context_artifact_reader

DEFAULT_MAX_ARCHIVE_FILES = 100_000
DEFAULT_MAX_EXPANDED_BYTES = 64 * 1024 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_ARCHIVE_PATH_BYTES = 4_096
_MAX_ARCHIVE_PATH_COMPONENTS = 256
_MAX_ARCHIVE_ENVELOPE_BYTES = 16 * 1024 * 1024
_MAX_ARCHIVE_MEMBER_OVERHEAD = 4_096
_MAX_ZIP_COMMENT_BYTES = (1 << 16) - 1


@dataclass(slots=True)
class _ArchiveCleanupRecord:
    resource: object
    closed: bool = False


class _ArchiveCleanupOwner:
    """Retryable owner spanning Windows authority and child HANDLE handoffs."""

    def __init__(self) -> None:
        self._records: list[_ArchiveCleanupRecord] = []

    def own(self, resource: object) -> None:
        if resource is None:
            raise TypeError("archive cleanup resource must not be null")
        if any(record.resource is resource for record in self._records):
            return
        record: _ArchiveCleanupRecord | None = None
        try:
            record = _ArchiveCleanupRecord(resource)
            self._records.append(record)
        except BaseException:  # noqa: B036 - commit resource ownership
            if record is not None and not any(
                candidate is record for candidate in self._records
            ):
                try:
                    self._records.append(record)
                except BaseException:  # noqa: B036 - preserve primary failure
                    pass
            raise

    @staticmethod
    def _reports_closed(resource: object) -> bool:
        try:
            return bool(resource.closed)  # type: ignore[attr-defined]
        except BaseException:  # noqa: B036 - uncertain ownership stays retained
            return False

    @property
    def closed(self) -> bool:
        for record in self._records:
            if not record.closed and self._reports_closed(record.resource):
                record.closed = True
        return all(record.closed for record in self._records)

    def close(self) -> None:
        deferred: BaseException | None = None
        for record in reversed(tuple(self._records)):
            if record.closed or self._reports_closed(record.resource):
                record.closed = True
                continue
            close = getattr(record.resource, "close", None)
            if not callable(close):
                failure = TypeError("archive cleanup resource has no close method")
                if deferred is None:
                    deferred = failure
                else:
                    _annotate_secondary_error(
                        deferred,
                        "additional archive cleanup also failed",
                        failure,
                    )
                continue
            try:
                close()
                record.closed = True
            except BaseException as failure:  # noqa: B036 - visit every owner
                if self._reports_closed(record.resource):
                    record.closed = True
                if deferred is None:
                    deferred = failure
                else:
                    _annotate_secondary_error(
                        deferred,
                        "additional archive cleanup also failed",
                        failure,
                    )
        if deferred is not None:
            raise deferred
        if not self.closed:
            raise RuntimeError("Windows archive cleanup remains pending")


def _attach_archive_cleanup_owner(
    failure: BaseException,
    owner: _ArchiveCleanupOwner,
) -> None:
    if owner.closed:
        return
    try:
        failure.archive_cleanup_owner = owner  # type: ignore[attr-defined]
    except BaseException:  # noqa: B036 - local owner remains authoritative
        pass


class _ImmutableArchiveSnapshot(io.RawIOBase):
    """Seekable read-only chunks for platforms without sealed descriptors."""

    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        super().__init__()
        self._chunks = chunks
        self._size = sum(len(chunk) for chunk in chunks)
        self._offset = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def write(self, _payload: bytes) -> int:
        raise io.UnsupportedOperation("archive snapshot is read-only")

    def read(self, size: int = -1) -> bytes:
        self._checkClosed()
        if size is None or size < 0:
            end = self._size
        else:
            end = min(self._size, self._offset + size)
        if end <= self._offset:
            return b""
        output = bytearray(end - self._offset)
        output_offset = 0
        chunk_start = 0
        for chunk in self._chunks:
            chunk_end = chunk_start + len(chunk)
            if chunk_end <= self._offset:
                chunk_start = chunk_end
                continue
            if chunk_start >= end:
                break
            source_start = max(self._offset, chunk_start) - chunk_start
            source_end = min(end, chunk_end) - chunk_start
            segment = chunk[source_start:source_end]
            output[output_offset : output_offset + len(segment)] = segment
            output_offset += len(segment)
            chunk_start = chunk_end
        self._offset = end
        return bytes(output)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._checkClosed()
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self._offset + offset
        elif whence == os.SEEK_END:
            position = self._size + offset
        else:
            raise ValueError("invalid archive snapshot seek mode")
        if position < 0:
            raise ValueError("negative archive snapshot seek position")
        self._offset = position
        return position

    def tell(self) -> int:
        self._checkClosed()
        return self._offset

    def close(self) -> None:
        self._chunks = ()
        self._size = 0
        self._offset = 0
        super().close()


def _read_archive(descriptor: int, size: int) -> bytes:
    return os.read(descriptor, size)


def _archive_os_name() -> str:
    return os.name


def _create_archive_snapshot() -> int:
    try:
        return _create_sealable_memfd()
    except _SnapshotUnavailable as exc:
        raise ValueError(
            "secure context archive parsing requires immutable sealed snapshots"
        ) from exc


def _write_snapshot(descriptor: int, block: bytes) -> None:
    _write_all(descriptor, block)


def _seal_archive_snapshot(descriptor: int) -> None:
    try:
        _seal_snapshot_descriptor(descriptor)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            "context artifact archive snapshot could not be sealed"
        ) from exc


def _stable_identity(metadata: os.stat_result) -> tuple[int, ...]:
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


def _directory_binding_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Identify one retained lexical ancestor without mutable directory times."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0) & 0x400,
    )


def _windows_archive_entry(
    authority: _windows_fs.WindowsDirectoryAuthority,
    name: str,
) -> _windows_fs.WindowsDirectoryEntry:
    iterator = getattr(authority.api, "iter_directory", None)
    entries = (
        iterator(authority.handle)
        if callable(iterator)
        else authority.api.enumerate_directory(authority.handle)
    )
    matches = [entry for entry in entries if entry.name == name]
    if len(matches) != 1:
        raise ValueError("context artifact archive path is missing or ambiguous")
    return matches[0]


@contextmanager
def _authenticated_windows_archive_snapshot(
    lexical: Path,
    *,
    physical_limit: int,
) -> Iterator[BinaryIO]:
    """Read one exact Windows file generation through retained HANDLEs."""

    owner = _ArchiveCleanupOwner()
    snapshot: _ImmutableArchiveSnapshot | None = None
    try:
        authority = _windows_fs.open_lexical_directory_authority(
            lexical.parent,
            cleanup_slot=owner,
        )
        api = authority.api
        cleanup = _windows_fs._WindowsHandleCleanup(api)
        owner.own(cleanup)
        entry = _windows_archive_entry(authority, lexical.name)
        if entry.attributes & (
            _windows_fs.FILE_ATTRIBUTE_DIRECTORY
            | _windows_fs.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError(
                f"context artifact archive is not a bounded regular file: {lexical}"
            )
        handle = _windows_fs._open_owned_windows_handle(
            lambda: api.open_relative(
                authority.handle,
                entry.name,
                desired_access=(
                    _windows_fs.FILE_READ_DATA
                    | _windows_fs.FILE_READ_ATTRIBUTES
                    | _windows_fs.SYNCHRONIZE
                ),
                is_directory=False,
                allow_reparse=False,
            ),
            cleanup.handles,
        )
        opened = api.metadata(handle)
        cleanup.retain(handle, opened)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_file_attributes & _windows_fs.FILE_ATTRIBUTE_REPARSE_POINT
            or not opened.st_dev
            or not _windows_fs.windows_file_id_is_reliable(opened.file_id_128)
            or entry.file_id_128 != opened.file_id_128
            or opened.delete_pending
            or opened.st_size < 0
            or opened.st_size > physical_limit
        ):
            raise ValueError(
                f"context artifact archive is not a bounded regular file: {lexical}"
            )
        expected = _windows_fs.windows_metadata_identity(opened)
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            block = api.read(handle, min(remaining, _COPY_CHUNK_BYTES))
            if not block:
                raise ValueError("context artifact archive was truncated")
            chunks.append(block)
            remaining -= len(block)
        if api.read(handle, 1):
            raise ValueError("context artifact archive grew while reading")

        def verify_binding() -> None:
            observed = api.metadata(handle)
            rebound = _windows_archive_entry(authority, lexical.name)
            if (
                _windows_fs.windows_metadata_identity(observed) != expected
                or rebound.file_id_128 != opened.file_id_128
            ):
                raise ValueError("context artifact archive changed while reading")
            authority.verify()

        verify_binding()
        snapshot = _ImmutableArchiveSnapshot(tuple(chunks))
        chunks.clear()
        yield snapshot
        verify_binding()
    finally:
        active_failure = sys.exc_info()[1]
        cleanup_failure: BaseException | None = None
        if snapshot is not None:
            try:
                snapshot.close()
            except BaseException as exc:  # noqa: B036 - visit every authority
                cleanup_failure = exc
        try:
            owner.close()
        except BaseException as exc:  # noqa: B036 - retain retryable owner
            cleanup_failure = cleanup_failure or exc
        if cleanup_failure is not None:
            if active_failure is not None:
                _annotate_secondary_error(
                    active_failure,
                    "Windows archive authority cleanup also failed",
                    cleanup_failure,
                )
                _attach_archive_cleanup_owner(active_failure, owner)
            else:
                _attach_archive_cleanup_owner(cleanup_failure, owner)
                raise cleanup_failure


@contextmanager
def _authenticated_archive_snapshot(
    value: str | Path,
    *,
    max_files: int,
    max_bytes: int,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
) -> Iterator[tuple[Path, BinaryIO]]:
    """Copy one lexical no-follow regular file into a verified snapshot."""

    candidate = Path(value).expanduser()
    lexical = Path(os.path.abspath(os.fspath(candidate)))
    encoded = os.fsencode(lexical)
    if (
        lexical.name in {"", ".", ".."}
        or len(encoded) > _MAX_ARCHIVE_PATH_BYTES
        or len(lexical.parts) > _MAX_ARCHIVE_PATH_COMPONENTS
    ):
        raise ValueError("context artifact archive path is invalid or too long")
    physical_limit = min(
        max_archive_bytes,
        max_bytes
        + max_files * _MAX_ARCHIVE_MEMBER_OVERHEAD
        + _MAX_ARCHIVE_ENVELOPE_BYTES,
    )
    platform_name = _archive_os_name()
    if platform_name == "nt":
        with _authenticated_windows_archive_snapshot(
            lexical,
            physical_limit=physical_limit,
        ) as snapshot:
            yield lexical, snapshot
        return
    if platform_name != "posix" or not (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    ):
        raise ValueError(
            "secure context archive reads require no-follow directory handles"
        )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    owner = _ArchiveCleanupOwner()
    descriptor_cleanup = _PosixDescriptorCleanup()
    owner.own(descriptor_cleanup)
    bindings: list[tuple[int, str, int, tuple[int, ...]]] = []
    source = -1
    snapshot_descriptor = -1
    snapshot: BinaryIO | None = None
    try:
        descriptor = descriptor_cleanup.open(lexical.anchor, directory_flags)
        root = os.fstat(descriptor)
        if not root.st_dev or not root.st_ino or not stat.S_ISDIR(root.st_mode):
            raise ValueError("context artifact archive root has no stable identity")
        for part in lexical.parts[1:-1]:
            before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if (
                not stat.S_ISDIR(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or not before.st_dev
                or not before.st_ino
            ):
                raise ValueError(
                    "context artifact archive parent is not a real directory"
                )
            child = descriptor_cleanup.open(
                part,
                directory_flags,
                dir_fd=descriptor,
            )
            opened = os.fstat(child)
            if _directory_binding_identity(opened) != _directory_binding_identity(
                before
            ):
                raise ValueError("context artifact archive parent changed")
            bindings.append(
                (descriptor, part, child, _directory_binding_identity(opened))
            )
            descriptor = child

        before = os.stat(
            lexical.name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or not before.st_dev
            or not before.st_ino
            or before.st_size < 0
            or before.st_size > physical_limit
        ):
            raise ValueError(
                f"context artifact archive is not a bounded regular file: {lexical}"
            )
        source = descriptor_cleanup.open(
            lexical.name,
            file_flags,
            dir_fd=descriptor,
        )
        opened = os.fstat(source)
        expected = _stable_identity(before)
        if _stable_identity(opened) != expected:
            raise ValueError("context artifact archive changed while opening")

        snapshot_descriptor = _create_archive_snapshot()
        descriptor_cleanup.retain(snapshot_descriptor)
        remaining = opened.st_size
        while remaining:
            block = _read_archive(source, min(remaining, _COPY_CHUNK_BYTES))
            if not block:
                raise ValueError("context artifact archive was truncated")
            _write_snapshot(snapshot_descriptor, block)
            remaining -= len(block)
        if _read_archive(source, 1):
            raise ValueError("context artifact archive grew while reading")

        def verify_binding() -> None:
            if (
                _stable_identity(os.fstat(source)) != expected
                or _stable_identity(
                    os.stat(
                        lexical.name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                )
                != expected
            ):
                raise ValueError("context artifact archive changed while reading")
            for parent, name, child, identity in bindings:
                if (
                    _directory_binding_identity(os.fstat(child)) != identity
                    or _directory_binding_identity(
                        os.stat(name, dir_fd=parent, follow_symlinks=False)
                    )
                    != identity
                ):
                    raise ValueError("context artifact archive parent changed")

        verify_binding()
        _seal_archive_snapshot(snapshot_descriptor)
        # The descriptor cleanup remains the sole native-fd owner.  The file
        # wrapper is separately closeable but must never close a reused raw fd
        # if its own close is interrupted and later retried.
        snapshot = os.fdopen(snapshot_descriptor, "rb", closefd=False)
        owner.own(snapshot)
        snapshot_descriptor = -1
        yield lexical, snapshot
        verify_binding()
    except FileNotFoundError as exc:
        raise ValueError(f"context artifact archive does not exist: {lexical}") from exc
    except OSError as exc:
        raise ValueError(
            f"context artifact archive could not be opened safely: {lexical}"
        ) from exc
    finally:
        active_failure = sys.exc_info()[1]
        try:
            owner.close()
        except BaseException as cleanup_failure:  # noqa: B036 - retain owner
            if active_failure is not None:
                _annotate_secondary_error(
                    active_failure,
                    "POSIX archive authority cleanup also failed",
                    cleanup_failure,
                )
                _attach_archive_cleanup_owner(active_failure, owner)
            else:
                _attach_archive_cleanup_owner(cleanup_failure, owner)
                raise


def _member_path(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("context artifact archive contains an invalid path")
    path = PurePosixPath(value)
    normalized = path.as_posix().rstrip("/")
    if (
        path.is_absolute()
        or not normalized
        or value.rstrip("/") != normalized
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"context artifact archive path is unsafe: {value!r}")
    return PurePosixPath(normalized)


def _member_kind(info: zipfile.ZipInfo) -> int:
    return stat.S_IFMT(info.external_attr >> 16)


def _preflight_zip_envelope(
    snapshot: BinaryIO,
    *,
    max_files: int,
) -> None:
    """Bound the ZIP directory before ``ZipFile`` allocates ``ZipInfo`` objects."""

    snapshot.seek(0, os.SEEK_END)
    archive_size = snapshot.tell()
    tail_size = min(archive_size, 22 + _MAX_ZIP_COMMENT_BYTES)
    snapshot.seek(archive_size - tail_size)
    tail = snapshot.read(tail_size)
    end_index = tail.rfind(b"PK\x05\x06")
    if end_index < 0 or len(tail) - end_index < 22:
        raise zipfile.BadZipFile("missing ZIP end record")
    end_offset = archive_size - tail_size + end_index
    try:
        (
            signature,
            disk_number,
            central_disk,
            disk_members,
            member_count,
            central_size,
            central_offset,
            comment_size,
        ) = struct.unpack_from("<4s4H2LH", tail, end_index)
    except struct.error as exc:
        raise zipfile.BadZipFile("truncated ZIP end record") from exc
    if (
        signature != b"PK\x05\x06"
        or disk_number != 0
        or central_disk != 0
        or end_offset + 22 + comment_size != archive_size
    ):
        raise zipfile.BadZipFile("invalid or multi-disk ZIP envelope")

    if (
        disk_members == 0xFFFF
        or member_count == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    ):
        locator_offset = end_offset - 20
        if locator_offset < 0:
            raise zipfile.BadZipFile("missing ZIP64 locator")
        snapshot.seek(locator_offset)
        locator = snapshot.read(20)
        try:
            locator_signature, locator_disk, zip64_offset, total_disks = struct.unpack(
                "<4sLQL", locator
            )
        except struct.error as exc:
            raise zipfile.BadZipFile("truncated ZIP64 locator") from exc
        if (
            locator_signature != b"PK\x06\x07"
            or locator_disk != 0
            or total_disks != 1
            or zip64_offset < 0
            or zip64_offset + 56 > locator_offset
        ):
            raise zipfile.BadZipFile("invalid ZIP64 locator")
        snapshot.seek(zip64_offset)
        record = snapshot.read(56)
        try:
            (
                zip64_signature,
                record_size,
                _create_version,
                _extract_version,
                zip64_disk,
                zip64_central_disk,
                zip64_disk_members,
                zip64_members,
                zip64_central_size,
                zip64_central_offset,
            ) = struct.unpack("<4sQ2H2L4Q", record)
        except struct.error as exc:
            raise zipfile.BadZipFile("truncated ZIP64 end record") from exc
        if (
            zip64_signature != b"PK\x06\x06"
            or record_size < 44
            or zip64_disk != 0
            or zip64_central_disk != 0
            or zip64_disk_members != zip64_members
        ):
            raise zipfile.BadZipFile("invalid or multi-disk ZIP64 envelope")
        member_count = zip64_members
        central_size = zip64_central_size
        central_offset = zip64_central_offset
        central_end = zip64_offset
    else:
        if disk_members != member_count:
            raise zipfile.BadZipFile("invalid multi-disk ZIP member count")
        central_end = end_offset

    max_entries = max_files * 2 + 64
    if member_count > max_entries:
        raise ValueError(f"context artifact archive exceeds {max_entries} entries")
    maximum_central_size = _MAX_ARCHIVE_ENVELOPE_BYTES + max_entries * (
        _MAX_ARCHIVE_PATH_BYTES + _MAX_ARCHIVE_MEMBER_OVERHEAD
    )
    if central_size > maximum_central_size:
        raise ValueError(
            "context artifact archive central directory exceeds its budget"
        )
    if central_offset > central_end or central_size > central_end - central_offset:
        raise zipfile.BadZipFile("ZIP central directory lies outside its envelope")
    # EOCD counts are attacker-controlled. Walk only the fixed central headers
    # and their bounded variable sections to prove the real count before
    # ``ZipFile`` creates one Python object per member.
    central_limit = central_offset + central_size
    snapshot.seek(central_offset)
    actual_members = 0
    while snapshot.tell() < central_limit:
        header = snapshot.read(46)
        if len(header) != 46 or header[:4] != b"PK\x01\x02":
            raise zipfile.BadZipFile("invalid ZIP central directory header")
        try:
            name_size, extra_size, member_comment_size = struct.unpack_from(
                "<3H", header, 28
            )
        except struct.error as exc:
            raise zipfile.BadZipFile("truncated ZIP central directory header") from exc
        variable_size = name_size + extra_size + member_comment_size
        if variable_size > central_limit - snapshot.tell():
            raise zipfile.BadZipFile("ZIP central directory entry exceeds its envelope")
        snapshot.seek(variable_size, os.SEEK_CUR)
        actual_members += 1
        if actual_members > max_entries:
            raise ValueError(f"context artifact archive exceeds {max_entries} entries")
    if snapshot.tell() != central_limit or actual_members != member_count:
        raise zipfile.BadZipFile(
            "ZIP central directory count does not match its envelope"
        )
    snapshot.seek(0)


def _validated_members(
    archive: zipfile.ZipFile,
    *,
    max_files: int,
    max_bytes: int,
) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    entries = archive.infolist()
    max_entries = max_files * 2 + 64
    if len(entries) > max_entries:
        raise ValueError(f"context artifact archive exceeds {max_entries} entries")
    raw: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    metadata_paths: list[PurePosixPath] = []
    for info in entries:
        path = _member_path(info.filename)
        kind = _member_kind(info)
        if kind == stat.S_IFLNK:
            raise ValueError(
                f"context artifact archive contains a symbolic link: {path}"
            )
        if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise ValueError(
                f"context artifact archive contains a special file: {path}"
            )
        if info.flag_bits & 0x1:
            raise ValueError(f"context artifact archive member is encrypted: {path}")
        raw.append((info, path))
        if not info.is_dir() and path.name == CONTEXT_ARTIFACT_MANIFEST:
            metadata_paths.append(path)

    if len(metadata_paths) != 1:
        raise ValueError(
            "context artifact archive must contain exactly one metadata file"
        )
    prefix = metadata_paths[0].parent
    prefix_parts = () if str(prefix) == "." else prefix.parts

    result: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    seen: set[str] = set()
    file_count = 0
    total_bytes = 0
    for info, path in raw:
        if prefix_parts:
            if path.parts[: len(prefix_parts)] != prefix_parts:
                raise ValueError(
                    "context artifact archive contains files outside its root"
                )
            stripped_parts = path.parts[len(prefix_parts) :]
            if not stripped_parts:
                continue
            path = PurePosixPath(*stripped_parts)
        relative = path.as_posix()
        if relative in seen:
            raise ValueError(f"duplicate context artifact archive path: {relative}")
        seen.add(relative)
        if not info.is_dir():
            file_count += 1
            total_bytes += info.file_size
            if file_count > max_files:
                raise ValueError(f"context artifact archive exceeds {max_files} files")
            if total_bytes > max_bytes:
                raise ValueError(
                    f"context artifact archive exceeds {max_bytes} expanded bytes"
                )
        result.append((info, path))
    return result


def _extract_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    relative: PurePosixPath,
    stage: OwnedDirectoryStage,
) -> None:
    if info.is_dir():
        return

    def chunks():
        written = 0
        with archive.open(info, "r") as source:
            while chunk := source.read(_COPY_CHUNK_BYTES):
                written += len(chunk)
                if written > info.file_size:
                    raise ValueError(
                        "context artifact archive member exceeded declared size: "
                        f"{relative}"
                    )
                yield chunk
        if written != info.file_size:
            raise ValueError(
                f"context artifact archive member size mismatch: {relative}"
            )

    stage.write_file(relative, chunks(), max_bytes=info.file_size)


def extract_context_artifact_archive(
    archive_path: str | Path,
    output_dir: str | Path,
    *,
    expected_repository: str | None = None,
    expected_commit: str | None = None,
    max_files: int = DEFAULT_MAX_ARCHIVE_FILES,
    max_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
) -> VerifiedContextArtifact:
    """Extract and verify an artifact ZIP before publishing it."""

    if (
        isinstance(max_files, bool)
        or not isinstance(max_files, int)
        or max_files <= 0
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes <= 0
        or isinstance(max_archive_bytes, bool)
        or not isinstance(max_archive_bytes, int)
        or max_archive_bytes <= 0
    ):
        raise ValueError("context artifact archive limits must be positive integers")
    output = lexical_directory_path(Path(output_dir))
    try:
        stage = OwnedDirectoryStage.prepare(
            output,
            required_destination_file=CONTEXT_ARTIFACT_MANIFEST,
            allow_empty_destination=True,
        )
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            "refusing to replace a non-empty directory that is not a "
            f"CodeNib context artifact: {output}"
        ) from exc

    published: list[VerifiedContextArtifact] = []
    try:
        with _authenticated_archive_snapshot(
            archive_path,
            max_files=max_files,
            max_bytes=max_bytes,
            max_archive_bytes=max_archive_bytes,
        ) as (archive_display, snapshot):
            _preflight_zip_envelope(snapshot, max_files=max_files)
            with zipfile.ZipFile(snapshot) as archive:
                members = _validated_members(
                    archive,
                    max_files=max_files,
                    max_bytes=max_bytes,
                )
                for info, relative in members:
                    _extract_member(archive, info, relative, stage)

        def validate_staged_artifact(candidate: PublicationDirectoryReader) -> None:
            verify_context_artifact_reader(
                candidate,
                expected_repository=expected_repository,
                expected_commit=expected_commit,
                max_files=max_files,
                max_bytes=max_bytes,
            )

        def validate_published_artifact(
            candidate: PublicationDirectoryReader,
        ) -> None:
            verified = verify_context_artifact_reader(
                candidate,
                expected_repository=expected_repository,
                expected_commit=expected_commit,
                max_files=max_files,
                max_bytes=max_bytes,
            )
            published.append(
                replace(
                    verified,
                    root=output,
                    metadata_path=output / CONTEXT_ARTIFACT_MANIFEST,
                    manifest_path=output / verified.manifest_path.name,
                )
            )

        stage.publish(
            validate_staged_directory=validate_staged_artifact,
            validate_published_destination=validate_published_artifact,
        )
    except zipfile.BadZipFile as exc:
        stage.discard()
        failure = ValueError(
            f"context artifact archive is not a valid ZIP: {archive_display}"
        )
        cleanup_owner = getattr(exc, "archive_cleanup_owner", None)
        if cleanup_owner is not None:
            try:
                failure.archive_cleanup_owner = cleanup_owner  # type: ignore[attr-defined]
            except BaseException:  # noqa: B036 - chained error still retains owner
                pass
        raise failure from exc
    except BaseException:
        stage.discard()
        raise

    if len(published) != 1:
        raise RuntimeError("published context artifact was not authority-verified")
    return published[0]


__all__ = [
    "DEFAULT_MAX_ARCHIVE_BYTES",
    "DEFAULT_MAX_ARCHIVE_FILES",
    "DEFAULT_MAX_EXPANDED_BYTES",
    "extract_context_artifact_archive",
]
